"""Smoke tests for runner.py / judge.py / cache.py — no API key needed.

Run: python tests/test_runner_judge.py   (from D:/CODING/kaizen)
Uses a temp cache file and a fake model client, so every path executes for real.
"""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import cache as cache_mod
import judge as judge_mod
import runner as runner_mod
import skill_loader

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  ok   " if cond else "  FAIL ") + name + (("  -> " + str(detail)) if detail and not cond else ""))


def fresh_cache():
    cache_mod.configure(os.path.join(tempfile.mkdtemp(prefix="kaizen-cache-"), "responses.json"))


FAKE_SKILL = {
    "name": "code-review", "description": "Review code for silent failures.",
    "body": "## Rubric\n- Identifies the swallowed exception\n- Explains the blast radius\n- Suggests a concrete fix\n",
    "scripts": {}, "dir": None, "metadata": {},
}

TASK = "Review this function that swallows exceptions and tell me what's wrong."


def scripted_client(scores, judge_scores=None):
    """A MODEL_CLIENT stand-in returning fixed text, and a matching judge.

    `scores` maps model -> (baseline_score, treatment_score); the client emits
    text encoding that score, and the judge echoes it back, so lift math is
    verified end-to-end without any network call.
    """
    def client(model, prompt, system=""):
        pair = scores.get(model, (30, 90))
        phase = "treatment" if system.strip() else "baseline"
        return "MODEL=%s PHASE=%s MARK=%d" % (model, phase, pair[1 if phase == "treatment" else 0])

    def gen(prompt, model, backend="gemini", temperature=0.0, max_tokens=1024, timeout=60):
        # The judge prompt embeds the output; recover the mark from it.
        import re as _re
        m = _re.search(r"MARK=(\d+)", prompt)
        score = int(m.group(1)) if m else 0
        if judge_scores and model in judge_scores:
            score = judge_scores[model]
        return json.dumps({"score": score, "reasoning": "auto",
                           "criteria": [{"name": "c", "met": score >= 50}]})
    return client, gen


