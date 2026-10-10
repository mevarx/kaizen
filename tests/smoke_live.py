"""Live smoke test against a running kaizen server. Not part of the unit suite.

Usage: python tests/smoke_live.py [port]
"""
import json
import sys
import time
import urllib.error
import urllib.request

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8321
BASE = f"http://127.0.0.1:{PORT}"
FAILURES = []


def check(label, ok, detail=""):
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + (f" - {detail}" if detail else ""))
    if not ok:
        FAILURES.append(label)


def get(path, timeout=20):
    with urllib.request.urlopen(BASE + path, timeout=timeout) as r:
        return r.status, r.headers, r.read()


def post(path, body, timeout=20):
    req = urllib.request.Request(BASE + path, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status, json.loads(r.read())


def read_stream(run_id, timeout=60):
    """Read the SSE stream to completion, timestamping each frame."""
    t0 = time.time()
    resp = urllib.request.urlopen(f"{BASE}/events?run_id={run_id}", timeout=timeout)
    ctype = resp.headers.get("Content-Type", "")
    buf, events, stamps = b"", [], []
    try:
        while time.time() - t0 < timeout:
            chunk = resp.read1(8192)
            if not chunk:
                break
            buf += chunk
            done = False
            while b"\r\n\r\n" in buf:
                frame, buf = buf.split(b"\r\n\r\n", 1)
                text = frame.decode("utf-8", "replace")
                if text.startswith(":"):
                    continue
                lines = [l[6:] for l in text.split("\r\n") if l.startswith("data: ")]
                if not lines:
                    continue
                events.append(json.loads("\n".join(lines)))
                stamps.append(time.time() - t0)
                if events[-1].get("type") == "summary":
                    done = True
            if done:
                break
    finally:
        resp.close()
    return ctype, events, stamps


print(f"=== kaizen live smoke test - {BASE} ===")

print("\n[1] GET /  (dashboard shell)")
status, headers, body = get("/")
check("HTTP 200", status == 200, f"got {status}")
check("content-type html", "text/html" in headers.get("Content-Type", ""))
check("no external assets",
      b"http://" not in body.split(b"<script")[0].replace(b"http://www.w3.org", b"")
      and b"src=\"//" not in body and b"href=\"//" not in body,
      "self-contained (venue WiFi insurance)")

print("\n[2] GET /skills")
status, _, body = get("/skills")
data = json.loads(body)
names = [s["name"] for s in data["skills"]]
check("4 fixtures", len(names) >= 3, ", ".join(names))
check("fault-inject present", "fault-inject" in names)
check("probe.py discovered",
      any(s.get("scripts") and "probe.py" in s["scripts"] for s in data["skills"]))
check("rubrics extracted", all(len(s.get("rubric") or []) >= 4
                               for s in data["skills"] if not s.get("error")))
check("default models are Gemma 4",
      data["defaults"]["models"] == ["gemma-4-31b-it", "gemma-4-26b-a4b-it"])

print("\n[3] GET /cached/latest  (demo insurance, no key)")
status, _, body = get("/cached/latest")
seed = json.loads(body)
check("HTTP 200", status == 200)
check("provenance labeled SEEDED", "SEEDED" in seed["provenance"].upper())
check("divergent signs present",
      {r["class"] for r in seed["per_model"]} >= {"POSITIVE", "NEGATIVE"})
check("spread consistent",
      seed["spread"] == max(r["lift"] for r in seed["per_model"])
      - min(r["lift"] for r in seed["per_model"]),
      f"spread={seed['spread']}")
check("lift = treatment - baseline",
      all(r["lift"] == r["treatment"] - r["baseline"] for r in seed["per_model"]))

print("\n[4] POST /run  (no key -> no-key banner + cached replay)")
t0 = time.time()
status, data = post("/run", {
    "skill": {"dir": "skills/fault-inject"},
    "task": "Rows in: 1000. Rows out: 880. Exit code 0. Did anything go wrong?",
    "models": ["gemma-4-31b-it", "gemma-4-26b-a4b-it"]})
check("HTTP 202", status == 202, f"got {status}")
check("live=false without a key", data["live"] is False)
check("run_id returned", bool(data.get("run_id")))
check("rubric wired through", data.get("rubric_criteria") == 6,
      f"{data.get('rubric_criteria')} criteria")

print("\n[5] GET /events  (SSE stream, incremental)")
ctype, events, stamps = read_stream(data["run_id"])
check("content-type is text/event-stream", "text/event-stream" in ctype, ctype)
check("banner emitted", any(e.get("type") == "banner" for e in events))
check("banner level is no-key",
      next((e["level"] for e in events if e.get("type") == "banner"), None) == "no-key")
check("result events streamed", sum(1 for e in events if e.get("type") == "result") >= 4,
      f"{sum(1 for e in events if e.get('type') == 'result')} results")
check("summary terminates the stream", events and events[-1]["type"] == "summary")
check("result event shape",
      all(set(e) >= {"type", "model", "phase", "score", "reasoning", "output", "cached"}
          for e in events if e.get("type") == "result"))
check("summary shape",
      all(set(r) == {"model", "baseline", "treatment", "lift", "class"}
          for r in events[-1]["per_model"]))
span = (stamps[-1] - stamps[0]) if len(stamps) > 1 else 0.0
check("events arrive incrementally (paced)", span > 0.05, f"span={span:.2f}s")
check("cached replay under 2s (NFR-7)", stamps[-1] < 2.0,
      f"total={stamps[-1]:.2f}s")

print("\n[6] POST /run with a pasted skill (md, no dir)")
status, data = post("/run", {
    "skill": {"md": "---\nname: pasted-skill\ndescription: pasted inline\n---\n\n"
                    "## Rubric\n\n" + chr(96) * 3 + "json\n"
                    '[{"name": "is-clear", "weight": 1, "check": "clear"}]\n'
                    + chr(96) * 3 + "\n\nBe clear.\n"},
    "task": "say something clear", "models": ["gemma-4-31b-it"]})
check("HTTP 202", status == 202, f"got {status}")
check("pasted skill named", data.get("skill") == "pasted-skill")
check("pasted rubric extracted", data.get("rubric_criteria") == 1)

print("\n[7] error paths")
for label, body, want in [
    ("empty body rejected", {}, 400),
    ("bad JSON rejected", None, 400),
    ("missing task rejected", {"skill": {"dir": "skills/summarize"}}, 400),
    ("path traversal rejected",
     {"skill": {"dir": "../../etc"}, "task": "x"}, 400),
    ("unknown skill dir rejected",
     {"skill": {"dir": "skills/nope"}, "task": "x"}, 400),
]:
    try:
        if body is None:
            req = urllib.request.Request(BASE + "/run", data=b"not json",
                                         headers={"Content-Type": "application/json"})
            urllib.request.urlopen(req, timeout=10)
            got = 200
        else:
            got = post("/run", body, timeout=10)[0]
    except urllib.error.HTTPError as exc:
        got = exc.code
    check(label, got == want, f"got {got}, want {want}")

try:
    get("/events?run_id=no-such-run-id")
    check("unknown run_id is 404", False, "got 200")
except urllib.error.HTTPError as exc:
    check("unknown run_id is 404", exc.code == 404, f"got {exc.code}")

try:
    get("/cached/no-such-run")
    check("unknown cached run is 404", False, "got 200")
except urllib.error.HTTPError as exc:
    check("unknown cached run is 404", exc.code == 404, f"got {exc.code}")

try:
    get("/definitely-not-a-route")
    check("unknown route is 404", False, "got 200")
except urllib.error.HTTPError as exc:
    check("unknown route is 404", exc.code == 404, f"got {exc.code}")

print("\n[8] GET /health")
status, _, body = get("/health")
h = json.loads(body)
check("ok", h["ok"] is True)
check("key absent", h["key"] is False)
check("sibling modules loaded",
      all(h["modules"][m] for m in ("skill_loader", "runner", "judge", "cache")),
      str(h["modules"]))
check("no load errors", h["load_errors"] == {}, str(h["load_errors"]))
check("seeded run present", h["seeded_run"] is True)

print("\n[9] concurrency: 3 simultaneous /events readers on distinct runs")
import threading
results = {}


def worker(i):
    try:
        _, d = post("/run", {"skill": {"dir": "skills/summarize"},
                             "task": f"concurrent task {i}",
                             "models": ["gemma-4-31b-it"]})
        _, evs, _ = read_stream(d["run_id"], timeout=60)
        results[i] = len(evs)
    except Exception as exc:
        results[i] = f"ERROR {type(exc).__name__}: {exc}"


threads = [threading.Thread(target=worker, args=(i,)) for i in range(3)]
for t in threads:
    t.start()
for t in threads:
    t.join(timeout=120)
check("all 3 streams completed",
      all(isinstance(v, int) and v >= 5 for v in results.values()),
      str(results))

print("\n" + "=" * 62)
if FAILURES:
    print(f"FAILED ({len(FAILURES)}): " + "; ".join(FAILURES))
    sys.exit(1)
print("ALL SMOKE CHECKS PASSED")
