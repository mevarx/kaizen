---
name: fault-inject
description: Detects silent tool failures — a command that exits 0 while quietly dropping rows, truncating output, or swallowing an error. Use when a data pipeline, ETL step, or batch job reports success but the record counts, byte sizes, or row sums do not reconcile.
license: MIT
allowed-tools: Read Bash
metadata:
  version: "1.0"
  source: "arXiv:2610.10062 - loud vs. quiet tool failures"
  category: reliability
  kaizen-role: fixture
---

# fault-inject

Loud failures announce themselves with a non-zero exit code. Silent failures are
the dangerous ones: the process exits 0, prints a tidy summary, and has quietly
dropped 12% of the rows. This skill teaches you to distrust a successful exit
code and reconcile the *numbers* instead.

## Procedure

1. **Never trust exit 0 alone.** Treat `returncode == 0` as "the process did not
   crash", never as "the work is correct".
2. **Reconcile three counts** for any data-moving step:
   - `rows_in` — records read from the source
   - `rows_out` — records written to the destination
   - `rows_expected` — what the step *claims* it handled
   A silent failure shows up as `rows_in != rows_out` with no error raised.
3. **Check the tail, not the head.** Quiet failures emit their one real signal in
   the last few lines of stderr — a truncated write, a skipped partition, a
   swallowed retry. Always read the final 5 lines of stderr.
4. **Verify a checksum or a total**, not a spot check. Summing a numeric column
   before and after catches lossy re-encoding that spot checks miss.
5. **Classify what you found** as one of:
   - `LOUD` — non-zero exit or explicit error text
   - `QUIET` — exit 0, but counts/checksums disagree
   - `CLEAN` — exit 0 and every reconciliation passes
6. **Report the delta explicitly.** If 1,000 rows went in and 880 came out, say
   "120 rows lost (12.0%)" — never "some data was lost".

## Using the bundled script

`scripts/probe.py` implements step 2 for a pair of JSON-lines files:

```bash
python scripts/probe.py --in rows_in.jsonl --out rows_out.jsonl --column amount
```

It prints one JSON object: `{rows_in, rows_out, rows_expected, delta,
delta_pct, column_sum_in, column_sum_out, classification, evidence}`. Feed that
object straight into your answer — it is the reconciliation, already computed.
`classification` is `LOUD` / `QUIET` / `CLEAN` per step 5.

## Output format

Always answer in this shape, even when everything reconciles:

```
classification: QUIET
rows: 1000 in -> 880 out (delta 120, 12.0%)
column sum: 45231.50 -> 45231.50
evidence: <the specific line or number that proves it>
verdict: <one sentence naming the failing stage, or "no silent failure found">
```

## Rubric

```json
[
  {"name": "reconciles-row-counts", "weight": 3, "check": "States rows_in and rows_out as concrete numbers, not 'some rows were lost'"},
  {"name": "names-the-classification", "weight": 2, "check": "Explicitly labels the failure LOUD, QUIET, or CLEAN"},
  {"name": "distrusts-exit-zero", "weight": 2, "check": "Says or shows that exit code 0 does not establish correctness"},
  {"name": "cites-concrete-evidence", "weight": 2, "check": "Quotes a real count, delta, percentage, or stderr line rather than hedging"},
  {"name": "computes-delta-percent", "weight": 1, "check": "Gives the loss as a percentage, not just a raw count"},
  {"name": "verdict-names-the-stage", "weight": 1, "check": "Closes with a single sentence naming which stage failed, or that none did"}
]
```
