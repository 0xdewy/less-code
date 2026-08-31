# Iteration 7 — rust dead-`pub` removal (D3), the python rule library (C4), and the first hybrid bench

## Attempt

Iteration 6 left a static-only "before" picture (js 8.41%, py 0%, rs 0%) and an
explicit gap: *no hybrid row exists*. This pass does the two CPU-side items the
gap diagnosis named — roadmap **D3** (rust dead `pub` items) and roadmap **C4**
(the deterministic rule library) — corrects the stale fixture doc, and then
spends the available GPU on the first real hybrid measurement.

Everything here is measured against the working tree at commit `2dee2c1` plus
the uncommitted iteration-6 work. Nothing was committed.

## What landed

### 1. Rust dead `pub` item removal (`less_code/static.py`)

`_rust_remove_dead_pub` scans every source file for **column-0 `pub` items**
(`fn`, `struct`, `enum`, `trait`, `const`, `static`, `type`) and deletes the
ones no file in the project mentions — `src/` **and** `tests/`, comments
included. Three details matter:

- **Span extraction is brace-matched, not regex.** `_rust_item_end` walks the
  text tracking `(`/`[` depth so it can tell `pub fn fixed(n: u8) -> [u8; 2] {`
  (a body) from `pub struct S;` (a declaration) — a naive `find(";")` takes the
  semicolon inside the return type for the end of the item.
- **Attached doc comments and attributes come with it.** `_rust_attached_start`
  walks backwards line by line over `///`, `//!`, `//` and `#[…]` lines, so the
  three-line "Deprecated: …" block above `reverse_words` is removed with it and
  the previous item's doc block is *not* swallowed.
- **Conservative by construction.** One `\bname\b` hit anywhere outside the
  item's own span keeps the item, so `pub struct Orphan` with an `impl Orphan`
  block survives (the `impl` mentions it). Whatever survives that scan still
  faces the pipeline's frozen-suite gate, which reverts the whole static edit
  set on red.

It runs *after* `cargo fix`/`clippy --fix` and returns `changed_files` rather
than mutating the tree, so — unlike the pre-existing `cargo fix` step — its
edits are inside the pipeline's revert set. Gate semantics are unchanged.

### 2. The python rule library (`less_code/rules.py`, new)

Five deterministic AST rewrites, applied file-wide to fixpoint:

| rule | before | after |
|---|---|---|
| `bool-return` | `if c: return True` / `return False` (and the `else:` form) | `return c` when `c` is provably a bool, else `return bool(c)`; the inverted pair becomes `return not c` |
| `append-loop-to-comprehension` | `x = []` + `for t in it: x.append(e)` | `x = [e for t in it]`, or `x = list(it)` when `e` is the target itself |
| `accumulate-to-sum` | `t = 0` + `for v in xs: t += f(v)` (and the legacy `t = t + f(v)`) | `t = sum(f(v) for v in xs)` |
| `sort-to-sorted` | a provably-fresh list + `x.sort(...)` | `x = sorted(<the list expr>, ...)` |
| `drop-bare-reraise` | `try: B except X: raise` | `B` |

Two design decisions are load-bearing:

- **Splice, don't unparse the module.** Each rewrite is a *line-range
  replacement* carrying its own `col_offset`, so comments, docstrings and
  formatting outside the rewritten statements survive verbatim. Running
  `ast.unparse` over the whole module — which the existing python dead-code
  pass does — would silently delete every comment in the file. A rewrite whose
  first or last line is shared with another statement (`x = 1; total = 0`) is
  refused rather than clobbered.
- **The gate stays the truth.** `static_pass` now takes an optional
  `runner(root, lang)`. The pipeline passes one. All rules go on at once; if the
  frozen suite goes red, the pass narrows — dead-code-only first, then each
  rule on its own, keeping what stays green and *recording every revert in the
  notes*. Without a runner `static_pass` is pure and the caller's own gate makes
  the all-or-nothing call, so the old contract still holds.

