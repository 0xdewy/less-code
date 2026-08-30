# Iteration 2 — Tool implementation (C2)

## Attempt
Direct implementation of `less_code` package + `lc` CLI: loc.py (gaming-proof
code-LOC metric), langdetect.py, testrunners.py, mutator.py (built-in mutation
engine: Python AST + JS/Rust masked token swaps), audit.py (mutation score),
backends.py (none/ollama/openai-compat), api_check.py, static.py (L1),
llm_reduce.py (L2 verify-gated), pipeline.py, cli.py. 17 unit + 4 integration
tests (scripted fake/broken/not-smaller backends through the real pipeline).

## Evidence
- `uv run pytest -q` → 21 passed (exit 0)
- `uv run lc --help` → exit 0
- Verify gate proven: test_pipeline_rejects_behavior_breaking (broken backend
  contributed 0 reduction, original logic intact),
  test_pipeline_rejects_not_smaller, test_pipeline_accepts_good_reduction
  (18 → 15 static → 4 LLM, tests green, API preserved)
- Fixed en route: comment-line misclassification in loc.py (NEWLINE/docstring
  boundary chain), static-revert no-op, API-reference timing (post-static)

## Verdict
- C2 tool: PASS

## Gap diagnosis
None. Next: realistic fixtures for py/js/rust (>=300 LOC each) + strong tests,
then mutation audit + live ollama reduction demos (C3-C5).
