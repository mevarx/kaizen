# PRD — kaizen
### Measure whether an Agent Skill actually improves your agent — with Gemma 4 as the open-weight reference arm

**Version:** 1.0 · **Status:** Final (build-ready) · **Date:** 2026-10-11
**Event:** MLH Hacktoberfest campus Hack Day · **Track fit:** Best Open-Source AI Project (DigitalOcean) + Best Use of Gemma 4 (Google DeepMind)

> Name: *kaizen* (改善) — continuous improvement. You can't improve what you don't measure.

> **Research basis:** five-stream crawl (HN/arXiv/GitHub/social/stack verification), all claims verified live 2026-10-10/11. This folder was renamed from `lazarus-hackfest`; the earlier "Lazarus" concept was dropped after research.

---

## 1. Executive summary

Agent skills (SKILL.md) became the dominant packaging unit for AI agent capabilities in 2026. Nobody can tell whether a skill actually works. NVIDIA's own ACES measurement (Aug 2026) found **27% of paired runs show zero or negative lift**, and scanning a skill's *structure* predicts its *runtime behavior* at **ρ ≈ −0.02**. The only existing tools are harness-bound CLIs (Claude Code/Codex/Cursor only) — nothing measures open-weight models in a browser.

**kaizen** is a full-stack web app: paste a SKILL.md, and it runs the same task **with and without** that skill across multiple models, scores every output against the skill's own rubric using **Gemma 4**, and streams per-model lift scores to a live dashboard. The measurement *is* the demo.

**One-line pitch:** *"NVIDIA found a skill makes your agent worse or does nothing 27% of the time — and structure predicts behavior at negative-zero-two correlation. kaizen measures it in your browser, with an open-weight model as the judge."*

---

## 2. Problem statement

1. **The unit is unmeasured.** A SKILL.md is a folder of instructions + scripts. Whether it helps depends on the model, the task, and the harness — and nothing measures this.
2. **Structural analysis is worthless as a predictor.** NVIDIA ACES: structural scan ↔ runtime correlation ρ ≈ −0.02. Reading the skill tells you almost nothing about what it does.
3. **Negative lift is common.** 27% of paired runs: zero or negative improvement. A skill can actively harm your agent.
4. **Lift varies wildly by model.** Spread of +2 to +46 across products — wider than harness differences. A skill that works on one model may hurt on another.
5. **No tool covers open-weight models.** NVIDIA's ACES covers Claude Code/Codex/Cursor. Nothing exists for Gemma or other open-weight models, and nothing is browser-based.

**Evidence:** NVIDIA ACES (Aug 2026); HN corpus (3,927 stories, 86 comment trees — the emptiest gap in the whole crawl; tldraw paused contributions over slop, 2026-01-15); GitHub (Gemma × Agent-Skill intersection: 7 repos, all 0★; two dead repos named this exact gap); arXiv 2610.10062 (loud vs. quiet failures — the fixture-skill source).

---

## 3. Goals & success metrics

**Product goals**
- G1: Paste any SKILL.md → get a defensible per-model lift measurement in < 2 minutes
- G2: The measurement runs live during the demo, not pre-baked
- G3: Gemma 4 is load-bearing (reference arm + judge), not decorative
- G4: Zero build tooling — one command runs the whole app

**Success metrics (demo day)**
- ≥ 3 models measured in one live run
- ≥ 2 fixture skills producing divergent lift scores (proves the point visually)
- One headline number on screen: *"27% of skills do nothing or hurt — here's one doing exactly that"*

---

## 4. Non-goals

- **Not a skill authoring tool.** The SKILL.md is the unit under test, an input artifact.
- **Not a skill security auditor.** (That space is saturated: skillguard ×4, nova-proximity, skillbadge, NVIDIA SkillSpector.)
- **Not a harness.** Doesn't execute arbitrary agent runtimes; it runs the skill's *scripts* and its *instructions* against a model.
- **Not production telemetry.** No multi-tenant infra, no persistence requirement beyond a cache.

---

## 5. Users & personas

| Persona | Need | kaizen's answer |
|---|---|---|
| **Skill author** ("did my skill help?") | Proof their skill works, per model | Paired A/B lift score per model, in seconds |
| **Agent user** ("should I install this skill?") | Evidence before install | Paste → verdict, browser-only |
| **Open-weight advocate** ("Gemma is untested in this space") | A reference arm for open-weight models | Gemma 4 is the default reference model |
| **Hackathon judge** | Novel, working, honest, open-source | All four, with published citations |

---

## 6. User stories

