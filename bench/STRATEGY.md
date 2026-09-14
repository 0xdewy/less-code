# Making less-code useful

Investigation: 2026-09-04. This is a proposed direction, not a claim that the
architecture or training pipeline below is already implemented.

Implementation follow-up: the context-aware experiment is now available through
`shrink` and `corpus`. It ranks eligible symbols, retrieves bounded
imports/source/test excerpts, supports `--ml-attempts 1..3` with gate feedback,
re-anchors after accepted edits, preserves declarations, stops duplicate
proposals, and saves `ml_records`. Corpus manifests support a separate `audit`
command that cannot feed search. See README for the comparison commands.
Public bodies became eligible after declaration checks were strengthened;
their declarations remain frozen. Cross-symbol restructuring, a resolved
dependency graph, differential-test
generation, tool integrations, and training remain later stages. The evidence
below describes the repository at the start of the investigation.

## Verdict

Build a repository simplifier whose advantage is finding larger opportunities
and producing trustworthy, reviewable changes. More peephole rules alone will
not deliver large reductions on mature libraries. Training a model before
improving the verifier would teach it to exploit incomplete tests.

Keep canonical LOC as one outcome, but optimize accepted maintenance reduction:
dead internal dependency chains, duplicated implementations, unnecessary
wrappers, and obsolete compatibility paths with explicitly established support
requirements. Distinguish applications with known entrypoints from libraries
whose consumers are outside the repository. Never infer that a feature is
unnecessary just because tests do not exercise it.

## Evidence from this repository

- The previous checked-in benchmark reported 352 lines removed from 13,620
  (2.58%); four of six projects had zero reduction. This is a historical result,
  not an independently audited estimate of behavior preservation.
- `rules.py` included `strip-main-block`: deleting executable script behavior
  on the rationale that pytest never reaches it. This investigation removes it.
- The previous criteria advised selecting libraries with removable main blocks
  and permissive style. That selects for the implementation rather than testing
  general usefulness. Retain difficult projects and zero-yield results.
- `pipeline._ml_proposals` selects at most eight private symbols per file and
  64 total in discovery order. `build_prompt` supplies the symbol alone, without
  imports, callers, tests, or failed-proposal feedback. No cross-function or
  cross-file model changes are possible. This restricts both opportunity and
  understanding; it does not demonstrate a model capability ceiling.
- File bisection already exists. Reimplementing generic delta debugging would
  duplicate work. The missing granularity is independent edits within one file,
  plus transactions for changes that must move together across files.
- The tests passed before investigation (208), despite the main-block deletion.
  Additional known risks remain: the dictionary-ladder rule documents changed
  behavior on unhashable inputs; `_boolish` treats rich comparisons as always
  returning bool; API extraction is not a complete behavior contract.
- The corpus runner could crash aggregating setup failures, dropped the LOC of
  failed baseline tests, could not receive a model backend, and labeled a
  deterministic percentage "audited" without running an independent audit.
  Those accounting and model-wiring issues are repaired in this change.

The rerun after removing main-block deletion accepted 263 lines from boltons
(2.94%, previously 320 lines), 32 from more-itertools (1.21%), and zero from
yocto-queue. P-limit failed its unchanged baseline with
`ERR_INVALID_PACKAGE_CONFIG`; both Rust clones failed with disk-quota errors.
The saved report therefore has only 3/6 valid projects and is explicitly
incomplete. Its 2.43% partial aggregate is not comparable to the old six-project
2.58%. Dependency versions and environment capacity remain reproducibility
limitations; pinning source commits alone is insufficient.

## Where the remaining lines are (2026-09-05)

Measured on the reduced Python trees after the assignment-packing, single-use
temp, second-ruff-pass and call-window outlining changes. Percentages are of
the canonical code LOC that remains.

- Continuation lines of statements the formatter has to wrap (long `if`
  conditions, calls, literals): 20-45% per project. Nothing semantic shortens
  them; pyupgrade alone keeps 451 lines only because magic trailing commas
  pin its signatures and literals exploded. Stripping those commas is a
  formatting change and is deliberately not a rule, even though the metric
  would count it.
- `def` headers, `if` headers and `return` statements: about a third. Early
  returns (`if c: return x` followed by more code; 267 sites) have no
  one-line form.
- Assignments: a quarter. Packing already takes every run whose packed line
  fits 88 columns and whose later right-hand sides do not read earlier
  targets; the width limit, not safety, declines most of the rest.
- Levers measured and declined as golf rather than simplification: joining
  consecutive call statements into a tuple expression (93 lines), a generic
  deferred-raise helper for two-line guards (about 90), deleting string
  statements used as developer notes (118), joining implicitly concatenated
  literals (55).
- Levers measured and found too small to build: inlining single-call private
  helpers (16 lines under safe conditions), dead private constants (2, both
  compatibility aliases), `append` runs on fresh lists (3), Clippy `--fix`
  on the Rust crates (0).