# ---------- T-3: judge parser ----------
def test_judge_parser():
    print("\n[T-3] judge defensive JSON parsing")
    good = '{"score": 82, "reasoning": "solid", "criteria": [{"name": "a", "met": true}, {"name": "b", "met": false}]}'
    p = judge_mod.parse_judge_response(good)
    check("bare object parses", p and p["score"] == 82 and len(p["criteria"]) == 2)

    fenced = "Sure! Here is my evaluation:\n```json\n" + good + "\n```\nHope that helps."
    p2 = judge_mod.parse_judge_response(fenced)
    check("fenced + prose parses", p2 and p2["score"] == 82)

    embedded = 'blah blah {"score": 41, "reasoning": "meh", "criteria": []} trailing junk'
    p3 = judge_mod.parse_judge_response(embedded)
    check("embedded object parses", p3 and p3["score"] == 41)

    check("garbage returns None", judge_mod.parse_judge_response("I cannot comply with that request.") is None)
    check("empty returns None", judge_mod.parse_judge_response("") is None)
    check("empty object returns None", judge_mod.parse_judge_response("{}") is None)
    check("non-dict returns None", judge_mod.parse_judge_response("[1,2,3]") is None)
    check("out-of-range rejected", judge_mod.parse_judge_response('{"score": 150, "criteria": []}') is None)
    check("bool score rejected", judge_mod.parse_judge_response('{"score": true, "criteria": []}') is None)
    check("string score accepted", judge_mod.parse_judge_response('{"score": "77", "criteria": []}')["score"] == 77)
    check("string 'n/a' rejected", judge_mod.parse_judge_response('{"score": "n/a", "criteria": []}') is None)

    braces = '{"score": 55, "reasoning": "has } inside a string", "criteria": [{"name": "x", "met": true}]}'
    check("braces inside strings handled", judge_mod.parse_judge_response(braces)["score"] == 55)

    # malformed -> retry -> fallback (the full score_output path)
    calls = {"n": 0}
    def gen(prompt, model, backend="gemini", temperature=0.0, max_tokens=1024, timeout=60):
        calls["n"] += 1
        return "not json at all"
    judge_mod._generate = gen
    v = judge_mod.score_output(TASK, [{"name": "a"}], "some output")
    check("malformed twice -> unjudged fallback", v["score"] is None and v["reasoning"] == "unjudged")
    check("retry prompt used (2 calls)", calls["n"] == 2, calls["n"])
    check("fallback keeps judge_model", v["judge_model"] == "gemma-4-31b-it")

    calls["n"] = 0
    def gen_then_ok(prompt, model, backend="gemini", temperature=0.0, max_tokens=1024, timeout=60):
        calls["n"] += 1
        return "junk" if calls["n"] == 1 else good
    judge_mod._generate = gen_then_ok
    v2 = judge_mod.score_output(TASK, [{"name": "a"}], "some output")
    check("retry recovers valid JSON", v2["score"] == 82 and calls["n"] == 2)

    calls["n"] = 0
    def gen_no_key(prompt, model, backend="gemini", temperature=0.0, max_tokens=1024, timeout=60):
        calls["n"] += 1
        raise judge_mod.NoKeyError("no key")
    judge_mod._generate = gen_no_key
    try:
        judge_mod.score_output(TASK, [{"name": "a"}], "out")
        check("NoKeyError propagates", False)
    except judge_mod.NoKeyError:
        check("NoKeyError propagates", True)

    # network error -> no retry storm, unjudged
    calls["n"] = 0
    def gen_net(prompt, model, backend="gemini", temperature=0.0, max_tokens=1024, timeout=60):
        calls["n"] += 1
        raise OSError("connection refused")
    judge_mod._generate = gen_net
    v3 = judge_mod.score_output(TASK, [{"name": "a"}], "out")
    check("network error -> unjudged, no retry loop", v3["score"] is None and calls["n"] == 1)

    check("judge_backend_for ollama", judge_mod.judge_backend_for("ollama") == "ollama")
    check("judge_backend_for gemma", judge_mod.judge_backend_for("gemma-4-31b-it") == "gemini")


# ---------- T-2: paired run + lift math ----------
def test_paired_run():
    print("\n[T-2] paired A/B run, lift math, classes, spread")
    fresh_cache()
    client, gen = scripted_client({"gemma-4-31b-it": (30, 90),
                                   "gemma-4-26b-a4b-it": (78, 66)})
    saved_client, saved_gen = runner_mod.MODEL_CLIENT, judge_mod._generate
    try:
        runner_mod.MODEL_CLIENT, judge_mod._generate = client, gen
        events = list(runner_mod.run_paired(
            FAKE_SKILL, TASK, ["gemma-4-31b-it", "gemma-4-26b-a4b-it"],
            judge_model="gemma-4-31b-it"))
    finally:
        runner_mod.MODEL_CLIENT, judge_mod._generate = saved_client, saved_gen

    kinds = [e["type"] for e in events]
    check("yields result events + 1 summary", kinds.count("result") == 4 and kinds[-1] == "summary", kinds)
    check("first event is a result", events[0]["type"] == "result")

    r0 = events[0]
    check("result event shape", all(k in r0 for k in ("model", "phase", "score", "reasoning", "output", "cached")))
    check("phases per model are baseline then treatment",
          [e["phase"] for e in events if e["type"] == "result"] ==
          ["baseline", "treatment", "baseline", "treatment"])

    s = events[-1]
    check("summary shape", all(k in s for k in ("per_model", "spread", "headline")))
    check("per_model has 2 entries", len(s["per_model"]) == 2)
    for m in s["per_model"]:
        check("per_model entry keys (%s)" % m["model"],
              all(k in m for k in ("model", "baseline", "treatment", "lift", "class")))

    check("lift math correct",
          s["per_model"][0]["baseline"] == 30 and s["per_model"][0]["treatment"] == 90
          and s["per_model"][0]["lift"] == 60)
    check("class POSITIVE", s["per_model"][0]["class"] == "POSITIVE")
    check("spread = max-min", s["spread"] == 60 - (-12), s["spread"])
    check("summary is JSON-serializable", isinstance(json.dumps(s), str))
    check("headline mentions divergence", "Divergent" in s["headline"], s["headline"])
    check("both per-model lifts present",
          s["per_model"][1]["lift"] == -12 and s["per_model"][1]["class"] == "NEGATIVE",
          s["per_model"][1])


