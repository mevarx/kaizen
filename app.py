#!/usr/bin/env python
"""kaizen — measure whether an Agent Skill actually improves an agent.

Stdlib-only HTTP server (http.server.ThreadingHTTPServer). No third-party
imports, no build step, one command to run everything (NFR-1 / NFR-2).

Routes (see INTERFACES.md §app.py — the authoritative contract):

    GET  /                     dashboard shell (static/index.html, or the
                               built-in fallback page if that file is absent)
    GET  /events?run_id=...    SSE stream, Content-Type: text/event-stream,
                               chunked; streams the events produced by a
                               background thread running runner.run_paired
    POST /run                  body {"skill": {"dir"|"md"}, "task", "models"}
                               -> starts a run in a thread, returns {"run_id"}
    GET  /skills               {"skills": [{name, description, dir}]}
    GET  /cached/<run_id>      full cached run JSON (demo insurance)

Every route works with GEMINI_API_KEY unset. POST /run then emits a "no-key"
banner event and replays the seeded cached run from cached/latest.json, so the
dashboard renders a complete, contract-shaped stream with zero configuration.

Owned by Agent D. This file does not implement skill loading, paired running,
judging, or caching — it delegates to skill_loader.py / runner.py / judge.py /
cache.py (owned by Agents A and B) and degrades gracefully when they are absent.
"""

import argparse
import hashlib
import inspect
import json
import os
import re
import sys
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlparse

HERE = os.path.dirname(os.path.abspath(__file__))
SKILLS_DIR = os.path.join(HERE, "skills")
STATIC_DIR = os.path.join(HERE, "static")
CACHED_DIR = os.path.join(HERE, "cached")
SEEDED_RUN = os.path.join(CACHED_DIR, "latest.json")

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8000
DEFAULT_JUDGE = "gemma"
DEFAULT_JUDGE_MODEL = "gemma-4-31b-it"

# Gemma 4 reference arms (INTERFACES.md "Shared environment facts").
DEFAULT_MODELS = ["gemma-4-31b-it", "gemma-4-26b-a4b-it"]

MAX_MODELS = 6          # free tier is ~16k TPM; refuse to DoS ourselves
MAX_BODY = 1 << 20      # 1 MiB pasted SKILL.md / task body
MAX_RUNS = 64           # prune finished runs so a long session can't leak

# Seconds between events when replaying the seeded cached run. Non-zero on
# purpose: a cached run that lands in one burst looks broken, a paced one looks
# live. Total stays well under NFR-7's 2 s for a handful of events.
REPLAY_DELAY_S = 0.30

# Optional backend modules. Every one of them is optional: app.py must start and
# serve /skills and /cached/<id> even if a sibling agent's file is not landed
# yet (or is mid-edit and raising on import).
skill_loader = None
runner = None
judge = None
cache_mod = None
LOAD_ERRORS = {}

for _name in ("skill_loader", "runner", "judge", "cache"):
    try:
        _mod = __import__(_name)
    except Exception as exc:  # pragma: no cover - depends on sibling agents
        LOAD_ERRORS[_name] = f"{type(exc).__name__}: {exc}"
    else:
        if _name == "skill_loader":
            skill_loader = _mod
        elif _name == "runner":
            runner = _mod
        elif _name == "judge":
            judge = _mod
        else:
            cache_mod = _mod

# Judge backend selection. "gemma" -> Gemini API; "ollama" -> localhost:11434.
JUDGE_BACKEND = DEFAULT_JUDGE


# --------------------------------------------------------------------------
# .env (no dependency; PRD §16 says the key lives in the environment only)
# --------------------------------------------------------------------------

def load_dotenv(path=None):
    """Load KEY=VALUE lines from .env into os.environ (never overwriting)."""
    path = path or os.path.join(HERE, ".env")
    if not os.path.isfile(path):
        return False
    try:
        with open(path, encoding="utf-8") as fh:
            for raw in fh:
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, val = line.partition("=")
                key = key.strip()
                val = val.strip().strip('"').strip("'")
                if key and key not in os.environ:
                    os.environ[key] = val
    except OSError:
        return False
    return True


def has_api_key():
    return bool(os.environ.get("GEMINI_API_KEY", "").strip())


# --------------------------------------------------------------------------
# Rubric extraction — the skill carries its own judging criteria (FR-3.1)
# --------------------------------------------------------------------------

_FENCE_RE = re.compile(r"```([A-Za-z0-9_-]*)[ \t]*\r?\n(.*?)```", re.DOTALL)
_HEADING_RE = re.compile(r"^#{1,6}[ \t]*rubric[ \t]*$", re.IGNORECASE | re.MULTILINE)


