# Iteration 8 — per-symbol proposals (C2), API-violation feedback, the guarded/walrus rules, and D1/D2 static

## Attempt

Iteration 7's gap diagnosis had one finding that dominated all the others:

> **The proposal granularity is wrong.** Almost every accepted LOC in this
> pass came from C1 salvaging a *rejected* whole-file rewrite. […] A 500-LOC
> rewrite is a 10-minute all-or-nothing gamble; the same GPU minutes spent on
> per-symbol diffs would be ~20 independently gated bets.

At 3B *and* at 7B, **no whole-file candidate ever passed outright**. This pass
executes gap items 1, 2, 4 and 5: per-symbol proposals as the default L2
strategy, API-violation feedback, the D1/D2 static layers, the two declined
rule shapes, and the bench-diagnosability fixes. Then it re-benches with the
same 3B model so the change is measured, not asserted.

**Mid-pass, iteration 7's deferred 7B js settlement run finished and was
committed as `3ac8d23`** — on the pre-change code, and it clears the 25 % bar
canonically. It is analysed below, and it materially qualifies this pass's
conclusion rather than confirming it.

## What landed

### 1. Per-symbol proposals are the default L2 strategy (C2)

`less_code/llm_reduce.py` gains `reduce_symbols()`, and
`less_code/pipeline.py` calls it for **every** language before anything else.
The loop is:

1. index the file's top-level symbols with `hunks.symbol_spans` — python via
   `ast`, js/rust via the brace matcher already used by `hunks.py` and
   `grpo/build_dataset.py`, so one code path covers all three languages;
2. take the biggest not-yet-attempted symbol;
3. prompt with **that symbol only**, a signatures-only `file_map()` of the
   rest of the file, and the frozen test suite as the behaviour spec;
4. splice the reply back over the symbol's span and run the *same*
   `verify_candidate` gate (not-smaller → parse → API → tests);
5. on acceptance, re-read the file and re-resolve spans by key, because every
   acceptance shifts the line numbers below it.

`SYMBOL_SYSTEM_PROMPT` asks for one symbol and states the expected output
shape; the generation-length expectation drops from a whole file
(7-8k tokens on the py fixture) to one symbol (a few hundred).

Two details are load-bearing:

- **Duplicate names get distinct keys** (`name`, `name#2`). Rust has two
  `impl Inventory` blocks in the same file; without keys, re-resolution after
  an acceptance would rewrite the wrong one.
- **A whole-file reply is verified as a whole file, not spliced.** A 3B model
  frequently ignores "one symbol" and returns the entire file. Splicing that
  in duplicates every other symbol — and the duplicate *still passes the
  suite*, because in Python and JS the later definition wins. `_as_whole_file`
  detects the shape (≥2 symbols shared with the original) and verifies it as
  the whole-file candidate it is; `_duplicate_keys` refuses any splice that
  would define a name twice, and tells the model so.

The whole-file pass survives as an optional final sweep
(`reduce_project(..., whole_file_sweep=True)`, on by default) with hunk
salvage on its rejects, because a whole-file rewrite is the only shape that
can dedup *across* symbols.

### 2. API-violation feedback (gap item 2)

`verify_candidate` used to return `api-changed: <first violation, truncated to
120 chars>` and the next prompt was handed that string. It now carries the
whole violation list, and `api_check.api_feedback()` turns it into
instructions naming every missing / changed / added symbol and what to do
about each. `llm_reduce._feedback_for()` routes an `api-changed` outcome
through it and passes a test failure through verbatim as before.

`tests/test_per_symbol.py::test_api_violation_feedback_lets_the_next_attempt_fix_it`
scripts a backend that renames a public symbol on attempt 1 and repairs it
**only after seeing `missing=` in the prompt** — the loop is asserted, not
assumed.

### 3. Rule library: guarded comprehensions and the walrus binding

Both shapes iteration 7 deliberately declined are now taken, in
`_collapse_loop_body`:

| shape | before | after |
|---|---|---|
| guard | `out = []` + `for v in xs:` / `if c:` / `out.append(e)` | `out = [e for v in xs if c]` |
| twice-read temp | `for k in ks:` / `t = d[k]` / `if t.a <= t.b:` / `out.append(k)` | `out = [k for k in ks if (t := d[k]).a <= t.b]` |