The guards are where the safety lives, and they are what stop the fixture's
remaining loops from being taken:

- A `for` target leaks into the enclosing **function** scope in Python, so a
  comprehension rewrite only fires when every mention of that name in the scope
  falls inside the loop's own line span.
- Multi-statement loop bodies are collapsed only when each temp is bound once
  and read **exactly once** downstream — inlining a twice-read temp would
  evaluate `self.products[sku]` twice. That is precisely why `total_value`,
  `valuate` and `low_stock` in the py fixture are *not* rewritten. They are left
  for the LLM, which is the correct division of labour.
- `total = 0.0` keeps its start value (`sum(gen, 0.0)`), so an empty iterable
  still returns a float rather than an int.
- `x = other` + `x.sort()` is refused: `other` may be someone else's list.

### 3. Docs and hygiene

- `fixtures/py/FIXTURE.md` gains a **"Status 2026-08-30"** section rather than a
  rewrite: the dead legacy bullets and the 525-LOC figure describe the fixture
  as built, the file is 500 code-LOC today, and the bullets the rule library
  now takes are marked done. The rest of the opportunity list is still live.
- `.gitignore` created with `fixtures/rs/target/` (plus `target/`,
  `__pycache__/`, `.pytest_cache/` and the ad-hoc report filenames).
  **Honest caveat:** `fixtures/rs/target/` is already *tracked* in git — 40-odd
  binary object files show up in `git status` after any `cargo test` — and
  `.gitignore` does not untrack it. Untracking needs `git rm --cached`, which
  was explicitly out of scope for this pass, so the noise remains.

## Evidence

### Tests

```
$ uv run pytest -q
109 passed in 14.46s        # was 71
```

New files: `tests/test_rules.py` (30) and `tests/test_static_rust.py` (8).

`tests/test_static_rust.py` builds a real synthetic crate under `tmp_path`
(lib + integration test + `Cargo.toml`) and asserts, among other things, that
`cargo test --quiet` is green **before and after** the removal:

```
$ uv run pytest tests/test_static_rust.py -q
8 passed in 0.54s
```

The interesting negatives are the ones that must *not* fire:
`test_reference_from_a_test_file_keeps_the_item`,
`test_append_loop_not_rewritten_when_temp_used_twice`,
`test_append_loop_not_rewritten_when_loop_var_leaks`,
`test_sort_not_rewritten_on_an_aliased_list`,
`test_try_with_finally_is_kept`,
`test_semicolon_packed_lines_are_not_clobbered`.

`test_pipeline_gate_reverts_a_misfiring_rule` monkeypatches `bool-return` into
a rule that always emits `return False`, runs the **real** pipeline, and
asserts the narrowing path keeps `accumulate-to-sum`, restores the sabotaged
`if`, and writes `rule bool-return reverted by the gate` into the notes.

### The gate caught a real bug in my own code

First run of the rust pass on the fixture:

```
notes: ['lib.rs: removed dead pub item is_blank', ..., 'static pass broke tests; reverted all static edits']
```

`_rust_attached_start` had its "does this prefix end in a newline" branch
inverted, so it deleted a *character offset* into the middle of each doc
comment and produced:

```
error: expected item after doc comment
   --> src/lib.rs:282:1
    |
282 | /// Reverse the word order of/// Prefix every line of `input` wit/// True when `input` is empty
```

The pipeline reverted the whole edit set and reported 0% rather than shipping
an unparsable crate. That is the gate doing its job on the tool's own author;
it is recorded here rather than quietly fixed. The fix is the rewritten
`_rust_attached_start`, plus `test_attached_start_lands_on_a_line_boundary`
which asserts every span start is at column 0.

### Static-only bench, before and after

Same command both times: `uv run lc bench --static-only`.

**Before** (`bench/results/20260830T213208Z.jsonl`)

