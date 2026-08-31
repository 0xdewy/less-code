# less-code

Hybrid **static + LLM + RL (GRPO)** lines-of-code reduction for Rust /
JavaScript / Python, with a hard verify gate: **a reduction is accepted only
if the frozen test suite stays green, the public API is preserved, and
code-LOC shrinks.** Everything else reverts.

"Public API preserved" means preserved **relative to the post-static surface**:
the L1 static layer removes provably-dead public items by design (nothing in the
project, tests included, references them), and every layer above it is then held
to that surface exactly. So a reduced tree's API can be *smaller* than the
original's by exactly the dead items L1 dropped — the report names the baseline
(`api_baseline: "post-static"`) and lists them (`static_removed_symbols`), and
`REPRODUCE.md` in each evidence directory repeats the list. Read those two
fields before treating a reduced tree as a drop-in replacement: on the rust
fixture L1 dropped `is_blank`, `indent_lines` and `reverse_words`, and on the
python one the five symbols of the legacy section.

## Architecture

```
L0  normalize (format-only; not counted as reduction)
L1  static provably-safe pass
      python: AST dead-code/unused-symbol removal, `ruff check --fix` safe
              tier automatically and the unsafe tier as its own gated layer
              (LOC-reducing families: RET/SIM/C4/PIE/UP/PLR1/PERF/FURB), plus
              unreachable-code removal after `return`/`raise` — all test-gated
      rust:   cargo fix + clippy --fix (compiler-verified), then the
              `clippy::pedantic`/`::complexity` tier as its own gated,
              tree-restoring step (on the rs fixture it ADDS 19 lines —
              `#[must_use]`, `# Panics` docs — so the size gate drops it),
              then dead `pub` item removal — brace-matched extraction incl. doc comments and
              attributes, kept only if no project file (tests included)
              mentions the name
      js:     dead exports via import-graph scan, cross-checked with
              `npx knip` when it is available (exports only — never
              knip's unused-*files* report, which would delete
              `tests_hidden/`), plus non-exported top-level functions
              and consts nothing in the project references
L1b rule library (less_code/rules.py): deterministic, semantics-preserving
      AST rewrites, applied file-wide and gated by the frozen suite
      python: `if c: return True/return False` -> `return c`;
              append-loop -> comprehension / `list()`, including an `if`
              guard as a comprehension clause and a twice-read temp bound with
              a walrus in that clause when it is the guard's first-evaluated
              name (so evaluation order is provably unchanged);
              `t = 0` + `+=` loop -> `sum()` (float start preserved);
              fresh-list + `.sort()` -> `sorted()`;
              `try/except X: raise` -> the try body;
              `if x == "A": return 1 / elif ... / return d` ladders ->
              `{...}.get(x, d)` (one hashable non-bool key type only);
              ordered threshold ladders -> a lazy `next(...)` tuple scan,
              which keeps the comparisons and their order exactly;
              a `None`-sentinel manual max loop -> `max(it, key=..., 
              default=None)` — a numeric sentinel is declined, because
              `-1` is not provably equivalent to `default=None`
      on red the gate narrows rule by rule, keeping only what stays green
L1c guard-block outlining (less_code/outline.py, python): project-wide, not
      peephole. Runs of `if <test>: raise` / `name = expr` statements that
      repeat >= 3 times across function bodies — after alpha-renaming and
      constant abstraction — are outlined into one module-level `_`-prefixed
      helper and each site becomes a single call line. Names the block binds
      are returned and unpacked; names it reads are passed by value, and only
      `Name`/`Constant` leaves may become arguments so an eagerly-evaluated
      argument can never raise before a guard does. `return`/`break`/`yield`,
      lambdas, nested defs, walruses and `global`/`nonlocal` targets are
      refused, as is any free name not provably bound at the window. Error
      messages are never rewritten: a message that is the same everywhere is
      re-emitted verbatim, a message that differs becomes a literal argument.
      A group that does not pay for its own helper in code lines is declined.
      On the py fixture this is 107 code lines from four helpers.
L1.5 audit: mutation score of the test suite (trust oracle)
      built-in mutation engine (py: AST, js/rust: masked token swaps)
