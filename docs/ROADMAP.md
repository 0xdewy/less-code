# ROADMAP — making `less-code` the most effective code-size minimizer that exists

Written 2026-08-30 against commit `2dee2c1`. This is the long-horizon plan; the
short-horizon runbook is `RESUME.md`. Everything below is grounded in what the
code does *today* (file refs are clickable) and in the measured result so far:
**py fixture 525 → ~458 LOC (12.8%) with 6 LLM calls of qwen2.5-coder:7b**,
against a 25% target and a 40-50% ceiling the fixtures were built to allow.

## 0. What "most effective" has to mean

A tool is the best at this job when it wins on all four axes *at once*,
measured on a public benchmark nobody else has:

| axis | metric (north star) | today |
|---|---|---|
| **yield** | % code-LOC removed, counted after canonical formatting | 12.8% py, js/rs unmeasured |
| **safety** | 0 behaviour regressions: frozen tests + *hidden* tests + differential fuzz all green | tests+API only |
| **readability** | no degradation (formatted-LOC, nesting, identifier length; LLM judge at eval only) | avg-line-length heuristic |
| **cost** | LLM calls and wall-seconds per accepted LOC | 1 call ≈ 1 whole-file gamble |

Everything in this plan exists to move one of those four numbers. The rule for
prioritising: **verification quality is the ceiling on everything else** — a
stronger oracle lets every layer (static, LLM, RL) be more aggressive without
becoming less safe. So the order is *oracle → search → static → data → learning
→ product*, not the reverse.

## 1. Honest diagnosis of the current tool

What blocks yield today, ranked by impact:

1. **All-or-nothing verification.** `verify_candidate` in
   `less_code/llm_reduce.py:129` writes a whole-file rewrite and rejects the
   entire thing if *one* test fails. A 500-LOC file rewritten by a 7B model
   almost always contains one mistake, so 38 of 43 candidates in iteration 4
   were thrown away — including all the correct hunks inside them. This is the
   single biggest lever in the codebase.
2. **The LOC metric is formatting-sensitive.** `less_code/loc.py` counts
   physical lines, so `a; b` on one line or a 200-char comprehension "wins".
   The reward's `_looks_minified` heuristic (`grpo/rewards.py:57`) papers over
   it. Counting after `ruff format` / `prettier` / `rustfmt` at a fixed width
   removes the whole class of gaming, for the tool *and* for RL.
3. **The oracle is only as good as the repo's tests.** Nothing today
   strengthens a weak suite; the mutation audit merely reports the score. Real
   repos have 30-60% mutation scores, and on those the gate is a rubber stamp.
4. **API-surface check is thin.** `less_code/api_check.py` uses top-level names
   for Python (no methods, defaults, or return contracts) and regexes for
   JS/Rust. In RL that is exactly the crack reward hacking finds.
5. **Static layer is a sketch.** Python removes only unused top-level defs;
   JS uses a regex over `export function` bodies; Rust runs `cargo fix`. No
   unused imports/locals, no unreachable branches, no dead files, no dep
   pruning, no dedup detection — all of which the research (`docs/research/static.md`)
   identifies as the highest-yield static wins.
6. **Search is greedy, sequential, per-file.** No best-of-N, no beam over
   accepted states, no cross-file context, no fixpoint loop. Cross-module
   duplication — the biggest real-world win — is invisible to the prompt.
7. **Verification is slow and serial.** Full suite per candidate, no test
   selection, no result cache, no parallel worktrees, no cheap pre-gates
   (`ast.parse` / `node --check` / `cargo check`) before spending a test run.
8. **Project mapping is heuristic.** `is_test_file` in
   `less_code/langdetect.py:37` matches `"test" in name` (so `latest.py`,
   `contest.js` are "tests"); one language per repo; TypeScript is treated as
   JS with no `tsc` gate.
9. **GRPO data is a toy.** 66 samples from 3 hand-written fixtures. No mining
   pipeline exists yet; no held-out eval; no SFT warm start.

## 2. Phased plan

Each phase has an exit gate measured on the benchmark from Phase A. Phases
overlap where they don't depend on each other; the dependency graph is at the
end.

