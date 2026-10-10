#!/usr/bin/env python
"""Reconcile row counts and a column sum between two JSON-lines files.

Part of the `fault-inject` fixture skill. This is the "silent tool failure"
probe from arXiv 2610.10062: a pipeline step that exits 0 while quietly
dropping rows still shows up as a count/sum mismatch here.

Prints exactly one JSON object on stdout:

    {"rows_in", "rows_out", "rows_expected", "delta", "delta_pct",
     "column_sum_in", "column_sum_out", "classification", "evidence"}

Exit code is 0 whenever the *probe itself* ran - a QUIET failure is reported in
the JSON, not in the exit code. That is the point of the fixture.

Usage:
    python scripts/probe.py --in a.jsonl --out b.jsonl [--column amount]
                            [--expected N]
"""

import argparse
import json
import sys


def _count_and_sum(path, column):
    """Return (row_count, column_sum, note) or (None, None, reason) on failure."""
    rows = 0
    total = 0.0
    numeric = 0
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                rows += 1
                if not column:
                    continue
                try:
                    obj = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(obj, dict) or column not in obj:
                    continue
                value = obj[column]
                if isinstance(value, bool):
                    continue
                if isinstance(value, (int, float)):
                    total += float(value)
                    numeric += 1
    except OSError as exc:
        return None, None, f"cannot read {path}: {exc}"
    note = None
    if column and numeric == 0:
        note = f"column {column!r} absent or non-numeric in {path}"
    return rows, (round(total, 2) if column else None), note


def main(argv=None):
    ap = argparse.ArgumentParser(description="Reconcile two JSON-lines files.")
    ap.add_argument("--in", dest="src", required=True, help="input .jsonl path")
    ap.add_argument("--out", dest="dst", required=True, help="output .jsonl path")
    ap.add_argument("--column", default=None,
                    help="numeric column to sum before/after (optional)")
    ap.add_argument("--expected", type=int, default=None,
                    help="rows the step claims it processed (optional)")
    args = ap.parse_args(argv)

    rows_in, sum_in, note_in = _count_and_sum(args.src, args.column)
    rows_out, sum_out, note_out = _count_and_sum(args.dst, args.column)

    if rows_in is None or rows_out is None:
        print(json.dumps({
            "classification": "LOUD",
            "evidence": note_in or note_out,
            "error": True,
        }, indent=2))
        return 0

    delta = rows_in - rows_out
    delta_pct = round((delta / rows_in) * 100, 2) if rows_in else 0.0
    sum_delta = None
    if sum_in is not None and sum_out is not None:
        sum_delta = round(sum_in - sum_out, 2)

    problems = []
    if delta != 0:
        problems.append(f"{delta} rows lost ({delta_pct}%)")
    if sum_delta not in (None, 0):
        problems.append(f"column sum off by {sum_delta}")
    if note_in:
        problems.append(note_in)
    if note_out:
        problems.append(note_out)

    if args.expected is not None and args.expected != rows_out:
        problems.append(
            f"rows_expected={args.expected} but rows_out={rows_out}")

    classification = "QUIET" if problems else "CLEAN"
    evidence = "; ".join(problems) if problems else (
        f"{rows_in} rows in, {rows_out} rows out, "
        f"column sums match ({sum_in})")

    print(json.dumps({
        "rows_in": rows_in,
        "rows_out": rows_out,
        "rows_expected": args.expected if args.expected is not None else rows_out,
        "delta": delta,
        "delta_pct": delta_pct,
        "column_sum_in": sum_in,
        "column_sum_out": sum_out,
        "column_sum_delta": sum_delta,
        "classification": classification,
        "evidence": evidence,
    }, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
