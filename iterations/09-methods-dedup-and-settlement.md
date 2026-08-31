# Iteration 9 — per-method decomposition, duplicate-group proposals, two ladder rules, and the py/rs 7B settlement

## Attempt

Iteration 8's gap diagnosis ranked two blockers above everything else:

> 1. **"Top-level symbol" is the wrong unit for Python.** `Inventory` is a
>    186-line class and it is one unit. […] This is the single highest-value
>    change.
> 7. **The budget is spent before the whole-file sweep runs.** […] the
>    cross-symbol dedup pass — the one thing per-symbol proposals structurally
>    cannot do — never executed.

And both `FIXTURE.md` files name the same thing as their largest embedded
opportunity, in language the tool could not act on at any granularity it had:
*copy-pasted near-duplicate blocks*. py's SKU shape validation pasted six
times across two classes; rust's `csv_escape_row` / `_owned` / `_rows`,
`count_leading_spaces` / `count_trailing_spaces`, `word_frequencies` /
`char_frequencies`; js's `buildMonthly/Quarterly/RegionSummary`.

A single-symbol proposal cannot *see* the twin. A whole-file proposal can see
it but is one all-or-nothing bet. So this pass adds a third granularity
between them, decomposes python classes into methods, takes two more
deterministic rules, deepens the rust static layer, and then settles C3/C5
against the 25 % bar with the authorized bounded 7B runs.

The three code changes all work and are unit-tested. The measurement is a
**miss**: C3 lands at **7.4 %** and C5 at **5.1 %** against a 25 % bar, and
the entire gain over iteration 8 is deterministic — the LLM layer contributed
**2 code lines across four benched runs**. That is written up below without
softening.

## What landed

### 1. Per-method decomposition for Python (gap item 1)

`llm_reduce.proposal_units()` replaces `_symbol_index()` in the per-symbol
loop. For js and rust it is the same list as before (their units already
average 12-20 lines). For python, a class over `CLASS_METHOD_THRESHOLD = 40`
code lines with at least two methods becomes one unit **per method**, keyed
`Class.method`, spliced over its own `ast` line span through the *same*
`verify_candidate` gate.

Four details are load-bearing:

- **The prompt carries the class, not the file.** `class_context()` emits the
  class line, its class-level attributes, `__init__`'s **full body** (so the
  model knows which attributes exist) and every *other* method as a
  signature-only stub with its line count. Bodies are what this pass exists
  not to pay tokens for.
- **Replies are re-indented.** A model asked for `Inventory.receive` answers
  with `def receive(...)` at column 0 about half the time and at column 4 the
  rest; `_reindent()` makes the splice work either way.
- **A class-shaped reply is refused before any test runs.** Handed one method,
  a model often answers with the whole class; splicing that inside the class
  body nests a class where a method used to be. `_method_reply_ok()` requires
  the reply to be *only* `def` blocks, one of them the target, and returns
  `bad-method-reply` with the reason as the next prompt's feedback.
- **A decomposed class stays decomposed.** Without this, the first accepted
  method drops the class under 40 lines and the very next iteration proposes
  the whole class again — the all-or-nothing bet, re-entering by the back
  door. `proposal_units(..., force=...)` pins it.

The class docstring and class-level attributes are outside every method span,
so they are untouched by construction rather than by instruction.

Also fixed while here: an **empty** fenced block used to be spliced in as the
symbol's replacement, i.e. it deleted the symbol; it is now a `no-code-block`.

### 2. Duplicate-group proposals — the new granularity (C2b)

`less_code/dedup.py` finds near-duplicate units project-wide. Detection is
deliberately dumb and language-agnostic: strip comments, fold string literals
to one token, replace every non-keyword identifier with `ID`, compare the
token sequences with `difflib.SequenceMatcher` at ratio ≥ 0.6. Names are the
thing that differs between copy-pasted blocks, so erasing them is the trick.
Python classes are decomposed here too — the py fixture's copy-paste lives
*inside* 186-line classes, and comparing whole classes finds nothing.

**Grouping is greedy and tight, not a transitive closure.** The first
implementation used union-find over the above-threshold pairs; it chained
every loop-shaped function in `lib.rs` into one blob, and the 5-member cap
then discarded `csv_escape_row` / `csv_escape_row_owned` — a 0.99-ratio pair —
in favour of five merely-similar neighbours. Groups are now seeded on the
highest-ratio unused pair, and a unit joins only if it is above threshold
against *every* member already in it.