def test_negative_and_neutral():
    print("\n[T-2b] NEGATIVE and NEUTRAL lift classes")
    fresh_cache()
    client, gen = scripted_client({"neg-model": (78, 66), "flat-model": (55, 55)})
    saved_client, saved_gen = runner_mod.MODEL_CLIENT, judge_mod._generate
    try:
        runner_mod.MODEL_CLIENT, judge_mod._generate = client, gen
        events = list(runner_mod.run_paired(
            FAKE_SKILL, TASK, ["neg-model", "flat-model"],
            judge_model="gemma-4-31b-it"))
    finally:
        runner_mod.MODEL_CLIENT, judge_mod._generate = saved_client, saved_gen
    s = events[-1]
    neg, flat = s["per_model"][0], s["per_model"][1]
    check("NEGATIVE lift = -12", neg["lift"] == -12 and neg["class"] == "NEGATIVE")
    check("NEUTRAL lift = 0", flat["lift"] == 0 and flat["class"] == "NEUTRAL")
    check("spread across signed lifts", s["spread"] == 0 - (-12), s["spread"])
    check("classify_lift(0) is NEUTRAL", runner_mod.classify_lift(0) == "NEUTRAL")
    check("classify_lift(None) is UNJUDGED", runner_mod.classify_lift(None) == "UNJUDGED")


def test_no_key_degraded():
    print("\n[T-5] no API key -> degraded, no crash")
    fresh_cache()
    saved = os.environ.pop("GEMINI_API_KEY", None)
    saved_client = runner_mod.MODEL_CLIENT
    saved_gen = judge_mod._generate
    try:
        runner_mod.MODEL_CLIENT = None  # force the real client -> NoKeyError path
        judge_mod._generate = judge_mod._gemini_text
        events = list(runner_mod.run_paired(
            FAKE_SKILL, TASK, ["gemma-4-31b-it"],
            judge_model="gemma-4-31b-it"))
        check("still yields events with no key", len(events) == 3)
        r = events[0]
        check("no-key result has score None", r["score"] is None)
        check("no-key result explains why", "GEMINI_API_KEY" in r["reasoning"], r["reasoning"])
        check("no-key result keeps raw output empty", r["output"] == "")
        s = events[-1]
        check("summary lift None -> UNJUDGED", s["per_model"][0]["class"] == "UNJUDGED")
        check("summary spread 0 with nothing scored", s["spread"] == 0)
        check("headline tells user what to do", "GEMINI_API_KEY" in s["headline"], s["headline"])
        check("all events JSON-serializable", isinstance(json.dumps(events), str))
    finally:
        if saved is not None:
            os.environ["GEMINI_API_KEY"] = saved
        runner_mod.MODEL_CLIENT = saved_client
        judge_mod._generate = saved_gen


