# Iteration 6 — ROADMAP §3 days 1-9, CPU only (A2/A3, B1/B2/B3/B4, C1)

## Attempt
Executed the CPU-only half of the roadmap's "next ten working days" against
commit `2dee2c1`: hidden tests per fixture (A3), `lc bench` (A2),
canonical-format LOC (B1), pre-gates (B3), test-result cache (B4), hunk
decomposition + delta debugging (C1) and the finer API surface (B2, stdlib
extractors instead of tree-sitter). No GPU / no ollama call was made: the
daemon answered on 127.0.0.1:11434, but live-LLM runs were out of scope for
this pass, so every LLM path is exercised with scripted `FakeBackend`s.

## What landed
- `fixtures/{py,js,rs}/tests_hidden/` — 32 / 16 / 12 extra tests that pass on
  the current fixtures and are invisible to the reducer: `tests_hidden` is in
  `langdetect.SKIP_DIRS` (so it never reaches the prompt spec or a LOC count),
  the py gate passes `--ignore`, the js file is named `hidden.mjs` so plain
  `node --test` does not discover it, and the rs target is declared
  `[[test]] name="hidden" test=false` so `cargo test` skips it.
  `testrunners.run_hidden_tests(root, lang)` runs them; bench is the only
  caller.
- `less_code/bench.py` + `lc bench` — copies each fixture to a scratch tree
  (the repo is never mutated), reduces it, runs the hidden suite, appends one
  JSON row per (fixture, config, commit) to `bench/results/<ts>.jsonl` and
  prints a markdown table. Works with `--static-only`.
- `less_code/loc.py` — `count_source(..., format_first=True)` / `measure()`
  pipe the source through `ruff format` / `prettier` / `rustfmt` at width 88
  before counting, with a raw-count fallback recorded as `Loc.formatted`;
  `Loc` also carries `tokens` and `ast_nodes`. Every reduction measurement
  (pipeline, static, llm_reduce, grpo/rewards) now uses `measure`.
  `grpo/rewards._looks_minified` is only consulted when no formatter exists
  for the language.
- `verify_candidate` — ordered not-smaller → parse (`ast.parse` /
  `node --check` / `cargo check --tests`) → API → tests, each rejection
  recorded by stage name. The size check costs no disk write at all.
- `testrunners` — process-lifetime cache keyed by sha256 over every mapped
  source+test file, `use_cache=False` bypass, `STATS` counters.
- `less_code/hunks.py` — `decompose(original, candidate, lang)` splits a
  whole-file rewrite at top-level symbol boundaries (python: ast incl.
  decorators; js/rust: the brace matcher from `grpo/build_dataset.py`, plus
  `impl`/`struct` blocks), `apply()` splices any subset back, and `search()`
  does a ddmin-style divide-and-conquer for the largest passing subset
  followed by an individual retry of every rejected hunk. `reduce_file` calls
  it whenever a whole-file candidate is rejected by tests or the API gate;
  per-hunk outcomes land in `AttemptRecord.detail`.
- `less_code/api_check.py` — python: `Class.method` keys with full parameter
  lists *and* defaults, decorators, base classes; js: `export {a, b as c}`,
  `export default`, `module.exports = {…}` / `.x` / `= name`, `exports.x`;
  rust: `Type::method` with its parameter list from `impl` blocks and
  `Type.field` for `pub` fields. No tree-sitter dependency was added.
- Two static-layer defects the bench surfaced, fixed in passing: the JS dead
  export remover used a `[^}]*` body regex that truncated at the first nested
  block and left an unparsable file (all its edits were reverted by the
  gate — js static yield was silently 0); it now deletes the brace-matched
  span. `cargo fix/clippy` now pass `--allow-no-vcs` so they work in the
  bench's scratch copy.

## Evidence
- `uv run pytest -q` → **71 passed** (was 30). New: `tests/test_hunks.py`
  (12) and `tests/test_bench_and_metrics.py` (21), plus 8 API-surface tests.