Peephole rules therefore plateau around 8% weighted on this corpus; the
verdict above stands.

## The shadow oracle (2026-09-06)

Built after the ceiling analysis, as the first step of the "strengthen the
verifier before the proposer" plan. Every Python gate now re-runs the suite
with each rewritten function's original body executing alongside the
rewrite in the same module namespace (`less_code/shadow.py`). On boltons it
found no wrong rewrite among 263 rewritten functions (103 exercised, 7618
compared calls), and every mismatch it raised while being built was an
artifact of copying arguments: `datetime.now`, freezegun retyping copies,
bare `object()` sentinels compared by identity, `list`/`dict` subclasses with
instance state that `__reduce_ex__` restores twice, and a `lambda:
self.cache` closure reaching the real object. Each became a rule for what the
oracle refuses to judge. Two consequences for the model plan:

- Model proposals can be verified per function against recorded calls, not
  only against assertions, so the acceptance rate becomes a measure of the
  model rather than of test coverage.
- Roughly 40% of rewritten functions are exercised by the suites; the rest
  need generated inputs, which is the narrow, low-risk job proposed for a
  local model.

## Tactics worth adopting

| Priority | Change | Existing tool / evidence | Acceptance experiment |
|---|---|---|---|
| 1 | Validate semantics beyond existing examples | Hypothesis differential tests; mutmut to find weak test oracles | Compare original and candidate returns, exceptions, mutations, iterator consumption, and I/O on identical generated inputs; retain minimized counterexamples |
| 2 | Discover removals through entrypoint reachability | Knip for JS/TS; language-aware imports and packaging entrypoints for Python | Audit unused-file/export findings on applications and libraries separately before enabling edits |
| 3 | Preserve structure and scope during edits | LibCST metadata and codemods for Python | Port one productive but fragile rewrite; compare preserved comments, rejected candidates, and gate cost against current AST splicing |
| 4 | Give a capable existing model useful context | Bounded symbol plus imports, relevant callers/tests, and verifier feedback | Static-only vs one proposal vs three attempts on identical pinned projects, budgets, and independent tests |
| 5 | Simplify across symbols | Clone detection and model proposals with a host-owned edit set | Remove duplicated internal implementations; require all call sites to pass as one transaction |
| 6 | Distill only after search succeeds | TRL SFT + PEFT, later preference training if useful | A trained smaller model must beat its untrained base on held-out repositories at the same evaluation budget |

