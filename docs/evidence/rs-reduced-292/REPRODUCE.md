# C5 (rust) — how the 292-LOC tree was produced

Written in response to C7 review defect **D3**: the outcome (392 → 292 canonical
code-LOC = **25.51 %**, both suites green) was independently verified, but no
command in the repo produced it and no record of the run existed. This file is
that record.

## What is in this directory

| file | what it is |
|---|---|
| `src/lib.rs`, `src/stats.rs` | the delivered reduced sources, **292** canonical code-LOC |
| `round1.json` … `round4.json` | the `lc reduce` report from each round, in order |

To check the artifact yourself, with no GPU:

```bash
cp -r fixtures/rs /tmp/rs-check          # Cargo.toml, tests/, tests_hidden/
cp docs/evidence/rs-reduced-292/src/*.rs /tmp/rs-check/src/
cd /tmp/rs-check
cargo test --quiet                       # 39 integration tests — the frozen gate
cargo test --quiet --test hidden         # 12 hidden tests — excluded from the gate
uv run --project <repo> lc analyze .     # 292 canonical code-LOC
```

## The recipe

There is no `--rounds` flag. The chain was four **sequential, manual** `lc reduce`
invocations against one persistent tree — each round starts from the previous
round's output, which is why `round2.json`'s `loc_start` is `round1.json`'s
`loc_final`. Setup:

```bash
cp -r fixtures/rs /tmp/rs-work && cd /tmp/rs-work
```

Round 1 — whole-file rewrites with hunk salvage:

```bash
lc reduce /tmp/rs-work --model qwen2.5-coder:7b --strategy whole-file \
    --attempts 3 --max-llm-calls 8 --num-ctx 16384 --llm-timeout 2400 \
    --out round1.json
```

Round 2 — the mixed strategy (dedup groups + per-symbol + sweep), on round 1's
output:

```bash
lc reduce /tmp/rs-work --model qwen2.5-coder:7b --strategy mixed \
    --attempts 3 --max-llm-calls 10 --num-ctx 16384 --llm-timeout 2400 \
    --out round2.json
```

Rounds 3 and 4 — identical to round 1 (`--strategy whole-file`, `--max-llm-calls
8`), written to `round3.json` and `round4.json`. **Round 4 was run after commit
`17a5b35`** ("salvage syntax-error rejects too"); that is the whole difference
between round 3 and round 4 and it is the difference between 0 and 19 lines.

Today the same chain should be run with `--copy-to` (added for review defect
D10) rather than by hand-copying the fixture first:

```bash
lc reduce fixtures/rs --copy-to /tmp/rs-work ... --out round1.json   # round 1
lc reduce /tmp/rs-work ...                     --out round2.json    # rounds 2-4
```

## What each round actually bought

| round | strategy | start → end | gained | note |
|---|---|---|---|---|
| 1 | whole-file | 392 → 311 | **81** | 20 from L1 static (`cargo fix`, clippy, 3 dead `pub` items); 61 from L2 — one hunk-salvaged rewrite of `lib.rs` and two accepted rewrites in `stats.rs` |
| 2 | mixed | 311 → 311 | 0 | every dedup/per-symbol proposal came back `not-smaller`, `syntax-error` or `backend-error` |
| 3 | whole-file | 311 → 311 | 0 | 4 of 6 proposals were `cargo check` syntax errors; before `17a5b35` a syntax error was simply discarded |
| 4 | whole-file (post-`17a5b35`) | 311 → **292** | **19** | *every* proposal was still a syntax error — but hunk decomposition now runs on unparseable rewrites too, and 5 of 6 of them salvaged accepted hunks |

Round 4 is the load-bearing observation: at 7B, rust whole-file rewrites are
almost never syntactically valid, yet they contain valid, correct, smaller
*fragments*. Throwing the whole candidate away because it does not parse was
throwing away all of the model's usable output.

## The hidden suite caught a real break

Mid-campaign the visible suite stayed green while `tests_hidden` went red: an
accepted rewrite had changed `title_case`'s behaviour on input the frozen suite
did not pin. The affected function was **restored verbatim** from the fixture
and the round re-run. That is exactly the job `tests_hidden/` exists to do
(`fixtures/rs/Cargo.toml` sets `test = false` on the `hidden` target so
`cargo test` cannot see it, and it is never put in a prompt), and it is the
reason the chain has four rounds rather than three.

## Honest limits of this recipe

- **The LLM rounds are not deterministic.** Same commands, same model, same
  seedless ollama sampling: a re-run will make different proposals, salvage
  different hunks and land on a different number. It may land above 292 or
  below it. The commands here are the *recipe*, not a replay.
- **The verifiable evidence is the artifact**, not the log: the `src/` in this
  directory at 292 canonical code-LOC, with 39 frozen + 12 hidden tests green
  and the post-static public API intact. That check is fully deterministic and
  needs no GPU — it is the block at the top of this file.
- **`api_ok: true` is measured against the post-static surface.** L1 removed
  three provably-dead public items (`is_blank`, `indent_lines`, `reverse_words`
  — see `round1.json` `static_notes` and `static_removed_symbols`), so the
  delivered crate's public API is *smaller* than `fixtures/rs`'s by those three
  functions, by design. See review defect D4.
- **No bench row reaches 25.51 %.** `lc bench` runs a single round in a scratch
  copy it then deletes; the best single-round rs bench row is 14.29 %. The
  chain above is the only thing that produces 292, and this file plus the four
  round reports are its whole audit trail.