The safety argument is `_first_evaluated_name()`: the walrus is only allowed
when the temp is the condition's **first-evaluated name** (walking the
leftmost evaluation path: `BoolOp→values[0]`, `Compare→left`,
`Attribute/Subscript→value`, …). Then `(t := d[k])` lands exactly where the
original assignment stood and **nothing is reordered at all** — which is why
`test_walrus_is_refused_when_the_temp_is_not_evaluated_first` (a `limit > 0
and …` guard) must decline. A guard containing a call is refused outright;
only one walrus per rewrite is allowed, because the order proof covers one.

Both `append-loop-to-comprehension` and `accumulate-to-sum` use the shared
helper, so `sum(e for v in xs if c)` comes along for free.

**A real bug this found in the rule library's own safety net.** A walrus
inside a class-body comprehension is a `SyntaxError` — but `ast.parse` does
*not* raise it; it is a compile-stage error. `_one_pass` validated its output
with `ast.parse`, so the rule would have shipped an uncompilable file to the
gate. Fixed two ways: `_collect` now tracks whether it is scanning a class
body and the rule declines there, **and** the final validation is
`compile(..., "exec")` rather than `ast.parse`.
(`test_walrus_in_a_class_body_never_ships`.)

### 4. D1 (python) and D2 (js) static layers

**Python.** `_python_static` is rebuilt around an ordered list of independently
gateable *layers* (`_python_layers`): dead code → each C4 rule → unreachable
code → `ruff --fix` safe tier → `ruff --fix` unsafe tier. They compose left to
right; if the combined edit set goes red the pass re-applies them one at a
time on top of the last green state and keeps only what stays green, recording
every revert in the notes (the old dead-code-then-rule narrowing was a special
case of this).

- `_ruff_fix` runs ruff **over stdin** (`--stdin-filename … -`), so the pass
  stays *pure*: the fixed text comes back on stdout and nothing is written
  outside the pipeline's revert set. (`cargo fix`, by contrast, still mutates
  the tree behind the gate's back — a pre-existing wart, not fixed here.)
- **ruff's default rule set is a correctness set and removes almost no
  lines.** On the py fixture it reports 2 `E741`s and nothing fixable. The
  pass therefore selects the LOC-reducing families explicitly:
  `F,E4,E7,E9,RET,SIM,C4,PIE,UP,PLR1,PLW,PERF,FURB`. Safe tier: **0 lines** on
  this fixture. Unsafe tier: **10 lines** (`RET505` else-after-return, `UP004`
  `class C(object)`, …) — which is exactly why the unsafe tier is a separate
  gated layer rather than an automatic one.
- `_python_drop_unreachable` deletes statements after a `return`/`raise` (and
  `continue`/`break`) inside function scope, line-aligned only. Recorded
  caveat: deleting `return; x = 1` also deletes the binding that made `x`
  *local*, so a read of `x` earlier in the function would change from
  `UnboundLocalError` to a global read. That is why the layer is test-gated.
  On the py fixture it finds nothing.

**JavaScript.** `_js_static` now runs three things and narrows on red:

- `npx knip --reporter json`, best effort. It **did** run here and reported
  the same 3 unused exports the hand-rolled scan finds — a free cross-check,
  and the first independent confirmation that the regex scan is not missing
  anything on this fixture. Only knip's `exports` issue class is consumed:
  knip also reports unused *files*, and its very first finding is
  `tests_hidden/hidden.mjs` — acting on that would delete the fixture's hidden
  safety suite, which is the one thing the bench must never touch.
- the existing dead-export scan, now able to take knip's names too;
- `_js_remove_dead_internal`: non-exported top-level functions, classes and
  `const`/`let`/`var` bindings that nothing in the project references, using
  the same reference-scan shape as the rust dead-`pub` pass (one `\bname\b`
  hit anywhere outside the symbol's own span — any file, comments included —
  keeps it). On this fixture all six internal helpers are referenced, so it
  removes nothing. Zero yield here, correct behaviour, kept because it is the
  scan a real repo needs.

### 5. Bench diagnosability

- **Rows are appended as each fixture finishes.** Iteration 7 lost a completed
  run's evidence to a third fixture and then spent a session guessing at a row
  it could not inspect.
- `--fixture NAME` benches one fixture, so re-measuring a single language does
  not cost a full GPU run.
