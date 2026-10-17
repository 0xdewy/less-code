# How much could a learned "edit chooser" add? (2026-10-17)

Question: would a local model, trained (SFT / rejection sampling / RL) to pick
structural edits from a fixed menu, add enough on this corpus to justify
building it?

Method: a perfect chooser, by hand. After `lc shrink --aggressive` on
humanize (793 -> 732), every edit expressible as a menu operation (extract a
duplicated block into a helper, bind repeated call arguments, merge
duplicates) was applied manually, formatted with `ruff format`, and checked
with the project's suite (724 passed, 74 skipped). Nothing outside the menu
was allowed.

| edit | file | lines saved |
|---|---|---:|
| `naturalday`/`naturaldate` shared 11-line block -> `_today_and_date` helper | time.py | 13 (with the next row) |
| seven `_quotient_and_remainder(..., min_unit, suppress_set, format)` calls -> `functools.partial` | time.py | (incl. above) |
| triple "parse or bail out" block in number.py -> helper with sentinel return | number.py | ~2 (estimated) |

Ceiling: about 15 of 732 lines, roughly +2 percentage points on humanize.
Other opportunities in the reduced tree were either inherent control flow
(nested early returns in `naturaldelta`), deliberate design (per-function lazy
`import math` under `__lazy_modules__`), or data tables.

Two constraints surfaced:

- Deduplication conflicts with the documentation gate as written: merging
  two identical blocks keeps one copy of each comment, and the gate requires
  the exact multiset. Any dedup tool (rule or model) needs an exemption for
  exact duplicate comments of merged code.
- On these hand-tuned libraries the chooser's headroom is small; the earlier
  ladder (`LADDER.md`) also recorded zero accepted edits from free-form local
  7-8B rewrites. A learned component is more likely to pay off on less
  curated code (application code, legacy code, generated code), which this
  corpus does not contain.

Corpus figures for comparison: default profile 8.85%, `--aggressive` 11.69%
(see `BASELINE.md`, `AGGRESSIVE.md`).
