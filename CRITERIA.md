# Acceptance Criteria

Workspace: `/home/user/code/less-code`. The tool shrinks a Python /
JavaScript / Rust codebase using Ruff and a focused candidate rewrite rule
library, all gated by the frozen test suite and a public-API check.

## C1 tool
`lc` CLI exposes `analyze`, `shrink`, `report`, `corpus`. Gate:
`uv run pytest -q` exits 0; `uv run lc --help` exits 0.

## C2 shrink — python fixture
`fixtures/py/` (canonical code-LOC >= 300) shrinks
by >= 25% via `lc shrink`, with frozen tests green, public API
preserved, and the exact multiset of source comments + docstrings
preserved. Report cites LOC before/after per layer.

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

* No bundled LLM training or GPU requirement. The tool runs on CPU with
  optional external static tools and accepts bounded model proposals.
* No whole-file semantic rewrite by an LLM.
* Formatting as a reduction strategy is excluded — counting is done
  after canonical formatting at a fixed width, so joining lines or
  minifying buys nothing.
* The docs gate enforces the multiset contract documented in
  README.md (no comment or docstring added or removed). It does not
  pin column or line number, so a reformatting pass (prettier /
  rustfmt) is free to move comments as long as every comment and
  docstring in the source survives.