### Phase A — measure first (1 week, needs GPU for a day)

You cannot optimise what you cannot measure, and the current numbers are from
aborted runs.

- A1. Run `RESUME.md` §1-2 as written: reduce-py/js/rs evidence + post-audits.
  Record the *real* baseline for each fixture, LLM calls used, wall time.
- A2. `lc bench` command: runs the full pipeline over `bench/` (starts as the
  three fixtures) with a fixed seed/budget and writes one JSON row per
  (fixture, config, commit): yield, tests_ok, api_ok, calls, seconds. Commit
  rows to `bench/results/`. Every later PR carries a bench delta.
- A3. Add **hidden tests** to each fixture (`tests_hidden/`, excluded from the
  prompt spec and from the frozen gate) so the safety axis becomes measurable
  from day one: hidden-green rate is the number that must stay at 100%.
- Exit: a table with three fixtures × {static-only, hybrid} × {yield, hidden
  green, calls}. This is the "before" picture for the whole roadmap.

### Phase B — the verification engine (3-4 weeks, CPU)

The oracle. Every later gain compounds through this.

- B1. **Canonical-format LOC.** `count_source` formats first (`ruff format`,
  `prettier --print-width 88`, `rustfmt`) then counts. Falls back to raw count
  if the formatter is missing, and *says so* in the report. Also emit tokens
  and AST-node counts as secondary metrics so a reduction that only merges
  lines is visible. Remove `_looks_minified` once this lands.
- B2. **Tree-sitter API surface** for all three languages: exported/pub
  symbols, class/impl methods, parameter lists with defaults, TS types. One
  extractor, one violation checker, used identically by the tool and the RL
  reward.
- B3. **Cheap pre-gates** in `verify_candidate`: parse / `node --check` /
  `cargo check` before any test run; LOC-not-smaller before parse. Most
  rejections should cost milliseconds.
- B4. **Test-result cache** keyed by content hash of all source files → never
  run the same tree twice (identical candidates from different temperatures
  are common).
- B5. **Parallel verification** — copy the project to N scratch worktrees and
  verify candidates concurrently. `cargo test` and pytest are the wall-clock
  bottleneck; the LLM is idle while they run.
- B6. **Test selection.** Map tests → symbols once per run (coverage.py, c8,
  `cargo llvm-cov`); for a candidate touching symbol S, run S's tests first
  with `-x`, then the full suite only on a pass. Rust: `cargo nextest` and a
  warm incremental target dir.
- B7. **Characterization-test synthesis (the big one).** For any unit whose
  local mutation score is below threshold: ask the LLM for tests, keep only
  those that (a) pass on the original and (b) kill ≥1 surviving mutant. These
  tests are derived from *observed* behaviour, so they cannot be wrong about
  the original; they get frozen into the gate before reduction starts. This
  is what makes the tool usable on the 90% of real code that lacks great
  tests, and it is the direct answer to reward hacking in RL.