- **US-1:** As a skill author, I paste a SKILL.md and see per-model lift so I know whether to ship it.
- **US-2:** As a user, I click one fixture skill and watch the measurement stream live in the browser.
- **US-3:** As a judge, I see the NVIDIA citation on screen and the 27% claim reproduced on a real skill.
- **US-4:** As a developer, I run `python app.py` and the dashboard opens with zero setup.
- **US-5:** As a user on flaky venue WiFi, the app still shows cached results and degrades gracefully.

---

## 7. Functional requirements

### FR-1 — Skill loading
- FR-1.1: Parse a SKILL.md from a directory or pasted text
- FR-1.2: Extract YAML frontmatter per the Agent Skill Open Standard: `name` (required, ≤64 chars, lowercase+hyphens, must match directory), `description` (required, ≤1024), optional `license`, `compatibility`, `metadata`, `allowed-tools`
- FR-1.3: Validate name rules; reject non-conforming skills with a clear error
- FR-1.4: Discover and execute `scripts/` entries when the skill declares them
- FR-1.5: Fall back to a frontmatter-only parse if YAML parsing fails (spec-permitted)

### FR-2 — Paired A/B runner
- FR-2.1: For a given task prompt + rubric: run the model **without** skill instructions (baseline) and **with** skill instructions injected (treatment)
- FR-2.2: Run across a configurable model list; **Gemma 4 (`gemma-4-26b-a4b-it`, `gemma-4-31b-it`) is the default open-weight reference arm**; at least one Gemini model for contrast
- FR-2.3: Stream each result to the frontend as it completes (EventSource), never block the UI
- FR-2.4: Cache every model response (SQLite or JSON) keyed by (model, prompt, skill-hash) so reruns are instant
- FR-2.5: Honor rate limits: serialize per-model with backoff; free tier is ~16k TPM

### FR-3 — Gemma 4 judge
- FR-3.1: Score each output against the skill's rubric with Gemma 4, returning structured JSON: `{score: 0–100, reasoning: str, criteria: [{name, met: bool}]}`
- FR-3.2: Judge prompt is fixed and shipped in the repo (reproducibility)
- FR-3.3: Parse judge output defensively (Gemma is small; expect occasional malformed JSON → one retry, then fallback score)
- FR-3.4: The judge model is configurable but defaults to Gemma 4

### FR-4 — Lift computation
- FR-4.1: `lift(model) = treatment_score − baseline_score` (percentage points)
- FR-4.2: Report per model, plus the **spread** (max − min lift across models) — the spread is the headline
- FR-4.3: Classify each model: `POSITIVE` / `NEUTRAL` / `NEGATIVE` lift
- FR-4.4: Show raw outputs side-by-side for manual inspection

### FR-5 — Dashboard
- FR-5.1: Skill selector (paste field + fixture list)
- FR-5.2: Live run view: per-model cards streaming in — model name, baseline score, treatment score, lift, judge reasoning
- FR-5.3: Headline stat: the spread + the NVIDIA 27% citation
- FR-5.4: "Run live" vs "show cached" — a cached full run must be viewable without any API key (demo insurance)
- FR-5.5: Zero build tooling: single `index.html`, vanilla JS, EventSource

### FR-6 — Fixture skills (bundled)
- FR-6.1: ≥ 3 spec-compliant SKILL.md fixtures with real rubrics
- FR-6.2: At least one designed to show **divergent lift** across Gemma 4 sizes (the money demo)
- FR-6.3: One derived from arXiv 2610.10062 (silent-failure detection)

---

## 8. Non-functional requirements

- **NFR-1 Zero deps:** backend runs on Python stdlib only (`http.server`); no pip install required
- **NFR-2 One command:** `python app.py` → server up, dashboard reachable
- **NFR-3 Graceful degradation:** no API key → cached results + skill metadata still render; API 500s → exponential backoff, then cached/offline path
- **NFR-4 Offline fallback:** Ollama (`gemma3:4b`, installed) as a local judge when the API is down
- **NFR-5 Reproducibility:** judge prompt, rubrics, and fixture skills all in-repo
- **NFR-6 Privacy:** no telemetry; user prompts go only to the Gemini API (documented in README)
- **NFR-7 Speed:** a cached full run renders in < 2 s; a live 3-model run completes in < 90 s

---

## 9. Architecture