- A row whose language has no formatter is marked `"metric": "raw"`, shouted
  about on **stderr**, given an `UNSCOREABLE METRIC` note, and printed as
  `**RAW**` in the markdown table. Iteration 7's one promising result was
  invalidated because `formatted_loc: false` was a quiet field nobody read.
- Per-attempt records now print **live** (`llm_reduce.PROGRESS`), instead of
  only once a whole file finished.

## Evidence

### Tests

```
$ uv run pytest -q
153 passed in 18.23s        # was 132 mid-pass, 109 at iteration 07
```

New files: `tests/test_per_symbol.py` (15) and `tests/test_static_d1d2.py`
(16); `tests/test_bench_and_metrics.py` gains 5; `tests/test_rules.py` gains 6.

The interesting ones are the negatives — what must *not* happen:

- `test_a_whole_file_reply_is_verified_as_a_whole_file_not_spliced` and
  `test_duplicate_symbol_splice_is_refused` (the duplicate-definition trap);
- `test_walrus_is_refused_when_the_temp_is_not_evaluated_first`,
  `test_guard_with_a_call_is_refused`, `test_two_twice_read_temps_are_refused`,
  `test_walrus_in_a_class_body_never_ships`;
- `test_knip_is_optional_and_never_reports_files` (knip's unused-*files*
  finding is `tests_hidden/hidden.mjs`);
- `test_ruff_safe_tier_leaves_an_unused_local_alone` (the safe/unsafe split is
  real, not decorative);
- `test_module_level_code_after_return_is_not_considered` and
  `test_a_statement_sharing_the_return_line_is_left_alone`.

`test_one_bad_symbol_does_not_cost_the_good_one` is the whole thesis of this
pass in one test: a wrong `clamp` no longer throws away a correct `sign`.

### Two tests changed rather than were added, and why

`test_append_loop_with_guard_is_left_for_the_llm` asserted iteration 7's
*deliberate* limitation. It is now
`test_append_loop_with_guard_becomes_a_comprehension_if_clause`, and it
additionally runs the before and after side by side on the same input.

`test_pipeline_gate_reverts_a_misfiring_rule` asserted the exact post-narrowing
*text*. With ruff's `RET505` in the layer stack, a later layer legitimately
performs the collapse the sabotaged rule got wrong, so the assertion is now on
**behaviour** (`is_low(1, 5) is True`) plus the revert note. That is a weaker
text assertion and a stronger correctness assertion.

### Static-only bench, before and after

`uv run lc bench --static-only`, canonical metric on all three (prettier,
ruff and rustfmt all present).

| fixture | iteration 07 | iteration 08 | delta |
|---|---:|---:|---:|
| js | 7.99 % | 7.99 % | — |
| py | 2.6 % | **4.6 %** | +2.0 |
| rs | 5.1 % | 5.1 % | — |

py 500 → 477. The whole +2.0 comes from **ruff's unsafe tier** (10 LOC:
`RET505`, `UP004`). The other three new layers earn nothing on this fixture
and it is worth being precise about why:

- **ruff safe tier: 0.** Nothing in the fixture is a safe fix.
- **unreachable code: 0.** The fixture has none.
- **js knip + dead internals: 0.** knip found exactly the 3 unused exports the
  existing scan already removes; all 6 internal helpers are referenced.
- **the walrus rule: 0 canonical, +4 raw.** This is the honest one. The
  `low_stock` rewrite really does fire and really does turn 5 lines into 1:

  ```python
  result = [sku for sku in self.sorted_skus()
            if (product := self.products[sku]).on_hand <= product.reorder_level]
  ```

  …but that line is 116 characters, so `ruff format` at width 88 splits it
  back into 4. **Raw physical lines 600 → 583 (17 saved); canonical code-LOC
  500 → 487 (13 saved) — identical to iteration 7.** That is roadmap B1 doing
  precisely the job it exists for: the rewrite is genuinely better code, but
  it is not fewer *lines* once formatting is canonical, and the metric refuses
  to pay for line-joining. Recorded as a miss, not spun as a win.

### Hybrid bench — `qwen2.5-coder:3b`, the same model as iteration 07

```
uv run lc bench --model qwen2.5-coder:3b --attempts 3 --max-llm-calls 10 \
                --num-ctx 16384 --llm-timeout 1200
```

`bench/results/20260831T002525Z.jsonl`, commit `3ac8d23` + this working tree.

