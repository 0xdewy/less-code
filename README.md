# less-code

Hybrid **static + LLM + RL (GRPO)** lines-of-code reduction for Rust /
JavaScript / Python, with a hard verify gate: **a reduction is accepted only
if the frozen test suite stays green, the public API is preserved, and
code-LOC shrinks.** Everything else reverts.

## Architecture

```
L0  normalize (format-only; not counted as reduction)
L1  static provably-safe pass
      python: AST dead-code/unused-symbol removal (test-gated)
      rust:   cargo fix + clippy --fix (compiler-verified), then dead `pub`
              item removal — brace-matched extraction incl. doc comments and
              attributes, kept only if no project file (tests included)
              mentions the name
      js:     dead internal exports via import-graph scan
L1b rule library (less_code/rules.py): deterministic, semantics-preserving
      AST rewrites, applied file-wide and gated by the frozen suite
      python: `if c: return True/return False` -> `return c`;
              append-loop -> comprehension / `list()`;
              `t = 0` + `+=` loop -> `sum()` (float start preserved);
              fresh-list + `.sort()` -> `sorted()`;
              `try/except X: raise` -> the try body
      on red the gate narrows rule by rule, keeping only what stays green
L1.5 audit: mutation score of the test suite (trust oracle)
      built-in mutation engine (py: AST, js/rust: masked token swaps)
L2  LLM semantic reduction, verify-gated
      local ollama (qwen2.5-coder) or any OpenAI-compatible endpoint;
      test suite included in prompt as behavior spec; per-attempt feedback;
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
and hybrid %, tests/api/hidden green, LLM calls and wall seconds.

`--static-only` runs L1 only (no GPU). `--max-llm-calls N` bounds GPU time.

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
less_code/   the tool        tests/      109 tests (unit + integration + reward)
fixtures/    py/js/rs demo targets (mutation-scored suites + tests_hidden/)
bench/       results/*.jsonl — one bench row per fixture/config/commit
grpo/        dataset builder, reward fn, TRL trainer
docs/        research synthesis + sub-reports
iterations/  goal-loop records    status.json  durable run state
CRITERIA.md  fixed acceptance criteria for the build
```

## Status vs goal

research ✅ · tool ✅ · grpo scaffold ✅ (66 samples, config validated) ·
demos py/js/rs pending GPU time (resume commands in `status.json`) ·
review pending demos.
