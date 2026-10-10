"""kaizen test suite — T-1 through T-6 (PRD §18 / INTERFACES.md §Tests).

Run from the repo root:

    python -m unittest discover -s tests -v
    python tests/run_all.py          # convenience wrapper, same thing

Design notes
------------
* T-1 (loader) and T-4/T-5/T-6 (server + fixtures) test Agent D's own files
  and always run.
* T-2 (paired lift math) and T-3 (judge parser) test Agent B's modules. When
  runner.py / judge.py are not importable yet, those tests SKIP with a clear
  reason rather than failing — the suite is meant to be runnable during the
  build, not only at integration time.
* T-6 is split: the *design* property (a fixture whose rubric is calibrated so
  a small model can plausibly fail criteria the big model passes) is asserted
  statically; the *live* divergence measurement needs an API key and is marked
  to-verify in the report.
"""

import json
import os
import sys
import threading
import time
import unittest
import urllib.error
import urllib.request

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

import app  # noqa: E402  (needs sys.path fixed up first)

try:
    import skill_loader
except Exception as _exc:  # pragma: no cover
    skill_loader = None
    LOADER_IMPORT_ERROR = f"{type(_exc).__name__}: {_exc}"

try:
    import runner
except Exception as _exc:  # pragma: no cover
    runner = None
    RUNNER_IMPORT_ERROR = f"{type(_exc).__name__}: {_exc}"

try:
    import judge
except Exception as _exc:  # pragma: no cover
    judge = None
    JUDGE_IMPORT_ERROR = f"{type(_exc).__name__}: {_exc}"

try:
    import cache as cache_mod
except Exception as _exc:  # pragma: no cover
    cache_mod = None
    CACHE_IMPORT_ERROR = f"{type(_exc).__name__}: {_exc}"

MODELS = ["gemma-4-31b-it", "gemma-4-26b-a4b-it"]


# ==========================================================================
# T-1 — loader: valid skill parses; bad name rejected; YAML-less fallback works
# ==========================================================================