| fixture | lang | LOC start | after static | final | static % | hybrid % | tests | api | hidden | calls | s | metric |
|---|---|---:|---:|---:|---:|---:|---|---|---|---:|---:|---|
| js | javascript | 601 | 553 | **537** | 7.99 | **10.65** | ok | ok | ok | 10 | 82.0 | canonical |
| py | python | 500 | 477 | 477 | 4.6 | **4.6** | ok | ok | ok | 10 | 150.4 | canonical |
| rs | rust | 392 | 372 | **363** | 5.1 | **7.4** | ok | ok | ok | 10 | 82.4 | canonical |

**Hidden-test green 3/3. API preserved 3/3. No gate was relaxed.**

Against iteration 7's 3B rows:

| fixture | it-07 hybrid % | it-07 calls | it-07 s | it-08 hybrid % | it-08 calls | it-08 s |
|---|---:|---:|---:|---:|---:|---:|
| js | 10.86 (**raw**) | 3 | 434.5 | 10.65 (canonical) | 10 | **82.0** |
| py | 3.0 | 6 | 1086.8 | **4.6** | 10 | **150.4** |
| rs | 5.1 | 8 | 380.2 | **7.4** | 10 | **82.4** |
| **total GPU** | | 17 | **1901.5 s** | | 30 | **314.8 s** |

**30 candidates for a sixth of the GPU seconds that bought 17.** That is the
mechanical prediction of C2 — a per-symbol output is a few hundred tokens, a
whole-file rewrite is 7-8k — and it is the clearest result of the pass.

The js comparison is *not* apples to apples and should not be read as a 0.21
point regression: iteration 7's js row was scored on **raw physical lines**
against a 571-line baseline because prettier was missing, and this row is
canonical against a 601-line baseline. What is comparable is the LLM's own
contribution: 14 raw lines then, **16 canonical lines now**, at 1/5 the cost.

### The headline: acceptance rate, per-symbol vs whole-file

| | proposals | accepted outright | rate |
|---|---:|---:|---:|
| iteration 07, whole-file, 3B (js+py+rs) | 17 | **0** | **0 %** |
| iteration 07, whole-file, 7B (js+py+rs) | 11 | 0 recorded¹ | — |
| iteration 08, per-symbol, 3B (js+py+rs) | 30 | **3** | **10 %** |

¹ that 7B run predates `BenchRow.attempt_outcomes`, so its per-attempt tally
is gone; iteration 7's own reading of its logs was that no whole-file
candidate passed outright at either size, but the 7B row cannot be re-checked.

Iteration 7's finding was that *every* accepted line came from hunk-salvaging
a rejected whole-file rewrite. This pass produced the first outright LLM
acceptances in the project's history, and **every** accepted line here came
from an outright per-symbol acceptance — no salvage was needed, and the
optional whole-file sweep never even ran (the per-symbol loop spent the
10-call budget first on all three fixtures).

js — 2 of 10, and the file shrank 553 → 537:

```
  [L2] reporting.js:renderTable    attempt=1 tests-failed 553->511
  [L2] reporting.js:renderTable    attempt=2 tests-failed 553->518
  [L2] reporting.js:parseCsvLine   attempt=1 tests-failed 553->548
  [L2] reporting.js:parseCsvLine   attempt=2 tests-failed 553->542
  [L2] reporting.js:formatMoney    attempt=1 tests-failed 553->540
  [L2] reporting.js:formatMoney    attempt=2 tests-failed 553->546
  [L2] reporting.js:expenseReport  attempt=1 accepted     553->548
  [L2] reporting.js:parseCsv       attempt=1 accepted     548->537
  [L2] reporting.js:normalizeRow   attempt=1 tests-failed 537->520
  [L2] reporting.js:normalizeRow   attempt=2 tests-failed 537->516
```

Compare the same fixture, same model, iteration 7 — one whole-file gamble and
two API violations:

```
  [L2] reporting.js attempt=1 tests-failed   523->223
  [L2] reporting.js attempt=1 hunks-accepted 523->509
  [L2] reporting.js attempt=2 api-changed    509->480
  [L2] reporting.js attempt=3 api-changed    509->400
```

rs — **the first Rust acceptance the tool has ever produced**:

