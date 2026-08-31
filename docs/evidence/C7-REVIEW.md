# C7 — Independent review

Reviewer context: task + `CRITERIA.md` + `status.json` + `README.md` + the
diff `6ef9a1a..HEAD` + `docs/evidence/` + `bench/results/*.jsonl` +
`iterations/06`-`10`. No prior involvement in the build. CPU-only
reproduction (no ollama, no GPU). Nothing was modified except this file; the
one bench row my reproduction run appended
(`bench/results/20260831T071055Z.jsonl`) was deleted afterwards.

---

## What I reproduced

| check | command | result |
|---|---|---|
| tool test suite | `uv run pytest -q` | **236 passed**, exit 0 ✅ |
| CLI | `uv run lc --help` | exit 0 ✅ |
| static bench | `uv run lc bench --static-only` | js 7.99 / **py 28.6** / rs 5.1, all `metric: canonical`, tests+api+hidden ok ✅ (exactly matches `20260831T023655Z.jsonl`) |
| py fixture suites | `pytest` in `fixtures/py`, then `tests_hidden` | 97 + 32 passed ✅ |
| py evidence tree | fixture suites + `tests_hidden` against `docs/evidence/py-static-357/inventory.py` | 97 + 32 passed ✅ |
| rs evidence tree | `fixtures/rs` skeleton + `docs/evidence/rs-reduced-292/src` → `cargo test`; `cargo test --test hidden` | 39 + 12 passed ✅ |
| rs LOC arithmetic | `less_code.loc.measure` | orig 392, evidence 292 → **25.51 %** ✅ |
| js LOC arithmetic | `less_code.loc.measure` | orig 601 canonical; 601→445 = **25.96 %** (arithmetic ✅, artifact ❌ — see D2) |
| mutation audits, current fixtures | `lc audit fixtures/{py,js,rs} --max-mutants 40` | py **0.975**, js **0.85**, rs **1.0** — all ≥ 0.70 ✅ |
| GRPO builder | `python grpo/build_dataset.py --fixtures fixtures --out …` | 66 samples, exit 0, **byte-identical** to committed `grpo/dataset.jsonl` ✅ |
| GRPO config | `python grpo/train.py --validate-config` | exit 0, Qwen2.5-Coder-0.5B-Instruct + 4bit + LoRA ✅ |

## Checks the brief called out specifically

**(a) LOC metric.** Sound. `less_code/loc.py` counts non-blank, non-comment,
non-docstring lines and canonically formats first (ruff / prettier / rustfmt
at width 88); all three formatters are present on this machine and every
load-bearing bench row carries `"metric": "canonical"` / `formatted_loc:
true`. Joining statements or minifying cannot buy lines. Token and AST-node
counts are recorded as secondary metrics. The early js rows with
`formatted_loc: false` (`loc_start: 571`) predate prettier being installed and
are superseded by the canonical `601` rows. No metric gaming found.

**(b) Test tampering.** None. `git log --follow` shows
`fixtures/py/test_inventory.py`, `fixtures/js/reporting.test.js` and
`fixtures/rs/tests/integration.rs` untouched since `6ef9a1a`, and all three
`tests_hidden/` suites untouched since their introduction in `eb3a4db`. The
hidden suites are genuinely excluded from the frozen gate (e.g.
`fixtures/rs/Cargo.toml` sets `test = false` on the `hidden` target) and are
re-run afterwards by `lc bench`; `tests/test_bench_and_metrics.py:80,93` pins
that. No gate was weakened by editing a test.

**(c) API surface of the reduced trees.** **Does not match the originals** —
see D4.

**(d) Mutation scores / pristine fixtures.** Scores re-measured above and all
hold on the *current* fixtures. But the premise that `fixtures/` holds the
original unreduced fixtures is **false for python** — see D1.

**Reduction quality.** Genuine restructuring, not line-joining. py's 143
static lines come from four outlined guard helpers (`_check_sku` 72,
`_check_qty` 21, `_check_sku2` 7, `_check_inventory` 7) plus `if`-ladder →
`dict.get` / lazy `next(...)` scans; the diff reads as ordinary refactoring
with readability preserved. rs's reduction is real iterator-chain and
duplicate-merge work. The "minification does not count" trade-off is honoured.

**Numbers vs raw rows.** Every percentage I spot-checked in `iterations/09`,
`iterations/10` and the commit subjects reconciles with the jsonl rows
(9.2 %/14.29 % → `20260831T012153Z`/`015018Z`; 7.4 %/5.1 % → `010516Z`/
`011236Z`; the iteration-10 rs strategy table → `024149Z`/`024248Z`/`024743Z`).
No fabricated numbers found.

---

