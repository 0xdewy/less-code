# less-code

Hybrid **static + LLM + RL (GRPO)** lines-of-code reduction for Rust /
JavaScript / Python, with a hard verify gate: **a reduction is accepted only
if the frozen test suite stays green, the public API is preserved, and
code-LOC shrinks.** Everything else reverts.

## Architecture

```
L0  normalize (format-only; not counted as reduction)
L1  static provably-safe pass
      python: AST dead-code/unused-symbol removal, `ruff check --fix` safe
              tier automatically and the unsafe tier as its own gated layer
              (LOC-reducing families: RET/SIM/C4/PIE/UP/PLR1/PERF/FURB), plus
              unreachable-code removal after `return`/`raise` — all test-gated
      rust:   cargo fix + clippy --fix (compiler-verified), then the
              `clippy::pedantic`/`::complexity` tier as its own gated,
              tree-restoring step (on the rs fixture it ADDS 19 lines —
              `#[must_use]`, `# Panics` docs — so the size gate drops it),
              then dead `pub` item removal — brace-matched extraction incl. doc comments and
              attributes, kept only if no project file (tests included)
              mentions the name
      js:     dead exports via import-graph scan, cross-checked with
              `npx knip` when it is available (exports only — never
              knip's unused-*files* report, which would delete
              `tests_hidden/`), plus non-exported top-level functions
              and consts nothing in the project references
L1b rule library (less_code/rules.py): deterministic, semantics-preserving
      AST rewrites, applied file-wide and gated by the frozen suite
      python: `if c: return True/return False` -> `return c`;
              append-loop -> comprehension / `list()`, including an `if`
              guard as a comprehension clause and a twice-read temp bound with
              a walrus in that clause when it is the guard's first-evaluated
              name (so evaluation order is provably unchanged);
              `t = 0` + `+=` loop -> `sum()` (float start preserved);
              fresh-list + `.sort()` -> `sorted()`;
              `try/except X: raise` -> the try body;
              `if x == "A": return 1 / elif ... / return d` ladders ->
              `{...}.get(x, d)` (one hashable non-bool key type only);
              ordered threshold ladders -> a lazy `next(...)` tuple scan,
              which keeps the comparisons and their order exactly;
              a `None`-sentinel manual max loop -> `max(it, key=..., 
              default=None)` — a numeric sentinel is declined, because
              `-1` is not provably equivalent to `default=None`
      on red the gate narrows rule by rule, keeping only what stays green
L1.5 audit: mutation score of the test suite (trust oracle)
      built-in mutation engine (py: AST, js/rust: masked token swaps)
L2  LLM semantic reduction, verify-gated
      **per-symbol proposals are the default**: the model rewrites ONE
        top-level symbol at a time, biggest first (python via ast, js/rust via
        the brace matcher), given that symbol, a signatures-only map of the
        rest of the file and the test spec. Each proposal goes through the
        same gate, so one budget buys ~20 independent bets instead of one
        all-or-nothing whole-file gamble
      **a big python class is decomposed into its methods** (`Class.method`
        units over a 40-LOC threshold), each spliced over its own ast span,
        prompted with the class's other method signatures and `__init__`'s
        full body; the class docstring and attributes are outside every
        method span and so are never touched
      **duplicate-group proposals** (less_code/dedup.py) are the third
        granularity: near-duplicate symbols/methods are found project-wide
        (comments stripped, literals folded, identifiers erased, difflib
        ratio >= 0.6), and each group gets ONE proposal — a shared
        `_`-prefixed helper plus minimal rewrites of every member — applied
        as a single MULTI-FILE candidate through a gate that snapshots and
        restores every touched file. This is the only shape that can merge
        `csv_escape_row`/`_owned`/`_rows` or six pasted SKU validators
      an optional whole-file sweep runs last with hunk salvage on its rejects
      local ollama (qwen2.5-coder) or any OpenAI-compatible endpoint;
      test suite included in prompt as behavior spec; per-attempt feedback —
      a test failure *and* an `api-changed` rejection's concrete
      missing/changed/added symbol list both go into the next prompt;
      hard LLM-call budget for shared-GPU machines
      pre-gates (cheapest first): not-smaller -> parse (ast/node --check/
        cargo check) -> API surface -> frozen tests, + a content-hash cache
        so the same tree is never tested twice
      hunk decomposition: a rejected whole-file rewrite is split at top-level
        symbol boundaries and delta-debugged, so the correct 2 of 3 functions
        are accepted instead of the whole candidate being thrown away