```
Browser (static/index.html — vanilla JS + EventSource)
   │  GET /            → dashboard shell
   │  GET /events      → SSE stream of run progress
   │  POST /run        → {skill_dir|skill_md, task, models}
   ▼
app.py — stdlib http.server (threaded), no dependencies
   ├── skill_loader.py  — SKILL.md parse + validate + scripts/ exec   (verified working)
   ├── runner.py        — paired A/B per model, caching, backoff
   ├── judge.py         — Gemma 4 rubric scoring (structured JSON)
   └── cache.py         — JSON/SQLite response cache
        │
        ▼
Gemini API — generativelanguage.googleapis.com/v1beta/models/<model>:generateContent
   (Gemma 4: gemma-4-26b-a4b-it, gemma-4-31b-it · plus a Gemini model for contrast)
        │
        ▼ (fallback only)
Ollama localhost:11434 — gemma3:4b
```

**Verified environment facts (tested 2026-10-11):**
- `python` = **3.11.16 with requests+fastapi installed**; `python3` = **3.13.14 with zero packages** → always invoke `python`
- stdlib `http.server` incremental streaming: verified working
- SKILL.md loader (frontmatter parse, name validation, script exec): verified working — reference code at `%LOCALAPPDATA%\Temp\stackcrawl\skill_loader.py`
- EventSource frontend contract: verified in Node
- Gemini API reachable from India in 64 ms; India officially supported
- Ollama server: not auto-running (start with `ollama serve`); `gemma3:4b` (3.4 GB) + `qwen2.5vl:3b` installed
- FastAPI 0.143 / uvicorn installed but **not used** — stdlib is the safer one-day choice

---

## 10. Data model

```python
# cache entry
{"key": "sha256(model|prompt|skill_hash)", "model": str, "prompt": str,
 "response": str, "ts": iso8601}

# run result (streamed to UI as SSE events)
{"type": "result", "model": str, "phase": "baseline"|"treatment",
 "score": int, "reasoning": str, "output": str, "cached": bool}

# run summary (final SSE event)
{"type": "summary",
 "per_model": [{"model": str, "baseline": int, "treatment": int, "lift": int, "class": str}],
 "spread": int, "headline": str}
```

---

## 11. API surface

| Method | Path | Purpose |
|---|---|---|
| GET | `/` | Dashboard shell (static/index.html) |
| GET | `/events?run_id=…` | SSE stream of run progress |
| POST | `/run` | Body: `{skill: {dir|md}, task, models[]}` → starts run, returns run_id |
| GET | `/skills` | List bundled fixtures (name + description from frontmatter) |
| GET | `/cached/<run_id>` | Full cached run (demo insurance — no API needed) |

---

## 12. UI/UX spec

- **Header:** kaizen wordmark + the one-line pitch + the NVIDIA citation with a link
- **Left panel:** skill selector — paste textarea + fixture buttons; task prompt field; model checklist (Gemma 4 31B / 26B-A4B checked by default)
- **Main panel:** per-model result cards streaming in — model name, baseline → treatment, **big lift number** colored green/amber/red, collapsible judge reasoning, collapsible raw output
- **Footer stat:** the spread headline — *"this skill lifts +34 on 31B and −12 on 26B"*
- **States:** running (skeleton cards), done (full cards), degraded (banner: "Gemini API degraded — showing cached run"), no-key (banner: "Add GEMINI_API_KEY for live runs; showing cached")
- **Visual language:** dark, terminal-adjacent, monospace numbers. No external CSS/JS/fonts (venue WiFi insurance).

---

## 13. SKILL.md handling spec

Per agentskills.io/specification (verified):
- Directory: `SKILL.md` (required) + `scripts/` + `references/` + `assets/`
- Frontmatter fields: `name` (≤64, lowercase alphanumeric + hyphens, no leading/trailing/double hyphen, must equal directory name), `description` (≤1024), `license`, `compatibility` (≤500), `metadata` (string map), `allowed-tools` (space-separated)
- Body: free-form Markdown — the instructions injected into the treatment prompt
- `scripts/`: executable code the skill declares; kaizen runs them and includes their output in the treatment context
- Loader must handle: YAML parse failure → fallback line parser; missing `scripts/` → instructions-only mode

---

## 14. Model strategy

| Role | Model | Notes |
|---|---|---|
| Reference arm (open-weight) | `gemma-4-31b-it` | Largest documented on the API |
| Reference arm (open-weight) | `gemma-4-26b-a4b-it` | MoE; cheapest of the two |
| Contrast | one Gemini model | Shows the "lift varies by model" spread |
| Judge | `gemma-4-31b-it` (default, configurable) | Gemma judging Gemma's ecosystem — the track-relevant story |
| Offline fallback | `gemma3:4b` via Ollama | Local judge when the API is down |

**Not available:** `gemma-4-e2b` / `e4b` / `12b` are *not documented on the Gemini API* (HF/Kaggle/Ollama only). Do not build on them. Confirm live with `ListModels` on Day-0.

