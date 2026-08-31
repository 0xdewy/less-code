# Iteration 10 — py/rs settlement runs (canonical metric)

> **Superseded baseline (2026-08-31).** Every py number below starts from
> **500**, which was `fixtures/py/inventory.py` *after* an in-place `lc reduce`
> run had been committed over it in `8177df1`. The C7 review caught that
> (defect D1); the pristine 537-LOC fixture is restored and the static pass
> re-derived on a clean copy gives **537 -> 357 = 33.52 %**
> (`bench/results/20260831T073226Z.jsonl`, tree at
> `docs/evidence/py-static-357/`). The 357 endpoint, the four outlined helpers
> and the 107-line attribution below are all unchanged — only the denominator
> moved, and it moved in the conservative direction. rs and js numbers below
> are unaffected.


Machine: RTX 2060 Max-Q, 6 GB. Backend: ollama. All runs sequential,
`ollama stop qwen2.5-coder:7b` between models. Canonical metric = the
formatter-normalized code-LOC the bench reports; no gate was relaxed.

## Static-only (no GPU)

`uv run lc bench --static-only` — `bench/results/20260831T023655Z.jsonl`

| fixture | LOC start | after static | static % | tests | api | hidden | s |
|---|---:|---:|---:|---|---|---|---:|
| js | 601 | 553 | 7.99 | ok | ok | ok | 2.1 |
| **py** | 500 | **357** | **28.6** | ok | ok | ok | 1.7 |
| rs | 392 | 372 | 5.1 | ok | ok | ok | 1.9 |

py's 143 lines: 107 from the four outlined guard helpers (`_check_sku` 72,
`_check_qty` 21, `_check_sku2` 7, `_check_inventory` 7), the rest from the
dead-code, peephole-rule and ruff layers that already existed.

## rs — strategy comparison

| # | config | strategy | after static | final | hybrid % | calls | s | jsonl |
|---|---|---|---:|---:|---:|---:|---:|---|
| 1 | 3B | mixed | 372 | 367 | 6.38 | 8 | 36 | `20260831T024149Z` |
| 2 | **7B** | mixed | 372 | 356 | 9.18 | 14 | 261 | `20260831T024248Z` |
| 3 | **7B** | whole-file | 372 | **336** | **14.29** | 8 | 1092 | `20260831T024743Z` |

tests / api / hidden green on all three.

Run 1 is the cheap trial the settlement plan allows for choosing a strategy.
It produced the **first rust duplicate-group acceptance in the project's
history** — `csv_escape_row` + `csv_escape_row_owned` merged on attempt 1 at
**3B**, where iteration 09 was 0 for 12 at 3B and 0 for 6 at 7B. Run 2
reproduced it at 7B and added `csv_escape_field`. But run 3 shows the mixed
strategy still loses on totals: one accepted whole-file rewrite of `stats.rs`
(155 → 119) is worth more than every per-symbol and pair-wise acceptance
combined, so the whole-file recipe is the one recorded as rs's best.

Rejection breakdown, runs 1-3 (budget-exhaustion records excluded):

| granularity | proposals | accepted | rate |
|---|---:|---:|---:|
| dedup-pair | 10 | **2** | **20 %** |
| symbol | 12 | 1 | 8 % |
| whole-file | 8 | 1 | 13 % |

Compare iteration 09: dedup-group 18 proposals, 0 accepted, 0 %.

## py

### Attempt logs

`reduce-rs-7b-mixed-it10.log`, `reduce-rs-7b-wholefile-it10.log`,
`reduce-py-7b-wholefile-it10.log`. The 3B trial (run 1) was a foreground
validation run; its records, verbatim:

```
[L2] dedup:word_frequencies+char_frequencies attempt=1 syntax-error 155->134
[L2] dedup:word_frequencies+char_frequencies attempt=2 syntax-error 155->110
[L2] dedup:csv_escape_row+csv_escape_row_owned attempt=1 accepted 217->212
[L2] dedup:count_leading_spaces+count_trailing_spaces attempt=1 bad-dedup-reply 212->212
[L2] dedup:count_leading_spaces+count_trailing_spaces attempt=2 syntax-error 212->196
[L2] lib.rs:slugify attempt=1 tests-failed 212->186
[L2] lib.rs:slugify attempt=2 syntax-error 212->198
[L2] lib.rs:render_template attempt=1 not-smaller 212->216
[L2] lib.rs:render_template attempt=2 backend-error 212->212   # budget exhausted
[L2] lib.rs attempt=1 backend-error 212->212
[L2] stats.rs:analyze attempt=1 backend-error 155->155
[L2] stats.rs attempt=1 backend-error 155->155
```

## py — 7B whole-file settlement

`uv run lc bench --fixture py --model qwen2.5-coder:7b --strategy whole-file
--attempts 4 --max-llm-calls 12 --num-ctx 16384 --llm-timeout 2400`
— `bench/results/20260831T030620Z.jsonl`

| LOC start | after static | final | static % | **hybrid %** | tests | api | hidden | calls | s |
|---:|---:|---:|---:|---:|---|---|---|---:|---:|
| 500 | 357 | **348** | 28.6 | **30.4** | ok | ok | ok | 4 | 2019 |

```
[L2] inventory.py attempt=1 tests-failed  357->340
[L2] inventory.py attempt=2 api-changed   355->292
[L2] inventory.py attempt=3 tests-failed  355->328
[L2] inventory.py attempt=4 api-changed   351->306
```

**Every whole-file proposal was rejected. All 9 LLM lines came from C1 hunk
salvage** — the `loc_before` column walks 357 → 355 → 355 → 351, which is the
salvager keeping the individually-green hunks of a rejected rewrite. The
model's own bets were 0 for 4; the salvage machinery around them earned 2.5 %.
That is the same shape as the js pass, and it is worth being explicit about:
on py the LLM layer is now a 9-line addendum to a 143-line deterministic
result.

## GPU accounting

| run | wall |
|---|---:|
| rs 3B mixed | 36 s |
| rs 7B mixed | 261 s |
| rs 7B whole-file | 1092 s |
| py 7B whole-file | 2019 s |
| **total** | **3408 s (57 min)** |

Inside the 90-minute authorization; ≤ 40 min per fixture; no re-runs taken.