| fixture | lang | config | LOC start | after static | final | static % | hybrid % | tests | api | hidden | calls | s |
|---|---|---|---:|---:|---:|---:|---:|---|---|---|---:|---:|
| js | javascript | static-only | 571 | 523 | 523 | 8.41 | 8.41 | ok | ok | ok | 0 | 0.33 |
| py | python | static-only | 500 | 500 | 500 | 0.0 | 0.0 | ok | ok | ok | 0 | 0.86 |
| rs | rust | static-only | 392 | 392 | 392 | 0.0 | 0.0 | ok | ok | ok | 0 | 2.13 |

**After** (`bench/results/20260830T213851Z.jsonl`; re-run at the end of the
pass as `bench/results/20260830T234159Z.jsonl`, identical numbers)

| fixture | lang | config | LOC start | after static | final | static % | hybrid % | tests | api | hidden | calls | s |
|---|---|---|---:|---:|---:|---:|---:|---|---|---|---:|---:|
| js | javascript | static-only | 571 | 523 | 523 | 8.41 | 8.41 | ok | ok | ok | 0 | 0.32 |
| py | python | static-only | 500 | 487 | 487 | **2.6** | 2.6 | ok | ok | ok | 0 | 1.08 |
| rs | rust | static-only | 392 | 372 | 372 | **5.1** | 5.1 | ok | ok | ok | 0 | 1.74 |

Hidden-green rate 3/3 in both. py +13 LOC (5 rule firings: `bool-return`,
`append-loop-to-comprehension`, `sort-to-sorted`, `accumulate-to-sum` ×2,
`drop-bare-reraise`), rs +20 LOC (the three dead `pub fn` with their doc
comments). js is unchanged — nothing in this pass touches the JS layer.

Roadmap D3's exit gate is "static-only yield ≥ 15% per fixture". At 8.4 / 2.6 /
5.1 that is **not met**; D1 (ruff/vulture tiers, unused locals, unreachable
branches) and D2 (knip) are still untouched and are where the remaining static
yield lives.

## Hybrid bench

### Environment and the model switch

`ollama serve` was reachable on 127.0.0.1:11434 throughout. **`qwen2.5-coder`
was not installed** — `ollama list` showed only generic `qwen2.5` /
`llama3.2` / `smollm2` — so `ollama pull qwen2.5-coder:7b` ran first (186 GB
free, 4.7 GB model, success).

The GPU is the binding constraint:

```
$ nvidia-smi --query-gpu=name,memory.total --format=csv
NVIDIA GeForce RTX 2060 with Max-Q Design, 6144 MiB
```

`qwen2.5-coder:7b` at Q4 does **not** fit. `ollama ps` shows the spill, and it
is what every 7B number below is really measuring:

```
qwen2.5-coder:7b    6.5 GB    20%/80% CPU/GPU    16384
qwen2.5-coder:7b    6.0 GB    13%/87% CPU/GPU    12288
qwen2.5-coder:3b    3.1 GB    100% GPU           16384      <- fits
```

A throughput probe of the 7B at its most favourable split (`num_ctx=8192`,
8% on CPU) gave **25.5 tok/s** (`eval 150 tok in 5.9 s`). A whole-file rewrite
of `inventory.py` is 7-8k output tokens, i.e. **10-20 minutes per candidate**.
Partway through this pass the machine was reported to be overheating on the
7B, and the run switched to **`qwen2.5-coder:3b`**, which fits entirely in
VRAM. Which model produced which row is stated in every table below.

### The defect that ate the first hour: a 600 s backend timeout

The first completed 7B run returned this and nothing else:

```
  [L2] inventory.py attempt=1 backend-error 487->487   (x5)
  hybrid_pct 2.6, llm_calls 2, seconds 1201.7
```

`backend-error`, not `tests-failed` — the candidates never arrived.
`less_code/backends.py` hard-coded `urlopen(req, timeout=600)` in *both*
backends. On hardware where a correct rewrite takes longer than 600 s the
socket times out, the call is still charged against the budget, and the L2
layer can never contribute however good the model's answer would have been.
1201.7 s of GPU bought zero candidates.

