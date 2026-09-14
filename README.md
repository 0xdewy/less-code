# less-code

Shrink a Python / JavaScript / Rust codebase using the existing static
analysis ecosystem plus a focused semantic-preserving rule library,
gated by the frozen test suite and a public-API check.

```
L0  canonical formatter (not counted as reduction)
L1  static tools and language rules: ruff / JavaScript / Rust rewrites
L1b rule library: AST semantic-preserving rewrites
L1c ruff and the rule library once more over the rewritten tree
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
  `max(key=..., default=None)` with sentinel refusal, local self-defaults ->
  conditional assignments, adjacent from-import packing with execution order
  retained, consecutive independent assignments packed into one tuple
  assignment up to the canonical width, and single-use temps inlined across
  provably bound name and attribute lookups). Ruff runs a second time after
  the rule layers, since their output exposes shapes it fixes (superfluous
  else, useless trailing return).
- An optional LLM backend handles residual per-symbol simplifications; every
  proposal passes the same formatter, tests, API, and documentation gates.

## Shadow oracle (Python)

Tests check assertions, not functions, so a rewrite can be wrong and stay
green. For Python the gate therefore runs the suite a second time with every
rewritten function *shadowed*: its original body is compiled into the
rewritten module's own namespace and runs alongside the rewrite on every
call the suite makes (iterator arguments are tee'd, everything else is deep
copied). Return values, exceptions, the items of returned iterators (lazily)
and argument mutation must agree, else the layer is reverted. The oracle only
ever makes the gate stricter: whatever it cannot judge is reported as
unverified, never as a mismatch. That covers nondeterministic functions (the
original disagrees with itself on a spare copy of the input), functions with
external effects (file, process, clock and random APIs are excluded up front;
anything else whose double execution breaks the suite is bisected out),
calls whose arguments have no faithful deep copy (weak references, closures
and bound methods over shared state, container subclasses with instance
state, very large containers, and set arguments a deep copy would re-order —
a copy must iterate exactly like its source, since the comparison is
sequence-sensitive), and calls beyond a per-function budget. Iterator
mismatches get the same second-original-run check as the eager path: if the
original disagrees with itself, the call is nondeterministic, not a
mismatch. It
roughly multiplies gate time by ten on a large project. `bench/BASELINE.md`
shows, per project, how many rewritten functions the suite actually
exercised under the oracle. See `less_code/shadow.py`.

## Install & quickstart

```bash
uv sync                                # python deps (pytest, ruff)
uv run lc --help

uv run lc analyze path/to/code         # map + LOC baseline
uv run lc shrink  path/to/code         # shrink in place
uv run lc shrink  path/to/code \
         --copy-to /tmp/work           # shrink a copy instead, original untouched
uv run lc shrink  path/to/code --copy-to /tmp/work \
         --test-command '.venv/bin/python -m pytest -x -q'   # a project's own venv
uv run lc shrink  path/to/monorepo --copy-to /tmp/work \
         --test-command 'npm --prefix js test --silent'      # a package inside one
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

## Deletion proposals

`lc propose <path>` mines groups of symbols that are provably unreferenced by
anything the repository can express — including tests, docs, and config, which
are all scanned as references — and groups them so a chain of dead private
helpers is deleted together or not at all. A group carries its evidence: the
reference files (empty for a proposal), whether the frozen suite covered it
(when the optional `coverage` accelerator is available for the target's
interpreter), and the public-API delta it would disclose.

Nothing is ever deleted automatically, and nothing is deleted without explicit
group ids: `lc propose` only writes `proposals.json` and prints a table;
`lc propose --apply 1,3` (or `--apply all`) is the consent, one verification
gate per group, each reverted with its gate reason on any red. There is no
`--yes`. A proposals file older than the tree is rejected (`exit 3`), so spans
can never go stale, and the command works on a sibling copy
(`<path>-proposals`) unless `--in-place` is passed — originals are sacred.
Exit codes: 0 nothing to do / applied cleanly, 1 every requested group failed
its gate, 2 proposals awaiting consent, 3 stale proposals file.

Rails, each backed by a test: dynamically discovered packages (`pkgutil`,
`import_module`, `walk_packages`, `iter_modules`, `entry_points`,
`__subclasses__`) are never proposed, nor anything near string dispatch
(`getattr`/`globals()` drops all method candidates and 6+-character-prefix
name collisions), decorated definitions, dunder/protocol surface, entry-point
files (`noxfile.py`, `scripts/`, `bin/`, console-script modules), modules with
a module-level `__getattr__`/`__dir__`, non-literal assignments, anything a
test references, and — in library mode or any `[project]`/`setup.py`-packaged
repo — non-private symbols. A repo shaped like pyupgrade's plugin registry
yields zero candidates by construction. When nothing passes the rails, the
tool says so and exits 0 — financial_planner is the recorded negative case:
`lc propose --app` proposes nothing there because its only module lives
under `scripts/` (an entry-point directory) and every symbol it defines is
reachable from its `__main__` guard, its tests, or its docs.

```bash
uv run lc propose path/to/app                 # propose on a sibling copy
uv run lc propose path/to/app --app           # application mode (no packaging)
uv run lc propose path/to/app --apply 1,3     # consented groups only
```

## CLI

| command | purpose |
|---|---|
| `lc analyze <path>` | language, source/test files, canonical code-LOC baseline |
| `lc shrink <path>` | run the layered pipeline; report per-layer yield |
| `lc propose <path>` | mine consented deletion proposals; apply only by group id |
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
* Comma collapse is reported separately (`comma_collapse_loc` and a
  `comma-collapse` layer record), so the semantic reduction figure stays
  comparable across runs.
* `shrink` always restores the tree on `Ctrl-C` (signal handler).
* Every report states how many tests the gate ran; a zero-test gate is
  flagged as vacuous, so a green check never overstates its evidence.

## What it shrinks, in priority order

1. **Dead code** — unused top-level defs/classes (Python, JS, Rust via
   the static pass and the external tools).
2. **Mechanical simplifications** — `if c: return True/return False`
   -> `return c`, sorted/append/sum loops -> `sum()`/`sorted()`/
   comprehensions, single-use temporary inlining, same-module import merging,
   common branch-tail hoisting, JavaScript single-statement unbracing, and
   manual max/min -> `max(key=..., default=None)`
   (numeric sentinels declined because `-1` is not provably equivalent
   to `default=None`).
3. **External tool yield** — whatever Ruff and the language-specific static
   passes find.
4. **Optional model proposals** — bounded per-symbol rewrites supplied by
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
