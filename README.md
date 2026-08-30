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
      rust:   cargo fix + clippy --fix (compiler-verified)
      js:     dead internal exports via import-graph scan
L1.5 audit: mutation score of the test suite (trust oracle)
      built-in mutation engine (py: AST, js/rust: masked token swaps)
L2  LLM semantic reduction, verify-gated
      local ollama (qwen2.5-coder) or any OpenAI-compatible endpoint;
      test suite included in prompt as behavior spec; per-attempt feedback;
      hard LLM-call budget for shared-GPU machines
L3  GRPO-trained reducer (Qwen2.5-Coder QLoRA, 8GB-friendly)
      reward = frozen-test gate x LOC delta - anti-hack penalties
```

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
```

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
less_code/   the tool        tests/      30 tests (unit + integration + reward)
fixtures/    py/js/rs legacy-style demo targets (mutation-scored suites)
grpo/        dataset builder, reward fn, TRL trainer
docs/        research synthesis + sub-reports
iterations/  goal-loop records    status.json  durable run state
CRITERIA.md  fixed acceptance criteria for the build
```

## Status vs goal

research ✅ · tool ✅ · grpo scaffold ✅ (66 samples, config validated) ·
demos py/js/rs pending GPU time (resume commands in `status.json`) ·
review pending demos.