def _as_criterion(item):
    """Normalise one rubric entry to {"name", "weight", "check"}."""
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
    return {
        "name": name,
        "weight": weight,
        "check": str(item.get("check") or item.get("description") or name),
    }


def extract_rubric(skill):
    """Pull the rubric out of a SKILL.md body.

    Fixtures declare it as a JSON fence under a `## Rubric` heading; a bare
    ```json fence containing a list of {name, ...} objects is accepted too, so a
    pasted skill that follows the looser convention still gets judged. Returns
    [] when there is nothing machine-readable — the runner then judges against
    an empty rubric rather than failing the run.
    """
    body = (skill or {}).get("body") or ""
    blocks = []
    heading_at = None
    hm = _HEADING_RE.search(body)
    if hm:
        heading_at = hm.start()
    for m in _FENCE_RE.finditer(body):
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
        if not criteria:
            continue
        # Prefer the fence directly under a "Rubric" heading.
        blocks.append((0 if heading_at is not None and m.start() > heading_at else 1,
                       m.start(), criteria))
    if not blocks:
        return []
    blocks.sort(key=lambda t: (t[0], t[1]))
    return blocks[0][2]


# --------------------------------------------------------------------------
# Lift / spread / headline math (FR-4)
# --------------------------------------------------------------------------

def classify_lift(lift):
    """POSITIVE / NEUTRAL / NEGATIVE. lift == 0 is NEUTRAL (INTERFACES.md)."""
    if lift is None:
        return "UNJUDGED"
    if lift > 0:
        return "POSITIVE"
    if lift < 0:
        return "NEGATIVE"
    return "NEUTRAL"


def _signed(value):
    return f"+{value}" if value > 0 else str(value)


def summarize_results(result_events, models=None):
    """Fold `result` events into the contract's `summary` event.

    app.py owns this because it has to produce a summary for runs it did not
    originate too — replayed cached runs, and live runs whose runner omitted
    the trailing summary. lift = treatment - baseline in percentage points;
    spread = max(lift) - min(lift) across judged models.
    """
    order, per_model = [], {}
    for ev in result_events:
        if not isinstance(ev, dict) or ev.get("type") != "result":
            continue
        model = ev.get("model")
        if model is None:
            continue
        if model not in per_model:
            per_model[model] = {"baseline": None, "treatment": None}
            order.append(model)
        phase = ev.get("phase")
        if phase in ("baseline", "treatment"):
            per_model[model][phase] = ev.get("score")

    if models:
        for model in models:
            if model not in per_model:
                per_model[model] = {"baseline": None, "treatment": None}
                order.append(model)

    rows = []
    for model in order:
        base, treat = per_model[model]["baseline"], per_model[model]["treatment"]
        if base is None or treat is None:
            rows.append({"model": model, "baseline": base, "treatment": treat,
                         "lift": None, "class": "UNJUDGED"})
        else:
            lift = int(treat) - int(base)
            rows.append({"model": model, "baseline": base, "treatment": treat,
                         "lift": lift, "class": classify_lift(lift)})

    lifts = [r["lift"] for r in rows if r["lift"] is not None]
    spread = (max(lifts) - min(lifts)) if lifts else None
    return {"type": "summary", "per_model": rows, "spread": spread,
            "headline": build_headline(rows, spread)}


def build_headline(rows, spread):
    judged = [r for r in rows if r["lift"] is not None]
    missing = len(rows) - len(judged)
    if not judged:
        return ("no judged cells - the judge returned malformed JSON for every "
                "output; raw outputs are still available below")
    best = max(judged, key=lambda r: r["lift"])
    worst = min(judged, key=lambda r: r["lift"])
    if spread is None or best["model"] == worst["model"] or spread == 0:
        text = (f"no spread - every model lifts {_signed(best['lift'])}; this "
                f"skill behaves the same across models")
    else:
        text = (f"this skill lifts {_signed(best['lift'])} on {best['model']} "
                f"and {_signed(worst['lift'])} on {worst['model']} - "
                f"spread {spread} pp")
        if any(r["class"] == "NEGATIVE" for r in judged):
            text += (". NVIDIA ACES: 27% of skills do nothing or hurt - "
                     "this is one of them")
    if missing:
        text += f" ({missing} model(s) unjudged)"
    return text


