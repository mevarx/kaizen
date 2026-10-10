# kaizen

**Measure whether an Agent Skill actually improves your agent — with Gemma 4 as the open-weight reference arm.**

*kaizen* (改善) — continuous improvement. You can't improve what you don't measure.

## The problem

Agent skills (SKILL.md) became the dominant packaging unit for AI agent capabilities in 2026.
Nobody can tell whether a skill actually works. NVIDIA's ACES measurement (Aug 2026) found:

- **27% of paired runs show zero or negative lift** — a skill makes your agent worse or does nothing
- Scanning a skill's *structure* predicts its *runtime behavior* at **ρ ≈ −0.02**
- Lift varies wildly by model (+2 to +46 across products) — wider than harness differences

The only existing tools are harness-bound CLIs (Claude Code/Codex/Cursor only). Nothing measures
open-weight models in a browser.

## What kaizen does

Paste a SKILL.md, and kaizen runs the same task **with and without** that skill across multiple
models, scores every output against the skill's own rubric using **Gemma 4**, and streams
per-model lift scores to a live dashboard.

**One-line pitch:** *"NVIDIA found a skill makes your agent worse or does nothing 27% of the time —
and structure predicts behavior at negative-zero-two correlation. kaizen measures it in your
browser, with an open-weight model as the judge."*

The measurement *is* the demo.

## Quick start

```bash
# No API key needed — the app runs in cached/demo mode
python app.py
# open http://127.0.0.1:8000

# Live runs (optional)
export GEMINI_API_KEY=...   # free tier works; India supported
python app.py
```

Zero build tooling. Stdlib-only backend. One command.

## How it works

```
Browser (static/index.html — vanilla JS + EventSource)
   │  GET /            → dashboard shell
   │  GET /events      → SSE stream of run progress
   │  POST /run        → {skill_dir|skill_md, task, models}
   ▼
app.py — stdlib http.server (threaded), no dependencies
   ├── skill_loader.py  — SKILL.md parse + validate + scripts/ exec
   ├── runner.py        — paired A/B per model, caching, backoff
   ├── judge.py         — Gemma 4 rubric scoring (structured JSON)
   └── cache.py         — JSON response cache
        │
        ▼
Gemini API — gemma-4-31b-it, gemma-4-26b-a4b-it (+ a Gemini model for contrast)
        │
        ▼ (fallback only)
Ollama localhost:11434 — gemma3:4b
```

### Paired design

Same task, same model, same rubric — the only variable is skill-on vs skill-off.
This is what makes the delta meaningful:

```
lift(model) = treatment_score − baseline_score
```

Per-model lift plus the **spread** (max − min lift across models) — the spread is the headline.
Each model is classified POSITIVE / NEUTRAL / NEGATIVE.

## Honest limitations

- **"Lift" is Gemma-as-judge, not ground truth.** The paired design isolates the variable; the
  judge is a model. Single sample per cell by default (time-boxed). We document how to raise N
  in the roadmap. We do not claim significance we don't have.
- Judge scores depend on a fixed, in-repo judge prompt — inspect `judge.py` for exact wording.
- Gemma 4 API availability has varied in the wild; the app degrades to cached results and the
  Ollama local judge when the API is down.

## Tracks

Built for **MLH Hacktoberfest campus Hack Day** — DigitalOcean "Best Open-Source AI Project"
(open-weight model is the core, not a wrapper) + Google DeepMind "Best Use of Gemma 4"
(Gemma 4 is both the reference arm and the judge — load-bearing).

## Credits

- **NVIDIA ACES** (Aug 2026): 27% of paired runs show zero/negative skill lift;
  structural-vs-runtime correlation ρ ≈ −0.02; lift spread +2 to +46 across products
- **Agent Skill Open Standard** — agentskills.io/specification
- **arXiv 2610.10062** — loud vs. quiet tool failures (fixture-skill source)
- **arXiv 2406.10279** — USENIX Security 2025, package-hallucination study (ecosystem context)

## Privacy

No telemetry. User prompts go only to the Gemini API (or your local Ollama). Nothing else.

## License

MIT — see [LICENSE](LICENSE).
