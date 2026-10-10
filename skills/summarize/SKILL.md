---
name: summarize
description: Summarizes a document into a fixed-size brief with every claim traced to a source passage. Use when condensing long text into an executive brief where an unsourced or dropped number is a correctness failure.
license: MIT
allowed-tools: Read
metadata:
  version: "1.0"
  category: writing
  kaizen-role: fixture
---

# summarize

You are producing a fixed-size brief from a longer document. The brief has a
hard budget and every claim carries its source.

## Budget

- **Exactly 5 bullets.** Not 4, not 6. If the document has fewer than five
  points, pad with genuinely lower-priority findings — never pad with filler.
- **≤ 25 words per bullet.** Count them. A 30-word bullet is a failed bullet.
- **One headline line, ≤ 12 words**, above the bullets. No trailing punctuation.

## Ordering

Bullets are ordered by materiality to the reader, not by position in the source
document. The single most decision-relevant fact is bullet 1.

## Sourcing

Each bullet ends with a `¶` paragraph reference — the paragraph number from the
source document, 1-indexed. A bullet that synthesises across paragraphs cites
all of them (`¶4,7`).

**Any number that appears in the brief must appear verbatim in the source.** No
rounding, no unit conversion, no recomputation. If a figure is only stated in a
chart or a table, write `(unsourced)` after it.

## What must survive

Never drop these, even under budget pressure:

- Any number, date, or monetary figure
- Any explicit negative finding, risk, or blocker
- Any named owner, vendor, or deadline

If something must be cut to fit the budget, cut colour, narrative, and context —
never a figure or a risk.

## What must not appear

- No first person ("I", "we found")
- No hedging ("seems", "appears to", "may indicate")
- No claims absent from the source document
- No bullet that restates another bullet

## Output shape

```
<one headline line, ≤12 words>

- <bullet, ≤25 words, ends with ¶N>
- <bullet, ≤25 words, ends with ¶N>
- <bullet, ≤25 words, ends with ¶N>
- <bullet, ≤25 words, ends with ¶N>
- <bullet, ≤25 words, ends with ¶N>
```

## Rubric

```json
[
  {"name": "exactly-five-bullets", "weight": 3, "check": "Produces exactly 5 bullets, no more and no fewer"},
  {"name": "word-budget-respected", "weight": 2, "check": "Every bullet is at most 25 words"},
  {"name": "headline-within-limit", "weight": 1, "check": "One headline line of at most 12 words above the bullets"},
  {"name": "claims-sourced", "weight": 3, "check": "Every bullet ends with a paragraph reference back to the source"},
  {"name": "numbers-preserved-verbatim", "weight": 3, "check": "Every figure is copied exactly as written in the source, no rounding or conversion"},
  {"name": "no-unsourced-claims", "weight": 2, "check": "Contains nothing absent from the source document; no hedging language"}
]
```
