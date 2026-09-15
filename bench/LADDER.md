# Model ladder - the Iteration Protocol as a benchmark

Per cell: fresh pinned clone, file/item layer alone (no static pass),
`--attempts 3`, full cascade + oracle. Bars gate ITERATION, not deletion
(PLAN.md §0.3). Shrink-only controls from BASELINE.md:
itoa 414->410, humantime 1517->1502, shell-words 353->352, strsim 1062->1058.

| rung | variant | crate | LOC | delta % | accepts | funnel | wall | digest |
|---|---|---|---:|---:|---:|---|---:|---|
| qwen2.5-coder:7b | v1 | humantime | 1517 -> 1517 | 0.0% | 0 | syntax:4, symbol-set:4, docs:3, backend:2, duplicate:2, sentinel:1 | 2005.0s | dae161e27b0e |
| qwen2.5-coder:7b | v1 | itoa | 414 -> 414 | 0.0% | 0 | syntax:3, symbol-set:2, docs:2, backend:1, duplicate:1, loc:1 | 475.5s | dae161e27b0e |
| qwen2.5-coder:7b | v1 | shell-words | 353 -> 353 | 0.0% | 0 | docs:1, syntax:1, sentinel:1 | 387.3s | dae161e27b0e |
| qwen2.5-coder:7b | v1 | strsim-rs | 1062 -> 1062 | 0.0% | 0 | duplicate:2, backend:1, symbol-set:1 | 291.7s | dae161e27b0e |
| qwen2.5-coder:7b | v2 | itoa | 414 -> 414 | 0.0% | 0 | duplicate proposal:15, documentation changed:11, no canonical LOC reduction:7, model error:1 | 442.4s | dae161e27b0e |
| qwen2.5-coder:7b | v2 | shell-words | 353 -> 353 | 0.0% | 0 | duplicate proposal:4, model error:3, project lint/format failed:2, invalid syntax:1, documentation changed:1, no canonical LOC reduction:1 | 136.3s | dae161e27b0e |
| qwen2.5-coder:7b | v2 | strsim-rs | 1062 -> 1062 | 0.0% | 0 | duplicate proposal:27, no canonical LOC reduction:15, compile failed:13, documentation changed:12, tests failed:5 | 472.4s | dae161e27b0e |
| qwen3:8b | v1 | humantime | 1517 -> 1517 | 0.0% | 0 | backend:5, loc:3, duplicate:2, symbol-set:1 | 2338.8s | 500a1f067a9f |
| qwen3:8b | v1 | itoa | 414 -> 414 | 0.0% | 0 | duplicate:4, loc:3, backend:1, declaration:1, docs:1 | 689.7s | 500a1f067a9f |
| qwen3:8b | v1 | shell-words | 353 -> 353 | 0.0% | 0 | backend:1 | 240.3s | 500a1f067a9f |
| qwen3:8b | v1 | strsim-rs | 1062 -> 1062 | 0.0% | 0 | duplicate:2, backend:1, loc:1 | 302.1s | 500a1f067a9f |
| qwen3:8b | v2 | itoa | 414 -> 414 | 0.0% | 0 | duplicate proposal:22, documentation changed:8, no canonical LOC reduction:6 | 871.7s | 500a1f067a9f |
| qwen3:8b | v2 | shell-words | 353 -> 353 | 0.0% | 0 | duplicate proposal:6, no canonical LOC reduction:3, documentation changed:2, model error:1, invalid syntax:1 | 593.4s | 500a1f067a9f |
| qwen3:8b | v2 | strsim-rs | 1062 -> 1062 | 0.0% | 0 | duplicate proposal:44, no canonical LOC reduction:18, documentation changed:6, tests failed:2, compile failed:1, invalid syntax:1 | 2341.1s | 500a1f067a9f |

## Bar check (§2.4, decided before the runs)

API rung: NOT CONFIGURED (no LC_ML_API_* env). Documented impossibility per 0.4: the ladder requirement is satisfied by 2 local models (qwen2.5-coder:7b, qwen3:8b) without the API rung.

- qwen2.5-coder:7b x best variant, humantime: 0 LOC (bar >= 23): NOT MET - iterate per the funnel map

Rebuild with `python3 bench/rust_ladder.py --report`.