```
  [L2] lib.rs:slugify          attempt=1 syntax-error 217->200
  [L2] lib.rs:slugify          attempt=2 syntax-error 217->196
  [L2] lib.rs:render_template  attempt=1 not-smaller  217->221
  [L2] lib.rs:render_template  attempt=2 syntax-error 217->216
  [L2] lib.rs:wrap_words       attempt=1 not-smaller  217->218
  [L2] lib.rs:wrap_words       attempt=2 syntax-error 217->213
  [L2] lib.rs:csv_escape_field attempt=1 accepted     217->208
  [L2] lib.rs:csv_escape_rows  attempt=1 syntax-error 208->202
  ...
```

7 of 10 still die at the `cargo check` pre-gate — the 3B cannot reliably emit
compiling Rust even for one function — but a *function* is small enough that
it sometimes can, where a 217-line file never was. 5.1 % → 7.4 %.

py — **0 of 10, and this is the pass's clearest miss**:

```
  [L2] inventory.py:Inventory   attempt=1 tests-failed 477->458
  [L2] inventory.py:Inventory   attempt=2 tests-failed 477->448
  [L2] inventory.py:OrderLine   attempt=1 tests-failed 477->439
  [L2] inventory.py:OrderLine   attempt=2 tests-failed 477->452
  [L2] inventory.py:Product     attempt=1 tests-failed 477->464
  [L2] inventory.py:Product     attempt=2 api-changed  477->450
  [L2] inventory.py:apply_order attempt=1 not-smaller  477->477
  [L2] inventory.py:apply_order attempt=2 not-smaller  477->477
  [L2] inventory.py:parse_sku   attempt=1 tests-failed 477->474
  [L2] inventory.py:parse_sku   attempt=2 tests-failed 477->473
```

The reason is structural and is stated plainly in the gap diagnosis below:
**in Python the top-level symbols of this fixture are classes.** `Inventory`
is 186 lines. "One top-level symbol" bought js 29 units averaging 20 lines and
rust 18 units averaging 12, but bought python a 186-line class — i.e. the same
all-or-nothing gamble C2 was supposed to abolish, only slightly smaller.
py's hybrid number improved (3.0 → 4.6) entirely because of D1's ruff tier;
the LLM contributed **zero**.

### The API-feedback loop barely got exercised live

Iteration 7's js run died on `api-changed` twice and py once. This run
produced **one** `api-changed` across 30 proposals — per-symbol prompts
preserve the surface far better, because the model is not being asked to
reproduce 20 other signatures from memory. So gap item 2 is implemented and
unit-tested (`test_api_violation_feedback_lets_the_next_attempt_fix_it`
asserts a scripted backend repairs itself only after the violation list
reaches it) but is largely **untested against a live model**, because the
condition it treats mostly stopped occurring. Recorded as such.

## The 7B settlement run landed mid-pass, and it changes the verdict

Iteration 7 deferred one measurement: the js whole-file run re-scored with
prettier present. It completed and was committed as `3ac8d23` while this pass
was underway, on the code **before** any of these changes
(`bench/results/20260831T002030Z.jsonl`, `docs/evidence/reduce-js-7b.*`):

| fixture | config | LOC start | after static | final | static % | hybrid % | tests | api | hidden | calls | s |
|---|---|---:|---:|---:|---:|---:|---|---|---|---:|---:|
| js | hybrid:qwen2.5-coder:**7b** | 601 | 553 | **445** | 7.99 | **25.96** | ok | ok | ok | 2 | 677.1 |

```
  [L2] reporting.js attempt=1 tests-failed   553->296
  [L2] reporting.js attempt=1 hunks-accepted 553->455
  [L2] reporting.js attempt=2 api-changed    455->336
  [L2] reporting.js attempt=2 hunks-accepted 455->445
```

Iteration 7's "unresolved" js row is therefore resolved as a **PASS**:
25.96 % canonical, hidden suite green — and it is a whole-file result, with
both accepted chunks coming from C1 hunk salvage of rejected rewrites, exactly
the mechanism this pass argued was the symptom of wrong granularity.

That has to be said plainly: **whole-file + hunk salvage at 7B is still the
best js number by a wide margin, and per-symbol at 7B was never tested.** The
7B does not fit this 6 GB card and overheats the machine, so this pass was
restricted to the 3B by instruction. The claim this pass supports is
narrower than "per-symbol is better": it is **"at 3B, per-symbol converts a
0 % outright-acceptance rate into 10 % and costs 6× less GPU"**. Whether that
holds at 7B — where the model can actually carry a whole file — is open.

## Verdict against the 25 % bar (CRITERIA.md C3-C5)

