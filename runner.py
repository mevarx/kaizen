"""Paired A/B runner: same task, same model, skill off vs skill on (stdlib only).

run_paired() is the contract entry point (INTERFACES.md). For each model it
produces a baseline result (no skill instructions) and a treatment result
(skill body + scripts/ output injected as context), yields each as an SSE
event dict the moment it completes, then yields one summary event carrying
per-model lift and the spread.

Two failure rules drive the design:
  - No GEMINI_API_KEY -> the cache is checked first, so a seeded cached/
    directory still renders a complete scored run. A cache miss degrades to
    an unscored result event rather than an exception.
  - Rate limits (~16k TPM free tier) -> models run strictly one at a time and
    every call retries 3x with exponential backoff before giving up.
"""

import hashlib
import json
import os
import re
import time
import urllib.error
import urllib.request

import cache as cache_mod
import judge as judge_mod
import skill_loader

API_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"

MODEL_TEMPERATURE = 0.2
MODEL_MAX_TOKENS = 1024
MAX_SCRIPT_CHARS = 4000
SCRIPT_TIMEOUT = 30
RETRY_TRIES = 3
BACKOFF_BASE = 1.0
RETRYABLE_STATUSES = (408, 409, 429, 500, 502, 503, 504)

# Test seam: tests replace this with a fake that returns fixed text.
MODEL_CLIENT = None


class ModelCallError(RuntimeError):
    """A model call failed. `retryable` drives backoff; `no_key` drives the
    cached/degraded path."""

    def __init__(self, message, status=None, retryable=False, no_key=False):
        super().__init__(message)
        self.status = status
        self.retryable = retryable
        self.no_key = no_key


def _sleep(seconds):
    """Indirected so tests can collapse backoff waits to zero."""
    time.sleep(seconds)


