# Iteration 10 — deterministic guard outlining, compiler feedback, pair-wise merge templates, and the py/rs settlement

## Attempt

Iteration 09 ended with a ranked gap list and one blunt finding: across four
benched runs, 36 proposals and two model sizes, the LLM layer contributed
**2 code lines** to py and rs. The proposals were the right shape; the model
was what was short. Two of the failure modes were specific enough to attack
with tooling rather than hope:

- **py's replies compiled and then broke a pinned error message.** The
  `plan_restock` group merge at 7B was a 38-line reduction, correctly
  rejected. The largest documented opportunity in `fixtures/py/FIXTURE.md` —
  the SKU validator pasted six times, the qty guard pasted seven times, the
  `inventory is None` guard pasted eight times — is *copy-paste*, and a
  copy-paste merge is a thing a machine can prove. Asking a 7B to do it was
  the mistake.
- **rust's replies died at `cargo check`, and the model was never told why.**
  100 % of iteration 09's rust dedup-group proposals were rejected at the
  parse gate; the outcome string carried one line, truncated to 120
  characters, squeezed out of `first_failure()` — an extractor written for
  pytest tails, which throws away the `-->` location and the
  `expected … found …` note that make a diagnostic actionable.

So this pass builds three things and then settles C3/C5.

## What landed

### 1. `less_code/outline.py` — deterministic guard-block outlining (python)

A *window* is a run of consecutive statements at the top level of a function
body where every statement is either `if <test>: raise <exc>` (single-statement
body, no `else`) or `name = <expr>`. Windows that are identical after
**alpha-renaming** (free names → `P0…`, bound names → `B0…`) and **constant
abstraction** form a group. A group with ≥ 3 occurrences that pays for its own
helper in code lines is outlined into one module-level `_`-prefixed helper;
each occurrence becomes one call line.

Selection is greedy over *all* contiguous windows in every run: the
highest-scoring group is taken, its sites are marked used, and the search runs
again on what is left. That is what lets the 10-statement SKU block and the
3-statement qty block — which sit back to back in `receive` and `allocate` —
be found as two separate groups rather than one blurry compromise.

**Why the rewrite is safe.** Purity is never established and never needed. The
claim is narrower: executing a statement sequence inside a function called at
that point is the same as executing it inline, provided nothing escapes by a
route a call cannot carry. The routes are enumerated and each one is closed by
a precondition:

| route | precondition |
|---|---|
| values the block **computes** | every bound name is returned by the helper and unpacked at the call site |
| values the block **consumes** | every free name is an argument — and arguments are evaluated *eagerly*, so only `Name` loads and `Constant`s may become arguments, and a free name must be provably bound (a function parameter, or the target of an earlier unconditional top-level assignment) |
| **non-local control flow** | a window containing `return`/`break`/`continue`/`yield`/`await` is refused; `raise` crosses a call boundary fine, which is the entire point |
| **scope tricks** | `lambda`, nested `def`/`class`, walrus, and `global`/`nonlocal` targets are refused |

The eager-argument rule is the load-bearing one. `receive`'s guard raises
`InventoryError('quantity must be positive, got %d' % qty)`. Lift that
expression to the call site and `receive('AA-000001', 'seven')` raises
`TypeError` from the `%` operator *before* the `isinstance` guard runs — a
different exception from the one the suite pins. Because only bare names and
literals may be arguments, the `%`-format stays inside the helper and runs in
its original position.
`test_the_argument_is_evaluated_before_the_guards_so_only_names_are_lifted`
pins exactly that.

**Error messages are preserved by construction.** Nothing in the module ever
builds a string. A constant identical at every site is re-emitted verbatim
into the helper; a constant that differs becomes a parameter passed as a
literal from each call site. A message can therefore be moved, but never
reworded or merged. The one honest cost is that a traceback gains one frame;
`type(exc)`, `str(exc)` and the raise order do not move.

Multi-file groups are supported: the helper goes in the file with the most
occurrences and the others get `from <stem> import <helper>` at the top. The
fixtures are single-source-file, so this path exists for real repos and is
covered by `test_a_group_spanning_two_files_hosts_the_helper_where_it_is_busiest`
rather than by the bench.

It is wired in as its own gateable layer (`guard-outlining`) in
`_python_layers`, after the peephole rules — so it works on bodies the rules
have already shrunk — and before ruff, which cleans up after it.

**What it found on the py fixture, unprompted:**

| helper | sites | code lines saved |
|---|---:|---:|
| `_check_sku` — the whole SKU shape validator, `text`/`pieces`/`area`/`digits` and all six messages | 6 | **72** |
| `_check_qty` — the `None`/`int`/`bool`/positive ladder | 4 | 21 |
| `_check_sku2` — `if x is None: raise InventoryError(<message>)`, message parameterized | 10 | 7 |
| `_check_inventory` — `if x is None: raise ValueError(<message>)`, message parameterized | 10 | 7 |
| **total** | | **107** |