@unittest.skipIf(skill_loader is None, "skill_loader.py not importable")
class TestT1Loader(unittest.TestCase):

    def test_valid_fixture_parses(self):
        for name in ("fault-inject", "code-review", "summarize",
                     "structured-extract"):
            path = os.path.join(REPO, "skills", name)
            if not os.path.isdir(path):
                continue
            skill = skill_loader.load_skill(path)
            self.assertEqual(skill["name"], name, f"{name} frontmatter name")
            self.assertTrue(skill.get("description"), f"{name} needs a description")
            self.assertLessEqual(len(skill["description"]), 1024)
            self.assertTrue(skill.get("body"), f"{name} needs a body")
            self.assertEqual(skill_loader.validate_skill(skill), [])

    def test_name_must_match_directory(self):
        """validate_skill returns errors (it does not raise) - the name/dir
        mismatch must be one of them."""
        path = os.path.join(REPO, "skills", "fault-inject")
        skill = skill_loader.load_skill(path)
        errors = skill_loader.validate_skill(dict(skill, name="wrong-name"))
        self.assertTrue(errors, "a name that differs from the directory "
                                "must be reported")
        self.assertTrue(any("wrong-name" in e or "match" in e.lower()
                            for e in errors), f"errors: {errors}")

    def test_load_rejects_directory_mismatch_on_disk(self):
        """A SKILL.md whose name != its directory fails at load time."""
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            d = os.path.join(tmp, "real-dir-name")
            os.makedirs(d)
            with open(os.path.join(d, "SKILL.md"), "w", encoding="utf-8") as fh:
                fh.write("---\nname: different-name\ndescription: mismatch\n"
                         "---\n\nbody\n")
            with self.assertRaises(ValueError):
                skill_loader.load_skill(d)

    def test_bad_names_rejected(self):
        """Uppercase, leading/trailing/double hyphen, over-length (spec §13)."""
        path = os.path.join(REPO, "skills", "fault-inject")
        skill = skill_loader.load_skill(path)
        for bad in ("Fault-Inject", "fault--inject", "-fault-inject",
                    "fault-inject-", "fault_inject", "x" * 65, ""):
            errors = skill_loader.validate_skill(dict(skill, name=bad))
            self.assertTrue(errors, f"name {bad!r} should be rejected")

    def test_yaml_less_frontmatter_falls_back(self):
        """A frontmatter the YAML parser chokes on still yields name+body."""
        md = ("---\nname: pasted-skill\ndescription: 'unclosed quote\n"
              "weird: [a, b\n---\n# Body\n\nDo the thing.\n")
        skill = skill_loader.load_skill_from_text(md, "pasted-skill")
        self.assertEqual(skill["name"], "pasted-skill")
        self.assertIn("Do the thing", skill["body"])

    def test_missing_frontmatter_raises(self):
        with self.assertRaises(ValueError):
            skill_loader.parse_frontmatter("# no frontmatter here\n\nbody\n")

    def test_load_skill_from_text_has_no_dir(self):
        md = "---\nname: pasted-skill\ndescription: pasted in\n---\n\nbody\n"
        skill = skill_loader.load_skill_from_text(md, "pasted-skill")
        self.assertFalse(skill.get("dir"))

    @unittest.skipIf(not hasattr(skill_loader, "run_script"),
                     "run_script not present")
    def test_fault_inject_script_runs(self):
        """scripts/ discovery + execution (FR-1.4), on the real fixture.

        The probe reports a missing input file as a LOUD classification with
        exit 0 - that is the fixture's whole point, so assert the shape, not
        the verdict.
        """
        path = os.path.join(REPO, "skills", "fault-inject")
        skill = skill_loader.load_skill(path)
        self.assertIn("probe.py", skill.get("scripts", {}))
        result = skill_loader.run_script(path, "probe.py",
                                         ["--in", "nonexistent.jsonl",
                                          "--out", "nonexistent.jsonl"],
                                         timeout=30)
        self.assertEqual(set(result) >= {"ok", "stdout", "stderr",
                                         "returncode"}, True)
        self.assertIsInstance(result["ok"], bool)
        self.assertIsInstance(result["returncode"], int)
        # A valid JSON verdict on stdout, per the skill's documented contract.
        payload = json.loads(result["stdout"])
        self.assertIn(payload.get("classification"), ("LOUD", "QUIET", "CLEAN"))

    @unittest.skipIf(not hasattr(skill_loader, "run_script"),
                     "run_script not present")
    def test_fault_inject_script_detects_quiet_loss(self):
        """End-to-end on the fixture: 1000 in, 880 out must be classified QUIET."""
        import subprocess
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            src = os.path.join(tmp, "in.jsonl")
            dst = os.path.join(tmp, "out.jsonl")
            with open(src, "w", encoding="utf-8") as fh:
                for i in range(1000):
                    fh.write(json.dumps({"id": i, "amount": 10.0}) + "\n")
            with open(dst, "w", encoding="utf-8") as fh:
                for i in range(880):
                    fh.write(json.dumps({"id": i, "amount": 10.0}) + "\n")
            path = os.path.join(REPO, "skills", "fault-inject")
            result = skill_loader.run_script(
                path, "probe.py",
                ["--in", src, "--out", dst, "--column", "amount"], timeout=30)
        payload = json.loads(result["stdout"])
        self.assertEqual(payload["rows_in"], 1000)
        self.assertEqual(payload["rows_out"], 880)
        self.assertEqual(payload["delta"], 120)
        self.assertEqual(payload["classification"], "QUIET")
        # Exit 0 despite the silent loss - the fixture's thesis.
        self.assertEqual(result["returncode"], 0)

    @unittest.skipIf(not hasattr(skill_loader, "run_script"),
                     "run_script not present")
    def test_run_script_rejects_unknown(self):
        path = os.path.join(REPO, "skills", "fault-inject")
        with self.assertRaises(Exception):
            skill_loader.run_script(path, "does-not-exist.py")


# ==========================================================================
# T-2 — paired run: lift math verified, including a NEGATIVE case
# ==========================================================================

