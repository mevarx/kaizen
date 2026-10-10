"""SKILL.md loader for kaizen — parse, load, validate, and run Agent Skill files.

Stdlib only (no PyYAML). Public API — see INTERFACES.md §skill_loader.py:

    parse_frontmatter(text)                      -> (meta, body); ValueError if no frontmatter
    load_skill(skill_dir)                        -> skill dict; raises on non-conforming name
    load_skill_from_text(md_text, name)          -> skill dict (pasted text; dir is None)
    run_script(skill_dir, script, args, timeout) -> {ok, stdout, stderr, returncode}
    validate_skill(skill)                        -> list[str] violations ([] == valid)

Name rules (Agent Skill Open Standard): [a-z0-9]+(-[a-z0-9]+)*, <= 64 chars,
no leading/trailing/double hyphen, must equal the directory name for
directory-loaded skills. description is required, <= 1024 chars.
scripts/ is optional — a skill without it loads in instructions-only mode.

Design notes:
- Both loaders reject a non-conforming *name* with ValueError (fail fast, as
  in the verified reference). description/compatibility/metadata conformance
  is reported by validate_skill() so callers can show the full error list.
- Frontmatter: PyYAML is unavailable, so a line parser does the work
  (spec-permitted fallback). A structured pass handles quoting, one level of
  nesting, block scalars (| >), block lists, flow lists ([a, b]) and flow
  maps ({k: v}); a flat top-level scan of the known spec keys then fills any
  gaps the structured pass missed. Colons inside unquoted values survive
  ("description: Use when: the user asks" — a real YAML parser rejects that).
- CRLF and UTF-8 BOM are normalized; script output is capped so a runaway
  script cannot blow up a prompt.

Run: python skill_loader.py <skill-dir> [script] [args...]
     python skill_loader.py --validate <skill-dir>
"""

import json
import os
import re
import shutil
import subprocess
import sys

FRONTMATTER_RE = re.compile(r"^---[ \t]*\n(.*?)\n?---[ \t]*\n?(.*)$", re.DOTALL)
NAME_RE = re.compile(r"[a-z0-9]+(-[a-z0-9]+)*")

MAX_NAME_LEN = 64
MAX_DESC_LEN = 1024
MAX_COMPAT_LEN = 500
MAX_SCRIPT_OUTPUT = 200_000  # chars per stream; guards runaway script output

SPEC_KEYS = ("name", "description", "license", "compatibility", "metadata",
             "allowed-tools")
SCALAR_KEYS = ("name", "description", "license", "compatibility")
_FLAT_RE = re.compile(r"^([A-Za-z][A-Za-z0-9_-]*):[ \t]*(.*)$")
_BLOCK_STYLES = ("|", "|-", "|+", ">", ">-", ">+")

__all__ = [
    "parse_frontmatter", "load_skill", "load_skill_from_text",
    "run_script", "validate_skill",
]


# --------------------------------------------------------------------------
# frontmatter parsing
# --------------------------------------------------------------------------

def _unquote(val):
    """Strip one layer of matching single/double quotes."""
    val = val.strip()
    if len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'":
        return val[1:-1]
    return val


def _parse_flow(val):
    """Minimal YAML flow syntax: [a, b] -> list, {k: v} -> dict. None if not flow."""
    val = val.strip()
    if len(val) >= 2 and val.startswith("[") and val.endswith("]"):
        inner = val[1:-1].strip()
        return [_scalar(p) for p in inner.split(",")] if inner else []
    if len(val) >= 2 and val.startswith("{") and val.endswith("}"):
        out = {}
        inner = val[1:-1].strip()
        if inner:
            for part in inner.split(","):
                key, sep, v = part.partition(":")
                if sep:
                    out[key.strip()] = _scalar(v)
        return out
    return None


def _scalar(val):
    """Scalar value: flow list/map when present, else an unquoted string."""
    flow = _parse_flow(val)
    return flow if flow is not None else _unquote(val)


def _read_block(lines, i, key_indent, style):
    """Collect a YAML block scalar (| or >) starting after its key line.

    Returns (text, next_index). Indentation common to the block is stripped;
    '-' chomping (and the default) drops trailing newlines.
    """
    body = []
    while i < len(lines):
        line = lines[i]
        if not line.strip():
            body.append("")
            i += 1
            continue
        if len(line) - len(line.lstrip(" ")) <= key_indent:
            break
        body.append(line)
        i += 1
    while body and not body[-1].strip():
        body.pop()
    if not body:
        return "", i
    cut = min(len(l) - len(l.lstrip(" ")) for l in body if l.strip())
    content = [l[cut:] for l in body]
    if style.startswith("|"):
        text = "\n".join(content)
    else:  # folded: join non-blank lines with spaces (minimal semantics)
        text = " ".join(l.strip() for l in content if l.strip())
    if not style.endswith("+"):
        text = text.rstrip("\n")
    return text, i