- Hidden suites green on the untouched fixtures:
  `pytest fixtures/py/tests_hidden` → 32 passed;
  `node --test tests_hidden/hidden.mjs` → pass 16 fail 0;
  `cargo test --test hidden` → 12 passed.
  Frozen gates unchanged by their presence: py 97 passed, js 36 pass,
  rs 39 passed.
- C1 headline test (`test_two_good_hunks_are_accepted_and_the_bad_one_is_not`):
  a `FakeBackend` returns one rewrite that is correct for `sign` and `clamp`
  and wrong for `tally`. Pipeline log:
  ```
  [L2] mod.py attempt=1 tests-failed 22->6
  [L2] mod.py attempt=1 hunks-accepted 22->11
  detail: hunks 2/3 accepted=[sym:sign,sym:clamp]
          rejected=[sym:tally=tests-failed] verifications=6
  ```
  Before C1 that attempt contributed 0 lines; it now contributes 11 of the 16
  the (partly wrong) candidate claimed.
- B1: `count_source("def f(x):\n    y = x + 1; return y\n", "python").code == 2`
  but `measure(...).code == 3` — line joining no longer buys a smaller number.
  `lc analyze fixtures/py` now reports
  `code 500, tokens 4995, ast_nodes 3708, formatted true`.
- B3/B4 are asserted by tests, not by inspection: `not-smaller`,
  `syntax-error` and `api-changed` candidates all reach the runner zero
  times; a second `run_tests` on an unchanged tree returns `cached=True`.

## Bench (`uv run lc bench --static-only`, commit 2dee2c1, exit 0)

| fixture | lang | config | LOC start | after static | final | static % | hybrid % | tests | api | hidden | calls | s |
|---|---|---|---:|---:|---:|---:|---:|---|---|---|---:|---:|
| js | javascript | static-only | 571 | 523 | 523 | 8.41 | 8.41 | ok | ok | ok | 0 | 0.48 |
| py | python | static-only | 500 | 500 | 500 | 0.0 | 0.0 | ok | ok | ok | 0 | 1.15 |
| rs | rust | static-only | 392 | 392 | 392 | 0.0 | 0.0 | ok | ok | ok | 0 | 2.22 |

Row file: `bench/results/20260830T191917Z.jsonl`. Hidden-green rate 3/3.

Reading the table honestly:
- **py static is 0%, not the 9% quoted in the roadmap.** The committed
  `fixtures/py/inventory.py` is 599 lines and no longer contains the dead
  legacy section `FIXTURE.md` documents (it was removed in `8177df1`), so
  there is nothing left for the dead-code pass to take. The 500 vs 525 LOC
  start is the canonical-format count of the same file, not a reduction.
- **js static is 8.41%, up from an effective 0%**: the pass always found the
  three dead exports but its edits were reverted every time because the
  regex left the file unparsable (see above).
- **rs static is 0%**: `cargo fix`/`clippy --fix` have nothing to fix; the
  fixture's dead code is `pub`, which is roadmap D3 (`unreachable_pub` +
  `cargo-machete`), not something `cargo fix` removes.

## Verdict
- A2 `lc bench`: PASS (table + JSONL rows + hidden-test column).
- A3 hidden tests: PASS for all three fixtures.
- B1/B3/B4: PASS, each with tests.
- B2: PASS as a stdlib extractor pair per language (the tree-sitter variant
  the roadmap sketches was not needed and would have added a dependency).
- C1: PASS on the scripted 2-of-3 case; its effect on the real fixtures is
  unmeasured until a GPU/ollama pass runs `lc bench` without `--static-only`.

## Gap diagnosis
- The whole "before" picture for A1 is still static-only: no hybrid row
  exists, so the roadmap's "py 12.8% → 25-30%" bet is untested. That is one
  `uv run lc bench --model qwen2.5-coder:7b --max-llm-calls 6` away.
- `fixtures/py/FIXTURE.md` line references and the "525 LOC / dead legacy
  section" claims are stale relative to the committed file.
- Static yield on py/rs is 0%, so roadmap D1/D3 (ruff/vulture tiers,
  `unreachable_pub`) are now the obvious next CPU work, ahead of the rest of
  phase B.