Knip builds a graph from configured entrypoints; missing dynamic or framework
entrypoints can produce incorrect unused findings. Integrate its report before
autofix, and preserve package exports by default.
[Knip architecture](https://knip.dev/explanations/how-knip-works),
[fix capabilities](https://knip.dev/features/auto-fix).

LibCST retains formatting details and provides scope/qualified-name metadata.
Use it for transformations that actually need those capabilities, rather than
porting the entire rule library up front.
[LibCST](https://libcst.readthedocs.io/en/latest/),
[codemods](https://libcst.readthedocs.io/en/latest/codemods.html).

Hypothesis includes support for generating equivalence tests; mutmut measures
whether tests detect deliberate changes. Neither proves equivalence. Stateful
objects, environment access, concurrency, and iterator behavior need explicit
strategies and observations; comparing only return values is insufficient.
[Hypothesis integrations](https://hypothesis.readthedocs.io/en/latest/reference/integrations.html),
[mutmut](https://mutmut.readthedocs.io/en/latest/).

Keep Ruff as an inexpensive proposal source, but distinguish its safe and unsafe
fixes. Ruff explicitly permits behavior changes in unsafe fixes. Passing a suite
does not upgrade them into proven transformations.
[Ruff fix safety](https://docs.astral.sh/ruff/linter/#fix-safety).

C-Reduce is useful precedent for transformation search with an executable
acceptance predicate. Its goal is reducing a test case while retaining a
specific property; importing its deletions directly would not preserve an
application's complete behavior.
[C-Reduce design](https://blog.regehr.org/archives/1678).

## Proposed search and verification loop

1. Establish an unchanged baseline: source revision, dependency/tool versions,
   supported runtime versions, test commands, entrypoints, and measured code.
2. Rank opportunities by estimated removable code, duplication, affected
   callers, and strength of relevant tests. Include public function bodies
   only after their declarations/decorators and language-specific contracts
   have stronger checks. Public API preservation does not require freezing
   implementations, but the current extractor is too weak to assume safety.
3. Generate a small host-bounded patch with rationale and relevant context.
   Start with one proposal and compare a bounded retry policy experimentally.
4. Check syntax, documentation, API, and configured style first. Run targeted
   differential checks, then the full original suite. Reject new runtime or
   dependency costs unless the project contract permits them.
5. Return the precise rejection reason to a retry. Cache equivalent candidates
   and re-plan against the current accepted tree so offsets cannot go stale.
6. Record both accepted and rejected candidates, their evidence, and costs.
   Perform an independent final evaluation; its tests must not enter prompts
   or retry feedback. Review accepted patches for comprehensibility.

Measure LOC, tokens, changed files, dependencies removed, runtime, attempts,
test seconds, model cost, and independently detected regressions. Do not turn
these into an arbitrary weighted reward yet. Report tradeoffs directly; a
shorter implementation that is slower or harder to understand may be worse.
Avoid accepting syntactic compression solely because a formatter allows it.

## Training decision and concrete data plan

First establish whether an existing strong model with context and execution
feedback produces useful additional reductions. Training has no demonstrated
benefit here until that experiment yields trustworthy examples. No GPU job,
model download, or paid inference was started in this investigation.

Record each attempt as: repository and revision, license/provenance, task and
context, before/after patch, model/version/settings, every gate result, precise
failure category, canonical LOC/token deltas, elapsed time, and inference cost.
Keep source and tests tied to a reproducible environment. Split by repository
and repository family *before* generation; random function splits leak style
and near-duplicate code. Exclude held-out evaluation artifacts from training.

Use independently validated reductions for supervised fine-tuning of an existing
code model with LoRA. Include justified no-change cases. Preference pairs must
share a task and favor correct, readable solutions over shorter incorrect
ones; a gate timeout is unknown, not a semantic negative. Start with hundreds
of manually audited examples to estimate label quality before scaling data
generation. This sample size is an engineering starting point, not a proven
training threshold. Choose model size only after measuring available hardware,
context needs, inference cost, and the untrained baseline.

SWE-smith demonstrates scalable execution-grounded training-data generation
(50,000 tasks across 128 repositories), but its tasks repair introduced bugs;
they are not a ready-made code-reduction training set. Borrow the reproducible
environment and validated-trajectory approach, not its benchmark result as an
expected reduction score.
[SWE-smith](https://arxiv.org/abs/2504.21798).

TRL supports supervised and preference training and PEFT integration, so there
is no reason to build a trainer here. Only integrate it when the data and
baseline justify the experiment.
[TRL](https://github.com/huggingface/trl/blob/main/README.md).

Do not use a model's equivalence judgment as the correctness oracle. SeqCoBench
found substantial weaknesses in semantic-equivalence discrimination in its
evaluated models. That result motivates executable evidence; it does not prove
that all subsequent models have identical limitations.
[SeqCoBench](https://aclanthology.org/2025.findings-naacl.382/).

## Next decisive experiment

Use a frozen development set containing real applications, duplicate-heavy
internal modules, and mature libraries, plus a separate held-out set selected
before inspecting reduction yield. Run static-only, context-aware proposals,
and bounded feedback search. Compare per-project results and cost, not just
weighted aggregate LOC. Keep the six existing projects as regression anchors.

Proceed to training only if search produces useful independently checked
reductions, and cheaper inference is a measured bottleneck. If the verifier
rejects most proposals for style or documentation, fix editing fidelity first;
if tests fail, improve context and transformation preconditions; if there are
no opportunities, change the product's target workload rather than selecting
benchmarks to flatter the score.

## Comma collapse, consented deletion, and an oracle false-positive class (2026-09-13)

The 2026-09-11 audit measured where the remaining lines are: dead code by
reference scan is ~0 everywhere; duplicates ~250 LOC (boltons only); magic
trailing commas 519 LOC on the Python corpus (pyupgrade 454, humanize 35,
more-itertools 19, boltons 11); and ~30% headers / ~20% jump+import /
15-25% wrapped continuations are structurally irreducible. Deterministic
rewriting plateaus near 9-10% with commas; per-symbol micro-rewrite search
had added 26 lines on 21,854 LOC. The response shipped in two tracks.

**Track A — comma collapse.** Removing a magic trailing comma is a
semantically null source edit that lets the canonical formatter collapse the
block. It is its own tokenizer-based layer, gated like any rewrite and
reported separately (`comma_collapse_loc`), never inside the semantic
figure. The 2026-09-13 re-baseline lands the audited commas almost exactly:
pyupgrade 8.2% -> 17.13% (+454 LOC), humanize 7.1% -> 11.61% (+35), boltons
7.29% -> 7.42% (+11). Weighted corpus: **21872 -> 20010 = 8.51%** (was
6.17%), 12/12 valid.

**The oracle bug the comma layer exposed.** Two full runs reverted
more-itertools' comma layer with "shadow oracle mismatch in more.py:
gray_product (iterator-item)" while the same comparison passed at end of
run. Diagnosis, from evidence: `test_vs_product` feeds gray_product a set
literal; `deepcopy` re-inserts elements in iteration order, which lands in a
different hash-table layout, so the copy ITERATES differently (verified per
hash seed: 5 of 24 seeds diverge for the fixture's set); the rewrite
consumes the live set while the shadowed original consumes the copy, and the
lazy iterator comparison is order-sensitive — the recorded payload shows
tuples differing only at the set's position (`[3] ('j' vs 'i')`). Set
iteration order is not part of a value's meaning, so the fix is a soundness
preserving narrowing: set copies are now order-faithful (`set.copy()`
duplicates the table), an order-unfaithful copy is refused like any
unfaithful copy, iterator mismatches get the eager path's
second-original-run nondeterminism check, and mismatch diagnostics sort
set/frozenset element reprs. Post-fix: the comma-gate loop on the pinned
clone ran clean 7/7; the re-measured row commits the comma layer (2642 ->
2480, 6.13%). The same lottery had been silently perturbing the rules
layers (2493 vs 2494 finals across runs) — it will not miss real users.

**Track B — consented deletion.** `lc propose` mines reference-closed
groups behind nine never-propose rails (each unit-tested, including the
pyupgrade plugin-registry pattern that must yield zero), and nothing is
deleted without explicit group ids; every group is verified with the shrink
gate's primitives and reverted on any red, and proposals.json appends runs
as the audit trail. Recorded demonstration: boltons @ 967864f, library
mode — 8 groups (24 LOC, 0.27%) consented via `--apply all`, all gates
green, final suite 472 passed. Module promotion did not surface the audit's
~268 LOC of misc dev-tool modules: their symbols are public (ineligible in
library mode), and the one file with private candidates
(`misc/bench_omd.py`) dispatches them via `globals()['_do_' + action]` —
a string-dispatch shape rail 2's pre-decided shortcut (method drops plus
6+-character-prefix collisions) does not cover. That blind spot is recorded
here rather than papered over: the shortcut should eventually also refuse
names producible by prefix+literal concatenation feeding `globals()`.

The negative case is recorded too: financial_planner (single module under
`scripts/`, everything referenced from its `__main__` guard, tests and
docs) proposes zero groups in `--app` mode — and so does the same repo at
its HEAD revision, where 11 app-mode candidates all die on name-level
references from duplicated engines, tests, and implementation notes. Rails
win over targets: a zero is a result, not a failure to be tuned away.

## LOC-accounting honesty and the applier offset bug (2026-09-13, same day)

Three defects surfaced when the numbers were audited against the disk, and
all three are fixed with tests; the corpus rows were re-measured afterwards.

**The comma layer's gate measured under the wrong formatter config.** The
gate rendered proposals with the project's ruff config only when
`pyproject.toml` carried `[tool.ruff]`; a project configuring ruff via
`ruff.toml`/`.ruff.toml` fell through to the canonical-88 render, and joins
the project's own formatter would re-split were silently counted. The gate
now discovers ruff config in ruff's own precedence order and rejects such
joins before commit (reject, don't count) — unit-tested with a width-60
project whose collapsed call joins at 88 but not at 60.

**`loc_final` could disagree with the tree it reports.** Repro: shrink a
fresh humanize copy; the report claimed 775 -> 685 while `_tree_loc` of the
resulting tree said 703. Root cause: humanize's frozen test command
(`uv run --with-editable .`) makes hatch-vcs generate `src/humanize/_version.py`
(+18 LOC) during the baseline gate run — a file the start-time file list
never counted, so no in-run measurement could ever see it. (The comma layer
was exonerated: ruff format re-adds trailing commas to width-exploded
blocks, so its post-format measurement could not count a join that later
re-split; the 35 comma LOC it claimed for humanize are on disk.) Fix:
`loc_final` is re-measured over a freshly mapped tree after the last write
or clean-out pass, and report time asserts reported == disk, failing loud.
First honest corpus number: 21,872 -> 20,028 = **8.43%** — below the 8.5%
Track A target, and recorded as-is: the bar is truth, not the target. The
comma layer's own stat survives the audit unchanged (its joins were real);
what fell was humanize's total: 775 -> 703, 9.29%.

**The Track B applier did not recompute line offsets between sequential
group applications.** The boltons consented-apply run reverted 3 of 8 groups
with syntax errors at consecutive lines (76/80/84): each group was applied
against its propose-time line numbers, so an earlier group's deletion
shifted a later group's span into unrelated code. The applier now tracks the
propose-time lines each kept group removed and remaps every later span
through them before verifying the recorded text — any consent order applies,
a reverted group leaves no offset debt, and a span that genuinely moved
still fails loud as stale. Unit tests cover consecutive adjacent deletions
and a group spanning a region another group deleted. Demo re-run: 8/8
groups applied in one invocation, gates green, 8,967 -> 8,943 canonical LOC
= 0.27% deleted; group-claimed LOC now equals disk LOC exactly.