def _parse_structured(block):
    """Structured line parse: `key: value`, one nesting level, block scalars,
    block lists, flow lists/maps. Unknown keys are kept as-is."""
    meta, cur = {}, None
    lines = block.split("\n")
    i = 0
    while i < len(lines):
        line = lines[i]
        i += 1
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        indent = len(line) - len(line.lstrip(" "))
        stripped = line.strip()
        if indent == 0:
            if ":" not in stripped:
                continue
            key, _, val = stripped.partition(":")
            key, val = key.strip(), val.strip()
            if val in _BLOCK_STYLES:
                meta[key], i = _read_block(lines, i, indent, val)
                cur = None
            elif val == "":
                cur = key
                meta[cur] = {}
            else:
                meta[key] = _scalar(val)
                cur = None
        elif cur:
            if stripped.startswith("- "):
                item = _unquote(stripped[2:])
                if isinstance(meta.get(cur), list):
                    meta[cur].append(item)
                else:
                    meta[cur] = [item]
            elif ":" in stripped:
                k2, _, v2 = stripped.partition(":")
                if not isinstance(meta.get(cur), dict):
                    meta[cur] = {}
                meta[cur][k2.strip()] = _scalar(v2)
    # a bare `key:` with no children is an empty string, not an empty map
    for k, v in list(meta.items()):
        if isinstance(v, dict) and not v:
            meta[k] = ""
    return meta


def _parse_flat(block):
    """Last-resort scan: top-level `key: value` lines, known spec keys only.

    Indented lines are ignored so nested keys can never leak to the top level.
    """
    out = {}
    for line in block.split("\n"):
        m = _FLAT_RE.match(line)
        if not m:
            continue
        key, val = m.group(1), _scalar(m.group(2))
        if key in SPEC_KEYS and key not in out:
            out[key] = val
    return out


def parse_frontmatter(text):
    """Split SKILL.md text into (frontmatter dict, body markdown).

    Raises ValueError if the `---` frontmatter block is missing/unterminated.
    Normalizes CRLF and a UTF-8 BOM before parsing.
    """
    if isinstance(text, bytes):
        text = text.decode("utf-8", "replace")
    text = text.lstrip("\ufeff").replace("\r\n", "\n").replace("\r", "\n")
    m = FRONTMATTER_RE.match(text)
    if not m:
        raise ValueError("SKILL.md missing YAML frontmatter (--- ... ---)")
    block, body = m.group(1), m.group(2).strip()
    try:
        meta = _parse_structured(block)
    except Exception:
        meta = {}  # YAML failure -> fall back to the flat line parser
    # fill gaps (and repair nesting accidents on scalar keys)
    for key, val in _parse_flat(block).items():
        if key not in meta or (key in SCALAR_KEYS
                               and isinstance(meta[key], (dict, list))):
            meta[key] = val
    return meta, body


# --------------------------------------------------------------------------
# skill loading
# --------------------------------------------------------------------------

def _as_text(value):
    """Frontmatter scalar -> clean string ('' for absent/non-scalar values)."""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, list):
        return " ".join(str(v) for v in value)
    return ""


def _as_tools(value):
    """allowed-tools -> space-separated string (spec form), any YAML shape."""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, list):
        return " ".join(str(v).strip() for v in value if str(v).strip())
    return ""


def _name_error(name):
    if not NAME_RE.fullmatch(name):
        return (f"invalid skill name {name!r}: must match [a-z0-9]+(-[a-z0-9]+)* "
                "(lowercase alphanumerics and single hyphens; no leading, "
                "trailing or doubled hyphen)")
    if len(name) > MAX_NAME_LEN:
        return f"skill name too long: {len(name)} chars (max {MAX_NAME_LEN})"
    return None


def _build_skill(meta, body, name, skill_dir, scripts):
    md = meta.get("metadata")
    return {
        "name": name,
        "description": _as_text(meta.get("description")),
        "license": _as_text(meta.get("license")),
        "compatibility": _as_text(meta.get("compatibility")),
        "allowed_tools": _as_tools(meta.get("allowed-tools")),
        "metadata": {} if md is None else md,
        "body": body,
        "scripts": scripts,
        "dir": skill_dir,
    }


