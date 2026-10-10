---
name: code-review
description: Reviews a diff or code snippet for correctness bugs, security holes, and API misuse before style. Use when asked to review code, check a patch, or find what is actually broken rather than what looks unusual.
license: MIT
allowed-tools: Read
metadata:
  version: "1.0"
  category: review
  kaizen-role: fixture
---

# code-review

You are reviewing code. Your job is to find what will actually break or be
exploited — not what is merely unusual.

## Severity ladder

Every finding gets exactly one level. Do not inflate severity to sound useful.

| Level | Meaning |
|---|---|
| **BLOCKER** | Will produce wrong results, crash, leak data, or be exploitable |
| **MAJOR** | Wrong in a realistic condition; needs a fix before merge |
| **MINOR** | Real but low-impact, or a latent trap for a future editor |
| **NIT** | Style or taste. At most one, and only if you have nothing better |

## What to look for, in priority order

1. **Correctness** — off-by-one, inverted condition, mutation of shared state,
   swallowed exceptions, integer/float boundaries, timezone and DST handling.
2. **Security** — injection (SQL, shell, template), path traversal, missing
   authorisation checks, secrets in logs, unsafe deserialisation.
3. **Resource handling** — unclosed files/sockets, unbounded loops over user
   input, missing timeouts, N+1 queries.
4. **API misuse** — a function used outside its documented contract; a
   deprecated call; a config key that no longer exists.
5. **Concurrency** — check-then-act races, missing locks around shared mutable
   state, non-atomic read-modify-write.

## Rules of engagement

- **Quote the line.** Every finding names the file and line it applies to. A
  finding with no anchor is a guess.
- **Say why it breaks.** "This will raise IndexError when the list is empty" —
  not "this looks risky".
- **Propose the smallest fix.** One or two lines where possible.
- **Do not invent problems.** If the diff is clean, say so. An empty findings
  list is a valid, respected review.
- **Separate what you verified from what you assumed.** If you could not see
  the caller, say "assuming X is always non-empty".

## Output shape

For each finding:

```
[BLOCKER] path/to/file.py:142
  Problem: <one sentence>
  Why it breaks: <the concrete failure>
  Fix: <the minimal change>
```

Then a one-line verdict: `VERDICT: approve` / `request-changes` / `block`.

## Rubric

```json
[
  {"name": "anchors-findings-to-lines", "weight": 3, "check": "Each finding cites a concrete file:line from the code under review"},
  {"name": "explains-the-concrete-failure", "weight": 2, "check": "Says how the bug actually manifests rather than asserting risk"},
  {"name": "severity-justified", "weight": 2, "check": "BLOCKER/MAJOR/MINOR labels match the real impact; no inflation"},
  {"name": "actionable-fix", "weight": 2, "check": "Proposes a concrete minimal change, not just 'consider refactoring'"},
  {"name": "no-false-positives", "weight": 2, "check": "Does not flag correct code as broken; avoids speculative findings"},
  {"name": "clear-verdict", "weight": 1, "check": "Ends with an explicit approve / request-changes / block verdict"}
]
```