## Verdict per criterion

| # | criterion | verdict |
|---|---|---|
| C1 research | **sufficient, with a wording gap (D8)** — sections (a)–(d) present; 52 distinct URLs across `docs/research/{static,rl,data}.md`, but `docs/research.md` itself contains zero URLs and its own count ("53") is off by one. |
| C2 tool | **sufficient** — both objective gates pass (236 tests, `lc --help` exit 0); the verify gate is real and unit-pinned (`test_pipeline_rejects_behavior_breaking`, `test_pipeline_rejects_not_smaller`, `test_a_scripted_misfire_is_reverted_by_the_gate`, `test_a_merge_that_breaks_a_test_is_reverted_whole`). Tool defect D10 noted separately. |
| C3 demo-py | **sufficient but tainted baseline (D1)** — 500→357 (28.6 %) reproduced exactly on CPU, suites green on the preserved tree, mutation 0.975, static-vs-hybrid split present in `docs/evidence/reduce-py-rs-iteration10.md`. The 500 baseline is however a tool-modified fixture (true original = 537). The bar is still cleared either way (537→357 would be 33.5 %). |
| C4 demo-js | **INSUFFICIENT (D2)** — no preserved artifact anywhere in the repo; the 25.96 % rests on one bench row and a four-line log from a non-deterministic 7B run whose output was discarded. Static-only js is 7.99 %, so 18 of the 26 points are unverifiable. |
| C5 demo-rs | **outcome verified, process not reproducible (D3, D4)** — I independently confirmed 392→292 = 25.51 % with both suites green, so the objective gate holds. But no committed command produces it: every bench row for rs tops out at 14.29 %, and the 292 tree came from four manual chained `lc reduce` invocations with no driver, no log and no iteration record. |
| C6 grpo | **gates pass, one named component missing (D9)** — builder ≥ 20 samples exit 0 and reproduces byte-identically; `--validate-config` exit 0; TRL/QLoRA/0.5B sizing correct. There is no mutation-weighted reward variant, which the criterion names explicitly. |

I do not override C2–C6's command-verifiable gates; all of them pass on this
machine. The findings below are about evidence quality, provenance and one
tool defect.

---

## Defects

### D1 — `fixtures/py/inventory.py` is not the original fixture; the C3 baseline is tool output
`git log -- fixtures/py/inventory.py` shows a modification in **`8177df1`**
("tool hardening (num_ctx, budget backend, spec prompts, focused passes)…"),
127 insertions / 232 deletions. That commit committed the result of an
in-place `lc reduce` run over the fixture:

- 705 → 599 physical lines; canonical code-LOC **537 → 500** (measured with
  `less_code.loc.measure`).
- Five public symbols deleted: `LegacyReorderCalculator`,
  `legacy_ledger_rows`, `old_flag_code`, `reorder_quantity`,
  `weeks_of_cover` (the whole "LEGACY SECTION"), plus 23 comment lines.