Fixed here: `backends.DEFAULT_TIMEOUT`, a `timeout` parameter on both backends
and `make_backend`, and a `--llm-timeout` flag on `lc reduce` and `lc bench`
(covered by `test_backend_timeout_is_configurable`). Re-running py with
`--llm-timeout 2400` immediately produced a real candidate:

```
  [L2] inventory.py attempt=1 tests-failed     487->476
  [L2] inventory.py attempt=1 hunks-accepted   487->483
  hybrid_pct 3.4, llm_calls 1, seconds 637.5, tests/api/hidden all ok
```

That line is **C1 earning its keep on a live model for the first time**: the
whole-file rewrite failed the suite outright and would have contributed 0
before hunk decomposition; the salvaged subset contributed 4 LOC.

### `lc bench` could not be used as-is

**Correction (written after the fact).** This section originally claimed both
`lc bench` runs were killed and "yielded nothing at all". That was wrong, and
the error was mine: I issued `kill` / `pkill -f "lc bench --model"`, assumed
they had landed, and **never re-checked `bench/results/`**. The first run —
`--attempts 2 --max-llm-calls 8 --num-ctx 16384`, 7B — in fact survived both
kill attempts and ran to completion, writing
**`bench/results/20260830T224651Z.jsonl`** at 22:46:51Z with all three
fixtures. It is analysed below. The second run (`--max-llm-calls 3`) was
genuinely killed and wrote nothing.

That the kills missed is now established rather than assumed: a controlled
experiment (SIGTERM into a live `reduce_project`) shows the crash-safety
handler at `pipeline.py:95-108` restores the pre-static tree **and** the
process exits **130**, writing no rows. Since the file exists with three rows,
that bench never received the signal.

`lc bench` still writes its JSONL and its table only after all three fixtures
finish, which is why an interrupted run yields nothing and why the per-fixture
runs below drive `reduce_project` one fixture per process, on the same
scratch-copy + hidden-suite protocol (`scratchpad/run_hybrid.py`) — identical
pipeline code path, identical row shape. The missing per-fixture checkpoint is
a real bench-design gap, recorded in the diagnosis.

### The recovered 7B bench run (`bench/results/20260830T224651Z.jsonl`)

| fixture | config | LOC start | after static | final | static % | hybrid % | tests | api | hidden | calls | s | formatted LOC |
|---|---|---:|---:|---:|---:|---:|---|---|---|---:|---:|---|
| js | hybrid:qwen2.5-coder:7b | 571 | 523 | **426** | 8.41 | **25.39** | ok | ok | ok | 2 | 637.6 | **false** |
| py | hybrid:qwen2.5-coder:7b | 500 | 487 | **500** | 2.6 | **0.0** | ok | ok | ok | 6 | 2285.6 | true |
| rs | hybrid:qwen2.5-coder:7b | 392 | 372 | 372 | 5.1 | 5.1 | ok | ok | ok | 3 | 936.5 | true |

**The js row is the best hybrid result obtained in this pass and it nominally
clears the 25% bar.** It was produced by the identical pipeline and the
identical gate path as every other row — `lc bench` itself, on a scratch copy,
with `tests_ok`, `api_ok` and the independent `hidden_ok` all true. The fixture
timeline puts js at 21:39:00-21:49:37Z, finished and its scratch tree deleted
**before** either kill attempt, and `snapshots`/`_restore` are per-fixture, so
nothing that happened later could have touched it.

It nonetheless **cannot be scored against CRITERIA.md C4 as it stands**, for
one specific reason: `formatted_loc: false`. **`prettier` was not installed on
this machine**, so every js number in this document — the 8.41% static baseline
included — is a count of *raw physical lines*, which is exactly the metric
roadmap B1 exists to abolish because line-joining games it. The static pass
cannot game it (it only deletes whole functions), but an LLM whole-file rewrite
is precisely the thing that can.