---

## 15. Evaluation methodology (the defensible part)

- **Paired design:** same task, same model, same rubric; the only variable is skill-on vs skill-off. This is what makes the delta meaningful.
- **Rubric scoring:** each fixture ships an explicit rubric (named criteria); Gemma judges criterion-by-criterion and returns a 0–100 with reasoning.
- **Known limitation, stated openly:** "lift" is Gemma-as-judge, not ground truth. The paired design isolates the variable; the judge is a model. Say this before a judge asks.
- **Statistical honesty:** single sample per cell by default (time-boxed); the README documents how to raise N. Do not claim significance you don't have.

---

## 16. Security & safety rules

- Defensive only: no payload generation, no testing against live third-party services
- `scripts/` execution is sandboxed-by-scope: kaizen only runs scripts from skills the user explicitly pasted/selects, never from untrusted network input
- No secrets in the repo; `GEMINI_API_KEY` via env var only; `.env` gitignored
- README documents exactly what leaves the machine (prompts → Gemini API; nothing else)

---

## 17. Error handling & degradation ladder

1. API key missing → cached-run mode + banner
2. API 500/429 → exponential backoff (3 tries), then skip model with a visible warning card
3. Judge returns malformed JSON → one retry with stricter prompt, then fallback: mark "unjudged", keep raw outputs
4. Network dead → cached results only; banner explains
5. Ollama running + API down → local judge fallback (config flag `--judge ollama`)

---

## 18. Test plan

- **T-1 Loader:** valid skill parses; bad name (uppercase, double hyphen, mismatch with dir) rejected; YAML-less frontmatter falls back
- **T-2 Paired run:** mock model client returns fixed outputs → lift math verified (including a NEGATIVE case)
- **T-3 Judge parser:** malformed JSON → retry → fallback path
- **T-4 SSE:** stream events arrive incrementally in a browser (Node check already done for the contract)
- **T-5 Degradation:** missing key → cached mode renders
- **T-6 Fixture divergence:** at least one fixture produces opposite-signed lift on 31B vs 26B-A4B (the money demo — verify during the build)

---

## 19. Build plan (1 day, 6–7 h real work)

| Time | Task | Gate |
|---|---|---|
| **Day-0 (before event)** | **GEMINI_API_KEY from aistudio.google.com** (free; India OK); run `ListModels`; confirm the event lists the Gemma 4 track; repo `kaizen`, MIT, public | key works, one API call returns |
| 0:00–1:00 | Repo scaffold; adapt verified `skill_loader.py`; stdlib server skeleton | loader parses a fixture |
| 1:00–3:00 | `runner.py` (paired A/B, cache, backoff) + `judge.py` (Gemma rubric scoring) | one model measured end-to-end |
| 3:00–4:30 | `static/index.html` live dashboard (EventSource) | cards stream live |
| 4:30–5:30 | 3–4 fixture skills with real rubrics | divergence verified (T-6) |
| 5:30–7:00 | Wire live demo; seed one full cached run | cached run renders with no key |
| 7:00–8:00 | README (NVIDIA citation, methodology, limitations), screenshots, commit + push | repo presentable |

---

## 20. Demo script (90 seconds)

1. **Hook:** "NVIDIA measured agent skills in August. 27% of the time a skill makes your agent worse — or does nothing. And scanning the skill's structure predicts what it does at negative-zero-two correlation. You cannot know if a skill works without running it."
2. **Paste a skill** (a real bundled fixture) into the dashboard.
3. **Watch it run live:** same task, with and without, across Gemma 4 models — result cards stream in.
4. **The number:** per-model lift, then the spread — *"this skill lifts +34 on 31B and actively hurts on 26B. The structural scan said nothing."*
5. **Close:** "Every other tool tests skills on Claude Code and Codex. kaizen measures the open-weight side — and the model doing the measuring is Gemma 4 itself."

---

## 21. Judging alignment

**MLH canonical criteria (Technology / Design / Completion / Learning, equal weight):**
- **Technology:** stdlib-only backend, verified SKILL.md loader, Gemini API integration, SSE streaming — all real and running
- **Design:** dark dashboard, monospace numbers, live streaming — screenshot-ready
- **Completion:** one working thing, fully demoable; judge-quality README with honest limitations
- **Learning:** documented findings (which fixtures diverge, how Gemma 4 judges)

**DigitalOcean "Best Open-Source AI Project":** open-weight model core ✓ · public repo + open license (MIT) ✓ · agent skill used as a component ✓ · original (not a reskin) ✓

