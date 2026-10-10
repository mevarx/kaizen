# kaizen — Interface Contract (build agents compose against this file)

Project: **kaizen** — measure whether an Agent Skill (SKILL.md) improves an agent, with Gemma 4 as the open-weight reference arm and judge.
Read `PRD.md` for full context. This file is the authoritative contract for module boundaries.

## Shared environment facts (verified — do not re-verify)

- **Use `python` (3.11.16, has requests+fastapi installed). NEVER `python3` (3.13.14, ZERO packages).**
- Windows 11, git-bash shell. Native tools need `C:/Users/shree/...`-style paths, not `/c/Users/...`.
- Stdlib only. No pip installs. `urllib.request` for HTTP (reference: `static/reference-index.html` and the stackcrawl app).
- **GEMINI_API_KEY is NOT currently set.** Code must run and be testable without a key (cached mode is a first-class feature).
- Gemini API: `POST https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent`, header `x-goog-api-key: {key}`. Verified reachable from India.
- Models: `gemma-4-31b-it`, `gemma-4-26b-a4b-it` (Gemma 4 reference arms). `gemma-4-e2b/e4b/12b` are NOT on the API — do not use them.
- Ollama `gemma3:4b` installed (server needs `ollama serve`), used as offline judge fallback.
- Reference code from verified research crawl lives in `$LOCALAPPDATA/Temp/stackcrawl/` (read-only reference, already adapted into this repo's `skill_loader.py`).

## File ownership (one agent per module group, non-overlapping)

| Agent | Owns | Must not touch |
|---|---|---|
| A — Loader | `skill_loader.py` | everything else |
| B — Runner+Judge | `runner.py`, `judge.py`, `cache.py` | app.py, static/, skills/ |
| C — Frontend | `static/index.html` | backend files |
| D — Server+Fixtures | `app.py`, `skills/`, `cached/`, `tests/`, `README.md`, `LICENSE`, `.gitignore`, `.env.example` | skill_loader.py, runner.py, judge.py, cache.py |

D may not modify A/B/C's files; if D needs a contract change, note it in the final report and I will integrate.

## Module contracts

### skill_loader.py (Agent A)

Already adapted from the verified reference. Extend it. Public API:

```python
parse_frontmatter(text: str) -> tuple[dict, str]   # (meta, body); raises ValueError on missing frontmatter
load_skill(skill_dir: str) -> dict                  # {name, description, allowed_tools, metadata, body, scripts, dir}
load_skill_from_text(md_text: str, name: str) -> dict  # for pasted skills (in-memory, no dir)
run_script(skill_dir: str, script: str, args=None, timeout=60) -> dict  # {ok, stdout, stderr, returncode}
validate_skill(skill: dict) -> list[str]            # returns list of errors, [] = valid; enforces name rules
```

Name rules (Agent Skill Open Standard): `[a-z0-9]+(-[a-z0-9]+)*`, ≤64 chars, must match directory name (for dir-loaded skills). `description` required ≤1024. YAML failure → fallback line parser (spec-permitted). `scripts/` optional → instructions-only mode.

### judge.py (Agent B)

```python
def score_output(task: str, rubric: list[dict], output: str, model: str = "gemma-4-31b-it") -> dict
# -> {"score": int 0-100, "reasoning": str, "criteria": [{"name": str, "met": bool}], "judge_model": str}
```

- Fixed judge prompt, shipped in-repo (reproducibility, FR-3.2). Put it in `judge.py` as a module-level constant.
- Judge calls the Gemini API directly (urllib). Reads `GEMINI_API_KEY` from env; if absent, raises `NoKeyError`.
- Parse judge JSON defensively (FR-3.3): one retry with a stricter prompt, then fallback `{"score": None, "reasoning": "unjudged", ...}` — never crash the run.
- Judge model configurable, defaults to `gemma-4-31b-it`.

### runner.py (Agent B)

```python
def run_paired(skill: dict, task: str, models: list[str], judge_model: str = "gemma-4-31b-it") -> Iterator[dict]
# yields SSE-style event dicts per model:
#   {"type": "result", "model": str, "phase": "baseline"|"treatment",
#    "score": int|None, "reasoning": str, "output": str, "cached": bool}
# then one final:
#   {"type": "summary", "per_model": [{"model","baseline","treatment","lift","class"}],
#    "spread": int, "headline": str}
```

- Baseline = model answers the task with NO skill instructions. Treatment = skill body (+ scripts/ output when declared) injected as system/context.
- `lift = treatment_score − baseline_score` (percentage points). class: `POSITIVE` / `NEUTRAL` / `NEGATIVE` (lift == 0 is NEUTRAL).
- spread = max(lift) − min(lift) across models.
- Cache every model response keyed by sha256(`model|prompt|skill_hash`); a rerun with the same inputs reuses cached responses (`cached: true` in events). Cache is a module-level concern (see cache.py).
- Rate limits (~16k TPM free tier): serialize per-model, exponential backoff (3 tries).
- If judge is `ollama`, call Ollama localhost:11434 instead (config flag).

### cache.py (Agent B)

```python
def cache_get(key: str) -> str | None
def cache_put(key: str, value: str) -> None
def key_for(model: str, prompt: str, skill_hash: str) -> str  # sha256 hex of "model|prompt|skill_hash"
```

JSON file-backed (`cached/responses.json`), created on demand. No SQLite.

### app.py (Agent D)

stdlib `http.server.ThreadingHTTPServer`, routes:

- `GET /` → serve `static/index.html`
- `GET /events?run_id=…` → SSE stream (Content-Type: text/event-stream); streams the events from a background thread running `run_paired`
- `POST /run` → body `{skill: {dir|md}, task, models[]}` → starts run in a thread, returns `{run_id}`
- `GET /skills` → `{skills: [{name, description, dir}]}` from bundled fixtures
- `GET /cached/<run_id>` → full cached run JSON (demo insurance)
- No key → all endpoints still work; `/run` emits a "no-key" banner event and serves the seeded cached run.

`python app.py [--port N] [--judge gemma|ollama]` starts everything (NFR-2).

### static/index.html (Agent C)

Vanilla JS + EventSource, zero external CSS/JS/fonts (venue WiFi insurance). Dark, terminal-adjacent, monospace numbers.

- Header: kaizen wordmark, one-line pitch, NVIDIA 27% citation with link
- Left panel: skill selector (textarea paste + fixture buttons), task field, model checkboxes (`gemma-4-31b-it`, `gemma-4-26b-a4b-it` checked by default)
- Main panel: per-model cards streaming in — model name, baseline → treatment, big lift number (green/amber/red), collapsible judge reasoning, collapsible raw output
- Footer: spread headline
- States: running (skeleton cards), done, degraded (banner), no-key (banner: shows cached run)
- On load with no `?run=`, hit `GET /cached/latest` (or `/skills`) and render cached mode.

## Data model (streamed SSE events — already in PRD §10)

```json
{"type": "result", "model": "...", "phase": "baseline|treatment", "score": 0, "reasoning": "...", "output": "...", "cached": false}
{"type": "summary", "per_model": [{"model": "...", "baseline": 0, "treatment": 0, "lift": 0, "class": "POSITIVE"}], "spread": 0, "headline": "..."}
```

## Tests (Agent D writes tests/, everyone runs theirs)

- T-1 loader: valid skill parses; bad name rejected; YAML-less fallback works
- T-2 paired run: mock model client → lift math verified incl. a NEGATIVE case
- T-3 judge parser: malformed JSON → retry → fallback path
- T-4 SSE: events arrive incrementally (Node contract check already done for reference)
- T-5 degradation: missing key → cached mode renders
- T-6 fixture divergence: ≥1 fixture produces opposite-signed lift on 31B vs 26B-A4B (verify during build — this is the money demo)

## Done criteria per agent

Run your own tests + a smoke test, report: what you built, what you verified (actual command output), what's unverified. Self-reports are not green tests — I integrate and run the full suite myself.
