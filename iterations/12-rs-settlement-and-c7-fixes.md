# Iteration 12 — the rust settlement, and remediating the C7 review

Two things happened between iteration 10 and here. The first was finishing C5
(rust), which iteration 10 left 42 code lines short. The second was an
independent review of the whole build (`docs/evidence/C7-REVIEW.md`), which
returned **FAIL** with eleven numbered defects. This record covers both,
because the second is largely a consequence of how the first was done.

---

## Part 1 — how rust got from 14.29 % to 25.51 %

Iteration 10's blockers, quoted from `status.json` as it stood:

> rs is 42 code lines short of the bar and 7/8 whole-file proposals at 7B are
> cargo-check syntax errors even with the compiler's diagnostics in the prompt
>
> strategies do not compose: mixed found 2 merges (372→356), whole-file found 1
> rewrite (372→336), and nothing runs the second on the output of the first

Both were answered, but only one of them with code.

**The composition problem was solved by hand, not by a flag.** Four sequential
`lc reduce` invocations against one persistent tree, each starting from the
previous one's output: whole-file → mixed → whole-file → whole-file. That is
the entire mechanism. `lc reduce` still has no `--rounds`. The exact commands,
the per-round arithmetic and the caveats are now written down in
`docs/evidence/rs-reduced-292/REPRODUCE.md`, which is what should have existed
at the time (review D3).

**The syntax-error problem was solved by code**, and it is the interesting
finding of the campaign. Commit `17a5b35` extended hunk decomposition to
candidates that do not parse: previously a `cargo check` failure discarded the
whole rewrite, on the reasonable-sounding theory that an unparseable file has
nothing to offer. That theory is wrong at 7B. Round 3 and round 4 ran the same
command with the same model and differ only by `17a5b35`:

| round | proposals | syntax errors | lines gained |
|---|---|---|---|
| 3 (pre-`17a5b35`) | 6 | 4 | **0** |
| 4 (post-`17a5b35`) | 6 | **6** | **19** |

Round 4 got *worse* proposals and *more* lines. Every single one of its
whole-file rewrites was unparseable, and five of six still yielded accepted
hunks. A 7B model's rust output is a mixture of correct fragments and broken
glue; delta-debugging at top-level symbol boundaries separates them. Nothing
about that is rust-specific — it is a general lesson about what to do with a
small model's rejects.

Final: 392 → 311 (round 1) → 311 → 311 → **292** (round 4) = **25.51 %**, 39
frozen + 12 hidden tests green.

**The hidden suite earned its keep.** Mid-campaign `tests_hidden` went red while
the frozen suite stayed green: an accepted rewrite had changed `title_case`'s
behaviour on input the visible suite did not pin. The function was restored
verbatim and the round re-run. This is the one place in the whole project where
an accepted, gate-green reduction was actually wrong, and the mechanism that
was built to catch exactly that caught exactly that.

---

## Part 2 — the review, and what it cost

The reviewer reproduced everything reproducible on CPU: 236 tests, the static
bench row to the digit, both preserved trees against both suites, the LOC
arithmetic, the mutation scores, a byte-identical GRPO dataset rebuild. They
found no fabricated number, no weakened test, no metric gaming. The FAIL was
about **evidence quality and provenance**, and it was correct on every count.

The single most useful thing it surfaced is a pattern, not a bug: *every one of
the serious defects is a place where the artifact was thrown away and the
number kept.*

- D1 — `lc reduce` was pointed at `fixtures/py` **in place**, and the reduced
  output was committed over the fixture. Every C3 number was then measured from
  a 500-LOC tool-modified baseline instead of the 537-LOC original.
- D2 — the js demo ran under `lc bench`, which reduces in a scratch copy and
  then deletes it. C4 rested on one jsonl row and a four-line log; the reduced
  file existed nowhere.
- D3 — the rs chain ran by hand with no driver and no log, so the outcome was
  verifiable and the method was not.
- D5 — bench rows were stamped with `HEAD` while the tree was dirty, so three
  py rows name a commit whose tree has no `outline.py` — the pass credited with
  107 of the lines in those very rows.

D10 is the root cause of the first of those, and arguably of the habit: a tool
that only ever writes in place teaches you to run it on things you care about.

### What was changed

| defect | fix |
|---|---|
| D1 | `fixtures/py/inventory.py` restored byte-identical to `6ef9a1a` (537 canonical code-LOC, 97 + 32 tests green, mutation 0.925). C3 re-derived on a scratch copy: **537 → 357 = 33.5 %**, higher than the 28.6 % claimed off the tainted baseline. Tree + report replace `docs/evidence/py-static-357/`. |
| D2 | js reduction re-run with the tree preserved: **601 → 426 = 29.12 %** at `docs/evidence/js-reduced-426/`, plus `lc bench --keep-tree DIR` so a bench row need never again outlive its tree. See the C4 section below. |
| D3 | `docs/evidence/rs-reduced-292/REPRODUCE.md`: the exact four commands, the round-by-round arithmetic, the `title_case` catch, and a plain statement that LLM rounds are non-deterministic. |
| D4 | `ReduceStats` now records `api_baseline: "post-static"` and `static_removed_symbols`, so a report says out loud that L1 deleted public items. Wording corrected in README and `status.json`. Gate semantics unchanged — dead-public removal is the feature; the claim was the defect. |
| D5 | `git_commit()` appends `-dirty` when `git status --porcelain` is non-empty. Four tests. |
| D6, D7 | README status block and `status.json` brought current. |
| D8 | All 53 numbered sources expanded to followable URLs at the end of `docs/research.md`; the count corrected to "53 entries, 52 distinct". |
| D9 | `reward_fn_mutation_weighted` implemented — see below. |
| D10 | `lc reduce --copy-to DIR` reduces a copy; in-place use on a git tree with uncommitted changes warns on stderr. `lc bench --keep-tree DIR` is the complementary half. README Quickstart no longer points `reduce` at `fixtures/` in place. |
| D11 | Four tracked `.pyc` files untracked. |

