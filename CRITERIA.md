# Acceptance Criteria

Workspace: `/home/user/code/less-code`. The tool shrinks a Python /
JavaScript / Rust codebase using existing static tools (ruff, clippy,
snapshot-isolated clippy) plus a focused semantic-preserving rule
library, all gated by the frozen test suite and a public-API check.

## C1 tool
`lc` CLI exposes `analyze`, `shrink`, `report`, `corpus`. Gate:
`uv run pytest -q` exits 0; `uv run lc --help` exits 0.

## C2 shrink — python fixture
`fixtures/py/` (canonical code-LOC >= 300) shrinks
by >= 25% via `lc shrink`, with frozen tests green and public API
preserved. Report cites LOC before/after per layer.

## C3 shrink — js fixture
Same shape for Node (`node --test`). Fixture >= 300 LOC,
>= 25% reduction, tests green.

## C4 shrink — rust fixture
Same shape for Rust (`cargo test`). Fixture >= 300 LOC,
>= 25% reduction, tests green.

## Out of scope / accepted trade-offs

* No bundled LLM training or GPU requirement. The tool runs on CPU with
  optional external static tools and accepts bounded model proposals.
* No whole-file semantic rewrite by an LLM.
* Formatting as a reduction strategy is excluded — counting is done
  after canonical formatting at a fixed width, so joining lines or
  minifying buys nothing.