def normalize_judge_result(result):
    """Defensive normalisation of a judge dict (FR-3.3 fallback path).

    score is clamped to 0..100, criteria becomes a list of {name, met}, and an
    absent/malformed score becomes None -> rendered as "unjudged" upstream.
    """
    if not isinstance(result, dict):
        return {"score": None, "reasoning": "unjudged", "criteria": [],
                "judge_model": None}
    score = result.get("score")
    if isinstance(score, bool) or not isinstance(score, (int, float)):
        score = None
    else:
        score = max(0, min(100, int(score)))
    criteria = []
    for item in result.get("criteria") or []:
        if isinstance(item, dict) and item.get("name"):
            criteria.append({"name": str(item["name"]),
                             "met": bool(item.get("met"))})
        elif isinstance(item, str):
            criteria.append({"name": item, "met": False})
    return {"score": score,
            "reasoning": str(result.get("reasoning") or "unjudged"),
            "criteria": criteria,
            "judge_model": result.get("judge_model")}


# --------------------------------------------------------------------------
# Seeded cached run (demo insurance, NFR-3 / FR-5.4)
# --------------------------------------------------------------------------

_SEED_LOCK = threading.Lock()
_SEED_CACHE = {"path": None, "mtime": None, "data": None}


def load_seeded_run():
    """Read cached/latest.json once per mtime. Returns None if unavailable."""
    try:
        mtime = os.path.getmtime(SEEDED_RUN)
    except OSError:
        return None
    with _SEED_LOCK:
        if _SEED_CACHE["data"] is not None and _SEED_CACHE["mtime"] == mtime \
                and _SEED_CACHE["path"] == SEEDED_RUN:
            return _SEED_CACHE["data"]
    try:
        with open(SEEDED_RUN, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    with _SEED_LOCK:
        _SEED_CACHE.update(path=SEEDED_RUN, mtime=mtime, data=data)
    return data


def seeded_events():
    """The `result`/`summary` events of the seeded run, contract-shaped."""
    seed = load_seeded_run() or {}
    events = [e for e in (seed.get("events") or []) if isinstance(e, dict)]
    results = [e for e in events if e.get("type") == "result"]
    if not any(e.get("type") == "summary" for e in events):
        events.append(summarize_results(results))
    return events


# --------------------------------------------------------------------------
# Run registry + background execution
# --------------------------------------------------------------------------

class Run:
    """A run's event log plus the condition its SSE reader waits on."""

    def __init__(self, run_id, meta=None):
        self.run_id = run_id
        self.events = []
        self.meta = meta or {}
        self.cond = threading.Condition()
        self.done = False
        self.created = time.time()

    def push(self, event):
        with self.cond:
            self.events.append(event)
            self.cond.notify_all()

    def finish(self):
        with self.cond:
            self.done = True
            self.cond.notify_all()


RUNS = {}
RUNS_LOCK = threading.Lock()


def register_run(run):
    with RUNS_LOCK:
        RUNS[run.run_id] = run
        finished = [r for r in RUNS.values() if r.done]
        if len(RUNS) > MAX_RUNS:
            for old in sorted(finished, key=lambda r: r.created)[:len(RUNS) - MAX_RUNS]:
                RUNS.pop(old.run_id, None)
    return run


def get_run(run_id):
    with RUNS_LOCK:
        return RUNS.get(run_id)


def new_run_id(skill_name):
    slug = re.sub(r"[^a-z0-9]+", "-", (skill_name or "run").lower()).strip("-") or "run"
    digest = hashlib.sha1(os.urandom(16)).hexdigest()[:6]
    return f"{slug[:24]}-{int(time.time())}-{digest}"


def banner(level, message):
    return {"type": "banner", "level": level, "message": message}


def _judge_exception_types():
    """Exception classes that mean 'no usable API credentials/backend'."""
    types = []
    if judge is not None:
        for attr in ("NoKeyError", "NoApiKeyError", "JudgeUnavailable"):
            exc = getattr(judge, attr, None)
            if isinstance(exc, type) and issubclass(exc, BaseException):
                types.append(exc)
    types.extend([PermissionError, ConnectionError, OSError])
    return tuple(dict.fromkeys(types))


def _call_run_paired(skill, task, models, judge_model):
    """Call runner.run_paired passing only the kwargs it actually declares.

    INTERFACES.md fixes the positional signature; the extra kwargs (rubric,
    judge backend) are forwarded only when the runner opts in, so app.py keeps
    working unchanged if Agent B lands the minimal version.
    """
    kwargs = {"judge_model": judge_model}
    try:
        params = inspect.signature(runner.run_paired).parameters
    except (TypeError, ValueError):  # builtins / C callables
        params = {}
    if "rubric" in params:
        kwargs["rubric"] = skill.get("rubric") or []
    for key in ("judge", "judge_backend", "backend"):
        if key in params:
            kwargs[key] = JUDGE_BACKEND
    if "judge_model" not in params:
        kwargs.pop("judge_model", None)
    return runner.run_paired(skill, task, models, **kwargs)


def _replay_cached(run):
    """Push the seeded run's events, paced so the dashboard animates.

    Ends by finishing the run so the SSE stream terminates; callers that need
    to keep streaming afterwards (none today) must not use this directly.
    """
    events = seeded_events()
    if not events:
        run.push(banner("error", "No seeded cached run found (cached/latest.json "
                                 "is missing or unreadable) and no live backend "
                                 "is available."))
        run.finish()
        return
    seed = load_seeded_run() or {}
    provenance = seed.get("provenance") or "seeded demo run"
    run.meta.update(source="cached", run_id_seed=seed.get("run_id", "latest"),
                    provenance=provenance)
    try:
        for event in events:
            if REPLAY_DELAY_S > 0:
                time.sleep(REPLAY_DELAY_S)
            run.push(event)
        run.meta["headline"] = next(
            (e.get("headline") for e in events if e.get("type") == "summary"), "")
    finally:
        run.finish()


def _drive_live(run, skill, task, models, judge_model):
    """Background thread body: run the paired A/B and stream every event."""
    try:
        if not has_api_key():
            run.push(banner(
                "no-key",
                "GEMINI_API_KEY is not set - showing the seeded cached run. "
                "Add a free key from aistudio.google.com to measure live."))
            _replay_cached(run)
            return

        run.push({"type": "status", "text": f"running baseline+treatment on "
                                            f"{len(models)} model(s)"})
        results = []
        saw_summary = False
        for event in _call_run_paired(skill, task, models, judge_model):
            if not isinstance(event, dict):
                continue
            kind = event.get("type")
            if kind == "result":
                results.append(event)
            elif kind == "summary":
                saw_summary = True
            run.push(event)
        if not saw_summary:
            run.push(summarize_results(results, models))
    except _judge_exception_types() as exc:
        run.push(banner("no-key", f"Judge backend unavailable ({exc}) - "
                                  "falling back to the seeded cached run."))
        _replay_cached(run)
    except Exception as exc:
        detail = f"{type(exc).__name__}: {exc}".strip()
        run.push(banner("error", f"Run failed: {detail}"))
        traceback.print_exc(file=sys.stderr)
        run.push(banner("degraded", "Gemini API degraded - replaying the seeded "
                                    "cached run instead."))
        try:
            _replay_cached(run)
        except Exception:  # never let the thread die mid-stream
            traceback.print_exc(file=sys.stderr)
    finally:
        run.finish()


def start_run(skill, task, models, judge_model):
    run = register_run(Run(new_run_id(skill.get("name")), {
        "skill": skill.get("name"), "task": task, "models": list(models),
        "judge_model": judge_model, "live": has_api_key()}))
    thread = threading.Thread(target=_drive_live,
                              args=(run, skill, task, models, judge_model),
                              name=f"kaizen-run-{run.run_id}", daemon=True)
    thread.start()
    return run


# --------------------------------------------------------------------------
# Skill resolution for POST /run
# --------------------------------------------------------------------------

def _inside(path, root):
    try:
        return os.path.commonpath([os.path.abspath(path), os.path.abspath(root)]) \
            == os.path.abspath(root)
    except ValueError:
        return False


def resolve_skill(payload):
    """Turn a /run body's `skill` field into a loaded skill dict.

    Returns (skill, error). A pasted `md` is loaded in memory (no directory, so
    no scripts are ever executed from it); a `dir` must live inside skills/.
    """
    if skill_loader is None:
        return None, ("skill_loader.py is unavailable - cannot load a skill "
                      f"({LOAD_ERRORS.get('skill_loader', 'import failed')})")

    spec = payload.get("skill")
    if not isinstance(spec, dict):
        return None, "body.skill must be an object: {\"dir\": \"skills/x\"} or {\"md\": \"...\"}"

    try:
        if spec.get("md"):
            name = spec.get("name") or "pasted-skill"
            skill = skill_loader.load_skill_from_text(str(spec["md"]), name)
            skill.setdefault("dir", None)
        elif spec.get("dir"):
            raw = os.path.abspath(os.path.join(HERE, str(spec["dir"])))
            if not _inside(raw, SKILLS_DIR):
                return None, ("skill.dir must point inside skills/ - pasted "
                              "skills use {\"md\": ...} instead")
            if not os.path.isdir(raw):
                return None, f"no such skill directory: {spec['dir']}"
            skill = skill_loader.load_skill(raw)
        elif spec.get("name"):
            raw = os.path.join(SKILLS_DIR, str(spec["name"]))
            if not os.path.isdir(raw):
                return None, f"no bundled skill named {spec['name']!r}"
            skill = skill_loader.load_skill(raw)
        else:
            return None, "body.skill needs one of: dir, md, name"
    except Exception as exc:
        return None, f"{type(exc).__name__}: {exc}"

    validate = getattr(skill_loader, "validate_skill", None)
    if callable(validate):
        try:
            errors = validate(skill) or []
        except Exception as exc:
            return None, f"validate_skill raised {type(exc).__name__}: {exc}"
        if errors:
            return None, "skill failed validation: " + "; ".join(map(str, errors))

    skill["rubric"] = extract_rubric(skill)
    return skill, None


def resolve_models(payload):
    raw = payload.get("models")
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, list) or not raw:
        return list(DEFAULT_MODELS), None
    models, seen = [], set()
    for item in raw:
        model = str(item).strip()
        if model and model not in seen:
            seen.add(model)
            models.append(model)
    if not models:
        return list(DEFAULT_MODELS), None
    if len(models) > MAX_MODELS:
        return None, f"at most {MAX_MODELS} models per run (free-tier rate limits)"
    return models, None