L2  LLM semantic reduction, verify-gated
      **per-symbol proposals are the default**: the model rewrites ONE
        top-level symbol at a time, biggest first (python via ast, js/rust via
        the brace matcher), given that symbol, a signatures-only map of the
        rest of the file and the test spec. Each proposal goes through the
        same gate, so one budget buys ~20 independent bets instead of one
        all-or-nothing whole-file gamble
      **a big python class is decomposed into its methods** (`Class.method`
        units over a 40-LOC threshold), each spliced over its own ast span,
        prompted with the class's other method signatures and `__init__`'s
        full body; the class docstring and attributes are outside every
        method span and so are never touched
      **duplicate-group proposals** (less_code/dedup.py) are the third
        granularity: near-duplicate symbols/methods are found project-wide
        (comments stripped, literals folded, identifiers erased, difflib
        ratio >= 0.6), and each group gets ONE proposal — a shared
        `_`-prefixed helper plus minimal rewrites of every member — applied
        as a single MULTI-FILE candidate through a gate that snapshots and
        restores every touched file. This is the only shape that can merge
        `csv_escape_row`/`_owned`/`_rows` or six pasted SKU validators
      an optional whole-file sweep runs last with hunk salvage on its rejects
      local ollama (qwen2.5-coder) or any OpenAI-compatible endpoint;
      test suite included in prompt as behavior spec; per-attempt feedback —
      a test failure *and* an `api-changed` rejection's concrete
      missing/changed/added symbol list both go into the next prompt;
      hard LLM-call budget for shared-GPU machines
      pre-gates (cheapest first): not-smaller -> parse (ast/node --check/
        cargo check) -> API surface -> frozen tests, + a content-hash cache
        so the same tree is never tested twice
      hunk decomposition: a rejected whole-file rewrite is split at top-level
        symbol boundaries and delta-debugged, so the correct 2 of 3 functions
        are accepted instead of the whole candidate being thrown away
L3  GRPO-trained reducer (Qwen2.5-Coder QLoRA, 8GB-friendly)
      reward = frozen-test gate x LOC delta - anti-hack penalties, plus a
      mutation-weighted variant that scales the LOC term by the sample suite's
      mutant kill rate (a saving is only as trustworthy as the oracle that
      certified it); the gate itself is never scaled
```

**Metric**: code-LOC is counted *after canonical formatting* (`ruff format` /
`prettier` / `rustfmt` at width 88), so joining lines or minifying buys
nothing; the report records whether a formatter was available, plus token and
AST-node counts as secondary metrics.

**Safety**: each fixture carries a `tests_hidden/` suite that is excluded from
the prompt spec and from the frozen verify gate. Only `lc bench` runs it, as
an independent check that a reduction did not change behaviour the visible
suite failed to pin.

Research basis: `docs/research.md` (synthesis; 53 numbered sources, 52
distinct URLs, all listed in full at the end of that file, incl. the
verified gap that no published GRPO-for-LOC-reduction work exists).

## Quickstart

```bash
uv sync
uv run lc analyze fixtures/py            # map + LOC baseline
uv run lc audit fixtures/py              # mutation score of the suite
uv run lc reduce fixtures/py \
    --copy-to /tmp/py-work \             # reduce a COPY; fixtures/ stays pristine
    --model qwen2.5-coder:7b \           # needs: ollama serve
    --attempts 2 --max-llm-calls 6 \
    --num-ctx 16384 --out report.json
