"""Gemma 4 judge: score a model output against a skill's rubric (stdlib only).

score_output() is the contract entry point (INTERFACES.md). It calls the
Gemini API with urllib, parses the reply defensively, and never raises on a
malformed judge response - a bad judge degrades the run, it does not kill it.

Missing GEMINI_API_KEY raises NoKeyError; the runner treats that as "cannot
judge", keeps the raw outputs, and moves on.
"""

import json
import os
import re
import urllib.error
import urllib.request

API_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
OLLAMA_URL = "http://localhost:11434/api/generate"
DEFAULT_JUDGE_MODEL = "gemma-4-31b-it"
OLLAMA_MODEL = "gemma3:4b"

# FR-3.2: the judge prompt is fixed and shipped in-repo. Editing it changes
# every score kaizen has ever produced, so it stays a constant.
JUDGE_PROMPT = """You are a strict, impartial evaluator for an AI agent benchmark.

Score the candidate OUTPUT below against the RUBRIC for the TASK.

TASK:
{task}

RUBRIC (each criterion is one bullet):
{rubric}

CANDIDATE OUTPUT:
{output}

Rules:
- Judge only the OUTPUT, not how it was produced.
- Score 0-100 overall. Meet every criterion for a high score; fail any for a low one.
- Be harsh and consistent: a plausible-sounding but wrong answer scores low.
- If the output is empty or off-task, score 0.

Reply with ONLY this JSON object, no prose, no markdown fence:
{{
  "score": <integer 0-100>,
  "reasoning": "<one or two sentences>",
  "criteria": [
    {{"name": "<criterion text, copied verbatim>", "met": <true|false>}}
  ]
}}"""

# One retry with this stricter preamble when the first reply will not parse.
RETRY_PREAMBLE = """Your previous reply was not valid JSON. Reply with ONLY the JSON object specified, starting with { and ending with }}. No prose, no markdown fence, no comments.

"""

FALLBACK = {"score": None, "reasoning": "unjudged", "criteria": [], "judge_model": None}


class NoKeyError(RuntimeError):
    """GEMINI_API_KEY is not set, so no judge call can be made."""


class JudgeHTTPError(RuntimeError):
    """The judge endpoint failed (network, 5xx, quota) after all retries."""


def _api_key():
    key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not key:
        raise NoKeyError("GEMINI_API_KEY is not set")
    return key


def _format_rubric(rubric):
    """Render criteria for the judge prompt.

    A fixture criterion carries a `check` string ("States rows_in and rows_out
    as concrete numbers") alongside its `name`; the judge needs the check text
    to score anything, so both are sent when present.
    """
    items = []
    for entry in rubric or []:
        if isinstance(entry, dict):
            name = (entry.get("name") or entry.get("criterion") or entry.get("text") or "").strip()
            check = (entry.get("check") or entry.get("description") or "").strip()
            if check and check != name:
                items.append("- %s: %s" % (name, check))
            elif name:
                items.append("- " + name)
        else:
            text = str(entry).strip()
            if text:
                items.append("- " + text)
    return "\n".join(items) or "- (no criteria supplied)"


def build_judge_prompt(task, rubric, output, retry=False):
    """The exact text sent to the judge model (kept pure for tests)."""
    body = JUDGE_PROMPT.format(
        task=(task or "").strip() or "(no task)",
        rubric=_format_rubric(rubric),
        output=(output or "").strip() or "(empty output)")
    return (RETRY_PREAMBLE + body) if retry else body


def _post(url, payload, headers, timeout):
    req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"),
                                 headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _gemini_text(prompt, model, temperature, max_tokens, timeout):
    payload = {"contents": [{"role": "user", "parts": [{"text": prompt}]}],
               "generationConfig": {"temperature": temperature, "maxOutputTokens": max_tokens}}
    data = _post(API_URL.format(model=model), payload,
                 {"Content-Type": "application/json", "x-goog-api-key": _api_key()}, timeout)
    parts = (((data.get("candidates") or [{}])[0].get("content") or {}).get("parts") or [])
    return "".join(p.get("text", "") for p in parts if isinstance(p, dict))