L3  GRPO-trained reducer (Qwen2.5-Coder QLoRA, 8GB-friendly)
      reward = frozen-test gate x LOC delta - anti-hack penalties
```

**Metric**: code-LOC is counted *after canonical formatting* (`ruff format` /
`prettier` / `rustfmt` at width 88), so joining lines or minifying buys
nothing; the report records whether a formatter was available, plus token and
AST-node counts as secondary metrics.

**Safety**: each fixture carries a `tests_hidden/` suite that is excluded from
the prompt spec and from the frozen verify gate. Only `lc bench` runs it, as
an independent check that a reduction did not change behaviour the visible
suite failed to pin.

Research basis: `docs/research.md` (synthesis; 53 cited sources, incl. the
verified gap that no published GRPO-for-LOC-reduction work exists).

## Quickstart

```bash
uv sync
uv run lc analyze fixtures/py            # map + LOC baseline
uv run lc audit fixtures/py              # mutation score of the suite
uv run lc reduce fixtures/py \
    --model qwen2.5-coder:7b \           # needs: ollama serve
    --attempts 2 --max-llm-calls 6 \
    --num-ctx 16384 --out report.json
uv run lc report --json report.json      # markdown summary
uv run lc bench --static-only            # all fixtures + hidden tests, no GPU
```

`lc bench [dir]` reduces every fixture under `dir` (default `fixtures/`) in a
scratch copy — the repo is never mutated — runs the hidden suite afterwards,
prints a markdown table and appends one JSON row per (fixture, config, commit)
to `bench/results/<timestamp>.jsonl`: LOC before/after static/final, static
and hybrid %, tests/api/hidden green, LLM calls and wall seconds. **Rows are
appended as each fixture finishes**, so an interrupted run still leaves the
fixtures that completed. A row whose language has no formatter installed is
marked `"metric": "raw"`, shouted about on stderr and printed as `**RAW**` in
the table — raw physical lines are not comparable with canonical ones.

`--static-only` runs L1 only (no GPU). `--max-llm-calls N` bounds GPU time.
`--fixture NAME` benches a single fixture.

## GRPO

```bash
uv run python grpo/build_dataset.py      # CPU: verify-gated samples from fixtures
uv run python grpo/train.py --validate-config   # CPU: config check, no model load
uv run python grpo/train.py --steps 10   # GPU: QLoRA smoke (0.5B, ~minutes)
```

Dataset rows: `prompt` (instruction + verbose unit + frozen tests as spec),
`unit_source`/`unit_loc` for reward normalization. Reward (grpo/rewards.py):
`gate × (1 + 0.5·loc_delta) − penalties` with api-preservation,
minification, and degenerate-output guards; tests run on CPU inside the reward.

## Repo layout

```
less_code/   the tool        tests/      153 tests (unit + integration + reward)
fixtures/    py/js/rs demo targets (mutation-scored suites + tests_hidden/)
bench/       results/*.jsonl — one bench row per fixture/config/commit
grpo/        dataset builder, reward fn, TRL trainer
docs/        research synthesis + sub-reports
iterations/  goal-loop records    status.json  durable run state
CRITERIA.md  fixed acceptance criteria for the build
```

## Status vs goal

research ✅ · tool ✅ · grpo scaffold ✅ (66 samples, config validated) ·
**js demo (C4) ✅ 25.96 % canonical hybrid**, tests/API/hidden green
(`docs/evidence/reduce-js-7b.md`) · py and rs still short of the 25 % bar —
**py 7.4 %, rs 5.1 %** at 7B, tests/API/hidden green, 1 LLM acceptance in 36
proposals across two model sizes and three granularities
(`docs/evidence/reduce-py-rs-iteration09.md`,
`iterations/09-methods-dedup-and-settlement.md`) · review pending py/rs demos.
