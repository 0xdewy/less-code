# less-code

Shrink a Python / JavaScript / Rust codebase using the existing static
analysis ecosystem plus a focused semantic-preserving rule library,
gated by the frozen test suite and a public-API check.

```
L0  canonical formatter (not counted as reduction)
L1  external static tools: ruff / snapshot-isolated clippy
L1b rule library: AST semantic-preserving rewrites
L1c guard-block outlining: project-wide repeated guards -> helper
L2  optional per-symbol LLM rewrites
```

A reduction is accepted only if the frozen suite stays green, the public
API and documentation are preserved, and code-LOC (after canonical formatting) shrinks.
Everything else reverts.

## Architecture

The pipeline runs each layer independently and gates it with the frozen
test suite: applied, tested, kept iff `tests_ok AND api_ok AND loc_down`,
otherwise reverted (snapshot/restore). A misfiring layer costs only its
own yield.

**Why this works:**
- Static tools (ruff and clippy) are mature, free, and get the
  bulk of provably-safe deletions.
- The rule library covers semantic-preserving rewrites no general tool
  covers (`if-elif` -> `dict.get`, accumulator -> `sum()`,
  manual max -> `max(key=..., default=None)` with sentinel refusal, etc.).
- Guard-block outlining factors repeated `if X: raise` patterns into
  one shared `_check` helper across an entire project.
- An optional LLM backend handles residual per-symbol simplifications; every
  proposal passes the same formatter, tests, API, and documentation gates.

## Install & quickstart

```bash
uv sync                                # python deps (pytest, ruff)
uv run lc --help

uv run lc analyze path/to/code         # map + LOC baseline
uv run lc shrink  path/to/code         # shrink in place
uv run lc shrink  path/to/code \
         --copy-to /tmp/work           # shrink a copy instead
uv run lc report --json shrink-report.json
uv run lc corpus                         # six pinned real projects
```

External tools are picked up automatically if installed:
- Python: `ruff`
- JavaScript / TypeScript: `prettier` (via `npx`) plus explicit AST rules
- Rust: snapshot-isolated `cargo clippy --fix`, `cargo fmt`

## CLI

| command | purpose |
|---|---|
| `lc analyze <path>` | language, source/test files, canonical code-LOC baseline |
| `lc shrink <path>` | run the layered pipeline; report per-layer yield |
| `lc report --json <file>` | render a shrink report as markdown |
| `lc corpus` | clone and score the pinned Python/JavaScript/Rust corpus |

## Safety

* Every candidate change is gated by the frozen test suite + a public-API
  surface check. A red suite reverts the layer that caused it.
* Each candidate is counted after canonical formatting
  (`ruff format` / `prettier --print-width 88` / `rustfmt` at width 88),
  so joining lines or minifying buys nothing. When the formatter is
  absent, raw LOC is used and the report shouts about it.
* Every reduction layer preserves the exact multiset of source comments and
  docstrings. Documentation-losing candidates are reverted and score zero.
* `shrink` always restores the tree on `Ctrl-C` (signal handler).

## What it shrinks, in priority order

1. **Dead code** — unused top-level defs/classes (Python, JS, Rust via
   the static pass and the external tools).
2. **Mechanical simplifications** — `if c: return True/return False`
   -> `return c`, sorted/append/sum loops -> `sum()`/`sorted()`/
   comprehensions, manual max/min -> `max(key=..., default=None)`
   (numeric sentinels declined because `-1` is not provably equivalent
   to `default=None`).
3. **Repeated guards** — three or more identical `if X: raise ...` blocks
   across function bodies become one module-level helper, each site a
   one-line call. The helper travels along existing import edges, never
   introducing a new dependency.
4. **External tool yield** — whatever `ruff` / `clippy --fix` /
   the language-specific static tools find.
5. **Optional model proposals** — bounded per-symbol rewrites supplied by
   `--ml-cli`, accepted only when every normal gate passes.

## What it does NOT do

* No LLM training or bundled model. The deterministic reducer runs without
  one; `--ml-cli` can connect a model for bounded per-symbol proposals.
* No whole-file semantic rewrite by an LLM.
* No doc-eating. No comments are removed by the deterministic layers.

## Repo layout

```
less_code/    the tool        tests/    unit + end-to-end tests
fixtures/     py / js / rs demo targets
bench/        pinned real-project corpus + weighted post-formatter baseline
CRITERIA.md   acceptance criteria for the build
```