Along the way the review's D4 investigation turned up a real reporting bug:
`less_code/static.py`'s dead-symbol note read
`sorted(defs - set(referenced) & defs)`, which parses as
`defs - (set(referenced) & defs)` and is therefore **always empty**. Every
python run since the pass was written has logged `removed []` while removing
things. Fixed and pinned by a test.

### D9 — the mutation-weighted reward

CRITERIA C6 names "reward fn = tests_pass * loc_reduction (+ mutation-weighted
variant)". The variant did not exist. It does now:

```
reward_fn                     r = gate × (1 + λ·loc_delta) − penalties
reward_fn_mutation_weighted   r = gate × (1 + λ·loc_delta·mutation_score)
```

The argument for it is the same argument the whole tool rests on: the frozen
suite is the only oracle, so an accepted saving is exactly as trustworthy as
the suite that certified it. Scaling shaping by the suite's mutant kill rate
pushes the policy toward reductions that are actually pinned down, which
matters the moment training moves off these three hand-built fixtures and onto
a mined corpus of mixed quality.

What is deliberately *not* scaled is the gate. A red suite, a changed API or
unparseable output is −1.0 whatever the mutation score — weighting can never
buy a behavior break, only discount a legitimate one. At `mutation_score == 1.0`
the two functions are identical, and a sample with no score defaults to 1.0, so
older datasets keep their old rewards. Ten tests pin all of that.

`build_dataset.py` grew `--mutants N` (default 20) and stamps each sample with
its fixture's kill rate, measured by `less_code.audit` **on a throwaway copy** —
mutation testing rewrites source files, and after D1 that is not a mistake worth
repeating. Current scores: py 0.95, js 0.85, rs 1.0. The build stays
deterministic and reproducible (69 samples, up from 66 because restoring the py
fixture restored its legacy-section units).

### Not defects

The reviewer independently re-derived and accepted the `_rule_if_ladder_dict`
caveat — `{...}.get(x, d)` raises `TypeError` on an unhashable subject where the
`==` ladder returned `d` — noting it is documented in the rule's docstring, in
iterations 09 and 10, and in the README, is constrained by four preconditions,
and that the sibling threshold rule deliberately emits a lazy `next(...)` scan
rather than a dict precisely to stay exact. It stays as it is: gated, not
proved, and said so.

---

## Part 3 — C4 (js) reproduced with an artifact

**601 → 426 canonical code-LOC = 29.12 %**, 36 frozen + 16 hidden tests green,
in one bounded whole-file round of ~20 GPU minutes. Four rounds were budgeted;
the first cleared the bar. Tree, report, log and recipe:
`docs/evidence/js-reduced-426/`.

The run replicated rust's round-4 finding exactly, and more starkly:

| attempt | proposal | verdict | salvaged |
|---|---|---|---|
| 1 | 553 → 315 | `api-changed` | 553 → 535 |
| 2 | 535 → 174 | `syntax-error` | 535 → 488 |
| 3 | 488 → 333 | `tests-failed` | 488 → **426** |

**Every whole-file proposal was rejected, for three different reasons, and
every one of the 127 L2 lines came from salvaging the rejects.** Without hunk
decomposition this run scores 7.99 %. The generalisation is now supported by
two languages and three distinct rejection modes: at 7B a whole-file rewrite is
unreliable *as a whole* and useful *in parts*, so the interesting engineering is
in what you do with a candidate after you refuse it.

Note this is a **new** result, not a re-derivation of the discarded 25.96 % run.
It could not be a re-derivation — the rounds are non-deterministic, which is
said plainly in `REPRODUCE.md` and is the reason the preserved tree, rather than
the log, is what the criterion now rests on.

`lc bench --keep-tree DIR` was added alongside `--copy-to` so the next person
does not have to run a demo twice to have something to show for it.

---

## State

C1–C6 pass on their objective gates, and all three demos now clear the bar with
a preserved tree anyone can check on CPU: **py 33.5 %, js 29.1 %, rs 25.5 %**.
C7 goes back for re-review with, as far as this pass can tell, no unresolved
concrete defect. `state` stays `in_progress` until that re-review returns.

`uv run pytest -q`: **259 passed** (236 before this pass; +23 for the
mutation-weighted reward and its js/rust parse gate, the dirty-tree stamp,
`--copy-to`, `--keep-tree` and the api-baseline reporting).

The review also noted, correctly, that the reward's "parse/compile check" was
python-only. It now runs `node --check` / `cargo check` on the spliced file for
js and rust before the suite, matching what the docstring always claimed.
