# click 8.5.1 — Sprint A validation run (scheduling + trust + few-shot)

- baseline: pristine `36baa15`, editable-installed; `qwen2.5-coder:7b` on a
  shared RTX 3070 (another agent held ~5 GB — generation was ~5x slower
  than the uncontended runs, which degraded reply quality)
- result: **6973 -> 6931 = 0.60%** (static 0.56% + LLM 0.04%), tests_ok,
  api_ok, zero docstring/comment lines lost
- budget: 40 calls, `--trust trust.json` (per-file mutation scores),
  `--skip-files _compat.py,_winconsole.py,_textwrap.py,__init__.py`
- outcomes: 1 accepted, 14 docs-lost, 13 duplicate-symbol, 9 not-smaller,
  1 tests-failed, 2 bad-dedup-reply, 1 budget-exhausted

## What Sprint A changed, measured against run 3 (click-hybrid-6926)

| metric | run 3 | run 4 | mechanism |
|---|---|---|---|
| files the LLM reached | 1 (core.py) | **12 of 13** | round-robin sweeps + size x trust ranking |
| budget spent before stopping | 40/40 | ~35, stopped by the acceptance-free fixpoint | per-sweep fixpoint + cross-sweep symbol exclusion |
| doc-eating accepted | 0 | 0 | docs gate (14 proposals rejected) |

`lc audit --max-mutants 5` produces the trust map (`trust.json` here):
core/parser/exceptions 1.0, shell_completion/termui/testing/utils 0.8,
types 0.6, `_textwrap` 0.4, `_compat` 0.2, `_winconsole` 0.0 — the skip
list is the <0.5 tail.

## What did NOT move, honestly

- accept rate: 1/40 (2.5%) vs run 3's 12.5% — GPU contention degraded the
  7B's replies (whole-file duplicate-symbol answers doubled); the sprint's
  ≥25% exit gate FAILED this run. Run 3 already showed the ceiling: at 7B,
  free-form proposals on mature code are ~5% productive with docs off the
  table. The proposer, not the search, is the bottleneck — Sprint C's
  frontier-model ablation is the decision experiment.
- static-only 0.56% vs the ≥1.5% exit gate: the estimate assumed all 15
  else-after-terminator sites + mechanical dedup would land. Soundness
  halved the else sites (elif arms are not collapsible) and every click
  dedup group declines legitimately (differing kwarg keys / attribute
  names / async). The gates that rejected them are the same ones that
  would have caught behavior breaks — that is the system working.

## Recipe

as `docs/evidence/click-hybrid-6926/REPRODUCE.md`, plus:

```bash
PATH=<less-code>/.venv/bin:$PATH lc audit . --max-mutants 5 --out trust.json
PATH=<less-code>/.venv/bin:$PATH lc reduce . \
    --model qwen2.5-coder:7b --attempts 2 --max-llm-calls 40 \
    --num-ctx 16384 --trust trust.json \
    --skip-files _compat.py,_winconsole.py,_textwrap.py,__init__.py \
    --out reduce.json
```