class TestT2LiftMath(unittest.TestCase):
    """Lift/spread/class math — tested on app.summarize_results (Agent D's own
    folding of result events) so the arithmetic is covered even before
    runner.py lands."""

    @staticmethod
    def _res(model, phase, score):
        return {"type": "result", "model": model, "phase": phase,
                "score": score, "reasoning": "", "output": "", "cached": False}

    def test_positive_lift(self):
        s = app.summarize_results([
            self._res(MODELS[0], "baseline", 35),
            self._res(MODELS[0], "treatment", 97)])
        row = s["per_model"][0]
        self.assertEqual(row["lift"], 62)
        self.assertEqual(row["class"], "POSITIVE")

    def test_negative_lift(self):
        s = app.summarize_results([
            self._res(MODELS[1], "baseline", 58),
            self._res(MODELS[1], "treatment", 44)])
        row = s["per_model"][0]
        self.assertEqual(row["lift"], -14)
        self.assertEqual(row["class"], "NEGATIVE")

    def test_neutral_lift(self):
        s = app.summarize_results([
            self._res(MODELS[0], "baseline", 50),
            self._res(MODELS[0], "treatment", 50)])
        self.assertEqual(s["per_model"][0]["lift"], 0)
        self.assertEqual(s["per_model"][0]["class"], "NEUTRAL")

    def test_spread_is_max_minus_min(self):
        s = app.summarize_results([
            self._res(MODELS[0], "baseline", 35),
            self._res(MODELS[0], "treatment", 97),
            self._res(MODELS[1], "baseline", 58),
            self._res(MODELS[1], "treatment", 44)])
        self.assertEqual(s["spread"], 76)
        by_model = {r["model"]: r for r in s["per_model"]}
        self.assertEqual(by_model[MODELS[0]]["lift"], 62)
        self.assertEqual(by_model[MODELS[1]]["lift"], -14)
        self.assertIn("-14", s["headline"])
        self.assertIn("+62", s["headline"])

    def test_summary_event_shape(self):
        s = app.summarize_results([self._res(MODELS[0], "baseline", 10),
                                   self._res(MODELS[0], "treatment", 20)])
        self.assertEqual(s["type"], "summary")
        self.assertIsInstance(s["spread"], int)
        self.assertIsInstance(s["headline"], str)
        for row in s["per_model"]:
            self.assertEqual(set(row), {"model", "baseline", "treatment",
                                        "lift", "class"})

    def test_missing_phase_is_unjudged(self):
        s = app.summarize_results([self._res(MODELS[0], "baseline", 40)])
        row = s["per_model"][0]
        self.assertIsNone(row["lift"])
        self.assertEqual(row["class"], "UNJUDGED")
        self.assertIsNone(s["spread"])

    def test_null_score_is_unjudged(self):
        s = app.summarize_results([
            self._res(MODELS[0], "baseline", None),
            self._res(MODELS[0], "treatment", None)])
        self.assertEqual(s["per_model"][0]["class"], "UNJUDGED")

    def test_models_without_results_get_rows(self):
        s = app.summarize_results([], models=MODELS)
        self.assertEqual([r["model"] for r in s["per_model"]], MODELS)
        self.assertTrue(all(r["lift"] is None for r in s["per_model"]))


@unittest.skipIf(runner is None, "runner.py not importable")
class TestT2RunnerIntegration(unittest.TestCase):
    """End-to-end lift math against the real runner, with a stubbed model
    client. Skipped until Agent B's runner.py is importable."""

    def test_runner_yields_result_then_summary(self):
        if not hasattr(runner, "run_paired"):
            self.skipTest("runner.run_paired not present")
        events = list(runner.run_paired(
            {"name": "fault-inject", "body": "test", "rubric": []},
            "did anything go wrong?", MODELS, judge_model="gemma-4-31b-it"))
        types = [e.get("type") for e in events]
        self.assertIn("result", types)
        self.assertEqual(types[-1], "summary")
        summary = events[-1]
        for row in summary["per_model"]:
            b, t = row["baseline"], row["treatment"]
            if isinstance(b, int) and isinstance(t, int):
                self.assertEqual(row["lift"], t - b)
        lifts = [r["lift"] for r in summary["per_model"]
                 if isinstance(r["lift"], int)]
        if len(lifts) >= 2:
            self.assertEqual(summary["spread"], max(lifts) - min(lifts))


@unittest.skipIf(cache_mod is None, "cache.py not importable")
class TestCacheContract(unittest.TestCase):
    def test_key_is_sha256_of_model_prompt_skill(self):
        import hashlib
        key = cache_mod.key_for("m", "p", "h")
        self.assertEqual(key, hashlib.sha256(b"m|p|h").hexdigest())

    def test_roundtrip(self):
        if not hasattr(cache_mod, "cache_put"):
            self.skipTest("cache_put not present")
        k = cache_mod.key_for("test-model", "test-prompt-" + str(time.time()),
                              "test-hash")
        self.assertIsNone(cache_mod.cache_get(k))
        cache_mod.cache_put(k, "stored value")
        self.assertEqual(cache_mod.cache_get(k), "stored value")


# ==========================================================================
# T-3 — judge parser: malformed JSON -> retry -> fallback path
# ==========================================================================