What it finds, unprompted, on the three fixtures (`sim` = mean pairwise
ratio):

| fixture | group | LOC | sim |
|---|---|---:|---:|
| py | `parse_sku`, `Product.__init__`, `Inventory.receive`, `Inventory.allocate`, `OrderLine.__init__` | 132 | 0.81 |
| py | `Inventory.release`, `Inventory.adjust`, `Inventory.writeoff` | 55 | 0.88 |
| py | `plan_restock`, `valuate`, `tally_by_flag`, `reconcile` | 53 | 0.72 |
| py | `Inventory.sorted_skus/total_units/total_value/low_stock`, `Order.total_units` | 30 | 0.72 |
| rs | `title_case`, `longest_line`, `count_leading_spaces`, `count_trailing_spaces`, `indent_lines` | 51 | 0.68 |
| rs | `word_frequencies`, `char_frequencies` | 51 | 0.95 |
| rs | `csv_escape_row`, `csv_escape_row_owned`, `csv_escape_rows` | 37 | 0.81 |
| js | `buildMonthlySummary`, `buildQuarterlySummary`, `buildRegionSummary` | 57 | 0.81 |
| js | `creditSummary`, `debitSummary`, `columnIsNumeric` | 28 | 0.73 |

That is FIXTURE.md's "Embedded reduction opportunities" section, rediscovered
from the source alone. Every group the two fixtures document as copy-paste is
in the list.

Each group becomes **one** proposal: show all members, ask for a single
`_`-prefixed shared helper plus minimal rewrites of each member, public
signatures unchanged. The reply is applied as **one multi-file candidate**:

- `verify_multifile()` is the same contract as `verify_candidate` over several
  files — not-smaller → parse → API → tests — snapshotting and restoring
  **every** touched file in a `finally`, and running rust's whole-crate
  `cargo check` once rather than per file;
- members are spliced over their own spans (re-indented for python methods);
- the helper is appended to **each file that has a member**, so no cross-file
  import is invented. A helper duplicated across two files costs its own
  lines, and the size gate decides whether the merge still pays.

Two shape checks run before any test: a reply that does not contain every
member is `bad-dedup-reply` (with the member list fed back), and a helper
without a leading underscore is refused as public API.

The dedup pass runs **before** the per-symbol loop (gap item 7): it is
project-wide and it targets the biggest documented opportunity, so it should
not be starved by the greedy per-symbol loop. It is bounded to
`max_groups × attempts` calls. *This decision cost rs points at 3B — see the
results.*

### 3. Two more deterministic rules, and one deliberately declined

| rule | shape | rewrite |
|---|---|---|
| `if-ladder-to-dict` | `if x == "A": return 1 / elif … / return d` | `return {…}.get(x, d)` |
| `threshold-ladder-to-scan` | `if q >= 100: return .15 / elif … / return d` | `return next((v for t, v in ((100, .15), …) if q >= t), d)` |
| `max-loop-to-max` | `None`-sentinel manual max loop | `max(it, key=lambda k: …, default=None)` |

Both ladder rules accept the two spellings the fixture uses — a real
`if/elif/else` chain, and a run of consecutive `if c: return v` ending in a
bare `return d` (the same thing, because every arm returns). Both require ≥ 3
branches, a shared pure subject, constant results and a constant default.

Three honesty notes:

- **The `==` ladder becomes a dict; the threshold ladder does not.** A dict
  lookup is not equivalent to `>=` comparisons, and forcing it would be the
  kind of "reduction" this project exists to refuse. The `next(...)` scan
  evaluates the same comparisons in the same order, lazily, so a
  non-orderable subject still raises the same `TypeError` at the same
  comparison. `test_a_threshold_scan_keeps_the_comparison_so_a_type_error_still_raises`
  pins exactly that.
- **The dict rewrite has a recorded caveat.** If `x` is unhashable at runtime,
  `{...}.get(x, d)` raises `TypeError` where the `==` ladder returned `d`.
  Nothing in the AST can rule that out. The rule refuses bool keys (they alias
  ints), float keys (`1 == 1.0`), mixed key types and duplicate keys, and
  everything still goes through the layer gate — but this is a
  gated-not-proved rule, in the same class as `_python_drop_unreachable`, and
  it is written down rather than hidden.