def load_skill(skill_dir):
    """Load a skill directory -> {name, description, allowed_tools, metadata,
    body, scripts, dir} (plus license, compatibility).

    scripts maps bare filename -> absolute path; {} means instructions-only
    mode. Raises FileNotFoundError (no SKILL.md) or ValueError (no
    frontmatter, non-conforming name, name != directory name).
    """
    skill_dir = os.path.abspath(skill_dir)
    path = os.path.join(skill_dir, "SKILL.md")
    if not os.path.isfile(path):
        raise FileNotFoundError(f"No SKILL.md in {skill_dir}")
    try:
        with open(path, encoding="utf-8-sig") as f:
            raw = f.read()
    except UnicodeDecodeError as e:
        raise ValueError(f"SKILL.md is not valid UTF-8: {e}") from e
    meta, body = parse_frontmatter(raw)

    name = meta.get("name")
    if not isinstance(name, str) or not name:
        raise ValueError("SKILL.md frontmatter requires a non-empty `name`")
    err = _name_error(name)
    if err:
        raise ValueError(err)
    dir_name = os.path.basename(skill_dir)
    if name != dir_name:
        raise ValueError(
            f"skill name {name!r} must match directory name {dir_name!r}")

    scripts = {}
    scripts_dir = os.path.join(skill_dir, "scripts")
    if os.path.isdir(scripts_dir):
        for fn in sorted(os.listdir(scripts_dir)):
            if fn.startswith("."):
                continue
            full = os.path.join(scripts_dir, fn)
            if os.path.isfile(full):
                scripts[fn] = os.path.abspath(full)
    return _build_skill(meta, body, name, skill_dir, scripts)


def load_skill_from_text(md_text, name):
    """Load a pasted SKILL.md (no directory on disk) -> same dict as
    load_skill with scripts={} and dir=None.

    The `name` argument is authoritative — it stands in for the directory
    name; a frontmatter `name`, if present, is ignored. Raises ValueError on
    a non-conforming name or missing frontmatter.
    """
    if not isinstance(name, str) or not name.strip():
        raise ValueError("name is required for pasted skills")
    err = _name_error(name)
    if err:
        raise ValueError(err)
    meta, body = parse_frontmatter(md_text)
    return _build_skill(meta, body, name, None, {})


# --------------------------------------------------------------------------
# script execution
# --------------------------------------------------------------------------

def _cap(text):
    if len(text) > MAX_SCRIPT_OUTPUT:
        return (text[:MAX_SCRIPT_OUTPUT]
                + f"\n...[truncated, {len(text) - MAX_SCRIPT_OUTPUT} more chars]")
    return text