| fixture | bar | static-only | best hybrid | model / strategy | metric | verdict |
|---|---|---:|---:|---|---|---|
| py (C3) | ≥ 25 % | 4.6 % | 4.6 % | 3B per-symbol | canonical | **MISS** by ~20 points |
| js (C4) | ≥ 25 % | 7.99 % | **25.96 %** | 7B whole-file + salvage (`3ac8d23`) | canonical | **PASS** |
| js (C4) | ≥ 25 % | 7.99 % | 10.65 % | 3B per-symbol (this pass) | canonical | MISS by ~14 points |
| rs (C5) | ≥ 25 % | 5.1 % | 7.4 % | 3B per-symbol | canonical | **MISS** by ~18 points |

C4 is met, on the canonical metric, with hidden tests green — by the 7B and by
the *old* strategy. C3 and C5 miss decisively.

Roadmap D1/D2's exit gate (static-only ≥ 15 % per fixture) is also **not met**
at 7.99 / 4.6 / 5.1.

What did move at 3B: every fixture's hybrid number is now ≥ its static number for
the first time (iteration 7 had rs stuck at exactly static and a void py row),
all three are canonical, and the cost per accepted line fell about 6×.

## Gap diagnosis

Ranked by what blocks the 25 % bar next:

1. **"Top-level symbol" is the wrong unit for Python.** `Inventory` is a
   186-line class and it is one unit. The decomposition must descend into
   class bodies and propose **one method at a time** — `_symbol_index` already
   has the ast for it, and `api_check.python_api` already models
   `Class.method` with its signature, so the gate is ready. This is the
   single highest-value change and it is CPU-testable with a scripted backend.
   js and rust do not have this problem (their units are 12-20 lines), which
   is exactly why they moved and py did not.
2. **A rejected per-symbol proposal is thrown away whole.** C1's hunk salvage
   only runs on the whole-file sweep. A rejected *method* rewrite inside a
   rejected *class* rewrite is the same salvage opportunity one level down.
   Cheap to add once (1) lands.
3. **The 3B cannot emit compiling Rust.** 7 of 10 rust proposals died at
   `cargo check`. The pre-gate makes it cost milliseconds, which is the system
   working, but it does not create yield. Rust needs either a bigger model or
   diff-mode proposals (roadmap C2's other half: search/replace blocks rather
   than whole re-emissions).
4. **`not-smaller` is a wasted call, twice on py and twice on rust.** The
   model returned something the same size or larger. The prompt should state
   the symbol's current line count as a hard target and the loop should not
   spend a second attempt on a symbol that already failed to shrink at
   temperature 0.1 without new feedback — the feedback for `not-smaller` is
   currently the bare string.
5. **Canonical LOC caps what a rule can win.** The `low_stock` walrus rewrite
   is a 5→1 line improvement that the 88-column formatter re-expands to 4.
   Rules that produce long single expressions are worth less than they look;
   rules that *delete* (dead code, unreachable, `ruff --fix`) are worth
   exactly what they look. Future rule work should be biased accordingly.
6. **Static is still under D1/D2's 15 % gate**, and the remaining headroom is
   not in the layers added here. What is untouched: vulture at 60-99 %
   confidence under the gate, dead *files* via the import graph, dependency
   pruning, and jscpd-driven dedup (D4). On this fixture the ruff unsafe tier
   was the only thing left that ruff can see.
7. **The budget is spent before the whole-file sweep runs.** All three
   fixtures exhausted 10 calls inside the per-symbol loop, so the cross-symbol
   dedup pass — the one thing per-symbol proposals structurally cannot do —
   never executed. Budget scheduling (roadmap C7) should reserve a call or
   two for it rather than letting the greedy loop take everything.
8. **Per-symbol at 7B is the obvious untested cell.** The two strategies have
   only ever been measured at different model sizes: whole-file at 7B
   (25.96 % on js) and per-symbol at 3B (10.65 %). The comparison that matters
   — same model, both strategies — has not been run, and cannot be on this
   6 GB card without the thermal problem that stopped iteration 7. Until it
   is, "per-symbol is the right default" is supported for small models and
   *assumed* for large ones. The honest way to settle it is a rented GPU or a
   frontier model through the OpenAI-compatible backend.
9. **`fixtures/rs/target/` is still tracked in git** (iteration 7's item 10,
   unchanged: it needs `git rm --cached -r`, still out of scope).