- **`busiest_area` is declined.** It is FIXTURE.md's manual max-over-dict
  loop, and its sentinel is `best_units = -1`. `max(counts, key=…,
  default=None)` differs from it whenever every value is ≤ -1, which the AST
  cannot rule out. `max-loop-to-max` therefore only takes the `None`-sentinel
  shape, which *is* provable — and that shape does not occur in the fixture,
  so the rule earns **0 lines here** and is kept for the repos that have it.
  `test_a_numeric_sentinel_max_loop_is_refused` is the record.

### 4. Rust static depth: the clippy pedantic tier

`_rust_clippy_pedantic()` runs
`cargo clippy --fix -W clippy::pedantic -W clippy::complexity` as its own
gated step. `cargo clippy --fix` mutates the tree (unlike `ruff --fix` over
stdin), so the step snapshots every source file, runs it, reads the result
back and **restores the tree** — the edits leave as `changed_files`, inside
the pipeline's revert set, like `_rust_remove_dead_pub`. `static_pass` applies
them before the dead-`pub` scan so the two edit sets compose instead of being
alternative versions of the same file, then puts the tree back.

**Measured result on the rs fixture: the pedantic tier is a net +19 code
lines.** A good part of what it fixes is *adding* `#[must_use]` attributes and
`# Panics` doc sections — correct code, more lines. The size gate drops it and
says so in the notes (`clippy pedantic tier dropped: it ADDS 19 code lines`).
That is the layer working, and it is recorded as a measurement, not a win.
The suite-revert path is unit-tested separately.

## Evidence

### Tests

```
$ uv run pytest -q
199 passed in 19.4s        # was 153 at iteration 08
```

New file `tests/test_dedup_and_methods.py` (35); `tests/test_rules.py` gains
17; `tests/test_static_rust.py` gains 3.

The negatives are again the interesting ones — what must *not* happen:

- `test_a_merge_that_breaks_a_test_is_reverted_whole` — a bad merge leaves
  **both** touched files byte-identical (the multi-file gate's whole reason
  to exist), and `test_verify_multifile_restores_every_touched_file` proves
  the restore happens even on the failure path;
- `test_a_reply_missing_a_member_is_refused_before_any_test_runs` and
  `test_a_public_helper_is_refused` — the two shape checks, with the first
  additionally asserting the complaint reaches the *next* prompt;
- `test_grouping_is_tight_not_a_transitive_chain` — the union-find bug, pinned
  so it cannot come back;
- `test_a_whole_class_reply_to_a_method_prompt_is_refused` and
  `test_a_class_shaped_method_reply_is_recorded_and_fed_back`;
- `test_a_threshold_scan_keeps_the_comparison_so_a_type_error_still_raises`,
  `test_bool_keys_are_refused_because_they_alias_ints`,
  `test_a_numeric_sentinel_max_loop_is_refused`,
  `test_a_max_loop_whose_sentinel_is_read_afterwards_is_refused`;
- `test_clippy_pedantic_returns_edits_and_leaves_the_tree_untouched` — the
  purity property that keeps a mutating tool inside the revert set.

`test_a_merged_helper_plus_rewrites_is_accepted_across_two_files` is the
positive that carries the whole pass: a scripted backend returns
`_sum_kind` + two rewritten members living in different files, and the result
is one accepted multi-file candidate with the helper in each file and the
suite green.

### Static-only bench

`uv run lc bench --static-only`, canonical on all three.

| fixture | iteration 07 | iteration 08 | iteration 09 | delta |
|---|---:|---:|---:|---:|
| js | 7.99 % | 7.99 % | 7.99 % | — |
| py | 2.6 % | 4.6 % | **7.0 %** | +2.4 |
| rs | 5.1 % | 5.1 % | 5.1 % | — |

py 477 → 465. All 12 lines are the two new ladder rules:
`priority_for_status` (10 lines → a `.get()` the formatter wraps to 3) and
`discount_tier` (7 → 3). `max-loop-to-max` earned 0, as predicted above.
rs is flat because the clippy pedantic tier was measured and dropped for
adding lines. This is the first time py's static number has beaten rust's.

### 3B validation, and 7B settlement

```
# 3B (plumbing validation)
uv run lc bench --fixture {py,rs} --model qwen2.5-coder:3b \
                --attempts 3 --max-llm-calls 10 --num-ctx 16384 --llm-timeout 1200
# 7B (authorized settlement, sequential, `ollama stop` between)
uv run lc bench --fixture {py,rs} --model qwen2.5-coder:7b \
                --attempts 2 --max-llm-calls 8 --num-ctx 16384 --llm-timeout 2400
```

