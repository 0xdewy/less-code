# RESUME — runbook for the GPU machine

Everything up to commit `fa2752c` is done on CPU: research, tool (30 tests
green), fixtures, GRPO scaffold (66 samples, config validated). What remains
needs GPU: three reduce demos (C3-C5), optional GRPO smoke, review (C7).

## 0. Setup (once)

```bash
git clone git@github.com:0xdewy/less-code.git && cd less-code
uv sync                                   # python deps (pytest etc.)
ollama serve &                            # keep running
ollama pull qwen2.5-coder:7b              # 4.7GB, one-time
uv run pytest -q                          # must show 30 passed
```

## 1. Demo runs (C3/C4/C5) — ~10-15 min GPU each, run sequentially

```bash
mkdir -p docs/evidence
for lang in py js rs; do
  uv run lc reduce fixtures/$lang \
      --model qwen2.5-coder:7b \
      --attempts 2 --max-llm-calls 6 --num-ctx 16384 \
      --out docs/evidence/reduce-$lang.json
  ollama stop qwen2.5-coder:7b            # free VRAM between runs
done
```

Per-fixture gates (from CRITERIA.md): tests_ok true, api_ok true,
hybrid_pct >= 25. Each run prints progress per LLM attempt; the process
restores the tree on Ctrl-C.

- If a fixture lands under 25%: rerun that fixture with `--max-llm-calls 10
  --attempts 3` (yield compounds across runs — accepted reductions persist).
- js/rust take whole-file rewrites; python uses whole-file + focused passes.

## 2. Post-reduction evidence (CPU)

```bash
for lang in py js rs; do
  uv run lc audit fixtures/$lang --max-mutants 40 \
      --out docs/evidence/audit-after-$lang.json     # suite still strong
  uv run lc report --json docs/evidence/reduce-$lang.json \
      --out docs/evidence/REDUCTION-$lang.md
done
git add -A && git commit -m "demos: reduce + post-audit evidence" && git push
```

## 3. Optional GRPO smoke (GPU, ~10-15 min)

```bash
uv pip install torch transformers datasets trl peft bitsandbytes accelerate
uv run python grpo/train.py --steps 10
# expect: reward_mean rising above 1.0 across steps; adapter in grpo/adapter
```

Skip if the GPU is busy — C6 already passed on its CPU gates.

## 4. Review (C7) — CPU

Point a fresh reviewer agent at CRITERIA.md + docs/evidence/ + git diff
`6ef9a1a..HEAD` with only the task and criteria. Fix or refute findings,
then update status.json: criteria C3-C5 (+smoke note), state passed,
next_action null, validate with
`python3 ~/.config/opencode/skills/common/scripts/validate_state.py goal status.json`.

## Success line

All gates green => goal PASS with evidence = docs/evidence/ in this repo.