The size of that gap is now measurable, because `prettier` was installed while
answering this question:

```
reporting.js, unchanged:   raw code-LOC 571   canonical (prettier @88) code-LOC 601
```

A 30-line, 5% discrepancy on the *untouched* file, all of it long lines that
prettier splits. Re-running the static-only bench with prettier present moves
the js baseline accordingly:

| fixture | LOC start | after static | static % | formatted |
|---|---:|---:|---:|---|
| js | 601 | 553 | 7.99 | true |
| py | 500 | 487 | 2.6 | true |
| rs | 392 | 372 | 5.1 | true |

(`bench/results/20260830T234900Z.jsonl` — all three fixtures now canonical.)

So the honest statement is: **the 7B produced a js reduction that preserved
behaviour under the frozen suite, the API gate and the hidden suite, and that
removed 97 raw lines (18.5% on top of static). Whether it is ≥ 25% under the
canonical metric the criteria actually require is unknown**, because the
reduced tree was deleted by `bench_fixture`'s `finally` and cannot be
re-measured. It could plausibly land anywhere from well under to slightly over
25% depending on how many long lines the model produced.

**The re-run that would settle it**, now that prettier is present, is exactly
the original command:

```
uv run lc bench --model qwen2.5-coder:7b --attempts 2 --max-llm-calls 8 --num-ctx 16384
```

with the js scratch tree preserved for inspection. It costs ~11 minutes of 7B
GPU. It was **not** run here because the 7B does not fit this 6 GB card (20%
CPU spill) and the machine was reported to be overheating on it, which is why
the pass switched to the 3B. That is a deliberate deferral, not a result.

### The py row is an unexplained anomaly, not a measurement

`loc_final 500 == loc_start 500` while `loc_after_static 487`: the tree ended
the run back at its pre-static content, so `hybrid_pct 0.0` is *below* its own
static baseline, which is not a state the pipeline should be able to reach.
**I could not determine the cause**, and I would rather say so than invent one:

- The obvious hypothesis — my `kill`/`pkill` firing `_restore()` mid-py — is
  **experimentally ruled out**: SIGTERM into a live `reduce_project` restores
  the tree *and* exits 130, which would have prevented the file from being
  written at all.
- Every L2 write path was re-read (`verify_candidate` restores in a `finally`,
  `reduce_file` only writes on acceptance, `hunks.search` verifies through the
  same gate). None of them holds the *pre-static* text, which is the only place
  a 500-LOC restore could come from.
- It did **not** reproduce: three later py runs through the identical pipeline
  (7B ×1, 3B ×2) all ended at 483-485, below the 487 static baseline.
- It is not diagnosable after the fact, because `bench_fixture` deletes the
  scratch tree and `BenchRow` dropped the per-attempt records.

That last point was a real gap and is fixed here: `BenchRow.attempt_outcomes`
now persists the outcome tally (`accepted` / `tests-failed` / `api-changed` /
`syntax-error` / `backend-error` / `hunks-*`) into every row, so a future row
like this one can be read rather than guessed at
(`test_bench_row_records_attempt_outcomes`). Until it recurs with that tally
attached, the py row should be treated as **void**, not as a 0% result.

### Hybrid results — `qwen2.5-coder:3b`, `num_ctx=16384`, `--llm-timeout 1200`

| fixture | lang | config | LOC start | after static | final | static % | hybrid % | tests | api | hidden | calls | s |
|---|---|---|---:|---:|---:|---:|---:|---|---|---|---:|---:|
| js | javascript | hybrid:qwen2.5-coder:3b (≤6 calls, attempts 3) | 571 | 523 | **509** | 8.41 | **10.86** | ok | ok | ok | 3 | 434.5 |
| py | python | hybrid:qwen2.5-coder:3b (≤6 calls, attempts 3) | 500 | 487 | **485** | 2.6 | **3.0** | ok | ok | ok | 6 | 1086.8 |
| rs | rust | hybrid:qwen2.5-coder:3b (≤8 calls, attempts 4) | 392 | 372 | 372 | 5.1 | **5.1** | ok | ok | ok | 8 | 380.2 |