def test_cache_roundtrip():
    print("\n[cache] key stability, roundtrip, persistence, atomic write")
    fresh_cache()
    k = cache_mod.key_for("m1", "p1", "h1")
    k2 = cache_mod.key_for("m1", "p1", "h1")
    check("key_for deterministic", k == k2)
    check("key_for distinct per input",
          len({cache_mod.key_for("m1", "p1", "h1"), cache_mod.key_for("m2", "p1", "h1"),
               cache_mod.key_for("m1", "p2", "h1"), cache_mod.key_for("m1", "p1", "h2")}) == 4)
    import hashlib
    check("key_for is sha256(model|prompt|skill_hash)",
          k == hashlib.sha256(b"m1|p1|h1").hexdigest())
    check("cache miss -> None", cache_mod.cache_get(k) is None)

    cache_mod.cache_put(k, "cached response text", model="m1", prompt="p1")
    check("cache hit after put", cache_mod.cache_get(k) == "cached response text")
    check("unknown key -> None", cache_mod.cache_get("deadbeef") is None)

    stats = cache_mod.cache_stats()
    check("stats reports 1 entry", stats["entries"] == 1, stats)
    check("file created on disk", stats["exists"] and stats["bytes"] > 0)

    cache_mod.refresh()
    check("survives in-memory refresh (read from disk)", cache_mod.cache_get(k) == "cached response text")

    # corrupt file must not crash
    with open(cache_mod.cache_path(), "w", encoding="utf-8") as f:
        f.write("{not json")
    cache_mod.refresh()
    check("corrupt cache file degrades to empty", cache_mod.cache_get(k) is None)
    check("cache_get on corrupt file returns None not raise", cache_mod.cache_stats()["entries"] == 0)

    cache_mod.cache_put(k, "again")
    check("recovers after corruption", cache_mod.cache_get(k) == "again")

    # unicode roundtrip
    uk = cache_mod.key_for("m", "p", "h")
    cache_mod.cache_put(uk, "日本語 ✅ — em-dash")
    cache_mod.refresh()
    check("unicode roundtrip", cache_mod.cache_get(uk) == "日本語 ✅ — em-dash")

    # value that looks like a dict entry
    cache_mod.cache_put("plain", '{"a": 1}')
    check("json-looking values roundtrip", cache_mod.cache_get("plain") == '{"a": 1}')


def test_backoff():
    print("\n[backoff] 3 tries, exponential, then raise; no retry on 4xx")
    fresh_cache()
    calls = {"n": 0}
    sleeps = []
    runner_mod._sleep = lambda s: sleeps.append(s)

    def flaky(model, prompt, system=""):
        calls["n"] += 1
        if calls["n"] < 3:
            raise runner_mod.ModelCallError("429 rate limited", status=429, retryable=True)
        return "recovered on try 3"

    saved = runner_mod.MODEL_CLIENT
    try:
        runner_mod.MODEL_CLIENT = flaky
        out = runner_mod.call_model("m", "p")
        check("recovers on 3rd try", out == "recovered on try 3")
        check("exactly 3 calls", calls["n"] == 3, calls["n"])
        check("backoff sleeps 1s then 2s", sleeps == [1.0, 2.0], sleeps)
    finally:
        runner_mod.MODEL_CLIENT = saved

    calls["n"] = 0
    sleeps.clear()

    def dead(model, prompt, system=""):
        calls["n"] += 1
        raise runner_mod.ModelCallError("500", status=500, retryable=True)
    saved = runner_mod.MODEL_CLIENT
    try:
        runner_mod.MODEL_CLIENT = dead
        try:
            runner_mod.call_model("m", "p")
            check("raises after 3 tries", False)
        except runner_mod.ModelCallError as e:
            check("raises after 3 tries", True)
            check("3 tries total (no 4th)", calls["n"] == 3, calls["n"])
            check("3 sleeps recorded", len(sleeps) == 2)
    finally:
        runner_mod.MODEL_CLIENT = saved

    calls["n"] = 0
    sleeps.clear()

    def forbidden(model, prompt, system=""):
        calls["n"] += 1
        raise runner_mod.ModelCallError("403 bad key", status=403, retryable=False)
    saved = runner_mod.MODEL_CLIENT
    try:
        runner_mod.MODEL_CLIENT = forbidden
        try:
            runner_mod.call_model("m", "p")
            check("non-retryable raises", False)
        except runner_mod.ModelCallError:
            check("non-retryable raises immediately", calls["n"] == 1 and sleeps == [])
    finally:
        runner_mod.MODEL_CLIENT = saved