- B8. **Differential fuzzing.** For pure/deterministic functions, generate
  inputs (Hypothesis-style from the test suite's argument shapes) and require
  `original(x) == candidate(x)` on a few hundred inputs. Run original and
  candidate side by side in a subprocess. Cheap, strong, language-agnostic.
- B9. **Trust-scaled aggressiveness.** Per-symbol mutation score (not
  per-repo) sets how aggressive the reducer may be on that symbol: high
  score → whole-symbol rewrites allowed; low score → only deletions/rule-based
  rewrites until B7 has raised the score.
- Exit: on the fixtures, hidden-test green stays 100% while the verifier
  rejects a hand-made set of 30 "plausibly wrong" rewrites (regression
  fixtures for the oracle itself); verification time per candidate ≤ ⅓ of
  today's.

### Phase C — the search engine (3-4 weeks, needs GPU)

Turn every LLM call into many verifiable micro-candidates.

- C1. **Hunk decomposition + delta debugging.** Diff the whole-file rewrite
  against the current file, split into hunks at top-level-symbol boundaries,
  then ddmin/greedy over hunk subsets: accept the largest passing subset, then
  retry individual rejected hunks with the failure line as feedback. One
  7B rewrite that is 80% right now yields 80% of its reduction instead of 0.
  Expected effect on the py fixture alone: 12.8% → 25-30% at the same budget.
- C2. **Diff-mode proposals.** Ask for unified diffs / search-replace blocks
  for targeted passes (dedup, if-chain → table) — fewer output tokens, less
  hallucinated context, easier to decompose.
- C3. **Best-of-N + beam.** Sample N candidates per unit at varied
  temperature, verify in parallel (B5), keep the top-k accepted states, and
  continue from each. Stop at fixpoint (no accepted change in a full sweep).
- C4. **Rule library ("peephole optimizer for LOC").** Deterministic AST
  rewrites, each test-gated: `if c: return True else: return False` →
  `return c`; if/elif ladders over constants → dict lookup; append-loop →
  comprehension; manual max/sum loops → builtins; `match` six-arm booleans →
  `matches!`; `for` accumulations → `join`/`reduce`. Rules run before the LLM
  (cheaper, repeatable) and their names go into the prompt as hints for what
  the model should do next. Grow the library from what the LLM actually gets
  accepted (C6).
- C5. **Repo-level context.** Feed the model a symbol index of the whole repo
  and jscpd/CPD duplicate clusters, so cross-file dedup becomes a targeted
  pass: "these 3 functions in 3 files are 90% identical; produce one helper
  and 3 call sites". Verify as a multi-file candidate.
- C6. **Accepted-change mining.** Every accepted hunk is logged as
  (before, after, language, rule-tag). This is simultaneously the RL
  dataset (Phase E), the rule-library backlog (C4), and the few-shot examples
  for the prompt.
- C7. **Budget-aware scheduling.** Rank units by expected yield
  (size × duplication × mutation score × rule hits) and spend the LLM budget
  top-down; stop a unit after two consecutive zero-yield attempts.
- Exit: all three fixtures ≥ 35% with hidden tests green, at ≤ 12 calls of the
  7B model; cost per accepted LOC halves relative to Phase A.

### Phase D — static layer to state of the art (2-3 weeks, CPU)

Cheap, deterministic wins that also stop the LLM wasting tokens.

- D1. Python: `ruff --fix` safe tier automatically, unsafe tier test-gated;
  vulture at 100% confidence for dead code, 60-99% test-gated; unreachable
  branches; unused locals/params; dead files via import graph.
- D2. JS/TS: knip for dead exports/files/deps (replace the regex in
  `static.py`); `tsc --noEmit` as a gate for TS; ESLint `--fix` safe rules.
- D3. Rust: clippy `pedantic`/`nursery` rewrites test-gated, `cargo-machete`
  for deps, `unreachable_pub` + `dead_code` with `cargo fix`.
- D4. Dedup detector (jscpd) as a *report* feeding C5, not a rewriter.
- D5. Static passes become idempotent and re-run after every LLM sweep (LLM
  edits create new dead code).
- Exit: static-only yield on each fixture ≥ 15% (currently 9% py).

### Phase E — data and the public benchmark (4-6 weeks, mostly CPU)

This is how "most effective" becomes a defensible claim.

- E1. **Mining pipeline** per `docs/research/data.md`: GitHub/Stack-v2 seeds →
  clone → green twice → mutation score → unit extraction → dedupe. Target
  2k green repos, ~20k units across py/js/rs, hidden test splits per repo.
- E2. **LOCBench**: 50-100 held-out repos (never trained on) with strong
  tests, hidden tests, and a fixed budget. Metrics = the four axes in §0.
  Publish the harness and a leaderboard with rows for: static-only,
  `less-code` + qwen 7B, + frontier models via the openai-compat backend,
  and the RL policy. Also include QuixBugs as the reward-hacking canary
  (does it delete the buggy-but-tested branch?).
- E3. Regression suite for the *oracle*: the curated "plausibly wrong
  rewrites" corpus from B, grown from every rejected candidate that hidden
  tests caught but the visible gate missed.
- Exit: LOCBench v0 published with the Phase C tool as the first row.

### Phase F — learning (revised 2026-09-01 against the click/tenacity measurements)

No frontier models: the proposer ceiling is closed by TRAINING, and the
session's failure measurements make the case precise. Base-7B budget on
mature external repos: ~40% docs-lost, ~30% duplicate-symbol (whole-file
replies), ~30% not-smaller, 5-15% accepted. **~70% of the waste is
compliance, not capability** — output shape and doc preservation are
exactly what supervised training teaches, and every mined pair carries
them by construction (they passed the gate).

The full loop is now wired:

1. **Mine**: `lc reduce --mine-out pairs.jsonl` appends (system, prompt,
   response) for every ACCEPTED proposal — symbol, whole-file and dedup
   granularities. Mining is rejection sampling with the gate as filter:
   varied temperatures and targets on fixture-grade and real code.
2. **SFT** (`grpo/sft.py`): Qwen2.5-Coder-3B-Instruct QLoRA, conversations,
   same 8GB discipline as train.py. Target: docs-lost <5% (from 40%),
   duplicate-symbol <10% (from 30%), accept rate >=30% (from 5-15%).
   That alone doubles or triples yield per call at ZERO extra inference
   cost — same 7B/3B-class model, better obedience.
3. **GRPO on top** (`grpo/train.py`, TRL 1.x fixed): the reward now includes
   the docs gate (`docs-lost == -1`, same as a behavior break — without it
   RL re-learns doc-eating because code-LOC never counted docs). Rollouts
   on units with known headroom (the mining set says where), curriculum by
   mutation score, mutation-weighted shaping variant already implemented.
4. **Eval** (`grpo/eval_proposer.py`): proposer swap inside the same search,
   same budgets, pristine copies — accepted-LOC per call is the only claim
   that counts. Bars: trained-3B >= untrained-3B trivially; the real goal
   is trained-3B vs untrained-7B (halved serving cost at equal yield).

Risks, honestly: sparse rewards at 5-15% accept rate make pure-GRPO groups
zero-variance (no gradient) — the SFT warm start exists to lift rollouts
off the floor before F2; 8GB forces 3B-class GRPO (7B QLoRA possible but
slow); mining volume must reach hundreds of pairs before SFT generalizes
(`sft.py --validate-config` refuses <50).

- F1. **Expert iteration before GRPO.** Use the Phase C search with the best
  available model to generate accepted (verbose → reduced) pairs on E1 data
  (rejection-sampling fine-tuning). SFT Qwen2.5-Coder-1.5B on these. This
  alone usually captures most of the gain and is stable on 8GB.
- F2. **GRPO on top**, with the reward rebuilt on Phase B: formatted-LOC
  delta, tree-sitter API gate, hidden-test gate, differential fuzz, curriculum
  by mutation score. Group-mixing filter on `frac_reward_zero_std`. Keep
  `dr_grpo`, `beta=0`, add a small entropy floor against the empty-output
  attractor.
- F3. **Policy inside the search.** The trained model becomes a proposer in
  C3's beam alongside the 7B; measure marginal yield per call — the honest
  test of whether RL helped.
- F4. Ablations on LOCBench: base vs SFT vs SFT+GRPO; with/without hidden
  tests; with/without C1 decomposition. Write it up — the gap check in
  `docs/research/rl.md` says nobody has published this.
- Exit: the 1.5B policy beats the untrained 7B on LOCBench yield-per-call
  with zero hidden-test regressions.

### Phase G — product (ongoing from Phase C)

What makes people run it on real repos.

- G1. **Git-native**: work on a branch, one commit per accepted change with
  the verification evidence in the message, `lc reduce --pr` opens a PR with
  the report. Reviewers audit hunks, not a 2,000-line diff.
- G2. **Incremental mode**: only files changed since a base ref; cache
  keyed by file hash. Makes a GitHub Action / pre-merge bot viable.
- G3. **Config + safety modes**: `lc.toml` with per-path aggressiveness,
  protected symbols, minimum mutation score, budget; `--safe` = static +
  rules only, `--aggressive` = full search.
- G4. Correct project mapping: language per directory, real test discovery
  (pytest collection, `package.json` scripts, `Cargo` targets), monorepos.
- G5. Languages: TypeScript properly (D2), then Go (simple AST, `go vet`,
  strong std testing) as the fourth.
- G6. Sandboxed execution (no network, resource limits) for running untrusted
  repos' tests during mining and benchmark runs.

## 3. Dependency graph and sequencing

```
A (measure) ──► B (oracle) ──► C (search) ──► F (learning)
                   │              │
                   └──► D (static)│
                                  └──► E (data+bench) ──► F
G (product) starts after C1 and runs alongside.
```

Suggested order of the next ten working days, all CPU except day 1:
1. A1 + A3 (baseline evidence, hidden tests) — the GPU day.
2. A2 `lc bench`.
3-4. B1 canonical-format LOC, B3 pre-gates, B4 cache (small, high leverage).
5-7. C1 hunk decomposition + ddmin — the largest single yield win, testable
   with the scripted `FakeBackend` in `tests/test_pipeline.py`.
8-9. B2 tree-sitter API surface.
10. Re-run bench; expect py ≥ 25% at the same 6-call budget. That closes
   C3-C5 of `CRITERIA.md` *and* proves the roadmap's first bet.

## 4. Risks and how the plan handles them

| risk | mitigation |
|---|---|
| Weak tests make "tests green" meaningless | B7 characterization tests, B8 differential fuzz, B9 trust scaling, hidden tests everywhere |
| Metric gaming (line joining, minification) | B1 formatted-LOC + token/AST counts; readability judge at eval only |
| Reward hacking in RL | reward = same oracle as the tool (B); hidden tests; QuixBugs canary; API gate via tree-sitter |
| GPU scarcity (shared 8GB) | everything except A1, C, F is CPU; F1 (SFT) before F2 (GRPO); budgets are first-class |
| Flaky tests poison the gate | run baseline twice, quarantine flaky tests, cache by content hash |
| Over-reduction hurts humans | formatted-LOC caps line density; identifier-length and nesting guards; `--safe` mode |
| Claims without evidence | LOCBench with hidden tests; bench delta on every PR |

## 5. Definition of done for "most effective"

- LOCBench published; `less-code` is the top row on yield at 100% hidden-test
  green, ahead of frontier-model zero-shot refactoring and of static-only
  tools, at a stated cost per accepted LOC.
- Runs on an arbitrary well-tested Python/JS/TS/Rust repo from a single
  command and opens a reviewable PR with per-hunk evidence.
- The RL policy is measurably better than its base model *inside the same
  search*, with the ablation written up.

---

## Phase H — external repos (planned 2026-08-31, after the first click sprint)

Phase A-G were written against the fixtures. The first real-repo sprint
(click 8.5.1, commits `6b625a4..6699da9`) measured what actually happens on
a mature library, and every number below is from that run. The plan that
follows is ranked by those numbers, not by intuition.

### What click measured

| fact | number | consequence |
|---|---|---|
| static-only yield | **0.62 %** (dead code 0, ruff ≈ exhausted — click lints itself in CI, outline 1 line, rules 1 line) | deterministic layers bottom out on mature code; Phase D is done for this class of repo |
| 7B per-symbol yield | **+0.17 %** for 40 calls | thin but real; proposer and search both waste most of the budget |
| accept rate | 5/40 = 12.5 % (50 % tests-failed, 27 % not-smaller) | the 7B cannot hold pinned behavior on half its tries |
| dedup executed | 0/2 groups (bad reply format, not-smaller) | the one mechanical layer that targets real fat failed at the proposal step |
| budget allocation | 100 % of calls in `core.py` (biggest-first); 13/17 files never reached | scheduling, not model, cut coverage 13× |
| suite trust | mutation 0.626; 4/17 files invisible on linux (platform-dead + re-exports) → `--skip-files` | the oracle caps aggressiveness; on weak files it certifies nothing |
| accepted patterns | else-after-raise collapse, `None`-guard → `or {}`, loop → `dict.update`, import modernization — **exactly ruff's non-autofixed gap** (RET505/SIM108/PERF403/UP) | the 7B *discovered* deterministic rules; those belong in `rules.py`, not behind a GPU |
| docs | the model's default minimization is deleting documentation; took two gate rounds (docstrings, then `#`/`#:` comments) to make doc-eating impossible | docs are now outside the metric AND touching them rejects |

The deep read: on mature code the remaining fat is (a) lint families that
have no safe autofix, (b) cross-symbol duplication, (c) over-defensive
guards, (d) platform-conditional legacy. (a) is deterministic — the LLM
should *discover* those rules, then never be asked again. (b) is
mechanical — the token diff is already computed; the merge is template
filling, not design. That leaves (c) and (d) as the genuinely-semantic
residue, which is exactly where a stronger proposer and a stronger oracle
matter.

### Sprint A — harvest the autofix gap (CPU + one GPU evening)

Turn the click findings into deterministic, zero-cost yield.

- A1. **Rules from accepted hunks.** Every accepted rewrite from the hybrid
  runs is a candidate `rules.py` entry: else-after-`raise`/`return` collapse
  (provable), loop-merge → `dict.update` with the condition preserved
  (provable), `None`-guard → `or` only when falsy-equivalence is provable
  from types (else keep the guard — click's `or {}` passed only because a
  falsy MutableMapping is an empty one). Each rule ships with an equivalence
  argument and fixture tests, exactly like the existing library.
- A2. **Deterministic dedup merge.** `token_diff_slots` (less_code/dedup.py)
  already computes the exact differing runs. Build the merge mechanically —
  parameterize those runs in member 1's body, emit one-line call members —
  and verify-gate it. The LLM gets asked only when the mechanical merge is
  declined; on click it never got asked successfully at all.
- A3. **Yield-ranked scheduling (C7, now with data).** `lc audit --per-file`
  (the audit already iterates per file; record per-file totals) → rank
  files by code-LOC × per-file mutation score → round-robin the budget
  across them instead of biggest-first. 13 files never visited is a
  scheduling bug, not a model limit.
- A4. **Few-shot from accepted hunks (C6 lite).** One accepted rewrite as an
  example in the symbol prompt: teaches output shape, doc preservation and
  signature-pinning better than instructions do.
- Exit gate: same 40-call click budget → accept rate ≥ 25 %, ≥ 5 files
  touched, static-only click ≥ 1.5 %, ≥ 1 dedup group merged mechanically.

### Sprint B — manufacture trust (B7, mostly CPU + LLM for synthesis)

The external-repo safety story: hidden tests don't exist out there, so grow
them from observed behavior.

- B1. **`lc characterize`**: for units in files whose per-file mutation
  score is below threshold, propose tests; keep only those that pass on the
  original AND kill ≥ 1 surviving mutant; freeze into the gate before
  reduction. Both engines already exist (`mutator.py`, `testrunners.py`).
- B2. Re-audit → un-skip files whose score rose → re-run click on the wider
  surface (`--skip-files` shrinks).
- B3. Differential fuzzing (B8) for pure functions — the cheap supplement.
- Exit gate: click aggregate 0.626 → ≥ 0.75; ≥ 2 of the 4 skipped files
  unskippable; zero hidden-regression catches by the characterization suite
  on the Sprint A evidence tree (it must flag nothing that the frozen suite
  already caught — and at least one injected fault it alone catches).

### Sprint C — proposer ceiling + corpus (one GPU evening + CPU)

- C1. **The frontier-model ablation.** The identical click run through the
  openai-compat backend. This is a decision experiment, not a demo: if a
  frontier proposer multiplies yield (≥ 3×), the RL path (Phase F: SFT on
  mined accepted pairs first, GRPO after Sprint B's oracle) becomes the top
  priority; if it doesn't, the task on mature code is genuinely hard and
  corpus breadth wins instead.
- C2. **Corpus to 8-10 external repos** (py: packaging, tenacity, attrs,
  rich; js: chalk, p-limit; rs: small crates — the staged clones exist for
  three already). Each row: audit → static-only → bounded hybrid → preserved
  evidence dir. LOCBench v0 falls out of this for free.
- Exit gate: proposer ceiling quantified with one number; 8 external rows
  with preserved trees; the RL-vs-corpus decision written down with evidence.

### Sequencing

A before B: A's rules work at any trust level (they are gated), and A3's
per-file audit is B's targeting input. C1 can run any evening the GPU is
free — it needs no new code, only a model name. Nothing here touches the
fixtures' guarantees: every addition is a new layer or a stricter gate,
never a loosened one.
