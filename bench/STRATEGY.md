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
