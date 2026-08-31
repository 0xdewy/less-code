# Iteration 09 bench rows — py and rs, 3B validation and 7B settlement

Commit `0b6b69d` + working tree. All rows canonical (ruff / rustfmt present),
hidden suite green on every row, no gate relaxed.

| fixture | lang | config | LOC start | after static | final | static % | hybrid % | tests | api | hidden | calls | s | metric |
|---|---|---|---:|---:|---:|---:|---:|---|---|---|---:|---:|---|
| py | python | hybrid:qwen2.5-coder:3b | 500 | 465 | 465 | 7.0 | 7.0 | ok | ok | ok | 10 | 124.27 | canonical |
| rs | rust | hybrid:qwen2.5-coder:3b | 392 | 372 | 372 | 5.1 | 5.1 | ok | ok | ok | 10 | 58.89 | canonical |
| py | python | hybrid:qwen2.5-coder:7b | 500 | 465 | 463 | 7.0 | 7.4 | ok | ok | ok | 8 | 425.4 | canonical |
| rs | rust | hybrid:qwen2.5-coder:7b | 392 | 372 | 372 | 5.1 | 5.1 | ok | ok | ok | 8 | 186.61 | canonical |

Attempt logs: `reduce-py-3b.log`, `reduce-rs-3b.log`, `reduce-py-7b.log`,
`reduce-rs-7b.log`. Raw rows: `bench/results/20260831T010143Z.jsonl`,
`20260831T010400Z.jsonl`, `20260831T010516Z.jsonl`, `20260831T011236Z.jsonl`.

**Verdict: C3 (py) MISS at 7.4 %, C5 (rs) MISS at 5.1 %, against a 25 % bar.**
