---
name: structured-extract
description: Extract records from messy unstructured text into strict JSON with no inferred fields, no hedging, and no prose. Use when parsing incident reports, log excerpts, emails, or support tickets into machine-readable rows where a hallucinated field is worse than a null one.
license: MIT
allowed-tools: Read
metadata:
  version: "1.0"
  category: extraction
  kaizen-role: fixture-divergent
  divergence-note: >
    Designed so the constraint set is long enough to consume a small MoE
    model's effective capacity. The 31B arm holds all six constraints and
    jumps; the 26B-A4B arm tends to drop or partially emit the format block,
    which the rubric punishes harder than loose prose does. Verify live -
    predicted, not measured.
---

# structured-extract

You are extracting records from messy text into strict JSON. The single rule
that outranks everything else: **a null field is always better than an invented
one.** Downstream code will act on these rows.

## The six constraints

All six are graded. Violating any one costs real score.

1. **Valid JSON only.** The entire reply is one JSON array. No markdown fence,
   no leading sentence, no trailing explanation, no "Here is the JSON:".
2. **Never infer.** If the text does not state a field, emit `null`. Do not
   derive it from a similar row, do not guess a plausible value, do not carry a
   value forward from context.
3. **No hedging words.** `probably`, `likely`, `maybe`, `approximately`,
   `around`, `presumably`, `seems`, `appears to be`, `unclear`, `possibly`.
   These are banned in *every* string value. If you are unsure, the field is
   `null` — uncertainty is expressed by nullity, never by a hedge word.
4. **Exact keys, in this order**, nothing added, nothing renamed:
   `id`, `subject`, `status`, `reported_at`, `assignee`, `priority`, `notes`
5. **Types are fixed.** `id` string, `subject` string, `status` one of
   `open|closed|pending|unknown`, `reported_at` ISO-8601 date or `null`,
   `assignee` string or `null`, `priority` integer 1-5 or `null`, `notes`
   string or `null`. Never a number for a string field, never `"null"` for
   `null`, never `-1` for an unknown priority.
6. **One row per distinct subject.** Merge duplicate mentions of the same
   subject into one row; keep the earliest `reported_at`.

## Output shape

```json
[
  {"id": "INC-1042", "subject": "Checkout timeout on EU region",
   "status": "open", "reported_at": "2026-09-14", "assignee": "r.okafor",
   "priority": 2, "notes": null}
]
```

Empty input with no extractable records is `[]` — an empty array, not an error
message, not an explanation of why it is empty.

## Common failure modes

- Wrapping the array in ```json fences — breaks constraint 1
- Writing `"assignee": "probably Dana"` — breaks 2 and 3
- Emitting `"priority": "high"` — breaks 4
- Prefixing `Here are the extracted records:` — breaks 1
- Adding a `"confidence"` or `"source_line"` key — breaks 4

## Rubric

```json
[
  {"name": "valid-json-array", "weight": 3, "check": "The reply parses as a single JSON array with no prose, fence, or preamble"},
  {"name": "exact-key-set-and-order", "weight": 3, "check": "Every row has exactly the 7 keys in the documented order, no extras, no renames"},
  {"name": "no-inferred-values", "weight": 3, "check": "Fields absent from the source are null, never guessed or carried across rows"},
  {"name": "no-hedging-words", "weight": 2, "check": "No probably/likely/maybe/approximately/unclear-type words in any string value"},
  {"name": "correct-value-types", "weight": 2, "check": "priority is int 1-5 or null, status is from the closed set, dates are ISO-8601"},
  {"name": "duplicates-merged", "weight": 1, "check": "Repeated subjects collapse into one row keeping the earliest reported_at"}
]
```