uv run lc report --json report.json      # markdown summary
uv run lc bench --static-only            # all fixtures + hidden tests, no GPU
```

`lc bench [dir]` reduces every fixture under `dir` (default `fixtures/`) in a
scratch copy — the repo is never mutated — runs the hidden suite afterwards,
prints a markdown table and appends one JSON row per (fixture, config, commit)
to `bench/results/<timestamp>.jsonl`: LOC before/after static/final, static
and hybrid %, tests/api/hidden green, LLM calls and wall seconds. **Rows are
appended as each fixture finishes**, so an interrupted run still leaves the
fixtures that completed. A row whose language has no formatter installed is
marked `"metric": "raw"`, shouted about on stderr and printed as `**RAW**` in
the table — raw physical lines are not comparable with canonical ones.

`--static-only` runs L1 only (no GPU). `--max-llm-calls N` bounds GPU time.
`--fixture NAME` benches a single fixture. `--keep-tree DIR` copies each
reduced scratch tree to `DIR/<fixture>` instead of deleting it — **use it for
any run whose number you intend to cite**: a bench row without the tree that
produced it is the tool's own self-report and nothing more.

Likewise `lc reduce --copy-to DIR` reduces a copy and leaves the original
alone. `reduce` rewrites its target in place otherwise, and warns on stderr
when that target is a git tree with uncommitted changes.

## GRPO

```bash
uv run python grpo/build_dataset.py      # CPU: verify-gated samples from fixtures
uv run python grpo/train.py --validate-config   # CPU: config check, no model load
uv run python grpo/train.py --steps 10   # GPU: QLoRA smoke (0.5B, ~minutes)
uv run python grpo/train.py --steps 10 --reward mutation-weighted   # weighted variant
```

Dataset rows: `prompt` (instruction + verbose unit + frozen tests as spec),
`unit_source`/`unit_loc` for reward normalization, and `mutation_score` — the
kill rate of that sample's fixture suite, measured on a throwaway copy by
`less_code.audit` (`--mutants N`, default 20; `--mutants 0` skips it).

Two TRL-compatible reward callables in `grpo/rewards.py`:

| callable | reward | when |
|---|---|---|
| `reward_fn` | `gate × (1 + 0.5·loc_delta) − penalties` | default |
| `reward_fn_mutation_weighted` | `gate × (1 + 0.5·loc_delta × mutation_score)` | mixed-quality corpora |

Both share the same gate — frozen tests green **and** public API preserved,
else exactly −1 — plus api-preservation, minification and degenerate-output
guards; tests run on CPU inside the reward. The weighted variant scales only
the *shaping* term, so a weak oracle earns less credit for the same line delta
while a behavior break is still −1 no matter how strong the suite is. At
`mutation_score == 1.0`, and for samples with no score recorded, the two are
identical.

## Repo layout

```
less_code/   the tool        tests/      255 tests (unit + integration + reward)
fixtures/    py/js/rs demo targets (mutation-scored suites + tests_hidden/)
bench/       results/*.jsonl — one bench row per fixture/config/commit
grpo/        dataset builder, reward fn, TRL trainer
docs/        research synthesis + sub-reports
iterations/  goal-loop records    status.json  durable run state
CRITERIA.md  fixed acceptance criteria for the build
```

## Status vs goal

All three demos pass their ≥ 25 % bar, **each with a preserved, independently
checkable reduced tree** and both suites (frozen + hidden) green. Every number
is canonical code-LOC measured off the pristine fixture.

| criterion | result | artifact |
|---|---|---|
| C1 research | ✅ | `docs/research.md` + three sub-reports, 53 numbered sources |
| C2 tool | ✅ | `uv run pytest -q` → **259 passed**; `uv run lc --help` exit 0 |
| **C3 py** | ✅ **33.5 %** — 537 → 357, static only, 0 LLM calls | `docs/evidence/py-static-357/` |
| **C4 js** | ✅ **29.1 %** — 601 → 553 static → 426, 3 LLM calls | `docs/evidence/js-reduced-426/` (+ `REPRODUCE.md`) |
| **C5 rs** | ✅ **25.5 %** — 392 → 372 static → 292, 4 chained rounds | `docs/evidence/rs-reduced-292/` (+ `REPRODUCE.md`) |
| C6 grpo | ✅ | 69 samples with mutation scores; both reward variants; `--validate-config` exit 0 |
| C7 review | 🔄 in progress | first pass FAILed with 11 defects (`docs/evidence/C7-REVIEW.md`); all addressed in `iterations/12-rs-settlement-and-c7-fixes.md`, re-review pending |

Two things the numbers do not say on their own:

- **py's 33.5 % is entirely deterministic** — no model, no GPU, reproducible to
  the line. 107 of the 180 lines are guard-block outlining (four helpers over
  30 sites), the rest dead-code removal, the rule library and ruff's
  LOC-reducing tiers.
- **js and rs are carried by hunk salvage.** Across both, *every* whole-file
  proposal a 7B model made was rejected — wrong API, unparseable, or a red
  test — and *every* accepted line was recovered by splitting those rejects at
  symbol boundaries and delta-debugging them. Discarding a rejected candidate
  whole was throwing away all of the model's usable output.

LLM rounds are not deterministic: re-running the recipes in the two
`REPRODUCE.md` files will land on different numbers. The preserved trees, not
the logs, are the evidence.