Every C3 number — 500 → 357 → 348 — is therefore measured from a tool-modified
baseline, not the pristine fixture, and the `FIXTURE.md` "Measured" table
(525 code-LOC, mutation 0.925, "surviving mutants sit inside the dead legacy
section") no longer describes the file on disk. To the team's credit,
`fixtures/py/FIXTURE.md` has a "Status 2026-08-30" note disclosing exactly
this; `status.json` and `README.md` do not.

Mitigating: the direction is *conservative* (537 → 357 would be 33.5 %, not
28.6 %), and I re-measured the mutation score on the current file at **0.975**,
so neither the ≥ 25 % nor the ≥ 70 % gate is inflated by this. It remains a
reproducibility defect: the demo cannot be re-run from a clean baseline
because the clean baseline is no longer in `fixtures/`.

**Fix:** restore `fixtures/py/inventory.py` from `6ef9a1a`, re-run the C3
demo against it, and update `FIXTURE.md` + `status.json` with the numbers off
the true 537 baseline.

### D2 — C4 (js) has no preserved artifact and cannot be verified
`docs/evidence/` contains `py-static-357/` and `rs-reduced-292/` but nothing
for js. The entire C4 claim is:

- one row, `bench/results/20260831T002030Z.jsonl` (601 → 553 → 445, 25.96 %);
- `docs/evidence/reduce-js-7b.log`, four `[L2]` lines plus the same table.

The reduced `reporting.js` exists nowhere in the repo. `lc bench` reduces in a
scratch copy and deletes it, and has no flag to keep the tree (`--out-dir`
selects the *results* directory). Static-only js is 7.99 %, so ~18 of the
25.96 points came from a non-deterministic 7B rewrite that no longer exists.
A reviewer cannot check that the reduced js is readable, that its API matches,
or that it is even the file that produced the row — the only thing supporting
C4 is the tool's own self-report. This is the weakest criterion in the set,
and the only one for which I could establish nothing independently.

**Fix:** re-run the js demo with the reduced tree preserved under
`docs/evidence/js-reduced-445/`, exactly as py and rs were.

### D3 — C5's 25.51 % is not produced by any documented command
No bench row for rs exceeds **14.29 %** (392 → 336, `20260831T024743Z`). The
25.51 % came from four sequential `lc reduce` runs on a persistent tree
(`round1.json` 392→311, `round2`/`round3` 0 lines, `round4` 311→292 — the
arithmetic is internally consistent and matches the committed `src/`). But:

- `lc reduce` and `lc bench` have no `--rounds` / chaining flag (`--help`
  confirms), and no driver script is committed;
- no attempt log was preserved (py and js both have `.log` files);
- there is no `iterations/11` record for the run;
- `status.json`'s own `blockers` still say "strategies do not compose …
  nothing runs the second on the output of the first" — the resolution was to
  do it by hand, which was never written down.

The *artifact* is sound: I rebuilt the crate from `fixtures/rs` + the evidence
`src/` and got 39 integration + 12 hidden tests green at 292 canonical
code-LOC. It is the *method* that no one can repeat.

**Fix:** add a `--rounds N` (or a committed driver) that reproduces the chain,
and record the run in `iterations/` with its attempt log.

### D4 — "api preserved" overstates what was checked; the rust tree drops three `pub fn`s
`docs/evidence/rs-reduced-292/src` is missing `pub fn reverse_words`,
`pub fn indent_lines` and `pub fn is_blank` relative to `fixtures/rs/src`
(diff of the `pub` item lists). For a library crate that is a breaking API
change, yet every report says `api_ok: true`.

The gate is structurally unable to see it: `less_code/pipeline.py:141` takes
the API baseline **after** the static pass, and `less_code/static.py:420`
`_rust_remove_dead_pub` is what deletes them — `round1.json`'s `static_notes`
records "removed dead pub item is_blank / indent_lines / reverse_words". The
same applies to python's five removed symbols (D1). The code comments this
choice honestly ("static-layer removals are intentional (dead code); the LLM
layer must preserve the post-static surface exactly"), so this is not
concealment — but `status.json`'s C5 evidence string "api preserved" and the
README's "the public API is preserved" are both false as written, since the
public API of the delivered crate is smaller than the original's.

**Fix:** reword the claim to "public API preserved across the LLM layer;
provably-dead public items removed by the static layer (list them)", or gate
against the pre-static surface and count dead-pub removal as an opt-in.

### D5 — bench rows record a commit that cannot produce them
`20260831T023359Z`, `20260831T023655Z` (py 28.6 %) and `20260831T030620Z`
(py 30.4 %) all carry `"commit": "90073b1"`. But
`git ls-tree 90073b1 less_code/` has no `outline.py` — guard outlining, which
`docs/evidence/reduce-py-rs-iteration10.md` credits with 107 of those 143
lines, first appears in `f02ae9d`. The runs were made from a dirty tree and
stamped with the last commit, so the recorded provenance cannot reproduce the
row. (Current HEAD does reproduce it exactly, which is how I confirmed the
number.)

**Fix:** stamp `git describe --dirty` / the working-tree hash, or refuse to
write a row from a dirty tree.

### D6 — `README.md` contradicts the claimed state
README's "Status vs goal" still reads:

> rs demo (C5) still short of the bar — **14.29 %** at 7B whole-file+salvage …
> · review pending rs demo.

while `status.json` records C5 passed at 25.51 %. A reviewer handed the named
materials and reading the README first would conclude C5 failed. Same section
also claims `tests/ 153 tests` (actual **236**) and "53 cited sources"
(**52** distinct URLs).

**Fix:** update the README status block, test count and source count.

### D7 — `status.json` prose contradicts its own criteria states
- C2 evidence: "uv run pytest -q -> **21 passed**"; C6 evidence: "**30 pytest
  passed**". Actual: **236 passed**. Both strings are stale by many
  iterations and understate the suite by an order of magnitude.
- `"state": "in_progress"` with all of C1–C6 `passed`.
- `blockers` still say "rs is 42 code lines short of the bar" and
  `next_action` still proposes how to get rs over 25 %, directly contradicting
  the C5 `passed` entry immediately above them.

**Fix:** refresh the evidence strings, blockers and `next_action` to match the
recorded states.

### D8 — C1's ">= 15 cited URLs" are not in `docs/research.md`
`grep -c http docs/research.md` → **0**. Its "Consolidated source list (≥15)"
lists bare names ("knip.dev, cargo-machete, …", "DeepSeek-R1 (2501.12948), …")
with no URLs. The 52 real URLs are in `docs/research/{static,rl,data}.md`,
which the criterion does name as the synthesis source, so I judge C1 satisfied
in substance — but the headline document carries no citation a reader can
follow, and its self-reported total (53) is one high.

**Fix:** carry the URLs into the consolidated list, or correct the count.

### D9 — C6's mutation-weighted reward variant does not exist
CRITERIA.md C6 requires "reward fn = tests_pass * loc_reduction **(+
mutation-weighted variant)**". `grpo/rewards.py` implements only
`gate × (1 + 0.5·loc_delta) − penalties`; `grep -n "mutation" grpo/*.py`
matches a single docstring line in `build_dataset.py` describing corpus
*filtering*, not reward shaping. No variant is defined, referenced or tested.
`status.json`'s C6 evidence string does not mention it either. The two
command gates pass, so C6's objective bar is met, but a named component of the
criterion is simply absent.

Related, smaller: `compute_reward`'s pre-test syntax check is python-only
(`if lang == "python": ast.parse(code)`); js/rust rollouts reach the test
runner unparsed. Harmless (the runner catches it) but asymmetric with the
reward's own docstring, which claims a "parse/compile check".

**Fix:** implement the mutation-weighted variant, or get the criterion amended.

### D10 — `lc reduce` mutates the target tree in place with no dry-run
`lc reduce --help` offers no `--dry-run` and no `--out-dir`; it rewrites the
tree it is pointed at. README's own Quickstart says
`uv run lc reduce fixtures/py --model qwen2.5-coder:7b …` — which is precisely
how the python fixture was destroyed (D1). The complementary half is that
`lc bench`, which *is* safe (scratch copy), then throws the reduced tree away
with no way to keep it (D2). For a tool whose entire premise is a revert-on-red
verify gate, having no way to inspect a result before it lands on your source
is a material usability defect, and it has already cost this project its
pristine py fixture.

**Fix:** add `--out-dir` to `reduce` (write the reduced tree elsewhere,
default to in-place only with `--in-place`) and `--keep-tree` to `bench`.

### D11 — tracked `.pyc` files (minor)
`.gitignore` lists `__pycache__/` and `*.pyc`, yet four byte-compiled files are
tracked, two of them added by this diff
(`grpo/__pycache__/rewards.cpython-312.pyc`,
`tests/__pycache__/test_grpo.cpython-312-pytest-9.1.1.pyc`). Not
criteria-bearing; listed for completeness.

---

## Disclosed caveat I checked and accept (not a defect)

`_rule_if_ladder_dict` (`less_code/rules.py:644`) is **not** semantics
preserving: for an unhashable subject, `{...}.get(x, d)` raises `TypeError`
where the `==` ladder returned `d`. It is documented in the rule's own
docstring, in `iterations/09` §152 and §381, in `iterations/10` §352, and in
the README's rule list; it is constrained (≥ 3 branches, one hashable non-bool
key type, distinct keys, constant results) and gated by the frozen suite; and
the sibling threshold rule deliberately emits a lazy `next(...)` scan rather
than a dict precisely to stay exact. Recorded rather than hidden, and the
neighbouring refusals (`busiest_area`'s numeric sentinel,
`max-loop-to-max`) show the same discipline. Anyone reusing this tool outside
the fixtures should know the rule is gated-not-proved.

---

## Overall verdict

# FAIL

Not because a gate was faked — I could not find a fabricated number anywhere,
the metric is honest, no test was ever weakened, and every command gate I
could run on CPU passes. It fails because two of the three headline demos
cannot be independently confirmed from the repository as delivered:

**Must fix before C7 can pass:**

1. **D2** — preserve the reduced js tree; C4 is currently unverifiable by
   anyone but the machine that ran it.
2. **D3** — make C5's 25.51 % reproducible (a `--rounds` flag or a committed
   driver + attempt log); no command in the repo produces better than 14.29 %
   on rust.
3. **D1** — restore the pristine `fixtures/py/inventory.py` and re-baseline
   C3, or state the tool-modified baseline in `status.json` as plainly as
   `FIXTURE.md` already does.
4. **D4** — correct the "api preserved" claim, which is false against the
   original crate (three `pub fn`s removed).
5. **D9** — supply C6's mutation-weighted reward variant, which does not exist.
6. **D6, D7** — reconcile `README.md` and `status.json` with the claimed
   state; as written the README says C5 failed.

**Should fix:** D5 (dirty-tree commit stamps), D8 (URLs in `research.md`),
D10 (`reduce` has no dry-run — the root cause of D1), D11.
