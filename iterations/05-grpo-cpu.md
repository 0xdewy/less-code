# Iteration 5 — GRPO scaffold, CPU-only (C6)

## Attempt
Built grpo/ entirely without GPU: dataset builder (per-symbol units from the
3 fixtures, baseline-green gated, deduped), gated reward function
(frozen-test splice + api check + minification guard + LOC shaping), TRL
QLoRA trainer (0.5B, dr_grpo, beta=0, 8GB-sized) with --validate-config
CPU mode. Also: crash-safety signal handler in pipeline (kills can no longer
leave unverified candidates on disk — discovered from the aborted run), and
sys.executable for pytest subprocesses (PATH-shim flakiness).

## Evidence
- `uv run python grpo/build_dataset.py` → 66 samples (py 18, js 28, rs 20),
  exit 0 (gate: >= 20)
- `uv run python grpo/train.py --validate-config` → CONFIG OK, exit 0
- `uv run pytest -q` → 30 passed (incl. reward-gate tests: identity=+1.0,
  no-code-block/syntax-error/behavior-break=-1.0, minification penalized)
- grpo/dataset.jsonl committed

## Verdict
- C6 grpo: PASS (builder + config gates; optional GPU smoke-train is the
  only remaining extra evidence if VRAM ever allows)

## Gap diagnosis
None for C6. Remaining: C3-C5 demo runs (GPU), C7 review (after demos).
GPU-free work is exhausted; everything left needs the other machine.