| fixture | config | LOC start | after static | final | static % | hybrid % | tests | api | hidden | calls | s | metric |
|---|---|---:|---:|---:|---:|---:|---|---|---|---:|---:|---|
| py | 3B | 500 | 465 | 465 | 7.0 | **7.0** | ok | ok | ok | 10 | 124.3 | canonical |
| rs | 3B | 392 | 372 | 372 | 5.1 | **5.1** | ok | ok | ok | 10 | 58.9 | canonical |
| py | **7B** | 500 | 465 | 463 | 7.0 | **7.4** | ok | ok | ok | 8 | 425.4 | canonical |
| rs | **7B** | 392 | 372 | 372 | 5.1 | **5.1** | ok | ok | ok | 8 | 186.6 | canonical |

**Hidden-test green 4/4. API preserved 4/4. No gate was relaxed.** Total 7B
wall time 612 s — well inside the ~60 minute authorization, and no re-runs
were taken.

`bench/results/20260831T010143Z.jsonl`, `…010400Z`, `…010516Z`, `…011236Z`;
table `docs/evidence/reduce-py-rs-iteration09.md`; attempt logs
`docs/evidence/reduce-{py,rs}-{3b,7b}.log`.

### Acceptance rate by proposal granularity

Budget-exhaustion records are excluded — they are not proposals.

**3B (py + rs, 20 proposals):**

| granularity | proposals | accepted | rate | rejections |
|---|---:|---:|---:|---|
| dedup-group | 12 | 0 | **0 %** | syntax-error 5, tests-failed 4, bad-dedup-reply 2, api-changed 1 |
| method | 2 | 0 | 0 % | tests-failed 2 |
| symbol | 6 | 0 | 0 % | tests-failed 3, syntax-error 2, not-smaller 1 |
| **total** | **20** | **0** | **0 %** | |

**7B (py + rs, 16 proposals):**

| granularity | proposals | accepted | rate | rejections |
|---|---:|---:|---:|---|
| dedup-group | 6 | 0 | **0 %** | syntax-error 3, tests-failed 2, bad-dedup-reply 1 |
| method | 3 | 0 | 0 % | tests-failed 3 |
| symbol | 7 | 1 | **14 %** | syntax-error 3, tests-failed 2, not-smaller 1 |
| **total** | **16** | **1** | **6 %** | |

The single acceptance in four runs is `inventory.py:apply_order` at 7B —
**two code lines**.

### The three findings this pass actually produced

**1. The new granularity works mechanically and yields nothing yet.** Every
dedup group in the tables above was found correctly, prompted correctly and
applied correctly; the *reply* was the failure. On rust, 3 of 3 groups at 7B
and 5 of 6 at 3B died at `cargo check` — a merge across three functions is
harder to compile than one function, and iteration 08 already measured that
these models cannot reliably emit compiling Rust for *one*. On python the
replies compiled and reached the test gate (`465 → 427` on the
`plan_restock`/`valuate`/`tally_by_flag`/`reconcile` group at 7B: a **38-line**
reduction, correctly rejected because it changed behaviour). The proposals are
now the right shape and the right size; the model is what is short.

**2. The dedup pass starved the only granularity with a non-zero rate — and
this pass caused a measured regression.** rs went **7.4 % (iteration 08) →
5.1 % (this pass)** at 3B on the same model and budget. The mechanism is
plain in the log: the dedup pass consumed **6 of the 10 calls** with 0
acceptances, so the per-symbol loop never reached `csv_escape_field`, which is
the proposal that earned iteration 08's rust points. Running dedup first was a
deliberate decision (gap item 7 was that the greedy loop starved the
cross-symbol pass), and on this evidence it was the wrong way round: the
scheduler should give the *measured* granularity the budget and let the
speculative one have the remainder. Defaults are **left unchanged** here —
changing them after seeing the number, without the GPU budget to re-measure,
would be exactly the number-chasing this project refuses. It is gap item 1 for
the next pass.