One 7B row, kept because it is the best per-call result observed:

| fixture | lang | config | LOC start | after static | final | static % | hybrid % | tests | api | hidden | calls | s |
|---|---|---|---:|---:|---:|---:|---:|---|---|---|---:|---:|
| py | python | hybrid:qwen2.5-coder:7b (1 call, ctx 12288, llm_timeout 2400) | 500 | 487 | **483** | 2.6 | **3.4** | ok | ok | ok | 1 | 637.5 |

**Hidden-test green rate 4/4. API preserved 4/4. No gate was relaxed.**

### What the attempt logs say

js — the one clear win, and it is entirely C1's:

```
  [L2] reporting.js attempt=1 tests-failed   523->223
  [L2] reporting.js attempt=1 hunks-accepted 523->509
  [L2] reporting.js attempt=2 api-changed    509->480
  [L2] reporting.js attempt=3 api-changed    509->400
```

The 3B claimed a 523→223 rewrite (57%!) that failed the suite; decomposition
kept the 14 LOC that were actually correct. Its next two attempts changed the
public API and were rejected by the API gate before any test ran.

py — same shape, less salvage:

```
  [L2] inventory.py attempt=1 tests-failed   487->339
  [L2] inventory.py attempt=1 hunks-accepted 487->485
  [L2] inventory.py attempt=2 api-changed    485->285
  [L2] inventory.py attempt=1 tests-failed   485->479   (x3, hunks-none)
```

rs — the 3B cannot write compiling Rust at this size. 7 of 8 attempts died at
the `cargo check` pre-gate:

```
  [L2] lib.rs   attempt=1 syntax-error 217->165
  [L2] lib.rs   attempt=3 syntax-error 217->138
  [L2] stats.rs attempt=1 syntax-error 155->98
  [L2] stats.rs attempt=4 syntax-error 155->71
```

That the whole rust run cost only 380 s for 8 rejected candidates is B3
working exactly as designed: `cargo check` rejects in milliseconds and no test
suite was ever run on a broken tree.

## Verdict against the 25% bar (CRITERIA.md C3-C5)

| fixture | bar | static-only (canonical) | best hybrid | metric | verdict |
|---|---|---:|---:|---|---|
| py (C3) | ≥ 25% | 2.6% | 3.4% (7b, 1 call) | canonical | **MISS** by ~22 points |
| js (C4) | ≥ 25% | 7.99% | **25.39%** (7b, 2 calls) | **raw lines — prettier absent** | **UNRESOLVED** — nominally clears the bar, but not on the metric the criteria require |
| js (C4) | ≥ 25% | 7.99% | 10.86% (3b, 3 calls) | raw lines | **MISS** by ~14 points |
| rs (C5) | ≥ 25% | 5.1% | 5.1% | canonical | **MISS** by ~20 points |

Revised from the original "all three miss". **js is not a clean miss and it is
not a clean pass — it is unresolved**, and the honest reason is a measurement
defect on my side, not the model's: prettier was missing, so the js run was
scored on raw physical lines. One ~11-minute 7B re-run with prettier present
settles it, and is deferred only for the thermal reason stated above.

py and rs miss decisively and on the correct canonical metric.

Tests, API surface and the hidden suites are green on every row that ran — the
safety axis is intact and no number here was obtained by weakening a gate. The
reductions the gates rejected (my own rust doc-comment bug; every
over-aggressive model candidate) are written up as rejections. The py bench row
is recorded as void rather than as a 0% result, because I cannot explain it.