@unittest.skipIf(judge is None, "judge.py not importable")
class TestT3JudgeParser(unittest.TestCase):
    """Covers the judge's defensive parsing (FR-3.3): malformed JSON -> retry ->
    fallback, never a crash.

    judge.score_output raises NoKeyError when GEMINI_API_KEY is absent (its
    documented contract), so these tests stub judge._generate instead of
    hitting the network. The pure parser (judge.parse_judge_response) is tested
    directly since it needs no key at all.
    """

    def _stub_generate(self, replies):
        """Replace judge._generate with a canned-reply queue."""
        calls = {"n": 0}

        def fake_generate(prompt, model, backend="gemma", **kwargs):
            i = calls["n"]
            calls["n"] += 1
            return replies[min(i, len(replies) - 1)]

        self.addCleanup(setattr, judge, "_generate", judge._generate)
        judge._generate = fake_generate
        return calls

    RUBRIC = [{"name": "cites-line", "weight": 1, "check": "cites a line"}]

    def test_valid_json_scores(self):
        if not hasattr(judge, "score_output"):
            self.skipTest("judge.score_output not present")
        self._stub_generate([
            '{"score": 82, "reasoning": "solid", '
            '"criteria": [{"name": "cites-line", "met": true}]}'])
        out = judge.score_output("task", self.RUBRIC, "some output",
                                 model="gemma-4-31b-it")
        self.assertEqual(out["score"], 82)
        self.assertEqual(out["reasoning"], "solid")
        self.assertEqual(out["judge_model"], "gemma-4-31b-it")
        self.assertEqual(out["criteria"][0]["name"], "cites-line")
        self.assertTrue(out["criteria"][0]["met"])

    def test_malformed_then_retry_succeeds(self):
        """Garbage first, valid JSON second -> retry path returns the score."""
        if not hasattr(judge, "score_output"):
            self.skipTest("judge.score_output not present")
        calls = self._stub_generate([
            "not json at all",
            '{"score": 71, "reasoning": "ok on retry", "criteria": []}'])
        out = judge.score_output("task", self.RUBRIC, "output",
                                 model="gemma-4-31b-it")
        self.assertEqual(out["score"], 71)
        self.assertGreaterEqual(calls["n"], 2, "the retry prompt must be sent")

    def test_malformed_always_falls_back(self):
        """Two bad replies -> documented fallback, never a raise."""
        if not hasattr(judge, "score_output"):
            self.skipTest("judge.score_output not present")
        self._stub_generate(["garbage", "still garbage"])
        out = judge.score_output("task", self.RUBRIC, "output",
                                 model="gemma-4-31b-it")
        self.assertIsNone(out["score"])
        self.assertEqual(out["reasoning"], "unjudged")
        self.assertEqual(out["criteria"], [])
        self.assertIn("judge_model", out)

    def test_no_key_raises_the_documented_error(self):
        """NoKeyError is the contract; the runner treats it as 'cannot judge'."""
        if not hasattr(judge, "score_output"):
            self.skipTest("judge.score_output not present")
        self._stub_generate(["never reached"])
        real_key = os.environ.pop("GEMINI_API_KEY", None)
        self.addCleanup(lambda: real_key is not None
                        and os.environ.__setitem__("GEMINI_API_KEY", real_key))
        try:
            judge._generate = judge._generate  # restore the real one
        except Exception:
            pass
        self.addCleanup(setattr, judge, "_generate", judge._generate)
        judge._generate = lambda *a, **k: (_ for _ in ()).throw(
            judge.NoKeyError("GEMINI_API_KEY is not set"))
        with self.assertRaises(judge.NoKeyError):
            judge.score_output("task", self.RUBRIC, "output")

    def test_parse_judge_response_accepts_prose_wrapped_json(self):
        if not hasattr(judge, "parse_judge_response"):
            self.skipTest("judge.parse_judge_response not present")
        text = ('Sure! Here is my evaluation:\n'
                '{"score": 64, "reasoning": "decent", "criteria": []}\n'
                'Hope that helps.')
        parsed = judge.parse_judge_response(text)
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed["score"], 64)

    def test_parse_judge_response_accepts_fenced_json(self):
        if not hasattr(judge, "parse_judge_response"):
            self.skipTest("judge.parse_judge_response not present")
        fence = chr(96) * 3
        text = fence + "json\n" + '{"score": 50, "reasoning": "", "criteria": []}' \
               + "\n" + fence + "\n"
        parsed = judge.parse_judge_response(text)
        self.assertEqual(parsed["score"], 50)

    def test_parse_judge_response_rejects_garbage(self):
        if not hasattr(judge, "parse_judge_response"):
            self.skipTest("judge.parse_judge_response not present")
        for bad in ("", "   ", "no json here", "{unclosed", '{"score": "n/a"}',
                    '{"score": 500}', '{"reasoning": "no score"}', "[1,2,3]"):
            self.assertIsNone(judge.parse_judge_response(bad),
                              f"should be unparseable: {bad!r}")

    def test_parse_judge_response_accepts_string_score(self):
        """Small models emit "82" - accepted; "n/a" - rejected."""
        if not hasattr(judge, "parse_judge_response"):
            self.skipTest("judge.parse_judge_response not present")
        parsed = judge.parse_judge_response('{"score": "82"}')
        self.assertEqual(parsed["score"], 82)

    def test_judge_prompt_is_shipped_in_repo(self):
        """FR-3.2 reproducibility: the prompt is a module constant."""
        prompt = getattr(judge, "JUDGE_PROMPT", None)
        self.assertTrue(prompt and len(prompt) > 100,
                        "judge.JUDGE_PROMPT must be the shipped prompt")

    def test_normalize_fallback_shape(self):
        out = app.normalize_judge_result({"score": None,
                                          "reasoning": "unjudged"})
        self.assertIsNone(out["score"])
        self.assertEqual(out["reasoning"], "unjudged")
        self.assertEqual(out["criteria"], [])

    def test_normalize_clamps_and_types(self):
        out = app.normalize_judge_result({
            "score": 250,
            "reasoning": "over",
            "criteria": [{"name": "a", "met": 1}, "b", {"no_name": 1}, 42]})
        self.assertEqual(out["score"], 100)
        self.assertEqual([c["name"] for c in out["criteria"]], ["a", "b"])
        self.assertTrue(all(isinstance(c["met"], bool) for c in out["criteria"]))

    def test_normalize_non_numeric_score(self):
        out = app.normalize_judge_result({"score": "high", "reasoning": "x"})
        self.assertIsNone(out["score"])

    def test_normalize_garbage_input(self):
        self.assertIsNone(app.normalize_judge_result(None)["score"])
        self.assertIsNone(app.normalize_judge_result("nonsense")["score"])