def _bash_argv(path):
    """Resolve a bash interpreter that actually runs scripts.

    On Windows a bare "bash" hits C:\\Windows\\System32\\bash.exe (the WSL
    launcher) because CreateProcess checks System32 before PATH — even when
    Git's bash is first on PATH. Resolve explicitly, skipping the WSL
    launcher locations and falling back to well-known Git installs.
    """
    if os.name != "nt":
        return ["bash", path]
    exe = shutil.which("bash")
    if exe and not any(s in os.path.dirname(exe).lower()
                       for s in ("system32", "windowsapps")):
        return [exe, path]
    roots = [os.environ.get("ProgramW6432", r"C:\Program Files"),
             os.environ.get("ProgramFiles", r"C:\Program Files"),
             os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")]
    for root in roots:
        for sub in ("bin", "usr\\bin", "usr/bin"):
            cand = os.path.join(root, "Git", sub, "bash.exe")
            if os.path.isfile(cand):
                return [cand, path]
    return [exe, path] if exe else ["bash", path]


def run_script(skill_dir, script, args=None, timeout=60):
    """Run one of a skill's scripts/ entries -> {ok, stdout, stderr, returncode}.

    Dispatch by extension: .py (this interpreter), .sh (bash, resolved via
    PATH with Git-for-Windows fallbacks), .js (node), anything else executed
    directly; cwd is the skill directory. Script and runtime failures never
    raise — they come back as ok=False + stderr (timeout, missing
    interpreter, OS errors included). A non-existent or path-traversing
    script name raises (KeyError / ValueError).
    """
    if not isinstance(script, str) or not script:
        raise ValueError("script must be a non-empty filename")
    if ("/" in script or "\\" in script or os.sep in script
            or script in (".", "..")):
        raise ValueError(
            f"invalid script name {script!r}: must be a bare filename inside scripts/")
    skill = load_skill(skill_dir)
    if script not in skill["scripts"]:
        raise KeyError(
            f"script {script!r} not in scripts/ (have: {sorted(skill['scripts'])})")
    path = skill["scripts"][script]
    ext = script.rsplit(".", 1)[-1].lower() if "." in script else ""
    if ext == "py":
        argv = [sys.executable, path]
    elif ext == "sh":
        argv = _bash_argv(path)
    elif ext == "js":
        node = shutil.which("node")
        argv = [node or "node", path]
    else:
        argv = [path]
    if args is None:
        args = []
    elif isinstance(args, str):
        args = [args]
    argv += [str(a) for a in args]
    run_kwargs = {"capture_output": True, "text": True, "encoding": "utf-8",
                  "errors": "replace", "timeout": timeout, "cwd": skill["dir"]}
    if os.name == "nt":
        run_kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
    try:
        proc = subprocess.run(argv, **run_kwargs)
    except subprocess.TimeoutExpired:
        return {"ok": False, "stdout": "", "stderr": f"timeout {timeout}s",
                "returncode": -1}
    except FileNotFoundError as e:
        return {"ok": False, "stdout": "",
                "stderr": f"cannot execute {script!r}: {e}", "returncode": -1}
    except OSError as e:
        return {"ok": False, "stdout": "", "stderr": f"execution failed: {e}",
                "returncode": -1}
    return {"ok": proc.returncode == 0,
            "stdout": _cap(proc.stdout or ""),
            "stderr": _cap(proc.stderr or ""),
            "returncode": proc.returncode}


# --------------------------------------------------------------------------
# validation
# --------------------------------------------------------------------------

def validate_skill(skill):
    """Return a list of Agent Skill Open Standard violations ([] == valid).

    Enforces the name rules (format, <= 64 chars, match with the directory
    name when `dir` is set) plus description (required, <= 1024),
    compatibility (<= 500), metadata (string map) and allowed_tools shape.
    """
    if not isinstance(skill, dict):
        return [f"skill must be a dict (got {type(skill).__name__})"]
    errors = []

    name = skill.get("name")
    if "name" not in skill:
        errors.append("missing required field: name")
    elif not isinstance(name, str) or not name:
        errors.append(f"name must be a non-empty string (got {name!r})")
    else:
        if not NAME_RE.fullmatch(name):
            errors.append(
                f"invalid name {name!r}: must match [a-z0-9]+(-[a-z0-9]+)* "
                "(lowercase alphanumerics and single hyphens; no leading, "
                "trailing or doubled hyphen)")
        if len(name) > MAX_NAME_LEN:
            errors.append(
                f"name too long: {len(name)} chars (max {MAX_NAME_LEN})")
        skill_dir = skill.get("dir")
        if skill_dir and os.path.basename(os.path.abspath(skill_dir)) != name:
            errors.append(
                f"name {name!r} does not match directory name "
                f"{os.path.basename(os.path.abspath(skill_dir))!r}")

    desc = skill.get("description")
    if "description" not in skill:
        errors.append("missing required field: description")
    elif not isinstance(desc, str):
        errors.append(
            f"description must be a string (got {type(desc).__name__})")
    else:
        if not desc.strip():
            errors.append("description is required (empty)")
        if len(desc) > MAX_DESC_LEN:
            errors.append(
                f"description too long: {len(desc)} chars (max {MAX_DESC_LEN})")

    compat = skill.get("compatibility", "")
    if isinstance(compat, str):
        if len(compat) > MAX_COMPAT_LEN:
            errors.append(
                f"compatibility too long: {len(compat)} chars "
                f"(max {MAX_COMPAT_LEN})")
    elif compat is not None:
        errors.append(
            f"compatibility must be a string (got {type(compat).__name__})")

    md = skill.get("metadata")
    if md is not None:
        if not isinstance(md, dict):
            errors.append(
                f"metadata must be a string map (got {type(md).__name__})")
        else:
            for k, v in md.items():
                if not isinstance(v, str):
                    errors.append(
                        f"metadata[{k!r}] must be a string "
                        f"(got {type(v).__name__})")

    tools = skill.get("allowed_tools")
    if tools not in (None, ""):
        if not (isinstance(tools, str)
                or (isinstance(tools, list)
                    and all(isinstance(t, str) for t in tools))):
            errors.append(
                "allowed_tools must be a space-separated string "
                f"(got {type(tools).__name__})")

    scripts = skill.get("scripts")
    if scripts is not None and not isinstance(scripts, dict):
        errors.append(
            f"scripts must be a dict of filename -> path "
            f"(got {type(scripts).__name__})")

    return errors


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def _main(argv):
    args = argv[1:]
    validate = bool(args) and args[0] in ("--validate", "-v")
    if validate:
        args = args[1:]
    if not args:
        print("usage: python skill_loader.py [--validate] <skill-dir> "
              "[script] [args...]", file=sys.stderr)
        return 2
    try:
        skill = load_skill(args[0])
    except (ValueError, FileNotFoundError) as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1
    if validate:
        errors = validate_skill(skill)
        print(f"SKILL: {skill['name']}")
        if errors:
            print(f"VALIDATION: {len(errors)} error(s)")
            for err in errors:
                print(f"  - {err}")
            return 1
        print("VALIDATION: OK")
        return 0
    print("SKILL:", skill["name"])
    print("DESC:", skill["description"])
    print("TOOLS:", skill["allowed_tools"] or "(none)")
    print("SCRIPTS:", list(skill["scripts"]))
    print("---BODY (first 200)---")
    print(skill["body"][:200])
    if len(args) > 1:
        print("---RUN", args[1], "---")
        print(json.dumps(run_script(args[0], args[1], args[2:]), indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv))