def gemini_generate(model, prompt, system="", temperature=MODEL_TEMPERATURE,
                    max_tokens=MODEL_MAX_TOKENS, timeout=60):
    """One generation against the Gemini API (the default MODEL_CLIENT)."""
    key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not key:
        raise ModelCallError("GEMINI_API_KEY is not set", retryable=False, no_key=True)

    payload = {"contents": [{"role": "user", "parts": [{"text": prompt}]}],
               "generationConfig": {"temperature": temperature,
                                    "maxOutputTokens": max_tokens}}
    if system:
        payload["systemInstruction"] = {"parts": [{"text": system}]}

    req = urllib.request.Request(API_URL.format(model=model),
                                 data=json.dumps(payload).encode("utf-8"),
                                 headers={"Content-Type": "application/json",
                                          "x-goog-api-key": key},
                                 method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = ""
        try:
            body = exc.read().decode("utf-8", "replace")[:300]
        except Exception:
            pass
        raise ModelCallError("HTTP %s from %s: %s" % (exc.code, model, body),
                             status=exc.code,
                             retryable=exc.code in RETRYABLE_STATUSES) from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise ModelCallError("network error calling %s: %s" % (model, exc),
                             retryable=True) from exc
    except ValueError as exc:
        raise ModelCallError("bad JSON from %s: %s" % (model, exc),
                             retryable=True) from exc

    candidates = data.get("candidates") or []
    if not candidates:
        raise ModelCallError("no candidates from %s" % model, retryable=True)
    parts = ((candidates[0].get("content") or {}).get("parts") or [])
    text = "".join(p.get("text", "") for p in parts if isinstance(p, dict))
    if not text.strip():
        raise ModelCallError("empty response from %s" % model, retryable=True)
    return text


def model_client():
    """The active model client (tests swap runner.MODEL_CLIENT)."""
    return MODEL_CLIENT or gemini_generate


def call_model(model, prompt, system=""):
    """Generate with retry/backoff. Raises ModelCallError when all tries fail."""
    client = model_client()
    last = None
    for attempt in range(RETRY_TRIES):
        try:
            return client(model, prompt, system=system)
        except ModelCallError as exc:
            last = exc
            if not exc.retryable or attempt == RETRY_TRIES - 1:
                break
            _sleep(BACKOFF_BASE * (2 ** attempt))
    raise last


def skill_hash(skill):
    """Stable hash of the skill's instructions; part of every cache key."""
    parts = [str(skill.get("name") or ""),
             str(skill.get("description") or ""),
             str(skill.get("body") or ""),
             ",".join(sorted((skill.get("scripts") or {}).keys()))]
    return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()


_FENCE_RE = re.compile(r"```([A-Za-z0-9_-]*)[ \t]*\r?\n(.*?)```", re.DOTALL)
_HEADING_RE = re.compile(r"^#{1,6}[ \t]*rubric[ \t]*$", re.IGNORECASE | re.MULTILINE)
RUBRIC_HEADINGS = ("rubric", "criteria", "evaluation", "success criteria",
                   "scoring", "acceptance")


def _as_criterion(item):
    """Normalise one rubric entry to {name, weight, check}."""
    if isinstance(item, str):
        return {"name": item[:120], "weight": 1, "check": item}
    if not isinstance(item, dict):
        return None
    name = str(item.get("name") or item.get("criterion") or "").strip()
    if not name:
        return None
    try:
        weight = float(item.get("weight", 1))
    except (TypeError, ValueError):
        weight = 1.0
    return {"name": name, "weight": weight,
            "check": str(item.get("check") or item.get("description") or name)}


def _fenced_json_rubric(body):
    """Rubrics declared as a ```json fence — the format the bundled fixtures
    actually use (INTERFACES.md only sketched markdown bullets). Prefer the
    fence sitting under a `## Rubric` heading."""
    heading_at = None
    hm = _HEADING_RE.search(body or "")
    if hm:
        heading_at = hm.start()
    blocks = []
    for m in _FENCE_RE.finditer(body or ""):
        raw = m.group(2).strip()
        if not raw:
            continue
        try:
            parsed = json.loads(raw)
        except (ValueError, TypeError):
            continue
        if isinstance(parsed, dict) and isinstance(parsed.get("rubric"), list):
            parsed = parsed["rubric"]
        if not isinstance(parsed, list):
            continue
        criteria = [c for c in (_as_criterion(i) for i in parsed) if c]
        if criteria:
            blocks.append((0 if heading_at is not None and m.start() > heading_at else 1,
                           m.start(), criteria))
    if not blocks:
        return []
    blocks.sort(key=lambda t: (t[0], t[1]))
    return blocks[0][2]


def _rubric_from_body(body):
    """Pull criteria out of a '## Rubric'-style markdown section.

    Only used when the body carries no fenced-JSON rubric; a fence's opening
    line is not a criterion, so the bullet scan stops at the first fence.
    """
    lines = (body or "").splitlines()
    items, active = [], False
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("```"):
            active = False
            continue
        if stripped.startswith("#"):
            heading = stripped.lstrip("#").strip().lower()
            active = any(k in heading for k in RUBRIC_HEADINGS)
            continue
        if not active or not stripped:
            continue
        bullet = stripped.lstrip("-*+").strip()
        if bullet[:1].isdigit():
            bullet = bullet.split(".", 1)[-1].strip() if "." in bullet else bullet
        if bullet.startswith("[ ] ") or bullet.startswith("[x] "):
            bullet = bullet[4:].strip()
        if bullet:
            items.append({"name": bullet})
    return items


def extract_rubric(skill, override=None):
    """The rubric used to judge this skill, in priority order:
    explicit override, skill['rubric'], metadata.rubric/criteria, a fenced
    JSON rubric in the body, a markdown rubric section, then a task-derived
    default.
    """
    if override:
        return _normalize_rubric(override)
    for source in (skill.get("rubric"), (skill.get("metadata") or {}).get("rubric"),
                   (skill.get("metadata") or {}).get("criteria")):
        normalized = _normalize_rubric(source)
        if normalized:
            return normalized
    from_body = _fenced_json_rubric(skill.get("body") or "")
    if from_body:
        return from_body
    from_body = _rubric_from_body(skill.get("body") or "")
    if from_body:
        return from_body
    desc = (skill.get("description") or "").strip()
    name = desc.split(".")[0][:120] if desc else "completes the task correctly"
    return [{"name": name}]


def _normalize_rubric(source):
    if not source:
        return []
    if isinstance(source, str):
        chunks = [c.strip() for c in source.replace("\r", "").split("\n")]
        chunks = [c.lstrip("-*").strip() for c in chunks]
        return [{"name": c} for c in chunks if c]
    if isinstance(source, dict):
        source = [source]
    items = []
    for entry in source:
        if isinstance(entry, dict):
            text = (entry.get("name") or entry.get("criterion") or entry.get("text") or "").strip()
            if text:
                items.append({"name": text})
        elif isinstance(entry, str) and entry.strip():
            items.append({"name": entry.strip()})
    return items


def run_skill_scripts(skill, timeout=SCRIPT_TIMEOUT):
    """Execute each declared script and return {filename: stdout}.

    PRD 13: kaizen runs the skill's scripts and feeds their output into the
    treatment context. A failing or hanging script is reported, never fatal.
    """
    outputs = {}
    skill_dir = skill.get("dir")
    for filename in sorted((skill.get("scripts") or {}).keys()):
        if not skill_dir:
            outputs[filename] = "(skipped: in-memory skill has no scripts directory)"
            continue
        try:
            result = skill_loader.run_script(skill_dir, filename, timeout=timeout)
        except Exception as exc:
            outputs[filename] = "(error: %s)" % exc
            continue
        if result.get("ok"):
            outputs[filename] = (result.get("stdout") or "").strip()[:MAX_SCRIPT_CHARS]
        else:
            err = (result.get("stderr") or "").strip()[:MAX_SCRIPT_CHARS]
            outputs[filename] = "(exit %s) %s" % (result.get("returncode"), err)
    return outputs


def build_treatment_context(skill, script_outputs):
    """The system/context text injected for the treatment arm only."""
    blocks = []
    body = (skill.get("body") or "").strip()
    if body:
        blocks.append(body)
    for filename in sorted(script_outputs):
        blocks.append("Output of scripts/%s:\n%s" % (filename, script_outputs[filename]))
    return "\n\n".join(blocks)


def build_prompts(task, skill, script_outputs):
    """(baseline_prompt, treatment_prompt, baseline_system, treatment_system).

    The user-visible task is byte-identical in both arms; the only difference
    is the skill context, which is what makes the delta meaningful (PRD 15).
    """
    task = (task or "").strip()
    baseline_system = ""
    treatment_system = build_treatment_context(skill, script_outputs)
    return task, task, baseline_system, treatment_system


def cache_prompt(system, user_prompt):
    """The exact text sent to the model, flattened for the cache key."""
    if system:
        return "%s\n---\n%s" % (system, user_prompt)
    return user_prompt


def classify_lift(lift):
    """POSITIVE / NEUTRAL / NEGATIVE. lift == 0 is NEUTRAL, not a rounding
    accident. None (either phase unscored) is UNJUDGED — the vocabulary app.py
    and the dashboard already use (INTERFACES.md leaves the null case open).
    """
    if lift is None:
        return "UNJUDGED"
    if lift > 0:
        return "POSITIVE"
    if lift < 0:
        return "NEGATIVE"
    return "NEUTRAL"


def _judge(task, rubric, output, judge_model, judge_backend):
    """Judge one output; returns the verdict dict. Never raises."""
    try:
        return judge_mod.score_output(task, rubric, output, model=judge_model,
                                      backend=judge_backend)
    except judge_mod.NoKeyError:
        return {"score": None, "reasoning": "unjudged: GEMINI_API_KEY not set",
                "criteria": [], "judge_model": judge_model}
    except Exception as exc:
        return {"score": None, "reasoning": "unjudged: %s" % exc,
                "criteria": [], "judge_model": judge_model}


def _judge_cached(task, rubric, output, judge_model, judge_backend, shash):
    """Judge with a verdict cache so reruns are instant and offline-seeded.

    The key covers the full judge prompt, so a changed rubric or output
    correctly misses.
    """
    prompt = judge_mod.build_judge_prompt(task, rubric, output)
    key = cache_mod.key_for(judge_model, prompt, shash)
    raw = cache_mod.cache_get(key)
    if raw is not None:
        try:
            verdict = json.loads(raw)
            if isinstance(verdict, dict) and "score" in verdict:
                return verdict, True
        except ValueError:
            pass
    verdict = _judge(task, rubric, output, judge_model, judge_backend)
    if verdict.get("score") is not None:
        cache_mod.cache_put(key, json.dumps(verdict), model=judge_model, prompt=prompt)
    return verdict, False


def _result_event(model, phase, output, verdict, cached, judge_cached):
    return {"type": "result", "model": model, "phase": phase,
            "score": verdict.get("score"), "reasoning": verdict.get("reasoning", ""),
            "output": output, "cached": cached, "judge_cached": judge_cached,
            "criteria": verdict.get("criteria", []),
            "judge_model": verdict.get("judge_model")}


def _summary(per_model):
    scored = [m for m in per_model if m["lift"] is not None]
    lifts = [m["lift"] for m in scored]
    spread = (max(lifts) - min(lifts)) if lifts else 0

    if not scored:
        headline = ("No models scored - set GEMINI_API_KEY for a live run, "
                    "or seed cached/ for cached mode.")
    else:
        best = max(scored, key=lambda m: m["lift"])
        worst = min(scored, key=lambda m: m["lift"])
        classes = {m["class"] for m in scored}
        rng = "%+d to %+d" % (worst["lift"], best["lift"])
        if len(scored) == 1 or best["lift"] == worst["lift"]:
            rng = "%+d on every model" % best["lift"]
        elif best["model"] != worst["model"]:
            rng = "%s %+d vs %s %+d" % (best["model"], best["lift"],
                                        worst["model"], worst["lift"])
        if {"POSITIVE", "NEGATIVE"} <= classes:
            headline = ("Divergent: %s (spread %d) - the skill helps one model "
                        "and hurts another" % (rng, spread))
        elif "NEGATIVE" in classes:
            if best["lift"] == worst["lift"]:
                headline = ("Skill hurts every scored model: %+d across the "
                            "board" % best["lift"])
            else:
                headline = ("Skill hurts every scored model: %s (spread %d)"
                            % (rng, spread))
        elif classes == {"NEUTRAL"}:
            headline = "Skill does nothing: every model lift is 0 (spread 0)"
        else:
            if best["lift"] == worst["lift"]:
                headline = "Skill helps every model equally: %+d (spread 0)" % best["lift"]
            else:
                headline = "Skill helps everywhere: %s (spread %d)" % (rng, spread)

    return {"type": "summary", "per_model": per_model, "spread": spread,
            "headline": headline}


def run_paired(skill, task, models, judge_model="gemma-4-31b-it", rubric=None,
               run_scripts=True, judge_backend=None, script_timeout=SCRIPT_TIMEOUT):
    """Run the paired A/B measurement; yields SSE-style event dicts.

    Per model: a baseline result event, then a treatment result event. Finally
    one summary event with per-model lift, class, and the spread headline.
    """
    task = (task or "").strip()
    models = [m for m in (models or []) if m]
    if judge_backend is None:
        judge_backend = judge_mod.judge_backend_for(judge_model)
    shash = skill_hash(skill)
    rubric = extract_rubric(skill, override=rubric)

    script_outputs = run_skill_scripts(skill, timeout=script_timeout) if run_scripts else {}
    baseline_prompt, treatment_prompt, baseline_system, treatment_system = \
        build_prompts(task, skill, script_outputs)

    phases = [("baseline", baseline_prompt, baseline_system, ""),
              ("treatment", treatment_prompt, treatment_system, shash)]

    per_model = []
    for model in models:
        scores = {}
        for phase, user_prompt, system, phase_hash in phases:
            flat = cache_prompt(system, user_prompt)
            key = cache_mod.key_for(model, flat, phase_hash)

            output = cache_mod.cache_get(key)
            cached = output is not None
            if output is None:
                try:
                    output = call_model(model, user_prompt, system=system)
                except ModelCallError as exc:
                    if exc.no_key:
                        note = ("no GEMINI_API_KEY and no cached response for "
                                "%s/%s" % (model, phase))
                    else:
                        note = "%s/%s failed after %d tries: %s" % (
                            model, phase, RETRY_TRIES, exc)
                    yield _result_event(model, phase, "", {
                        "score": None, "reasoning": note, "criteria": [],
                        "judge_model": judge_model}, cached=False, judge_cached=False)
                    scores[phase] = None
                    continue
                cache_mod.cache_put(key, output, model=model, prompt=flat)

            verdict, judge_cached = _judge_cached(task, rubric, output,
                                                  judge_model, judge_backend, shash)
            scores[phase] = verdict.get("score")
            yield _result_event(model, phase, output, verdict, cached, judge_cached)

        lift = None
        if scores.get("baseline") is not None and scores.get("treatment") is not None:
            lift = scores["treatment"] - scores["baseline"]
        per_model.append({"model": model, "baseline": scores.get("baseline"),
                          "treatment": scores.get("treatment"), "lift": lift,
                          "class": classify_lift(lift)})

    yield _summary(per_model)
