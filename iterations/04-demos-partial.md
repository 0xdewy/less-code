# Iteration 4 — Live demo runs (PARTIAL, aborted for GPU contention)

## Attempt
Live ollama-backed reduce runs on fixtures/py with qwen2.5-coder:7b.
Intermediate tool hardening discovered by real runs:
- num_ctx: ollama default (4k) overflowed on 13k-token prompts → set 16k-32k
- spec-in-prompt: include the test suite as behavior specification (error
  messages pinned exactly) — accepted rewrites went 0 → 5 per run
- API check: allow `_`-prefixed helper additions (dedup instinct was correct)
- focused whole-file passes replace blind chunking (model sees all helpers)
- BudgetBackend: hard cap on LLM calls for shared-GPU machines
- verify gate rejected every broken rewrite (38 tests-failed rejections) —
  zero regressions ever accepted

## Evidence
- Best complete run so far: py 525 → 477 (static 9.14%) → 458-460 (hybrid
  ~12.4-12.8%), tests green, API preserved /tmp/opencode/reduce-py.json
- 21 tool tests green after all changes

## Verdict
C3-C5 not yet met (need ≥25% + evidence artifacts). Strategy working;
yield limited by GPU budget available per run.

## Gap diagnosis
Two aborted long runs: GPU is shared and another process needs it. Remaining
LLM-heavy work (demos + GRPO smoke) moves to another machine. Tool + fixtures
+ docs are complete and committed; resume instructions in status.json.
