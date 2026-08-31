# C4 (javascript) — how the 426-LOC tree was produced

Written in response to C7 review defect **D2**, the only criterion the reviewer
could establish nothing about: C4 rested on one `bench/results` row and a
four-line log from a 7B run whose output `lc bench` had deleted. This directory
is the artifact that was missing, produced by a fresh run under the same
protocol used for rust.

**601 → 426 canonical code-LOC = 29.12 %**, 36 frozen + 16 hidden tests green.
(The earlier, discarded run reported 25.96 %; this one is a different,
independently verifiable result, not a re-derivation of that number.)

## What is in this directory

| file | what it is |
|---|---|
| `reporting.js` | the delivered reduced source, **426** canonical code-LOC |
| `round1.json` | the `lc reduce` report for the single round |
| `round1.log` | its live per-attempt output |

Check it yourself, no GPU needed:

```bash
mkdir -p /tmp/js-check/tests_hidden
cp fixtures/js/{reporting.test.js,package.json} /tmp/js-check/
cp fixtures/js/tests_hidden/hidden.mjs /tmp/js-check/tests_hidden/
cp docs/evidence/js-reduced-426/reporting.js /tmp/js-check/
cd /tmp/js-check
node --test                        # 36 pass — the frozen gate
node --test tests_hidden/hidden.mjs   # 16 pass — excluded from the gate
uv run --project <repo> lc analyze .  # 426 canonical code-LOC
```

`node --test` with no arguments does not descend into `tests_hidden/` (the file
is named `hidden.mjs` precisely so it does not match), which is why the hidden
suite has to be named explicitly and why it never steers the reducer.

## The recipe

```bash
mkdir -p /tmp/js-work/tests_hidden
cp fixtures/js/{reporting.js,reporting.test.js,package.json} /tmp/js-work/
cp fixtures/js/tests_hidden/hidden.mjs /tmp/js-work/tests_hidden/

lc reduce /tmp/js-work \
    --model qwen2.5-coder:7b --strategy whole-file \
    --attempts 3 --max-llm-calls 8 --num-ctx 16384 --llm-timeout 2400 \
    --out round1.json
```

Since D10 the copying step is a flag — `lc reduce fixtures/js --copy-to
/tmp/js-work …` — and `lc bench --keep-tree DIR` preserves the tree of a bench
run instead of deleting it. Either one would have prevented this defect.

Budget: one round, 3 attempts, 20 minutes of GPU on an RTX 2060 Max-Q (6 GB).
Four rounds were budgeted; the first cleared the bar, so the rest were not run
and `ollama stop qwen2.5-coder:7b` followed.

## What actually produced the lines

| stage | LOC | note |
|---|---:|---|
| baseline | 601 | |
| after L1 static | 553 | `npx knip` found 3 unused exports; `deepCloneRow`, `formatPercent`, `pivotByRegion` dropped |
| after L2 | **426** | see below |

**Every one of the three whole-file proposals was rejected, and every one of the
127 L2 lines came from salvaging its hunks:**

| attempt | proposal | verdict | salvaged |
|---|---|---|---|
| 1 | 553 → 315 | `api-changed` | 553 → 535 |
| 2 | 535 → 174 | `syntax-error` | 535 → 488 |
| 3 | 488 → 333 | `tests-failed` | 488 → **426** |

Three different rejection reasons — a dropped export, unparseable output, a
behaviour break — and hunk decomposition recovered usable lines from all three.
This is the same finding rust's round 4 produced (`../rs-reduced-292/REPRODUCE.md`):
at 7B the model's whole-file rewrites are unreliable *as wholes* and useful *in
parts*, and throwing a rejected candidate away discards the good hunks with the
bad. Without salvage this run would have scored 7.99 %.

## Honest limits

- **The LLM round is not deterministic.** Re-running the command above will
  make different proposals and land on a different number. The commands are the
  recipe; `reporting.js` in this directory, at 426 canonical code-LOC with both
  suites green, is the evidence.
- **`api_ok: true` is measured against the post-static surface.** The delivered
  module exports **21** of the original's **24**: L1 removed `deepCloneRow`,
  `formatPercent` and `pivotByRegion` as provably dead (nothing in the project,
  tests included, imports them). Nothing else was added or changed. The current
  bench row records this explicitly as `api_baseline: "post-static"` and
  `static_removed_symbols` (`bench/results/20260831T073226Z.jsonl`);
  `round1.json` here was written by the binary as it stood at the start of the
  run, a few minutes before those two fields existed, so it carries only the
  equivalent `static_notes` lines.
- **Readability is preserved.** The reduction is real restructuring — the
  formatter runs first and the metric is canonical code-LOC, so joining lines
  or minifying buys exactly nothing.
