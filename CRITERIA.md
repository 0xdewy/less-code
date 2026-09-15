# Acceptance Criteria

Workspace: `/home/user/code/less-code`. The tool shrinks a Python /
JavaScript / Rust codebase using Ruff and a focused candidate rewrite rule
library, all gated by the frozen test suite and a public-API check.

## C1 tool
`lc` CLI exposes `analyze`, `shrink`, `report`, `corpus`. Gate:
`uv run pytest -q` exits 0; `uv run lc --help` exits 0.

## C2 shrink — python fixture
`fixtures/py/` (canonical code-LOC >= 300) shrinks
by >= 14% via `lc shrink`, with frozen tests green, public API
preserved, and the exact multiset of source comments + docstrings
preserved. Report cites LOC before/after per layer.

The bar was 25% until 2026-09-11, when `merge-imports` (`import a, b`
line-joining), `guard-call` (`if c: call()` -> `c and call()`), and the
guard-outlining subsystem were removed as style-hostile metric gaming;
the fixture's honest deterministic yield with the remaining rule library
is 14.9%.

## C3 shrink — js fixture
`fixtures/js/` (canonical code-LOC >= 300) shrinks via `lc shrink`
with frozen tests (`node --test`) green, public API preserved, and the
exact multiset of source comments preserved. JavaScript and Rust lack
Python's lexical-scope-sensitive lint ecosystem (ruff / clippy
pedantic / etc.), so the achievable deterministic yield is lower than
the Python bar; the criterion is a measurable reduction with no
regressions, not a specific percentage.

## C4 shrink — rust fixture
`fixtures/rs/` (canonical code-LOC >= 300) shrinks via `lc shrink`
with frozen tests (`cargo test`) green, public API preserved, and the
exact multiset of source comments + doc-comments preserved. Same
qualitative criterion as C3.

## C5 corpus
`lc corpus` against the pinned multi-project manifest at
`bench/corpus.toml` produces `bench/baseline.json` + `bench/BASELINE.md`
that record, for each project: starting canonical LOC, final canonical
LOC, reduction percentage, gate status. Invalid attempts (any failed
gate) score zero reduction.


## C6 context-aware model experiment

`shrink` and `corpus` accept `--ml-attempts` from one to three. The host ranks
eligible symbols, supplies bounded source/test context, and refreshes
positions after accepted changes. Rejected proposals receive gate feedback;
duplicate proposals stop retries. Declarations and the existing gates remain
protected; public bodies are eligible but their declarations remain frozen.
JSON reports record attempts, prompts, outcomes, timing, and LOC.
Executable regression tests must demonstrate recovery after a test failure,
stale-span prevention, hidden-test exclusion, and rejection of changed
declarations. An optional corpus `audit` command runs on baseline and final
trees without influencing model search; audit failures score zero reduction.
These traces are not automatically validated training examples.

## C7 comma layer

The magic-trailing-comma collapse is its own gated layer and its own stat:
`comma_collapse_loc` plus a `comma-collapse` layer record, never inside the
semantic reduction figure. It is Python-only and tokenizer-based (brackets
inside strings and f-strings never open or close anything); a comma is
removed only when it is the last token on its line, nothing but whitespace
follows it, and the matching closer starts a later line. The collapse goes
through the same gates as every rewrite (LOC, style, docs, API, tests,
shadow oracle). The corpus was re-baselined after it landed: the Python rows
gain the audited comma LOC (pyupgrade +454, humanize +35, boltons +11).

Amendment (2026-09-13, disk-truth audit): the comma layer's gate now renders
candidates under the project's own ruff config (`ruff.toml`/`.ruff.toml`
included), so joins the project's formatter would re-split are rejected
before commit - reject, never silently count. `loc_final` is re-measured
over a fresh file map after the last write, and report time asserts
reported `loc_final` == disk LOC, failing loud on any drift. This exposed
humanize's build-generated `_version.py` (+18 LOC mid-run, invisible to the
frozen start-time file list); the honest humanize row is 775 -> 703
(9.29%, comma contribution unchanged and real), and the corpus lands at
8.43% - below the 8.5% Track A target. Recorded as the honest number: the
bar is truth, not the target.

## C8 propose