def test_cache_reuse_in_run():
    print("\n[cache] second identical run reuses responses (cached: true)")
    fresh_cache()
    calls = {"n": 0}
    def counting(model, prompt, system=""):
        calls["n"] += 1
        # Score tracks the skill body so a different skill yields a different
        # treatment score, proving the treatment arm is genuinely re-run.
        mark = 60 if "Different body" not in system else 20
        return "MODEL=%s PHASE=%s MARK=%d" % (
            model, "treatment" if system.strip() else "baseline", mark)

    saved = runner_mod.MODEL_CLIENT
    saved_gen = judge_mod._generate
    try:
        runner_mod.MODEL_CLIENT = counting
        judge_mod._generate = scripted_client({"gemma-4-31b-it": (30, 90)})[1]
        first = list(runner_mod.run_paired(FAKE_SKILL, TASK, ["gemma-4-31b-it"],
                                           judge_model="gemma-4-31b-it"))
        n_after_first = calls["n"]
        check("first run calls model twice (2 phases)", n_after_first == 2, n_after_first)
        check("first run not cached", all(not e["cached"] for e in first if e["type"] == "result"))

        second = list(runner_mod.run_paired(FAKE_SKILL, TASK, ["gemma-4-31b-it"],
                                            judge_model="gemma-4-31b-it"))
        check("second run makes no model calls", calls["n"] == n_after_first, calls["n"])
        check("second run reports cached: true",
              all(e["cached"] for e in second if e["type"] == "result"))
        check("same scores on rerun",
              second[-1]["per_model"] == first[-1]["per_model"])

        # different task -> cache miss
        list(runner_mod.run_paired(FAKE_SKILL, "a totally different task",
                                   ["gemma-4-31b-it"], judge_model="gemma-4-31b-it"))
        check("different task busts cache", calls["n"] == n_after_first + 2, calls["n"])

        # A different skill only invalidates the treatment arm: the baseline
        # prompt carries no skill content, so its cache entry is legitimately
        # shared. That is the point of keying on the flattened prompt.
        other = dict(FAKE_SKILL, body="Different body entirely.\n## Rubric\n- x\n")
        third = list(runner_mod.run_paired(other, TASK, ["gemma-4-31b-it"],
                                           judge_model="gemma-4-31b-it"))
        check("different skill re-runs treatment only (baseline shared)",
              calls["n"] == n_after_first + 3, calls["n"])
        check("different skill changes the treatment score",
              third[-1]["per_model"][0]["treatment"] != first[-1]["per_model"][0]["treatment"],
              (third[-1]["per_model"][0], first[-1]["per_model"][0]))
        check("different skill keeps the same baseline score",
              third[-1]["per_model"][0]["baseline"] == first[-1]["per_model"][0]["baseline"])
    finally:
        runner_mod.MODEL_CLIENT = saved
        judge_mod._generate = saved_gen


def test_rubric_extraction():
    print("\n[rubric] extraction from body, override, defaults")
    r = runner_mod.extract_rubric(FAKE_SKILL)
    check("rubric from body section", [c["name"] for c in r] ==
          ["Identifies the swallowed exception", "Explains the blast radius", "Suggests a concrete fix"], r)
    r2 = runner_mod.extract_rubric(FAKE_SKILL, override=[{"name": "only this"}])
    check("override wins", len(r2) == 1 and r2[0]["name"] == "only this")
    r3 = runner_mod.extract_rubric({"name": "x", "description": "Does a thing.", "body": "no rubric here"})
    check("fallback derives from description", r3 and r3[0]["name"] == "Does a thing", r3)
    r4 = runner_mod.extract_rubric({"name": "x", "description": "", "body": ""})
    check("fallback always non-empty", len(r4) >= 1, r4)
    r5 = runner_mod.extract_rubric({"name": "x", "body": "## Rubric\n- [ ] checkbox item\n- [x] done item\n"})
    check("checkbox bullets parsed", [c["name"] for c in r5] == ["checkbox item", "done item"], r5)