**Google DeepMind "Best Use of Gemma 4":** focused AI tool ✓ · Gemma as reference arm + judge (load-bearing) ✓ · built via Gemini API ✓

**Winning patterns from the crawl:** lead with the problem, not the tech · scope to one working thing · demo, not slides · ship the boring requirements (repo + LICENSE + README crediting tools) · be explicit about what doesn't work yet (lift = judge-based, N=1 documented)

---

## 22. Repo structure

```
kaizen/
├── app.py                  # stdlib http.server + routes
├── skill_loader.py         # SKILL.md parse/validate/exec (verified base)
├── runner.py               # paired A/B, cache, backoff
├── judge.py                # Gemma 4 rubric scoring
├── cache.py                # response cache
├── static/index.html       # dashboard (vanilla JS + EventSource)
├── skills/                 # bundled fixture SKILL.md files
│   ├── fault-inject/SKILL.md + scripts/
│   ├── code-review/SKILL.md
│   └── summarize/SKILL.md
├── cached/                 # one seeded full run (demo insurance)
├── tests/                  # T-1..T-6
├── README.md               # problem, method, limitations, credits
├── LICENSE                 # MIT
└── .env.example            # GEMINI_API_KEY placeholder
```

---

## 23. Submission checklist

- [ ] Public repo, MIT LICENSE, README complete
- [ ] README credits: NVIDIA ACES (Aug 2026), Agent Skill Open Standard, arXiv 2610.10079/2610.10062, tools used
- [ ] `python app.py` runs from a clean clone (with `GEMINI_API_KEY` optional — cached mode must work without it)
- [ ] Screenshots of a live run in the README
- [ ] Fixture skills spec-compliant (name/description/scripts/)
- [ ] Cached full run committed (demo insurance)
- [ ] 90-second demo rehearsed from a cold start
- [ ] Honest limitations section present (judge-based scoring, N=1)

---

## 24. Risks & mitigations

| Risk | Likelihood | Mitigation |
|---|---|---|
| No API key on demo day | Medium | Cached-run mode is a first-class feature, not a fallback |
| Gemma 4 API flaky (documented ~30–50% uptime forum reports) | Medium | Backoff + skip-with-warning + Ollama fallback |
| Free-tier rate limits during live demo | Low-Medium | Serialize runs; pre-cache the headline run |
| Model IDs wrong | Low | `ListModels` on Day-0; IDs configurable |
| Judge scores look arbitrary | Medium | Ship the judge prompt + rubrics; state the limitation upfront |
| "This is just NVIDIA ACES again" | Low | ACES is harness-bound + CLI; kaizen is browser-first, any-SKILL.md, open-weight reference |
| Scope creep into authoring/auditing | Medium | Non-goals section; freeze scope at this PRD |

---

## 25. Open questions (resolve during build, not before)

- OQ-1: Exact judge prompt wording (ship whatever produces stable, defensible scores on fixtures)
- OQ-2: Which Gemini contrast model (whatever `ListModels` confirms as cheap/fast)
- OQ-3: Whether `--judge ollama` is demoed live or only documented

---

## 26. Post-hackathon roadmap (README "what's next" — signals ambition, not scope)

- N > 1 samples per cell with confidence intervals
- More open-weight arms (Llama, Qwen, Mistral via their APIs)
- Skill-version lift diffing (kaizen on a PR)
- Exportable measurement report (Markdown/JSON)

---

## 27. Appendix — citations

1. **NVIDIA ACES** (Aug 2026): 27% of paired runs show zero/negative skill lift; structural-vs-runtime correlation ρ ≈ −0.02; lift spread +2 to +46 across products
2. **Agent Skill Open Standard** — agentskills.io/specification (verified 2026-10-11)
3. **arXiv 2610.10062** — loud vs. quiet tool failures (fixture-skill source)
4. **arXiv 2406.10279** — USENIX Security 2025, package-hallucination study (ecosystem context)
5. **HN crawl** (2026-10-10): 3,927 stories, 86 comment trees — slop/quality triage = emptiest gap; tldraw paused contributions (2026-01-15)
6. **GitHub crawl** (2026-10-10): Gemma × Agent-Skill intersection = 7 repos, all 0★; `skill-cross-model-benchmark` and `agent-skill-cross-model-analysis` named this gap and died

**Environment notes (verified 2026-10-11):** use `python` (3.11, has packages), never `python3` (3.13, empty) · Ollama `gemma3:4b` installed, server needs `ollama serve` · Gemini API model IDs `gemma-4-26b-a4b-it` / `gemma-4-31b-it` · reference stack code at `%LOCALAPPDATA%\Temp\stackcrawl\`