`lc propose` never deletes without explicit group ids (`--apply 1,3` or
`--apply all`); `--yes` does not exist. Every never-propose rail has a unit
test — dynamic discovery (the pyupgrade plugin-registry pattern yields zero
candidates), string dispatch, decorators, dunder surface, entry points
(noxfile/scripts/bin/console-script modules), module `__getattr__`,
side-effectful assignments, test references, and non-private symbols outside
app mode. A proposals file older than the tree hash is rejected (exit 3:
re-run lc propose). Each applied group is verified with the shrink gate's
primitives and reverted with its gate reason on any red; the API delta is
disclosed per group, and a violation naming anything outside the group
disqualifies it. Originals are sacred: proposals run on a sibling copy
(`<path>-proposals`) unless `--in-place`.

Amendment (2026-09-13, plan author): Track B acceptance is an end-to-end
consented deletion on a real subject — at least one group applied with all
gates green and the consent recorded — not a specific percentage. The
recorded demonstration: boltons @ 967864f, 8 groups / 24 LOC (0.27%)
consented and applied, per-group gates green, final suite 472 passed;
financial_planner is the documented zero-proposal negative case (`lc
propose --app` proposes nothing there: its only module lives under
`scripts/` and every symbol is referenced).

## Corpus validity and growth

Keep the six pinned projects as regression anchors. Select additions by real
workload characteristics, before observing reduction yield: applications,
libraries, duplicate-heavy modules, and projects with strict style settings.
Preserve difficult projects and zero-yield results. Never select for removable
script entrypoints: those are behavior, not dead code.

Setup failures must produce a failed row, not crash the benchmark. Failed
baseline tests retain their measured LOC and score zero reduction. Unknown
baselines must be reported as unmeasured; an incomplete run cannot establish a
complete-corpus result. Static and model contributions must be reported
separately. Gate acceptance must not be described as an independent audit.

The earlier 2.58% result included a rule that deleted main blocks and is not a
semantic-preservation target. See `bench/BASELINE.md` for the latest run and
`bench/STRATEGY.md` for research, proposed experiments, and training criteria.

## Out of scope / accepted trade-offs

* LLM training and whole-file rewrites are optional layers, never
  requirements: the deterministic pipeline runs on CPU with optional
  external static tools. When used, model proposals are just proposals -
  every one passes the same formatter, docs, API, test and oracle gates
  (see C9/C10 below; PLAN.md Phase 1/6).
* Formatting as a reduction strategy is excluded — counting is done
  after canonical formatting at a fixed width, so joining lines or
  minifying buys nothing. Exception: a magic trailing comma is a source
  edit, not formatting config; its collapse is gated like any rewrite and
  reported as a separate layer and stat (`comma_collapse_loc`), never
  inside the semantic reduction figure.
## C9 ml-file layer (PLAN.md Phase 1)
`lc rewrite` and the `--ml-file-cli` shrink/corpus layers: whole-file Rust
rewrites where test spans are masked (`/*__LC_FROZEN_N__*/` sentinels,
restored byte-exact by the host - a dropped/reordered/duplicated sentinel is
a reject), declarations and docs are host-verified, and the full cascade
(syntax, symbol set, docs, width-88 AND project-rustfmt LOC, API, style,
`cargo check`, frozen suite, differential oracle) gates every candidate.
Reported as its OWN stat (`ml_file_loc`), never inside the deterministic
figure. Model digests and prompts are recorded per attempt.

## C10 learning track (PLAN.md Phase 6)
Training never touches the exam: `training/anticontamination.py` scans every
mined pair and dataset file for the four benchmark crates' names, repo URLs
and pinned commits; a hit fails the build. `role = "eval"` crates are held
out of every fine-tune. Promotion of a trained proposer onto the benchmark
requires the held-out F3 win at equal call budget
(`training/EVAL.md`); otherwise the trained model is recorded as a negative.

## C11 test compaction (PLAN.md Phase 8, only if built)
Mutation-certified test compaction requires the owner's explicit consent,
`cargo-mutants` kill-superset safety (candidate kills every mutant the
original tests kill, same mutant IDs; unmeasured mutants excluded from both
sides), a consent flow like `lc propose`, and a separate stat
(`test_compaction_loc`) - never inside the semantic figure.

* The docs gate enforces the multiset contract documented in
  README.md (no comment or docstring added or removed). It does not
  pin column or line number, so a reformatting pass (prettier /
  rustfmt) is free to move comments as long as every comment and
  docstring in the source survives.
