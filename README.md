# less-code

Shrink a Python / JavaScript / Rust codebase using the existing static
analysis ecosystem plus a focused semantic-preserving rule library,
gated by the frozen test suite and a public-API check.

```
L0  canonical formatter (not counted as reduction)
L1  static tools and language rules: ruff / JavaScript / Rust rewrites
L1b rule library: AST semantic-preserving rewrites
L1c guard-block outlining: project-wide repeated guards -> helper
L2  optional per-symbol LLM rewrites
```

A reduction is accepted only if the frozen suite stays green, the public
API and documentation are preserved, and code-LOC (after canonical formatting) shrinks.
Everything else reverts.

For the effectiveness investigation, tool recommendations, and staged model
training proposal, see [the research strategy](bench/STRATEGY.md).
Passing the existing tests and API checks is evidence, not proof of semantic
equivalence. Script entrypoints are executable behavior and must survive.

## Architecture

The pipeline runs each layer independently and gates it with the frozen
test suite: applied, tested, kept iff `tests_ok AND api_ok AND loc_down`,
otherwise reverted (snapshot/restore). A misfiring layer costs only its
own yield.

**Why this works:**
- Ruff supplies Python simplification candidates; language-specific rules
  supply JavaScript and Rust candidates. These still require verification.
- The rule library covers candidate rewrites no general tool covers
  (accumulator -> `sum()`, append loops -> comprehensions, manual max ->
  `max(key=..., default=None)` with sentinel refusal, etc.).
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
uv run lc corpus                         # twelve pinned real projects
uv run lc corpus --ml-cli 'your-model-command' # compare static + model yield
```

## Model search experiment

Compare static-only reduction with one proposal and feedback-driven search:

```bash
uv run lc corpus --out bench/static.json --markdown bench/static.md
uv run lc corpus --ml-cli 'your-model-command' --ml-attempts 1 \
  --out bench/model-one.json --markdown bench/model-one.md
uv run lc corpus --ml-cli 'your-model-command' --ml-attempts 3 \
  --out bench/model-feedback.json --markdown bench/model-feedback.md
```

The command receives JSON on stdin and returns either JSON `null` (abstain) or
`{"symbol_id": "<supplied id>", "replacement": "<complete function>"}` on stdout.
It should use `system`, `source`, `context`, and `feedback` from the input when
calling its model. Logs belong on stderr. Timeout, nonzero exit, and malformed
output are reported separately from abstention.

Search ranks eligible Python functions by executable statement count
(not docstring length), and JS/Rust functions by line count, caps selection at eight
per file and 64 overall, and allows one to three attempts per function (default
three). `--ml-symbols` can lower the total budget for experiments. Symbols over
16,000 characters are skipped. Context contains up to
12,000 characters of imports and lexical source/test references; it is not a
resolved call graph. Each accepted edit refreshes the source used for subsequent
proposals. Rejected proposals receive gate feedback; repeats stop that symbol's
search. Public function bodies are eligible, but their declarations, defaults,
decorators and public API must remain unchanged. Rust inline tests stay frozen.

Shrink and corpus JSON include `ml_records`: prompts, replacements, rejection
reasons, source hashes, timings, and LOC changes. These artifacts contain source
code. They are experiment traces, **not automatically approved training data**;
`independently_validated` is false for individual proposals.

For additional regression validation, a corpus project can specify an `audit` command:

```toml
audit = ["python", "-m", "pytest", "-q", "tests_hidden"]
```

Keep those tests under `tests_hidden/`, which source discovery excludes from
prompts and the default test gate. Use the project's prepared interpreter in the
command. The audit checks the baseline, deterministic checkpoint and completed
model layer; its output never becomes retry feedback. A failing model layer is
restored to the audited static checkpoint; a failing static checkpoint restores
the original and skips model search. The returned tree is checked again. An
invalid final tree scores zero. JSON retains raw model acceptances and marks
rolled-back edits, with separate retained counts and LOC. Since audit outcomes
select checkpoints, these are validation tests, **not independent held-out evidence**.
The bundled manifest now includes boltons and fastq regression
probes derived from the first model run; `{manifest_dir}` resolves to the
manifest's directory. These were added after the recorded experiment. See
[results](bench/EXPERIMENT.md) and [model review](bench/MODEL_REVIEW.md).
The raw historical model trace is retained as `bench/model-experiment.json`;
`bench/baseline.json` remains the current static-only corpus baseline.
Run `uv run python -m bench.replay` to replay both known unsafe proposals on fresh
pinned checkouts and verify automatic rollback, without new model inference.
To make the comparison reproducible, use the same pinned
manifest, prepared dependencies, model/version, and held-out tests for all runs.

An installed local Ollama model can be used with the adapter in `bench/ollama.py`:

```bash
uv run lc corpus --ml-cli 'python /absolute/path/to/less-code/bench/ollama.py qwen2.5-coder:7b' \
  --ml-attempts 1 --ml-symbols 8 --ml-timeout 100
uv run python bench/compare.py bench/before.json bench/baseline.json
```

The comparison separates the original and expansion cohorts and only counts
improvement between matching commits, starting LOC, and passing gates. Each
project's temporary checkout is released before preparing the next project.

External tools are picked up automatically if installed:
- Python: `ruff`
- JavaScript / TypeScript: `prettier` (via `npx`) plus explicit AST rules
- Rust: `rustfmt` for measurement, `cargo test` for verification; the current
  reducer uses its own Rust rules and does not invoke Clippy.

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
4. **External tool yield** — whatever Ruff and the language-specific static
   passes find.
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