**3. Per-method decomposition removed the structural blocker and exposed the
next one.** py's proposals are now `Inventory.allocate`, `Inventory.receive`,
`OrderLine.from_row` — 10-25 line units, not a 186-line class. Every one of
them was rejected by the *test* gate, not by size or syntax: the model
rewrites the method plausibly and gets an exact error message or a boundary
case wrong. That is a much better failure than iteration 08's, and it is a
different problem (behavioural fidelity, and the method-level hunk salvage
that was gap item 2 and is still not built) than the one this pass fixed.

## Verdict against the 25 % bar (CRITERIA.md C3-C5)

| fixture | bar | static-only | best hybrid | model / strategy | metric | verdict |
|---|---|---:|---:|---|---|---|
| py (C3) | ≥ 25 % | 7.0 % | **7.4 %** | 7B, per-method + dedup | canonical | **MISS** by ~18 points |
| js (C4) | ≥ 25 % | 7.99 % | **25.96 %** | 7B whole-file + salvage (`3ac8d23`) | canonical | **PASS** (unchanged) |
| rs (C5) | ≥ 25 % | 5.1 % | **5.1 %** | 7B, per-symbol + dedup | canonical | **MISS** by ~20 points |

`status.json` therefore keeps `demo-py` and `demo-rs` **pending**, with the
honest numbers recorded as their evidence. C4 is untouched by this pass and
still stands on `3ac8d23`.

D1/D2's exit gate (static-only ≥ 15 % per fixture) is also still unmet at
7.99 / 7.0 / 5.1, though py has now closed a third of its distance to it
using only provable rewrites.

## Gap diagnosis

Ranked by what blocks the 25 % bar next.

1. **Budget scheduling is now the top blocker, and it is cheap.** Measured, not
   argued: dedup took 6/10 calls at 0 % and cost rs 2.3 points. The scheduler
   should allocate by *measured* acceptance rate — per-symbol/per-method first,
   dedup with what is left — or interleave, so no granularity can take the
   whole budget. This is the one change in this list that is free of GPU risk.
2. **A rejected method/dedup proposal is still thrown away whole** (iteration
   08's gap item 2, still open). C1 hunk salvage runs only on the whole-file
   sweep, which never executes because the budget is gone. The 38-line
   `plan_restock` group merge at 7B is exactly the candidate salvage exists
   for: it is almost certainly right about two of its four members.
3. **Rust's real blocker is `cargo check`, at every granularity.** 3B: 7/12
   syntax-errors. 7B: 5/10. A dedup group makes it worse, not better, because
   a merged generic helper is the hardest Rust to get right. Rust needs either
   diff-mode proposals (search/replace blocks, roadmap C2's other half — the
   model then only has to write the changed lines) or a bigger model. On this
   evidence, more granularity will not fix Rust.
4. **The 25 % bar on py and rs probably is not reachable with a 7B on a 6 GB
   card at all.** js cleared it with 2 calls of *whole-file* rewriting at 7B;
   py and rs have now had 4 runs, 36 proposals and 1 acceptance across two
   model sizes and three granularities. The honest reading is that the fixtures
   differ (js's 601 lines are shallow report-formatting code; py's are a
   stateful class hierarchy with exact error messages, rust's are ownership
   puzzles) and that the missing ingredient is model capability, not proposal
   shape. The next real move is a frontier model through the
   OpenAI-compatible backend, or the GRPO-trained reducer C6 exists for.
5. **The test spec is 40 000 characters inside a 16 384-token context.** Every
   prompt in every run carried the full frozen suite, and on py that is most
   of the window before the code even arrives. A spec *selector* — only the
   test functions that mention the symbols in the proposal — is likely worth
   more than another granularity, and it is CPU-testable.
6. **`bad-dedup-reply` on the 5-member py group at both sizes.** The largest,
   highest-value group (132 LOC, the six-times-pasted SKU validation) is the
   one neither model could answer in the required shape. Groups should
   probably be capped at 3 members for proposal purposes even though detection
   finds 5.
7. **The `if-ladder-to-dict` unhashable-subject caveat is unproved, not
   unknown.** It is documented in the rule and here, and gated by the frozen
   suite plus the hidden suite. A reviewer should look at it; a
   `try/except TypeError` fallback would cost the line saving, and a
   `next(...)` scan (as used for thresholds) would be provable at the cost of
   the idiom the fixture documentation asks for. Recorded as a deliberate,
   reversible choice.
8. **`fixtures/rs/target/` is still tracked in git** (iteration 07 item 10,
   iteration 08 item 9, unchanged: `git rm --cached -r`, still out of scope).