def test_prompt_construction():
    print("\n[prompts] baseline has no skill; treatment injects it")
    bp, tp, bs, ts = runner_mod.build_prompts(TASK, FAKE_SKILL, {})
    check("user prompt identical in both arms", bp == tp)
    check("baseline system is empty", bs == "")
    check("treatment system carries skill body", "Rubric" in ts)
    ctx = runner_mod.build_treatment_context(FAKE_SKILL, {"check.py": "found 3 issues"})
    check("scripts appear in treatment context", "scripts/check.py" in ctx and "found 3 issues" in ctx)


def test_judge_cache_in_run():
    print("\n[cache] judge verdicts are cached too")
    fresh_cache()
    judge_calls = {"n": 0}
    def gen(prompt, model, backend="gemini", temperature=0.0, max_tokens=1024, timeout=60):
        judge_calls["n"] += 1
        import re as _re
        m = _re.search(r"MARK=(\d+)", prompt)
        score = int(m.group(1)) if m else 50
        return json.dumps({"score": score, "reasoning": "r", "criteria": []})
    saved_gen = judge_mod._generate
    client, _ = scripted_client({"m1": (30, 90)})
    saved_client = runner_mod.MODEL_CLIENT
    try:
        judge_mod._generate = gen
        runner_mod.MODEL_CLIENT = client
        first = list(runner_mod.run_paired(FAKE_SKILL, TASK, ["m1"], judge_model="gemma-4-31b-it"))
        n1 = judge_calls["n"]
        check("judge called twice on first run", n1 == 2, n1)
        ev = list(runner_mod.run_paired(FAKE_SKILL, TASK, ["m1"], judge_model="gemma-4-31b-it"))
        check("judge not re-called on identical rerun", judge_calls["n"] == n1)
        check("judge_cached true on rerun", all(e.get("judge_cached") for e in ev if e["type"] == "result"))
        check("judge_cached false on first run",
              all(not e.get("judge_cached") for e in first if e["type"] == "result"))
        check("scores preserved through judge cache",
              ev[-1]["per_model"] == first[-1]["per_model"])

        # A different rubric must miss the judge cache even for the same output.
        other_rubric = [{"name": "a different criterion entirely"}]
        before = judge_calls["n"]
        list(runner_mod.run_paired(FAKE_SKILL, TASK, ["m1"], judge_model="gemma-4-31b-it",
                                   rubric=other_rubric))
        check("changed rubric re-judges (judge cache keyed on prompt)",
              judge_calls["n"] > before, (before, judge_calls["n"]))
    finally:
        judge_mod._generate = saved_gen
        runner_mod.MODEL_CLIENT = saved_client


def test_empty_models():
    print("\n[edge] empty model list")
    fresh_cache()
    events = list(runner_mod.run_paired(FAKE_SKILL, TASK, [], judge_model="gemma-4-31b-it"))
    check("empty models -> just a summary", len(events) == 1 and events[0]["type"] == "summary")
    check("empty models -> spread 0", events[0]["spread"] == 0)