That is `FIXTURE.md`'s three named copy-paste families, plus one it does not
name, taken deterministically, with the frozen suite and the hidden suite both
green.

### 2. Compiler diagnostics as feedback (everywhere)

`compiler_errors(text, lang)` replaces `first_failure()` inside
`syntax_check()`. For rust it keeps each `error[…]`/`error:` diagnostic as a
**block** — the message, its `-->` location and its notes — for the first
three errors, and drops the `error: aborting` / `rustc --explain` boilerplate
that names no code. For js it keeps node's `path:line`, the offending source,
the caret run and the `SyntaxError:` line, and drops node's own internal
stack. The budget in the outcome string went from 120 to 600 characters.

`_feedback_for()` then frames it as an instruction rather than a status:
*"Your previous reply DID NOT COMPILE. The compiler said: … Fix exactly these
errors."* Python's `ast` message travels the same road (it always did reach
the record; it now reaches the *prompt* with the same framing).

`test_a_syntax_error_reaches_the_next_prompt_and_is_fixed` is the end-to-end
proof: a scripted backend emits code that does not parse and repairs it *only*
when the next prompt quotes the parser's complaint. If the diagnostic stops at
the record, the backend loops and the test fails.

### 3. Pair-wise duplicate groups with a computed merge template

Three changes to `dedup.py` and the dedup prompt:

- **Groups are capped at 3 members and a third member must be a *tight* twin**
  (ratio ≥ 0.9 against every member, versus 0.6 to seed the pair). Iteration
  09's `csv_escape_row` / `_owned` / `_rows` triple had a token diff twice the
  size of the pair's and none of its proposals compiled; the pair alone has a
  diff of **one item**.
- **Groups are ranked by `loc × similarity`, not `loc`.** A 0.99-similar pair
  is a template to fill in; a 0.71-similar pair is a redesign. The models can
  only do the first, so the budget should meet the first.
- **`token_diff_slots(group, lang)` computes what actually differs** — raw
  tokens with comments stripped but names and string literals kept, aligned
  with `SequenceMatcher`, with runs that are only the definitions' own names
  dropped (renaming is not parameterization). The prompt now carries that list
  and says it is complete: *"the helper needs at most N parameters, one per
  item above; write it by copying member 1 verbatim and replacing only those N
  items."* The system prompt gained the line the failures were about: **exact
  error messages and exact public signatures are pinned by the tests; copy
  every string literal character for character.**

What the computation hands the model on the rust fixture:

| pair | sim | computed diff |
|---|---:|---|
| `csv_escape_row` / `csv_escape_row_owned` | 0.994 | `& str` → `String` (one item) |
| `count_leading_spaces` / `count_trailing_spaces` | 0.963 | `(nothing)` → `. rev ( )` (one item) |
| `word_frequencies` / `char_frequencies` | 0.949 | 8 items |

### 4. What was measured and *not* taken

The roadmap item for this pass also listed a `Counter` conversion for the
count-into-dict loops. It was measured against the **post-outlining** file and
declined:

- `low_stock` no longer exists as a loop — `append-loop-to-comprehension`
  already takes it, walrus and all.
- `tally_by_flag` is the only remaining site. A `Counter` rewrite there is
  worth about 5 code lines net (the loop body has two statements before the
  count, so it needs the multi-statement collapse machinery, and it costs an
  `import collections` line), and it changes the returned object from `dict`
  to `Counter`. `Counter` is a `dict` subclass, so `==` still holds and
  `type(...) is dict` does not — a gated-not-proved rule in the same class as
  `if-ladder-to-dict`, for 1 % of a criterion that is already met by 18 lines.
  Recorded, not taken.
- `busiest_area` stays where iteration 09 left it: deterministically declined
  (its `-1` sentinel is not provably `default=None`), and left to the LLM
  layer.

## Evidence

### Tests

```
$ uv run pytest -q
235 passed in 21.0s        # was 200 at iteration 09
```

New: `tests/test_outline.py` (20) and `tests/test_compiler_feedback.py` (15).

The negatives carry the safety argument:

- `test_the_argument_is_evaluated_before_the_guards_so_only_names_are_lifted`
  — the eager-argument rule, stated as behaviour;
- `test_a_name_the_helper_could_not_resolve_is_declined` — a name bound only
  inside a branch would become an argument and raise `UnboundLocalError`
  where the original raised the pinned error;