def list_skills():
    """[{name, description, dir}] from the bundled fixtures."""
    out = []
    if not os.path.isdir(SKILLS_DIR):
        return out
    for entry in sorted(os.listdir(SKILLS_DIR)):
        path = os.path.join(SKILLS_DIR, entry)
        if not os.path.isdir(path) or not os.path.isfile(os.path.join(path, "SKILL.md")):
            continue
        item = {"name": entry, "description": "", "dir": os.path.relpath(path, HERE).replace(os.sep, "/")}
        if skill_loader is None:
            item["error"] = "skill_loader.py unavailable"
            out.append(item)
            continue
        try:
            skill = skill_loader.load_skill(path)
            item["name"] = skill.get("name") or entry
            item["description"] = skill.get("description") or ""
            item["scripts"] = sorted((skill.get("scripts") or {}).keys())
            item["rubric"] = extract_rubric(skill)
        except Exception as exc:
            item["error"] = f"{type(exc).__name__}: {exc}"
        out.append(item)
    return out


# --------------------------------------------------------------------------
# Built-in fallback dashboard (used only when static/index.html is absent)
# --------------------------------------------------------------------------

FALLBACK_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>kaizen - cached mode</title>
<style>
:root{--bg:#0d1117;--card:#161b22;--line:#30363d;--fg:#e6edf3;--dim:#8b949e;
--ok:#3fb950;--warn:#d29922;--err:#f85149}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.55
ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}
header{padding:16px 20px;border-bottom:1px solid var(--line)}
h1{font-size:16px;margin:0 0 4px}
p{margin:0;color:var(--dim);font-size:12.5px}
main{padding:20px;display:grid;gap:14px;grid-template-columns:repeat(auto-fit,minmax(320px,1fr))}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px 16px}
.card h2{font-size:11px;text-transform:uppercase;letter-spacing:.09em;color:var(--dim);margin:0 0 10px}
.lift{font-size:34px;font-weight:700;letter-spacing:-.02em}
.pos{color:var(--ok)}.neg{color:var(--err)}.neu{color:var(--warn)}.un{color:var(--dim)}
.banner{border:1px solid var(--line);border-left-width:3px;padding:10px 14px;border-radius:8px;
background:#11161d;font-size:12.5px;margin:0 20px 16px}
.banner.no-key{border-left-color:var(--warn)}
.banner.degraded{border-left-color:var(--err)}
details{margin-top:8px;font-size:12.5px}summary{cursor:pointer;color:var(--dim)}
pre{margin:8px 0 0;white-space:pre-wrap;word-break:break-word;color:var(--dim);font-size:12px}
.foot{padding:0 20px 24px;color:var(--dim);font-size:13px}
code{background:#1f242c;padding:1px 5px;border-radius:4px}
</style></head><body>
<header><h1>kaizen</h1>
<p>Measure whether an Agent Skill actually improves your agent - Gemma 4 as the
open-weight reference arm and judge. NVIDIA ACES (Aug 2026): 27% of paired runs
show zero or negative lift; structure predicts runtime behaviour at rho ~= -0.02.</p></header>
<div id="banners"></div><main id="cards"></main>
<div class="foot" id="foot"></div>
<script>
const $=id=>document.getElementById(id);
const esc=s=>String(s==null?'':s).replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));
const cls=l=>l>0?'pos':(l<0?'neg':(l==null?'un':'neu'));
const signed=l=>l==null?'-':(l>0?'+'+l:''+l);