def test_ollama_backend():
    print("\n[ollama] judge backend selection needs no API key")
    fresh_cache()
    saved = os.environ.pop("GEMINI_API_KEY", None)
    saved_gen = judge_mod._generate
    try:
        seen = {"model": None, "backend": None}
        def gen(prompt, model, backend="gemini", temperature=0.0, max_tokens=1024, timeout=60):
            seen["model"], seen["backend"] = model, backend
            return '{"score": 40, "reasoning": "local judge", "criteria": []}'
        judge_mod._generate = gen
        client, _ = scripted_client({"m1": (40, 40)})
        saved_client = runner_mod.MODEL_CLIENT
        try:
            runner_mod.MODEL_CLIENT = client
            events = list(runner_mod.run_paired(FAKE_SKILL, TASK, ["m1"], judge_model="ollama"))
            check("ollama backend routed", seen["backend"] == "ollama", seen)
            check("ollama uses gemma3:4b", seen["model"] == "gemma3:4b", seen)
            check("ollama run scores without a key",
                  events[-1]["per_model"][0]["lift"] == 0)
        finally:
            runner_mod.MODEL_CLIENT = saved_client
    finally:
        if saved is not None:
            os.environ["GEMINI_API_KEY"] = saved
        judge_mod._generate = saved_gen


def test_judge_failure_keeps_outputs():
    print("\n[degrade] judge fails -> outputs kept, model still reported")
    fresh_cache()
    client, _ = scripted_client({"m1": (30, 90)})
    saved_client, saved_gen = runner_mod.MODEL_CLIENT, judge_mod._generate
    try:
        runner_mod.MODEL_CLIENT = client
        judge_mod._generate = lambda *a, **k: "totally not json"
        events = list(runner_mod.run_paired(FAKE_SKILL, TASK, ["m1"], judge_model="gemma-4-31b-it"))
        check("raw outputs kept when judge fails",
              all(e["output"] for e in events if e["type"] == "result"))
        check("scores None when judge fails",
              all(e["score"] is None for e in events if e["type"] == "result"))
        check("summary marks UNJUDGED",
              events[-1]["per_model"][0]["class"] == "UNJUDGED")
    finally:
        runner_mod.MODEL_CLIENT, judge_mod._generate = saved_client, saved_gen


def test_model_failure_partial():
    print("\n[degrade] one phase fails -> run completes")
    fresh_cache()
    def half_broken(model, prompt, system=""):
        if system.strip():
            raise runner_mod.ModelCallError("500 server error", status=500, retryable=True)
        return "MODEL=%s PHASE=baseline MARK=30" % model
    saved_client = runner_mod.MODEL_CLIENT
    saved_sleep = runner_mod._sleep
    try:
        runner_mod.MODEL_CLIENT = half_broken
        runner_mod._sleep = lambda s: None
        events = list(runner_mod.run_paired(FAKE_SKILL, TASK, ["m1"], judge_model="gemma-4-31b-it"))
        check("run still yields 3 events", len(events) == 3)
        check("baseline succeeded", events[0]["output"].startswith("MODEL="))
        check("failed treatment explains", "failed after 3 tries" in events[1]["reasoning"],
              events[1]["reasoning"])
        check("summary lift None", events[-1]["per_model"][0]["lift"] is None)
    finally:
        runner_mod.MODEL_CLIENT = saved_client
        runner_mod._sleep = saved_sleep


def test_script_execution():
    print("\n[scripts] skill scripts run and feed the treatment context")
    import tempfile as _tf
    # skill_loader enforces name == directory name, so the fixture dir must match.
    base = _tf.mkdtemp(prefix="kaizen-skill-")
    d = os.path.join(base, "script-fixture")
    os.makedirs(os.path.join(d, "scripts"))
    with open(os.path.join(d, "SKILL.md"), "w", encoding="utf-8") as f:
        f.write("---\nname: script-fixture\ndescription: test fixture\n---\nbody\n")
    with open(os.path.join(d, "scripts", "check.py"), "w", encoding="utf-8") as f:
        f.write("print('found 2 issues')\n")
    skill = {"name": "script-fixture", "description": "d", "body": "body",
             "scripts": {"check.py": None}, "dir": d, "metadata": {}}
    out = runner_mod.run_skill_scripts(skill)
    check("script stdout captured", "found 2 issues" in out.get("check.py", ""), out)
    ctx = runner_mod.build_treatment_context(skill, out)
    check("script output in context", "scripts/check.py" in ctx and "found 2 issues" in ctx)

    broken = dict(skill, dir=None)
    out2 = runner_mod.run_skill_scripts(broken)
    check("no-dir skill degrades gracefully", "skipped" in out2.get("check.py", ""), out2)

    with open(os.path.join(d, "scripts", "hang.sh"), "w", encoding="utf-8") as f:
        f.write("sleep 30\n")
    hanging = {"name": "script-fixture", "scripts": {"hang.sh": None}, "dir": d,
               "body": "", "description": ""}
    out3 = runner_mod.run_skill_scripts(hanging, timeout=1)
    check("hanging script times out, does not hang the run",
          "timeout" in out3.get("hang.sh", "").lower(), out3)


