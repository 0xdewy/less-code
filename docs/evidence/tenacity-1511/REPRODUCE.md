# tenacity @26f719d — second external repo, two compounding hybrid passes

- baseline: pristine clone at `26f719d` ("Respect enabled=False on the
  direct-call path"), editable-installed; suite 183 passed in 2.3 s
- trust: `lc audit --max-mutants 6` -> **0.81** aggregate (retry.py 1.00,
  wait.py 0.83, _utils.py 0.50, conf/_version 0.00 -> skipped)
- pass 1 (30 calls, 7B): 1533 -> 1514, LLM +0.85% on top of static 0.39%
- pass 2 (20 calls): 1514 -> 1511, LLM +0.2% — compounding works, returns
  diminish fast per pass
- **total: 1533 -> 1514+... = 1511 code-LOC = 1.44%**, tests_ok and api_ok
  on both passes, zero doc/comment lines lost
- outcomes pass 1: 4 accepted, 13 not-smaller, 5 duplicate-symbol,
  3 tests-failed, 3 docs-lost, 2 bad-dedup — 9 of 10 eligible files reached
  (click run 3 reached 1 of 13: the round-robin + trust scheduling is what
  spreads the budget)

## Recipe

```bash
git clone https://github.com/jd/tenacity && cd tenacity
git checkout 26f719d
uv pip install -e .   # into the less-code host venv
PATH=<less-code>/.venv/bin:$PATH lc audit . --max-mutants 6 --out trust.json
PATH=<less-code>/.venv/bin:$PATH lc reduce . \
    --model qwen2.5-coder:7b --attempts 2 --max-llm-calls 30 \
    --num-ctx 16384 --trust trust.json --skip-files conf.py,_version.py \
    --out reduce-pass1.json
# second pass compounds (accepted reductions persist)
```

## Reading

tenacity yields 2x click per call (1.24% vs 0.60% in one pass) because it
has real headroom: decorator-heavy code, sync/async mirror classes, and a
strong suite (0.81) that certifies more of what the LLM touches. The
mechanical dedup found nothing (the sync/async twins are async on one side
— declined soundly). Steady state at 7B on mature external repos:
**~1-1.5% per repo with zero regressions**, diminishing ~4x on the second
pass. The proposer, not the search or the gate, is the ceiling — the
frontier-model ablation (ROADMAP Sprint C1) decides what to build next.