def _ollama_text(prompt, model, temperature, max_tokens, timeout):
    payload = {"model": model, "prompt": prompt, "stream": False,
               "options": {"temperature": temperature, "num_predict": max_tokens}}
    data = _post(OLLAMA_URL, payload, {"Content-Type": "application/json"}, timeout)
    return data.get("response", "") or ""


def _generate(prompt, model, backend="gemini", temperature=0.0, max_tokens=1024, timeout=60):
    """One judge generation. backend 'ollama' needs no API key (NFR-4)."""
    if backend == "ollama":
        return _ollama_text(prompt, OLLAMA_MODEL, temperature, max_tokens, timeout)
    return _gemini_text(prompt, model, temperature, max_tokens, timeout)


def _extract_json_object(text):
    """Pull the first balanced {...} object out of a reply, or return None."""
    start = text.find("{")
    while start != -1:
        depth = 0
        in_str = False
        escape = False
        for i in range(start, len(text)):
            ch = text[i]
            if in_str:
                if escape:
                    escape = False
                elif ch == "\\":
                    escape = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    return text[start:i + 1]
        start = text.find("{", start + 1)
    return None


def parse_judge_response(text):
    """Parse a judge reply into {score, reasoning, criteria}; None if unparseable.

    Accepts a bare object, a fenced ```json block, or an object embedded in
    prose. Returns None rather than raising so the caller can retry or fall
    back - FR-3.3.
    """
    if not text or not text.strip():
        return None
    candidate = _extract_json_object(text.strip())
    if candidate is None:
        return None
    try:
        data = json.loads(candidate)
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None

    score = data.get("score")
    if isinstance(score, bool) or not isinstance(score, (int, float)):
        # A string like "82" is common from small models; "n/a" is not.
        if isinstance(score, str) and re.fullmatch(r"\s*\d{1,3}\s*", score):
            score = int(score.strip())
        else:
            return None
    score = int(round(float(score)))
    if score < 0 or score > 100:
        return None

    criteria = []
    raw_criteria = data.get("criteria")
    if isinstance(raw_criteria, list):
        for item in raw_criteria:
            if not isinstance(item, dict):
                continue
            name = item.get("name") or item.get("criterion") or item.get("text") or ""
            met = item.get("met")
            if isinstance(met, str):
                met = met.strip().lower() in ("true", "yes", "1", "met")
            criteria.append({"name": str(name).strip(), "met": bool(met)})

    reasoning = data.get("reasoning")
    return {"score": score,
            "reasoning": reasoning if isinstance(reasoning, str) else "",
            "criteria": criteria}


def score_output(task, rubric, output, model=DEFAULT_JUDGE_MODEL, backend="gemini",
                 retries=2):
    """Score `output` against `rubric` for `task`.

    Returns {score, reasoning, criteria, judge_model}. score is None when the
    judge could not be made to return valid JSON twice - the run keeps the raw
    output and marks it unjudged instead of crashing.

    `backend` is "gemini" (default) or "ollama" for the local fallback (NFR-4).
    `retries` is the number of parse attempts, so retries=2 means one retry
    with the stricter prompt before the fallback.
    """
    if backend == "ollama":
        model = OLLAMA_MODEL
    prompts = [build_judge_prompt(task, rubric, output, retry=(i > 0))
               for i in range(max(1, retries))]
    last_error = None
    for prompt in prompts:
        try:
            text = _generate(prompt, model, backend=backend)
        except NoKeyError:
            raise
        except (urllib.error.URLError, OSError, ValueError, TimeoutError) as exc:
            last_error = exc
            break
        parsed = parse_judge_response(text)
        if parsed is not None:
            return {"score": parsed["score"], "reasoning": parsed["reasoning"],
                    "criteria": parsed["criteria"], "judge_model": model}
        last_error = text
    result = dict(FALLBACK)
    result["judge_model"] = model
    result["reasoning"] = "unjudged"
    return result


def score_output_with_backend(*args, **kwargs):
    """Alias kept for callers that pass backend explicitly (see runner)."""
    return score_output(*args, **kwargs)


def judge_backend_for(judge_model):
    """Map a judge_model string to its backend: 'ollama' -> local Ollama."""
    return "ollama" if (judge_model or "").strip().lower() == "ollama" else "gemini"