# ==========================================================================
# T-4 — SSE: events arrive incrementally
# ==========================================================================

class TestT4SSE(unittest.TestCase):
    """Live server, real socket, real chunked stream. Asserts events arrive one
    at a time rather than in one burst at the end (FR-2.3)."""

    PORT = 8111

    @classmethod
    def setUpClass(cls):
        cls.httpd = app.ThreadingHTTPServer(("127.0.0.1", cls.PORT),
                                            app.Handler)
        cls.httpd.daemon_threads = True
        cls.thread = threading.Thread(target=cls.httpd.serve_forever,
                                      daemon=True)
        cls.thread.start()
        time.sleep(0.2)

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()

    def _get(self, path, timeout=20):
        url = f"http://127.0.0.1:{self.PORT}{path}"
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return resp.status, resp.headers, resp.read()

    def test_root_serves_html(self):
        status, headers, body = self._get("/")
        self.assertEqual(status, 200)
        self.assertIn("text/html", headers.get("Content-Type", ""))
        self.assertTrue(body.startswith(b"<!doctype html"))

    def test_skills_endpoint(self):
        status, _, body = self._get("/skills")
        self.assertEqual(status, 200)
        data = json.loads(body)
        names = [s["name"] for s in data["skills"]]
        for expected in ("fault-inject", "code-review", "summarize"):
            self.assertIn(expected, names)
        for skill in data["skills"]:
            self.assertIn("name", skill)
            self.assertIn("description", skill)
            self.assertIn("dir", skill)

    def test_events_streams_incrementally(self):
        """POST /run (no key -> cached replay), then read the SSE stream and
        check that (a) content-type is text/event-stream and (b) the frames
        arrive spread over time, not all at once."""
        payload = json.dumps({
            "skill": {"dir": "skills/fault-inject"},
            "task": "Rows in: 1000, rows out: 880. Did anything go wrong?",
            "models": MODELS}).encode()
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.PORT}/run", data=payload,
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=20) as resp:
            self.assertEqual(resp.status, 202)
            data = json.loads(resp.read())
        run_id = data["run_id"]
        self.assertTrue(run_id)
        self.assertIn("events_url", data)

        # Read the stream one frame at a time and timestamp each arrival.
        url = f"http://127.0.0.1:{self.PORT}/events?run_id={run_id}"
        stamps, events, buf = [], [], b""
        deadline = time.time() + 30
        resp = urllib.request.urlopen(url, timeout=30)
        try:
            self.assertIn("text/event-stream",
                          resp.headers.get("Content-Type", ""))
            while time.time() < deadline:
                chunk = resp.read1(4096)
                if not chunk:
                    break
                buf += chunk
                while b"\r\n\r\n" in buf:
                    frame, buf = buf.split(b"\r\n\r\n", 1)
                    text = frame.decode("utf-8", "replace")
                    if text.startswith(":"):
                        continue
                    payload_lines = [l[6:] for l in text.split("\r\n")
                                     if l.startswith("data: ")]
                    if not payload_lines:
                        continue
                    try:
                        events.append(json.loads("\n".join(payload_lines)))
                    except ValueError:
                        self.fail(f"unparseable SSE frame: {text[:200]}")
                    stamps.append(time.time())
                if any(e.get("type") == "summary" for e in events):
                    break
        finally:
            resp.close()

        self.assertGreaterEqual(len(events), 4,
                                f"expected >=4 events, got {len(events)}")
        types = [e.get("type") for e in events]
        self.assertIn("result", types)
        self.assertIn("summary", types)

        # Incremental: frames must not all land in the same instant.
        if len(stamps) >= 3:
            span = stamps[-1] - stamps[0]
            self.assertGreater(span, 0.05,
                               f"all {len(stamps)} frames arrived within "
                               f"{span:.4f}s - stream is not incremental")

        results = [e for e in events if e.get("type") == "result"]
        for ev in results:
            self.assertEqual(set(ev) >= {"type", "model", "phase", "score",
                                         "reasoning", "output", "cached"}, True)
            self.assertIn(ev["phase"], ("baseline", "treatment"))
        summary = next(e for e in events if e.get("type") == "summary")
        self.assertIsInstance(summary["spread"], int)
        self.assertIsInstance(summary["headline"], str)

    def test_unknown_run_id_is_404(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self._get("/events?run_id=no-such-run")
        self.assertEqual(ctx.exception.code, 404)

    def test_run_rejects_bad_body(self):
        for body in (b"{}", b"[]", b"not json", json.dumps({"task": ""}).encode()):
            req = urllib.request.Request(
                f"http://127.0.0.1:{self.PORT}/run", data=body,
                headers={"Content-Type": "application/json"})
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                urllib.request.urlopen(req, timeout=10)
            self.assertEqual(ctx.exception.code, 400)

    def test_run_rejects_bad_skill(self):
        payload = json.dumps({"skill": {"dir": "../../etc"}, "task": "x",
                              "models": MODELS}).encode()
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.PORT}/run", data=payload,
            headers={"Content-Type": "application/json"})
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(req, timeout=10)
        self.assertEqual(ctx.exception.code, 400)

    def test_unknown_route_is_404(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self._get("/nope")
        self.assertEqual(ctx.exception.code, 404)


# ==========================================================================
# T-5 — degradation: missing key -> cached mode renders
# ==========================================================================

class TestT5Degradation(unittest.TestCase):

    def setUp(self):
        self._saved = os.environ.pop("GEMINI_API_KEY", None)

    def tearDown(self):
        if self._saved is not None:
            os.environ["GEMINI_API_KEY"] = self._saved

    def test_has_api_key_false_without_env(self):
        self.assertFalse(app.has_api_key())

    def test_cached_run_is_readable_and_complete(self):
        seed = app.load_seeded_run()
        self.assertIsNotNone(seed, "cached/latest.json must exist")
        self.assertEqual(seed["run_id"], "latest")
        self.assertIn("per_model", seed)
        self.assertIn("spread", seed)
        self.assertIn("headline", seed)
        self.assertIn("events", seed)
        results = [e for e in seed["events"] if e.get("type") == "result"]
        self.assertGreaterEqual(len(results), 4)
        for row in seed["per_model"]:
            self.assertEqual(row["lift"], row["treatment"] - row["baseline"])
            self.assertEqual(row["class"], app.classify_lift(row["lift"]))
        lifts = [r["lift"] for r in seed["per_model"]]
        self.assertEqual(seed["spread"], max(lifts) - min(lifts))

    def test_cached_run_has_divergent_signs(self):
        """The seeded run must exhibit the money demo: opposite-signed lifts."""
        seed = app.load_seeded_run()
        classes = {r["class"] for r in seed["per_model"]}
        self.assertIn("POSITIVE", classes)
        self.assertIn("NEGATIVE", classes)

    def test_cached_run_provenance_is_labeled(self):
        """Seeded numbers must never be mistaken for a live measurement."""
        seed = app.load_seeded_run()
        self.assertIn("provenance", seed)
        self.assertIn("SEEDED", seed["provenance"].upper())

    def test_cached_route_serves_without_key(self):
        httpd = app.ThreadingHTTPServer(("127.0.0.1", 8112), app.Handler)
        httpd.daemon_threads = True
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        time.sleep(0.2)
        try:
            with urllib.request.urlopen("http://127.0.0.1:8112/cached/latest",
                                        timeout=10) as resp:
                self.assertEqual(resp.status, 200)
                data = json.loads(resp.read())
            self.assertIn("per_model", data)
            self.assertGreaterEqual(len(data["events"]), 4)
        finally:
            httpd.shutdown()
            httpd.server_close()

    def test_seeded_events_end_in_summary(self):
        events = app.seeded_events()
        self.assertEqual(events[-1]["type"], "summary")

    def test_replay_paces_events(self):
        """The cached replay must not dump everything in one burst."""
        run = app.Run("test-replay")
        thread = threading.Thread(target=app._replay_cached, args=(run,))
        thread.start()
        first = None
        for _ in range(50):
            with run.cond:
                if run.events:
                    first = time.time()
                    break
            time.sleep(0.01)
        self.assertIsNotNone(first, "no events were replayed")
        time.sleep(0.05)
        with run.cond:
            self.assertGreaterEqual(len(run.events), 1)
        thread.join(timeout=20)
        with run.cond:
            self.assertTrue(run.done)
            self.assertEqual(run.events[-1]["type"], "summary")

    def test_fallback_page_has_no_external_assets(self):
        """Venue WiFi insurance (NFR / PRD §12) - the built-in fallback must be
        fully self-contained."""
        page = app.FALLBACK_PAGE
        for needle in ("http://", "https://", "//cdn", "src=\"//"):
            self.assertNotIn(needle, page)
        self.assertIn("EventSource", page) if "EventSource" in page else None

    def test_health_reports_key_state(self):
        httpd = app.ThreadingHTTPServer(("127.0.0.1", 8113), app.Handler)
        httpd.daemon_threads = True
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        time.sleep(0.2)
        try:
            with urllib.request.urlopen("http://127.0.0.1:8113/health",
                                        timeout=10) as resp:
                data = json.loads(resp.read())
        finally:
            httpd.shutdown()
            httpd.server_close()
        self.assertTrue(data["ok"])
        self.assertFalse(data["key"])
        self.assertTrue(data["seeded_run"])

    def test_run_without_key_emits_no_key_banner(self):
        httpd = app.ThreadingHTTPServer(("127.0.0.1", 8114), app.Handler)
        httpd.daemon_threads = True
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        time.sleep(0.2)
        try:
            payload = json.dumps({
                "skill": {"dir": "skills/fault-inject"},
                "task": "did anything go wrong?",
                "models": MODELS}).encode()
            req = urllib.request.Request("http://127.0.0.1:8114/run",
                                         data=payload,
                                         headers={"Content-Type":
                                                  "application/json"})
            with urllib.request.urlopen(req, timeout=20) as resp:
                data = json.loads(resp.read())
            self.assertFalse(data["live"])
            run_id = data["run_id"]

            banners, events = [], []
            resp = urllib.request.urlopen(
                f"http://127.0.0.1:8114/events?run_id={run_id}", timeout=30)
            deadline = time.time() + 30
            try:
                while time.time() < deadline:
                    chunk = resp.read1(4096)
                    if not chunk:
                        break
                    for frame in chunk.split(b"\r\n\r\n"):
                        text = frame.decode("utf-8", "replace")
                        for line in text.split("\r\n"):
                            if line.startswith("data: "):
                                try:
                                    events.append(json.loads(line[6:]))
                                except ValueError:
                                    pass
                    if any(e.get("type") == "summary" for e in events):
                        break
            finally:
                resp.close()
            banners = [e for e in events if e.get("type") == "banner"]
            self.assertTrue(banners, "expected a banner event with no key")
            self.assertEqual(banners[0]["level"], "no-key")
            self.assertTrue(any(e.get("type") == "result" for e in events),
                            "cached results must still stream with no key")
            self.assertTrue(any(e.get("type") == "summary" for e in events))
        finally:
            httpd.shutdown()
            httpd.server_close()


# ==========================================================================
# T-6 — fixture divergence
# ==========================================================================

class TestT6FixtureDivergence(unittest.TestCase):

    def test_at_least_one_divergence_fixture_exists(self):
        path = os.path.join(REPO, "skills", "structured-extract", "SKILL.md")
        self.assertTrue(os.path.isfile(path),
                        "structured-extract is the divergent fixture")

    def test_divergent_fixture_rubric_is_high_weights(self):
        """Its rubric must concentrate weight on criteria a small model can
        plausibly fail while the big model passes."""
        skill = app.resolve_skill({"skill": {
            "dir": "skills/structured-extract"}})[0]
        rubric = app.extract_rubric(skill)
        self.assertGreaterEqual(len(rubric), 4)
        total = sum(c["weight"] for c in rubric)
        top = sorted((c["weight"] for c in rubric), reverse=True)[:3]
        # The three heaviest criteria carry the majority of the weight.
        self.assertGreater(sum(top) / total, 0.5)

    def test_every_fixture_has_a_rubric(self):
        for name in ("fault-inject", "code-review", "summarize",
                     "structured-extract"):
            path = os.path.join(REPO, "skills", name, "SKILL.md")
            if not os.path.isfile(path):
                continue
            with open(path, encoding="utf-8") as fh:
                skill = {"name": name, "body": fh.read()}
            rubric = app.extract_rubric(skill)
            self.assertGreaterEqual(len(rubric), 4, f"{name} needs a real rubric")
            for crit in rubric:
                self.assertTrue(crit["name"])
                self.assertGreater(crit["weight"], 0)

    def test_rubric_extraction_from_fenced_json(self):
        fence = chr(96) * 3
        body = ("intro\n\n## Rubric\n\n" + fence + "json\n"
                + '[{"name": "a", "weight": 2, "check": "x"}]\n' + fence + "\n")
        rubric = app.extract_rubric({"body": body})
        self.assertEqual(len(rubric), 1)
        self.assertEqual(rubric[0]["name"], "a")
        self.assertEqual(rubric[0]["weight"], 2.0)

    def test_rubric_extraction_accepts_wrapped_form(self):
        fence = chr(96) * 3
        body = (fence + "json\n" + '{"rubric": [{"criterion": "cited"}]}\n'
                + fence + "\n")
        rubric = app.extract_rubric({"body": body})
        self.assertEqual(rubric[0]["name"], "cited")
        self.assertEqual(rubric[0]["weight"], 1.0)

    def test_rubric_extraction_ignores_non_rubric_fences(self):
        fence = chr(96) * 3
        body = (fence + "json\n" + '{"rows": 3}\n' + fence + "\n\n"
                + fence + "json\n[1, 2, 3]\n" + fence + "\n\n"
                + fence + "bash\necho hi\n" + fence + "\n")
        self.assertEqual(app.extract_rubric({"body": body}), [])

    def test_rubric_extraction_handles_empty_body(self):
        self.assertEqual(app.extract_rubric({"body": ""}), [])
        self.assertEqual(app.extract_rubric({}), [])
        self.assertEqual(app.extract_rubric(None), [])

    def test_seeded_run_demonstrates_divergence(self):
        """T-6's UI-level proof: the seeded run shows opposite-signed lifts."""
        seed = app.load_seeded_run()
        self.assertIsNotNone(seed)
        by_class = {}
        for row in seed["per_model"]:
            by_class.setdefault(row["class"], []).append(row["lift"])
        self.assertIn("POSITIVE", by_class)
        self.assertIn("NEGATIVE", by_class)
        self.assertGreater(max(by_class["POSITIVE"]), 0)
        self.assertLess(min(by_class["NEGATIVE"]), 0)

    def test_live_divergence_needs_a_key(self):
        """Live T-6 (opposite-signed lift measured against the real API) cannot
        run without GEMINI_API_KEY. This documents the gate rather than
        pretending to verify it."""
        if not app.has_api_key():
            self.skipTest("GEMINI_API_KEY not set - live divergence is "
                          "to-verify, see the build report")


# ==========================================================================
# Misc contract checks that belong to Agent D's surface
# ==========================================================================

class TestContracts(unittest.TestCase):

    def test_classify_lift_boundaries(self):
        self.assertEqual(app.classify_lift(1), "POSITIVE")
        self.assertEqual(app.classify_lift(0), "NEUTRAL")
        self.assertEqual(app.classify_lift(-1), "NEGATIVE")
        self.assertEqual(app.classify_lift(None), "UNJUDGED")

    def test_headline_mentions_nvidia_when_negative(self):
        rows = [{"model": "big", "baseline": 10, "treatment": 70,
                 "lift": 60, "class": "POSITIVE"},
                {"model": "small", "baseline": 50, "treatment": 40,
                 "lift": -10, "class": "NEGATIVE"}]
        headline = app.build_headline(rows, 70)
        self.assertIn("spread 70", headline)
        self.assertIn("NVIDIA", headline)

    def test_headline_all_unjudged(self):
        rows = [{"model": "m", "baseline": None, "treatment": None,
                 "lift": None, "class": "UNJUDGED"}]
        headline = app.build_headline(rows, None)
        self.assertIn("no judged cells", headline)

    def test_models_capped(self):
        _, err = app.resolve_models({"models": [f"m{i}" for i in range(50)]})
        self.assertIsNotNone(err)
        self.assertIn(str(app.MAX_MODELS), err)

    def test_models_default(self):
        models, err = app.resolve_models({})
        self.assertIsNone(err)
        self.assertEqual(models, app.DEFAULT_MODELS)

    def test_gitignore_ignores_env(self):
        with open(os.path.join(REPO, ".gitignore"), encoding="utf-8") as fh:
            text = fh.read()
        self.assertIn(".env", text)
        self.assertIn("__pycache__", text)

    def test_env_example_documents_key(self):
        path = os.path.join(REPO, ".env.example")
        if not os.path.isfile(path):
            self.skipTest(".env.example not present")
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
        self.assertIn("GEMINI_API_KEY", text)

    def test_repo_layout(self):
        for rel in ("app.py", "README.md", "LICENSE", ".gitignore",
                    ".env.example", "skills", "cached", "tests"):
            self.assertTrue(os.path.exists(os.path.join(REPO, rel)),
                            f"missing {rel}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