function banner(level,msg){const d=document.createElement('div');
  d.className='banner '+(level||'');d.textContent=msg;$('banners').appendChild(d);}

function card(model){
  const el=document.createElement('section');el.className='card';el.id='c-'+model;
  el.innerHTML='<h2>'+esc(model)+'</h2><div class="lift un">--</div>'+
    '<pre>baseline - / treatment -</pre>';
  $('cards').appendChild(el);return el;}

const models={};
function render(ev){
  if(ev.type==='banner'){banner(ev.level,ev.message);return;}
  if(ev.type==='result'){
    const m=ev.model||'?';
    if(!models[m])models[m]={};models[m][ev.phase]=ev;
    let el=$('c-'+m); if(!el)el=card(m);
    const b=models[m].baseline,t=models[m].treatment;
    const lift=(b&&t&&b.score!=null&&t.score!=null)?t.score-b.score:null;
    el.querySelector('.lift').className='lift '+cls(lift);
    el.querySelector('.lift').textContent=lift==null?'--':signed(lift)+' pp';
    el.querySelector('pre').textContent=
      'baseline '+(b&&b.score!=null?b.score:'-')+' -> treatment '+
      (t&&t.score!=null?t.score:'-')+(ev.cached?' (cached)':'');
    if(ev.reasoning&&ev.reasoning!=='unjudged'){
      el.insertAdjacentHTML('beforeend','<details><summary>judge reasoning</summary>'+
        '<pre>'+esc(ev.reasoning)+'</pre></details>');
    }
    if(ev.output){
      el.insertAdjacentHTML('beforeend','<details><summary>raw output'+
        (ev.cached?' (cached)':'')+'</summary><pre>'+esc(ev.output)+'</pre></details>');
    }
    return;}
  if(ev.type==='summary'){
    $('foot').innerHTML='<strong>spread '+(ev.spread==null?'-':ev.spread)+
      ' pp</strong> - '+esc(ev.headline||'');
    for(const r of ev.per_model||[]){
      let el=$('c-'+r.model); if(!el)el=card(r.model);
      const l=r.lift;const big=el.querySelector('.lift');
      big.className='lift '+cls(l);big.textContent=l==null?'--':signed(l)+' pp';
      el.querySelector('pre').textContent='baseline '+(r.baseline==null?'-':r.baseline)+
        ' -> treatment '+(r.treatment==null?'-':r.treatment)+' ('+r.class+')';}
    return;}
  if(ev.type==='status'){$('foot').textContent=esc(ev.text);}
}