- `test_a_window_containing_a_return_is_declined`,
  `test_a_lambda_in_the_window_is_declined`,
  `test_a_global_declared_name_is_declined`;
- `test_a_group_that_cannot_pay_for_its_helper_is_declined` and
  `test_a_statement_sharing_a_line_is_not_clobbered` — the metric is code
  lines, and a rewrite must own whole lines;
- `test_more_than_two_varying_constant_classes_is_declined`, with the
  companion assertion that pinning one literal lets the same input through, so
  the test is about the cap and not about the shape;
- `test_a_scripted_misfire_is_reverted_by_the_gate` — the layer scripted to
  reword `'x required'` to `'x is required'` is dropped by the narrowing pass
  with `guard-outlining reverted by the gate` in the notes;
- `test_the_definition_name_is_not_a_parameter` — renaming is not
  parameterization, and a diff that says otherwise wastes the model's budget.

Three `exec`-and-compare tests run the original and the outlined module side
by side over a matrix of inputs and require identical exception types and
identical `str(exc)` — including
`test_the_real_fixture_block_keeps_every_message`, which is the exact shape
the py fixture pastes six times.

### Static-only bench

`uv run lc bench --static-only`, canonical metric, all three fixtures.

| fixture | it-07 | it-08 | it-09 | **it-10** | delta |
|---|---:|---:|---:|---:|---:|
| js | 7.99 % | 7.99 % | 7.99 % | 7.99 % | — |
| py | 2.6 % | 4.6 % | 7.0 % | **28.6 %** | **+21.6** |
| rs | 5.1 % | 5.1 % | 5.1 % | 5.1 % | — |

py: **500 → 357 code lines** with tests, API and the hidden suite green and
**zero LLM calls**. 107 of the 143 lines are the four outlined helpers; the
rest is the pre-existing dead-code, rule and ruff layers.

This clears D1/D2's static-only ≥ 15 % exit gate on python — the first fixture
to reach it — and, on its own, clears the 25 % bar for C3.

### GPU settlement

Sequential, `ollama stop qwen2.5-coder:7b` between runs, 3408 s (57 min) of
GPU total against a 90-minute authorization and a 40-minute-per-fixture cap.
No re-runs were taken. Full tables and attempt logs in
`docs/evidence/reduce-py-rs-iteration10.md`.

**Strategy choice.** The settlement plan allows the mixed strategy for the 7B
runs if a cheap 3B trial looks stronger. The 3B trial (rs, mixed, 36 s)
produced the **first rust duplicate-group acceptance in the project's
history** — `csv_escape_row` + `csv_escape_row_owned` merged on attempt 1 —
so it was promoted to a full 7B mixed run. That run reproduced the merge and
added `csv_escape_field`, and still **lost on totals**:

| fixture | config | strategy | after static | final | hybrid % | calls | s |
|---|---|---|---:|---:|---:|---:|---:|
| rs | 3B | mixed | 372 | 367 | 6.38 | 8 | 36 |
| rs | 7B | mixed | 372 | 356 | 9.18 | 14 | 261 |
| rs | **7B** | **whole-file** | 372 | **336** | **14.29** | 8 | 1092 |
| py | **7B** | **whole-file** | 357 | **348** | **30.4** | 4 | 2019 |

tests / api / hidden green on all four. **No gate was relaxed and the metric
is unchanged.** One accepted whole-file rewrite of `stats.rs` (155 → 119) is
worth more than every per-symbol and pair-wise acceptance combined, so
whole-file is what stands as rs's best — and it reproduces iteration 09's 336
exactly, which is a reassuring null result for the bench's determinism.

On py, **every one of the four whole-file proposals was rejected.** The
`loc_before` column walks 357 → 355 → 355 → 351: all 9 LLM code lines came
from C1 hunk salvage keeping the individually-green hunks of rejected
rewrites. The model's own bets were 0 for 4.

### Acceptance rate by proposal granularity (this pass, rs runs 1-3)

Budget-exhaustion records excluded.

| granularity | proposals | accepted | rate | iteration 09 |
|---|---:|---:|---:|---|
| dedup-pair | 10 | **2** | **20 %** | 18 proposals, 0 accepted, 0 % |
| symbol | 12 | 1 | 8 % | 13 proposals, 1 accepted, 8 % |
| whole-file | 8 | 1 | 13 % | — |

The dedup granularity went from 0 % to 20 % on rust. The change was the
computed token diff plus the compiler feedback; nothing about the detector or
the multi-file gate moved.

## Verdict against the 25 % bar (CRITERIA.md C3-C5)