def test_fixture_rubrics():
    print("\n[integration] real bundled fixtures yield clean weighted rubrics")
    import glob
    skills_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "skills")
    found = 0
    for d in sorted(glob.glob(os.path.join(skills_dir, "*"))):
        if not os.path.isdir(d):
            continue
        try:
            skill = skill_loader.load_skill(d)
        except Exception as exc:
            print("    (skip %s: %s)" % (os.path.basename(d), exc))
            continue
        found += 1
        rub = runner_mod.extract_rubric(skill)
        names = [c["name"] for c in rub]
        junk = [n for n in names if n.startswith("```") or n in ("[", "]", "{", "}")
                or n.startswith('"')]
        check("%s: rubric has real criteria" % os.path.basename(d),
              len(rub) >= 2 and not junk, names[:3])
        check("%s: every criterion has a name" % os.path.basename(d),
              all(c.get("name") for c in rub))

    check("at least one bundled fixture parsed", found >= 1, found)


def test_judge_prompt_includes_check_text():
    print("\n[judge] fixture criteria send the check text, not just the name")
    rub = runner_mod.extract_rubric(
        {"body": '## Rubric\n```json\n[{"name": "reconciles-row-counts", "weight": 3, '
                 '"check": "States rows_in and rows_out as concrete numbers"}]\n```'})
    prompt = judge_mod.build_judge_prompt("task", rub, "output")
    check("criterion name in prompt", "reconciles-row-counts" in prompt)
    check("check text in prompt", "concrete numbers" in prompt)


def test_fenced_rubric_beats_bullets():
    print("\n[rubric] fenced JSON wins over a stray bullet list")
    body = ("## Rubric\n\n```json\n"
            '[{"name": "json-criterion", "weight": 2, "check": "from the fence"}]\n'
            "```\n\n- not a criterion, just prose\n")
    rub = runner_mod.extract_rubric({"body": body})
    check("fenced JSON is used", [c["name"] for c in rub] == ["json-criterion"], rub)
    check("weight preserved", rub[0]["weight"] == 2, rub)


def main():
    fresh_cache()
    test_judge_parser()
    test_paired_run()
    test_negative_and_neutral()
    test_no_key_degraded()
    test_cache_roundtrip()
    test_backoff()
    test_cache_reuse_in_run()
    test_rubric_extraction()
    test_prompt_construction()
    test_judge_cache_in_run()
    test_empty_models()
    test_ollama_backend()
    test_judge_failure_keeps_outputs()
    test_model_failure_partial()
    test_script_execution()
    test_fixture_rubrics()
    test_judge_prompt_includes_check_text()
    test_fenced_rubric_beats_bullets()

    print("\n" + "=" * 60)
    print("PASSED: %d   FAILED: %d" % (len(PASS), len(FAIL)))
    if FAIL:
        print("FAILURES:")
        for f in FAIL:
            print("  - " + f)
        return 1
    print("ALL GREEN")
    return 0


if __name__ == "__main__":
    sys.exit(main())