async function cached(){
  try{
    const r=await fetch('/cached/latest');
    if(!r.ok)throw new Error('HTTP '+r.status);
    const d=await r.json();
    for(const e of d.events||[])render(e);
    if(d.summary)render(d.summary);
  }catch(e){banner('degraded','Could not load the cached run: '+e.message);}
}
cached();
</script></body></html>"""


# --------------------------------------------------------------------------
# HTTP handler
# --------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "kaizen/1.0"

    # -- plumbing ----------------------------------------------------------

    def log_message(self, fmt, *args):
        sys.stderr.write("  %s - %s\n" % (self.address_string(), fmt % args))

    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def send_json(self, obj, status=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self._cors()
        self.end_headers()
        self.wfile.write(body)

    def send_error_json(self, status, message):
        self.send_json({"error": message, "status": status}, status=status)

    def read_body(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return None, "invalid Content-Length"
        if length <= 0:
            return {}, None
        if length > MAX_BODY:
            return None, f"body too large (max {MAX_BODY} bytes)"
        raw = self.rfile.read(length)
        try:
            parsed = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            return None, f"body is not valid JSON: {exc}"
        if not isinstance(parsed, dict):
            return None, "body must be a JSON object"
        return parsed, None

    # -- routes ------------------------------------------------------------

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Content-Length", "0")
        self._cors()
        self.end_headers()

    def do_GET(self):
        parsed = urlparse(self.path)
        path = unquote(parsed.path).rstrip("/") or "/"
        query = parse_qs(parsed.query)

        if path in ("/", "/index.html"):
            return self.handle_root()
        if path == "/events":
            return self.handle_events(query)
        if path == "/skills":
            return self.send_json({"skills": list_skills(),
                                   "defaults": {"models": DEFAULT_MODELS,
                                                "judge_model": DEFAULT_JUDGE_MODEL,
                                                "judge": JUDGE_BACKEND}})
        if path.startswith("/cached/"):
            return self.handle_cached(path[len("/cached/"):])
        if path == "/health":
            return self.send_json({
                "ok": True, "key": has_api_key(), "judge": JUDGE_BACKEND,
                "modules": {k: (v is not None) for k, v in
                            (("skill_loader", skill_loader), ("runner", runner),
                             ("judge", judge), ("cache", cache_mod))},
                "load_errors": LOAD_ERRORS,
                "seeded_run": bool(load_seeded_run())})
        if path.startswith("/static/"):
            return self.handle_static(path[len("/static/"):])
        if path == "/favicon.ico":
            self.send_response(204)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        return self.send_error_json(404, f"no such route: {path}")

    def do_POST(self):
        parsed = urlparse(self.path)
        path = unquote(parsed.path).rstrip("/") or "/"
        if path != "/run":
            return self.send_error_json(404, f"no such route: {path}")
        payload, err = self.read_body()
        if err:
            return self.send_error_json(400, err)

        task = payload.get("task")
        if not isinstance(task, str) or not task.strip():
            return self.send_error_json(400, "body.task is required (non-empty string)")

        skill, err = resolve_skill(payload)
        if err:
            return self.send_error_json(400, err)

        models, err = resolve_models(payload)
        if err:
            return self.send_error_json(400, err)

        judge_model = payload.get("judge_model") or os.environ.get(
            "KAIZEN_JUDGE_MODEL") or DEFAULT_JUDGE_MODEL

        try:
            run = start_run(skill, task.strip(), models, str(judge_model))
        except Exception as exc:
            return self.send_error_json(500, f"could not start run: {exc}")
        return self.send_json({
            "run_id": run.run_id,
            "events_url": f"/events?run_id={run.run_id}",
            "skill": skill.get("name"),
            "models": models,
            "judge_model": str(judge_model),
            "live": has_api_key(),
            "rubric_criteria": len(skill.get("rubric") or []),
        }, status=202)

    # -- handlers ----------------------------------------------------------

    def handle_root(self):
        page = os.path.join(STATIC_DIR, "index.html")
        try:
            with open(page, "rb") as fh:
                body = fh.read()
        except OSError:
            body = FALLBACK_PAGE.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def handle_static(self, rel):
        rel = rel.lstrip("/\\")
        target = os.path.abspath(os.path.join(STATIC_DIR, rel))
        if not _inside(target, STATIC_DIR) or not os.path.isfile(target):
            return self.send_error_json(404, f"no such static file: {rel}")
        ctype = {".html": "text/html; charset=utf-8", ".css": "text/css",
                 ".js": "application/javascript", ".json": "application/json",
                 ".svg": "image/svg+xml", ".png": "image/png",
                 ".ico": "image/x-icon"}.get(os.path.splitext(target)[1].lower(),
                                             "application/octet-stream")
        try:
            with open(target, "rb") as fh:
                body = fh.read()
        except OSError as exc:
            return self.send_error_json(500, f"cannot read {rel}: {exc}")
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def handle_cached(self, run_id):
        run_id = (run_id or "").strip().strip("/") or "latest"
        if run_id == "latest":
            seed = load_seeded_run()
            if seed is None:
                return self.send_error_json(404, "no seeded cached run "
                                                 "(cached/latest.json is missing)")
            return self.send_json(seed)
        if not re.fullmatch(r"[A-Za-z0-9._-]{1,64}", run_id):
            return self.send_error_json(400, "invalid run_id")
        candidate = os.path.join(CACHED_DIR, run_id + ".json")
        if not _inside(candidate, CACHED_DIR) or not os.path.isfile(candidate):
            return self.send_error_json(404, f"no cached run named {run_id!r}")
        try:
            with open(candidate, encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError) as exc:
            return self.send_error_json(500, f"cannot read cached run: {exc}")
        return self.send_json(data)

    def handle_events(self, query):
        run_id = (query.get("run_id") or [""])[0].strip()
        run = get_run(run_id)
        if run is None and run_id == "latest":
            # Frontend convenience: GET /events?run_id=latest with no POST.
            run = register_run(Run("latest-replay", {"source": "cached"}))
            threading.Thread(target=_replay_cached, args=(run,),
                             name="kaizen-replay-latest", daemon=True).start()
        if run is None:
            return self.send_error_json(404, f"unknown run_id {run_id!r} - "
                                             "POST /run first")

        last_id = 0
        try:
            last_id = int((query.get("last_id") or
                           self.headers.get("Last-Event-ID") or 0))
        except ValueError:
            last_id = 0

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache, no-transform")
        self.send_header("X-Accel-Buffering", "no")
        self.send_header("Transfer-Encoding", "chunked")
        self.send_header("Connection", "close")
        self._cors()
        self.end_headers()
        self.close_connection = True

        index = max(0, last_id)
        keepalive = time.time()
        seq = 0
        try:
            while True:
                with run.cond:
                    while index >= len(run.events) and not run.done:
                        run.cond.wait(timeout=1.0)
                    batch = run.events[index:]
                    index += len(batch)
                    finished = run.done and index >= len(run.events)
                for event in batch:
                    seq += 1
                    self._sse_frame(event, seq)
                if finished:
                    break
                if time.time() - keepalive > 15:
                    self._sse_comment("keepalive")
                    keepalive = time.time()
        except (BrokenPipeError, ConnectionResetError):
            pass  # client navigated away mid-stream
        finally:
            try:
                self.wfile.write(b"0\r\n\r\n")
                self.wfile.flush()
            except Exception:
                pass

    def _sse_frame(self, event, seq):
        payload = json.dumps(event, ensure_ascii=False)
        frame = ("id: %d\r\n" % seq).encode("utf-8")
        frame += b"data: " + payload.encode("utf-8") + b"\r\n\r\n"
        # Proper chunked transfer framing (HTTP/1.1, RFC 9112 §7.1).
        self.wfile.write(b"%x\r\n" % len(frame) + frame + b"\r\n")
        self.wfile.flush()

    def _sse_comment(self, text):
        frame = b": " + text.encode("utf-8") + b"\r\n\r\n"
        self.wfile.write(b"%x\r\n" % len(frame) + frame + b"\r\n")
        self.wfile.flush()


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------

def build_arg_parser():
    p = argparse.ArgumentParser(
        prog="kaizen",
        description="Measure whether an Agent Skill improves an agent "
                    "(Gemma 4 as reference arm + judge).")
    p.add_argument("--port", type=int,
                   default=int(os.environ.get("KAIZEN_PORT") or DEFAULT_PORT),
                   help=f"port to listen on (default {DEFAULT_PORT})")
    p.add_argument("--host", default=os.environ.get("KAIZEN_HOST") or DEFAULT_HOST,
                   help=f"interface to bind (default {DEFAULT_HOST})")
    p.add_argument("--judge", default=os.environ.get("KAIZEN_JUDGE") or DEFAULT_JUDGE,
                   choices=["gemma", "ollama", DEFAULT_JUDGE_MODEL],
                   help="judge backend: 'gemma' (Gemini API) or 'ollama' "
                        "(local gemma3:4b), or an explicit judge model id")
    return p


def main(argv=None):
    global JUDGE_BACKEND
    args = build_arg_parser().parse_args(argv)
    load_dotenv()

    if args.judge in ("gemma", "ollama"):
        JUDGE_BACKEND = args.judge
    else:  # an explicit judge model id was passed
        os.environ["KAIZEN_JUDGE_MODEL"] = args.judge
        JUDGE_BACKEND = "gemma"
    os.environ.setdefault("KAIZEN_JUDGE", JUDGE_BACKEND)

    key = has_api_key()
    print("=" * 68)
    print("kaizen - does this Agent Skill actually help?")
    print("=" * 68)
    print(f"  dashboard   http://{args.host}:{args.port}")
    print(f"  judge       {JUDGE_BACKEND}"
          f"{'' if JUDGE_BACKEND == 'gemma' else ' (local, :11434)'}")
    print(f"  api key     {'set - live runs enabled' if key else 'NOT SET - cached/demo mode'}")
    if not key:
        print("               export GEMINI_API_KEY=...   (free: aistudio.google.com)")
    for name, err in LOAD_ERRORS.items():
        print(f"  WARNING     {name}.py failed to import: {err}")
        print(f"               routes that need it degrade; /skills and /cached still work")
    print(f"  fixtures    {len(list_skills())} bundled skill(s) in skills/")
    print(f"  cached run  {'seeded (renders with no key)' if load_seeded_run() else 'MISSING (cached/latest.json)'}")
    print("-" * 68)
    print("  Ctrl-C to stop")
    print()

    server = ThreadingHTTPServer((args.host, args.port), Handler)
    server.daemon_threads = True
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped.")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