Roadmap D3's own exit gate — static-only ≥ 15% per fixture — is **not** met at
7.99 / 2.6 / 5.1, though py and rs both moved off zero for the first time.

## Gap diagnosis

Ranked by what actually blocks the 25% bar:

1. **The metric was not canonical for JavaScript, and that invalidated the one
   promising result.** `prettier` was absent, so `formatter_available("javascript")`
   was false and every js number was raw physical lines — the exact quantity
   B1 exists to stop mattering. The unchanged fixture measures 571 raw vs 601
   canonical, a 5% gap, all of it long lines. `prettier` is now installed and
   all three fixtures measure canonically; **the environment should assert this
   rather than silently degrade.** `lc bench` should refuse to record a row, or
   mark it clearly unscoreable, when the language's formatter is missing —
   right now `formatted_loc: false` is a quiet field nobody reads. That is the
   single highest-value fix in this list.
2. **The proposal granularity is wrong.** Almost every accepted LOC in this
   pass came from C1 salvaging a *rejected* whole-file rewrite (js +14, py
   +2/+4). The one exception is the recovered 7B js row, and even that took 2
   calls and 637 s. A 500-LOC rewrite is a 10-minute all-or-nothing gamble;
   the same GPU minutes spent on per-symbol diffs would be ~20 independently
   gated bets. `reduce_file_chunked` exists but only triggers above 1000 LOC —
   lowering that threshold and making decomposed proposals the *default* shape
   (roadmap C2) is the highest-value LLM-side change.
3. **The API gate is where the model keeps dying, and it is never told why.**
   js attempts 2 and 3 and py attempt 2 were all `api-changed`. The violation
   list is computed by `api_violations` but is not fed back into the next
   prompt the way a test failure is. Small fix, obvious payoff.
4. **Bench rows were not diagnosable.** The py anomaly is permanently
   unexplainable because the scratch tree is deleted and the attempt records
   were dropped. `BenchRow.attempt_outcomes` now persists the outcome tally;
   `lc bench` should also gain a per-fixture checkpoint (rows appended as they
   land) and a `--fixture` selector, and should optionally keep the scratch
   tree behind a `--keep` flag so a surprising row can be inspected.
5. **My own process error.** I killed two bench runs, assumed the kills landed,
   asserted in this document that they "yielded nothing at all", and never ran
   `ls bench/results/`. A completed run with the best result of the pass sat on
   disk unreported for the rest of the session. The tool wrote its evidence
   correctly; I failed to read it. Checking the output directory is one command
   and belongs in the runbook after any interrupted run.
6. **Model capability is the rust ceiling.** 7 of 8 3B candidates were
   `syntax-error` — it cannot emit compiling Rust at 200+ LOC. The 7B managed 3
   calls without a single acceptance either. Rust needs smaller units (see 2).
   The `cargo check` pre-gate made this cheap to discover (380 s for 8
   rejections), which is the system working; it does not create yield.
7. **Static yield is still short of D3's 15%.** Untouched: D1 for python
   (`ruff --fix` unsafe tier under the gate, unused locals/params, unreachable
   branches) and D2 for JS (knip, ESLint `--fix`). Both CPU-only.
8. **The rule library's guards cost it yield, deliberately.** `total_value`,
   `valuate` and `low_stock` are declined because a loop body reads a temp
   twice or wraps the append in an `if`. A comprehension `if`-guard clause and
   walrus-binding a twice-read temp would take all three without weakening
   anything. Both are test-gated rule work.
9. **Hardware.** A 6 GB card is below the practical floor: the model that can
   do the job (7B) spills 20% to CPU and overheats the machine, and the model
   that fits (3B) breaks the API and the Rust parser. `--llm-timeout` makes
   slow hardware workable rather than silently zero-yield; it does not make it
   fast.
10. **`fixtures/rs/target/` is tracked in git.** `.gitignore` now lists it, but
    untracking needs `git rm --cached -r fixtures/rs/target`, out of scope here.
