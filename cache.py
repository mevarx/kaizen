"""JSON file-backed response cache for kaizen (stdlib only).

A response is stored under sha256(model|prompt|skill_hash) so a rerun with
identical inputs reuses it instead of spending an API call. Cached mode is a
first-class feature: with no GEMINI_API_KEY a fully seeded cache still renders
a complete run.

Writes are atomic (temp file + os.replace) and serialized by a lock, because
app.py streams a run from a background thread while the UI may read the file.
A failed write is not fatal - the next run just pays for the call again.
"""

import hashlib
import json
import os
import tempfile
import threading
from datetime import datetime, timezone

ENV_CACHE_FILE = "KAIZEN_CACHE"
DEFAULT_CACHE_FILE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "cached", "responses.json")

_lock = threading.RLock()
_path = None
_mem = None


def cache_path():
    """Absolute path of the cache file (KAIZEN_CACHE overrides the default)."""
    global _path
    with _lock:
        if _path is None:
            _path = os.path.abspath(os.environ.get(ENV_CACHE_FILE) or DEFAULT_CACHE_FILE)
        return _path


def configure(path):
    """Point the cache at another file (tests, alternate data dirs)."""
    global _path, _mem
    with _lock:
        _path = os.path.abspath(path)
        _mem = None


def refresh():
    """Drop the in-memory copy so the next read re-reads the file from disk."""
    global _mem
    with _lock:
        _mem = None


def key_for(model, prompt, skill_hash):
    """sha256 hex of 'model|prompt|skill_hash' - the documented cache key."""
    raw = "%s|%s|%s" % (model or "", prompt or "", skill_hash or "")
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _load():
    global _mem
    if _mem is None:
        try:
            with open(cache_path(), encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            data = {}
        _mem = data if isinstance(data, dict) else {}
    return _mem


def _flush(mem):
    path = cache_path()
    folder = os.path.dirname(path) or "."
    os.makedirs(folder, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=folder, prefix=".responses-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(mem, f, ensure_ascii=False, indent=1)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def cache_get(key):
    """Cached response text for key, or None on a miss."""
    with _lock:
        entry = _load().get(key)
    if entry is None:
        return None
    if isinstance(entry, str):
        return entry
    if isinstance(entry, dict):
        value = entry.get("response")
        return value if isinstance(value, str) else None
    return None


def cache_put(key, value, model=None, prompt=None):
    """Store a response. model/prompt are recorded for debuggability (PRD 10)."""
    with _lock:
        mem = _load()
        mem[key] = {"key": key, "model": model, "prompt": prompt,
                    "response": value, "ts": datetime.now(timezone.utc).isoformat()}
        try:
            _flush(mem)
        except OSError:
            pass


def cache_entries():
    """Shallow copy of every entry, for inspection and /cached/<run_id>."""
    with _lock:
        return dict(_load())


def cache_stats():
    """{entries, path, exists, bytes} - used by the smoke test and the UI."""
    with _lock:
        path = cache_path()
        try:
            size = os.path.getsize(path)
        except OSError:
            size = 0
        return {"entries": len(_load()), "path": path,
                "exists": os.path.exists(path), "bytes": size}


def cache_clear():
    """Empty the cache (in memory and on disk)."""
    with _lock:
        _flush({})
        refresh()


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "clear":
        cache_clear()
        print("cache cleared:", cache_path())
    else:
        print(json.dumps(cache_stats(), indent=2))
