"""Minimal SKILL.md loader: parse frontmatter + run scripts/ with stdlib only.

Run: python skill_loader.py <skill-dir>
"""
import os
import re
import json
import subprocess
import sys

FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n?(.*)$", re.DOTALL)


def parse_frontmatter(text):
    """Tiny YAML-subset parser: only the fields the Agent Skills spec allows.

    Handles `key: value`, quoted strings, and one level of `metadata:` nesting.
    No PyYAML needed -> zero dependencies.
    """
    m = FRONTMATTER_RE.match(text)
    if not m:
        raise ValueError("SKILL.md missing YAML frontmatter (--- ... ---)")
    fm_block, body = m.group(1), m.group(2)
    meta, cur = {}, None
    for line in fm_block.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        indent = len(line) - len(line.lstrip())
        if ":" not in line:
            continue
        key, _, val = line.strip().partition(":")
        key, val = key.strip(), val.strip()
        if indent == 0:
            if val == "":
                cur = key
                meta[cur] = {}
            else:
                meta[key] = val.strip("\"'")
                cur = None
        elif cur:
            meta[cur][key] = val.strip("\"'")
    return meta, body.strip()


def load_skill(skill_dir):
    """Load a skill directory -> {name, description, body, scripts, meta}."""
    path = os.path.join(skill_dir, "SKILL.md")
    if not os.path.isfile(path):
        raise FileNotFoundError(f"No SKILL.md in {skill_dir}")
    with open(path, encoding="utf-8") as f:
        meta, body = parse_frontmatter(f.read())

    name = meta.get("name")
    if not name:
        raise ValueError("SKILL.md frontmatter requires `name`")
    if not re.fullmatch(r"[a-z0-9]+(-[a-z0-9]+)*", name):
        raise ValueError(f"invalid skill name: {name!r} (lowercase/hyphens only)")
    if name != os.path.basename(os.path.abspath(skill_dir)):
        raise ValueError(f"name {name!r} must match directory name")

    scripts = {}
    sdir = os.path.join(skill_dir, "scripts")
    if os.path.isdir(sdir):
        for fn in sorted(os.listdir(sdir)):
            full = os.path.join(sdir, fn)
            if os.path.isfile(full):
                scripts[fn] = full
    return {"name": name, "description": meta.get("description", ""),
            "allowed_tools": meta.get("allowed-tools", ""),
            "metadata": meta.get("metadata", {}), "body": body,
            "scripts": scripts, "dir": os.path.abspath(skill_dir)}


def run_script(skill_dir, script, args=None, timeout=60):
    """Execute a skill script by extension: .py / .sh / .js.
    Returns {ok, stdout, stderr, returncode}.
    """
    scripts = load_skill(skill_dir)["scripts"]
    if script not in scripts:
        raise KeyError(f"script {script!r} not in scripts/ (have: {list(scripts)})")
    path = os.path.abspath(scripts[script])
    argv = {"py": [sys.executable, path], "sh": ["bash", path],
            "js": ["node", path]}.get(script.rsplit(".", 1)[-1], [path])
    try:
        r = subprocess.run(argv + list(args or []), capture_output=True,
                           text=True, timeout=timeout, cwd=skill_dir)
        return {"ok": r.returncode == 0, "stdout": r.stdout,
                "stderr": r.stderr, "returncode": r.returncode}
    except subprocess.TimeoutExpired:
        return {"ok": False, "stdout": "", "stderr": f"timeout {timeout}s",
                "returncode": -1}


if __name__ == "__main__":
    d = sys.argv[1] if len(sys.argv) > 1 else "."
    s = load_skill(d)
    print("SKILL:", s["name"])
    print("DESC:", s["description"])
    print("SCRIPTS:", list(s["scripts"]))
    print("---BODY (first 200)---")
    print(s["body"][:200])
    if len(sys.argv) > 2:
        print("---RUN", sys.argv[2], "---")
        print(json.dumps(run_script(d, sys.argv[2], sys.argv[3:]), indent=2))