| fixture | bar | static-only | best hybrid | model / strategy | metric | verdict |
|---|---|---:|---:|---|---|---|
| **py (C3)** | ≥ 25 % | **28.6 %** | **30.4 %** | 7B whole-file + salvage | canonical | **PASS** |
| js (C4) | ≥ 25 % | 7.99 % | 25.96 % | 7B whole-file + salvage (`3ac8d23`) | canonical | PASS (unchanged) |
| rs (C5) | ≥ 25 % | 5.1 % | **14.29 %** | 7B whole-file + salvage | canonical | **MISS** by ~11 points |

`status.json`: `demo-py` → **passed**. `demo-rs` stays **pending** with the
honest number.

D1/D2's static-only ≥ 15 % exit gate is now met on **python** (28.6 %) and
still unmet on js (7.99 %) and rust (5.1 %).

## The three findings this pass produced

**1. The py criterion was never a model problem.** 143 of py's 152 reduced
lines are deterministic, and 107 of those come from one transform that took
about 400 lines of code and a day's worth of proof obligations. Four runs,
two model sizes and three proposal granularities across iterations 08-09
bought py 2 code lines from the LLM; a rewrite the AST can *prove* bought 107.
The lesson is not "static beats LLM" — js's 25.96 % is almost entirely LLM —
it is that **copy-paste is the shape a machine should take, and the models
were being asked to do arithmetic with error-message strings.** Where a
fixture's documented opportunity is "this block is pasted six times", the tool
should notice before it spends a token.

**2. Telling the compiler's answer to the model changes the rust numbers.**
Iteration 09: 18 rust dedup-group proposals, 0 accepted, and the rejection
reason the model saw was up to 120 characters of a pytest-tail extractor.
This pass: 10 dedup-pair proposals, 2 accepted, including one at **3B** — a
model that had never landed a rust merge at any granularity. Two changes
combined to do it and they are not separable from these runs alone: the
compiler diagnostics reaching the prompt, and the pair cap plus the computed
token diff shrinking the task from "design a merge" to "replace `&str` with
`String` and add a generic". The honest reading is that both helped and the
experiment to separate them was not run.

**3. rust's ceiling is still `cargo check`, and it did not move for
whole-file.** In the whole-file run, **7 of 8 proposals were syntax errors** —
lib.rs failed all four attempts even with the diagnostics in the prompt. The
one acceptance was `stats.rs`, the smaller file. The compiler feedback helps
where the task is small (a two-function merge with a one-item diff); it does
not help a 217-line whole-file rewrite, because a model that produces four
different non-compiling versions of the same file is not failing for a reason
a diagnostic can fix. rs is 42 code lines short of the bar and there is no
proposal shape in this tool that is likely to find them at 7B.

## Gap diagnosis

Ranked by what blocks what is left (C5, and the review criterion).

1. **rs needs 42 lines and the local models cannot compile their way to
   them.** 7 of 8 whole-file proposals were syntax errors *after* the
   diagnostics landed in the prompt. The remaining honest routes are a
   frontier model through the OpenAI-compatible backend, the GRPO reducer
   (C6), or diff-mode proposals (search/replace blocks) so the model only
   emits the changed lines and cannot break the surrounding code. The last is
   CPU-testable and is the cheapest of the three.
2. **The strategies do not compose.** rs mixed found two real merges
   (372 → 356) and rs whole-file found one real rewrite (372 → 336); nothing
   in the tool runs the second on the output of the first. A `--strategy
   mixed,whole-file` that chains passes is a small change and, on these two
   rows, plausibly worth several points — though "plausibly" is doing work
   there and it has not been measured.
3. **Guard outlining is python-only.** The same transform is well-defined for
   rust (a `match`/`if` guard block returning `Err(...)`) and js (an
   `if (…) throw new Error(…)` run), and both fixtures have the shape. The
   proof obligations are different per language — rust's are about ownership,
   not about eager argument evaluation — so this is a real piece of work, not
   a port.
4. **The `Counter` rule and `busiest_area` are still unclaimed** and are
   recorded above as measured-and-declined rather than forgotten. They are
   worth about 5 and 18 lines on a criterion that is already met.
5. **The py static result should be read with its caveat visible.** Four of
   the outlined helpers now sit at the top of `inventory.py` with names
   (`_check_sku2`) that a human reviewer would improve. The transform picks
   the most common parameter name at the sites and dedupes; it does not
   understand what the block is *for*. That is cosmetic, it is inside private
   code, and it is not worth an LLM call — but a reviewer should see it named
   rather than discover it.
6. **`if-ladder-to-dict`'s unhashable-subject caveat is unchanged** from
   iteration 09 and still wants a reviewer.
7. **`fixtures/rs/target/` is still tracked in git** (iteration 07 item 10,
   08 item 9, 09 item 8; unchanged, still out of scope).
