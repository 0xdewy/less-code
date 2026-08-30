# Fixed Criteria — goal/loc-reduction-hybrid

Workspace: `/home/user/code/less-code`. Mode: full, cap 15, mixed gates.
Baseline rule for all demos: **tests must be green before and after reduction;
net LOC reduction measured on source files only (excl. tests, generated,
lockfiles); test-strength certified by mutation score >= 70%.**

## C1 research (observable)
`docs/research.md` exists with sections: (a) static LOC-reduction techniques
per language w/ tool table, (b) learned/LLM reduction + RL-for-code survey
incl. GRPO, (c) training-data sources with strong test suites, (d) chosen
hybrid architecture + rationale. >= 15 cited URLs. Synthesized from
`docs/research/{static,rl,data}.md`.

## C2 tool (command + observable)
`less_code` Python package + `lc` CLI at repo root with: `analyze`, `audit`
(mutation-based test-strength), `reduce` (static + LLM backends, verify-gated:
candidate applied only if tests stay green AND LOC shrinks), `report`.
Gate: `uv run pytest` exit 0; `uv run lc --help` exit 0.

## C3 demo-py (observable)
Python fixture >= 300 LOC realistic legacy-style module + pytest suite with
mutation score >= 70%. `lc reduce` achieves >= 25% net LOC reduction, tests
green, report artifact cited. Report includes static-only vs hybrid split.

## C4 demo-js (observable)
Same for Node (node:test), >= 300 LOC fixture, mutation score >= 70%,
>= 25% reduction, tests green.

## C5 demo-rs (observable)
Same for Rust (cargo test), >= 300 LOC fixture, mutation score >= 70%,
>= 25% reduction, tests green.

## C6 grpo (command + observable)
`grpo/` contains: dataset builder producing GRPO-formatted samples from
well-tested code (filter: tests pass), reward fn = tests_pass * loc_reduction
(+ mutation-weighted variant), TRL GRPOTrainer script targeting
Qwen2.5-Coder-0.5B/1.5B QLoRA sized for 8GB VRAM. Gate: builder smoke run on
sample corpus exits 0 producing >= 20 valid samples; `python grpo/train.py
--validate-config` exit 0. If VRAM allows: smoke train step evidence.

## C7 review (review)
Independent reviewer given only task+criteria+diff+evidence returns no
unresolved concrete defects. Cannot override C2-C6 objective gates.

## Out of scope / accepted trade-offs
- No cloud API training (no keys); local-only.
- GRPO full convergence not required; scaffold + smoke evidence suffices.
- Minification/obfuscation does not count as reduction (readability preserved).
