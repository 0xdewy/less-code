# Training Data for Test-Verified Code Reduction

Research notes for building RL (GRPO) training data where the **prompt is verbose code** and the **reward is "tests still pass" + LOC delta**. Scope: Rust, JavaScript/TypeScript, Python. All cost figures marked "(est.)" are back-of-envelope estimates from this research, not published numbers.

## Curated datasets

| dataset | languages | has tests? | size | license | suitability for LOC-reduction RL (citations) |
|---|---|---|---|---|---|
| The Stack v2 / StarCoder2 data [1] | 600+ (658 listed); train-smol subset = 17 langs incl. Python/JS/Rust | No (raw files; tests are files in repos, not paired) | 67.5 TB full / 32.1 TB dedup; ~900B tokens train-full; 3.28B files from 104.2M repos [1] | Various per-repo; per-file SPDX via ScanCode; gated access (SWH/INRIA agreement for bulk download) [1] | **High as mining substrate, not as pairs.** Gives repo grouping (`the-stack-v2-train-smol-ids` groups files by repo), star counts (`star_events_count`), license and `is_generated`/`is_vendor` flags per file [1] — exactly the metadata needed to pre-select well-tested small repos before cloning. Near-40% near-duplicate rate among permissive files means you must dedupe yourself [1]. |
| BigCodeBench [2][3] | Python only (executed via unittest harness) | Yes — full `test` field per task (568–14.8k chars of unittest code incl. mocks) [3] | 1,140 tasks (+ Hard subset ~150) [2][3] | Apache-2.0 [3] | **Eval-only.** Tasks are practical (stdlib + popular libs) with runnable, well-mocked tests [3], but 1,140 items is far too small for RL rollouts; contamination risk since it is a public leaderboard [2]. Use as held-out reward-verifier benchmark. |
| HumanEvalPack [5] | 6: Python, C++, Go, Java, JS, Rust (164 tasks × 6) | Yes — `test` + `example_test` fields per task; also ships buggy solutions + bug types [5] | 164 × 6 = 984 rows [5] | MIT [5] | **Eval / curriculum seed only.** Rust and JS variants give cross-language reduction probes; buggy-solution field is designed for repair, not reduction [5]. Too small and heavily contaminated for training. |
| MBPP+ / HumanEval+ (EvalPlus) [6] | Python (+ community ports listed) | Yes — hand-verified, extended test suites; MBPP+ = 399 well-formed tasks from MBPP-sanitized [6] | 399 (MBPP+) + 164 (HumanEval+) tasks [6] | Not stated on leaderboard; releases on GitHub (`evalplus/mbppplus_release`, `humanevalplus_release`) [6] | **Eval-only, high-quality verifier.** Tests were rigorously extended specifically because base tests are weak [6] — a good template for what "excellent tests" means. Too small for RL. |
| CodeContests (AlphaCode) [9] | C++, Python, Java (correct+incorrect human solutions) | Yes — paired input/output test cases per problem; sandboxed executor provided [9] | ~3 GiB; train/valid/test splits; ~13k train problems (train file prints ~13,000 problem lines) [9] | Code Apache-2.0; data CC BY 4.0 [9] | **Medium.** Not (code,unit-test) pairs, but stdin/stdout verification is a perfect executable reward. Reduction target: shorter accepted solution for the same problem — the dataset ships multiple human solutions per problem, so "a shorter passing version exists" is verifiable [9]. Note repo is archived (read-only since Dec 2024) [9]. |
| Project CodeNet [8] | 55 (95% in C++, Python, Java, C, Ruby, C#) | Partially — accepted status judged against hidden test I/O; sample input/output shipped per problem [8] | 13,916,868 submissions / 4,053 problems; 53.6% accepted; full tarball 7.8 GB [8] | Apache-2.0 (tools) [8] | **Medium.** Many accepted submissions per problem with CPU-time/code-size metadata (`code_size`, `cpu_time`) [8] → natural (verbose, terse) pairs selected by code_size among accepted solutions; but verification I/O is samples-only, so re-judging is approximate. Benchmark subsets are already deduped against near-duplicates/identical problem clusters [8]. |
| QuixBugs [7] | Python + Java (40 algorithms each) | Yes — pytest and JUnit suites with passing+failing cases per bug [7] | 40 programs × 2 languages [7] | MIT [7] | **Tiny; eval/canary only.** One-line defects across 14 defect classes, corrected Python versions included [7]. Use as reward-hacking probe (does the model delete the buggy-but-tested branch?). Not training volume. |
| Exercism tracks [10] | ~70 tracks; for us exercism/python, /rust, /javascript | Yes — every exercise ships an official test suite + community exemplar solutions under `.meta/`; CI checks exercises [10] | Python track alone: 100s of exercises (4,528 commits, concept + practice) [10] | MIT [10] | **High for small curated pairs.** Concept exercises are explicitly "constrained to a small set of language features" [10]; exemplar + tests = clean (code, tests) pairs. Size per task is small, so use as curriculum/warmup and few-shot seeds, not the main corpus. |
| CommitPackFT [4] | 277 langs; Python 56k, JS 53k, Rust 3k samples | **No tests** — fields are `old_contents`/`new_contents`/commit message [4] | 702,062 commits / ~1.5 GB [4] | MIT; per-repo license field [4] | **Low-medium.** Only commit-level supervision; no test harness attached. Filter commits whose messages match refactor/simplify regex to get weak (before, after) pairs, then re-verify against the repo's own tests if the repo is still alive. Note: salesforce/CodeRefinement and a public RefactorBench were checked during this research and returned 404/unavailable, so CommitPackFT is the practical public commit-level source. |
| Rosetta Code [14] | 1,005 languages, 1,354 tasks [14] | No — snippet chrestomathy, no tests [14] | ~1,354 tasks | GFDL 1.3 [14] | **Unsuitable** for test-verified RL (no tests; GFDL attribution burdens). Useful only for multilingual style diversity if a verifier is bolted on. |
| Mutation-testing tools (the "excellent tests" filter): cargo-mutants (Rust) [11], mutmut (Python) [12], StrykerJS (JS/TS, 30+ mutators) [13] | per-language | n/a | n/a | OSS (Stryker Apache-2.0 [13]) | Not datasets, but the scoring layer: cargo-mutants aims to run "on any Rust source tree" [11]; mutmut is incremental, knows which tests cover which mutants, parallel, and can emit mutation-score badges/JSON for CI gating [12]; StrykerJS produces per-mutant reports and a public dashboard [13]. |

## Repo mining strategy

GitHub code search supports `filename:`, `path:`, `language:`, `size:` qualifiers; it only indexes the default branch, requires login for global code search, files <384 KB, repos <500k files, and excludes archived repos [15]. `stars:` is a *repository*-search qualifier, so combine a repo search (stars/size/language) with a code search (coverage files) or filter locally after cloning [15].

**Queries (code search, logged in):**
- Committed coverage reports: `filename:cobertura.xml`, `filename:coverage.xml`, `filename:coverage-final.json path:coverage`, `filename:lcov.info`, `filename:lcov.info path:coverage language:typescript`
- CI that gates on coverage: `codecov/codecov-action path:.github/workflows language:yaml`, `pytest --cov-fail-under path:.github/workflows`
- Mutation testing in CI (strongest signal): `cargo mutants path:.github/workflows`, `filename:mutants.out` (cargo-mutants output [11]), `mutmut run path:.github/workflows language:yaml` [12], `stryker.conf.json path:/` [13]
- README coverage badges: `img.shields.io/codecov in:file filename:README.md language:python`

**Repo-search pre-filter (then verify locally):** `language:python stars:50..2000 size:<5000` (small, non-trivial adoption); same for `language:rust`, `language:javascript` / `language:typescript`. The Stack v2 metadata (star_events_count, gha_license_id, is_generated) reproduces this filter offline without hitting the GitHub API for 104M repos [1].

**Per-language candidate repos (small + famously test-focused; verify coverage/mutation score at clone time — coverage drifts):**

- *Python:* `pypa/trove-classifiers` (tiny, pytest, strict CI), `pypa/packaging`, `jaraco/path` (jaraco's suites are pytest-driven with coverage gating), `more-itertools/more-itertools` (huge assertion suite), `hynek/structlog`; plus `exercism/python` exemplars [10].
- *Rust:* `sourcefrog/cargo-mutants` (dogfoods mutation testing [11]), `dtolnay/itoa`, `dtolnay/ryu` (exhaustively tested micro-crates), `dtolnay/proc-macro2`, `mgeisler/textwrap` (documented high-coverage ambitions), `BurntSushi/memchr`; plus `exercism/rust` exemplars [10].
- *JS/TS:* `jonschlinkert/kind-of` (the canonical tiny well-tested util), `sindresorhus/is-odd` (ava-tested micro-lib), `micromatch/micromatch` (very large test suite), `chalk/chalk`, `date-fns/date-fns` (famously coverage-obsessed); plus `exercism/javascript` exemplars [10].

**Extraction rule:** prefer repos whose test files are deterministic (no network, seeded randomness) so the reward is reproducible; Exercism's practice exercises and the above libs largely qualify, while BigCodeBench explicitly needed heavy `unittest.mock` patching to be hermetic [3].

## Test-quality filtering pipeline

1. **Candidate pool:** metadata-only selection from The Stack v2 train-smol repo grouping (star count > 10, permissive license, not generated) [1] union the GitHub queries above [15]. Target 10k–20k repos/lang (est.).
2. **Clone:** shallow `git clone --depth=1`, pinned toolchains per language (Python 3.10–3.13 as Exercism supports [10]; stable Rust; Node LTS). Static filters: src 200–5,000 LOC, test files exist (`test_*.py`/pytest.ini, `tests/` + Cargo.toml `[dev-dependencies]`, `*.test.js`/jest/vitest config), permissive license only [1].
3. **Run tests → keep green:** sandboxed (no network, cgroup/time limits; CodeContests ships a sandboxed executor design worth copying [9]). Discard flakes by running twice (est. ~50–70% survive build+green, based on typical mined-repo attrition).
4. **Mutation-score threshold:** per repo, `cargo mutants` [11] / `mutmut run` [12] / `stryker run` [13], capped at first N≈100 mutants per repo for budget. Keep (repo, module) pairs with mutation score ≥ 60% (est. threshold; mutmut's CI badge export [12] shows teams gate on this). Modules below threshold are dropped or down-weighted: weak tests let the model "reduce" by deleting behavior the tests never checked. Budget: 30 s suite × 100 mutants = ~50 CPU-min/repo → ~1,700 CPU-h for 2k green repos → ~27 h wall at 64 workers (est.); mutmut's incremental cache and per-test mutant mapping cut this further [12].
5. **Extract (code, tests) pairs:** function/module-level pairing via coverage mapping (mutmut already tracks which tests execute each mutant [12]; `cargo mutants` works tree-wide on Rust [11]); keep units covered by ≥1 killing test. Prompt = the original (verbose) unit + test context; oracle LOC delta can be sharpened with CodeNet by checking whether a shorter accepted submission for the same problem exists (ranked by `code_size`) [8].
6. **Dedupe:** exact + near-dup (MinHash/LSH on normalized AST). The Stack v2 found ~40% near-duplicates among permissive files and explicitly warns custom splits leak without dedup [1]; CodeNet ships precomputed near-dup/identical-problem cluster info [8]. Dedupe **before** train/eval split.

**Total cost (est.):** clone+test 10k repos ≈ 300–500 CPU-h; mutation stage ≈ 1,500–2,000 CPU-h; storage trivial (CodeNet 7.8 GB [8], CodeContests ~3 GiB [9]); a few hundred USD of CI-class compute.

## Recommended mix

GRPO rollouts: prompt = verbose code + tests (or stdin/stdout spec), completion = reduced code, reward = `1[all tests pass] + λ·(LOC_orig − LOC_new)/LOC_orig` (plus small penalty for behavior-only-via-mutation-kill audit on eval).

| slice | share | source | rationale |
|---|---|---|---|
| Mutation-verified repo pairs (Py/Rust/JS) | ~60% | mined per strategy above, seeded by Stack v2 metadata [1][11][12][13] | The only slice where "tests pass" is a *trustworthy* reward; mutation score ≥60% certifies the tests actually pin behavior [11][12][13]. |
| Exercism exercise pairs | ~10% | [10] | Clean, small, MIT, hermetic, feature-constrained tasks [10]; ideal curriculum and few-shot anchors. |
| CodeContests/CodeNet shorter-solution pairs | ~15% | [8][9] | Executable I/O reward + provably-existing shorter oracle (min `code_size` among accepted) [8][9]; teaches aggressive but semantics-preserving shrinking on algorithmic code. |
| CommitPackFT refactor/simplify commits | ~10% | [4] | Weak supervision on *real* human reductions (filter `refactor|simplify|clean up` in commit subject [4]); re-verify against live repo tests where possible. |
| Held-out eval (never trained) | 0% train | BigCodeBench [2][3], HumanEval+/MBPP+ [6], HumanEvalPack Rust/JS [5], QuixBugs [7] | Public benchmarks with strong tests; QuixBugs specifically probes deletion-of-buggy-branch reward hacking [7]. |

Pitfalls the mix guards against: (a) weak tests → reward for deleting features (mutation gate); (b) leakage → The Stack v2's own near-dup warning [1]; (c) contamination → eval sets are leaderboard-famous [2][6]; (d) flaky reward → determinism requirement at step 3.

## Sources

1. https://huggingface.co/datasets/bigcode/the-stack-v2 — The Stack v2 dataset card (sizes, languages, SWHID access, license detection, star/repo metadata, near-dup rate).
2. https://bigcode-bench.github.io — BigCodeBench leaderboard (1,140 tasks, Hard subset ~150, calibrated Pass@1, contamination note).
3. https://huggingface.co/datasets/bigcode/bigcodebench — BigCodeBench dataset card (Apache-2.0, 1.14k rows, `test` field with unittest/mocks).
4. https://huggingface.co/datasets/bigcode/commitpackft — CommitPackFT dataset card (702k commits, 2 GB, MIT, fields incl. old/new contents; language split table).
5. https://huggingface.co/datasets/bigcode/humanevalpack — HumanEvalPack dataset card (164×6 languages, test/example_test/buggy_solution fields, MIT).
6. https://evalplus.github.io/leaderboard.html — EvalPlus leaderboard (HumanEval+ v0.1.10, MBPP+ v0.2.0, 399 hand-verified MBPP+ tasks).
7. https://github.com/jkoppel/QuixBugs — QuixBugs README (40 programs, one-line defects, pytest/JUnit tests, MIT).
8. https://github.com/IBM/Project_CodeNet — Project CodeNet README (13.9M submissions, 4,053 problems, 53.6% accepted, `code_size`/`cpu_time` metadata, near-dup clusters, 7.8 GB tarball, Apache-2.0).
9. https://github.com/google-deepmind/code_contests — CodeContests README (sources, paired I/O tests, ~3 GiB, sandboxed execution, CC BY 4.0 data, archived Dec 2024).
10. https://github.com/exercism/python — Exercism Python track (exercises + tests + exemplars, MIT, CI-checked, concept exercises constrained in scope).
11. https://mutants.rs/ — cargo-mutants book (Rust mutation testing, runs on any Rust tree, sourcefrog/cargo-mutants).
12. https://mutmut.readthedocs.io/en/latest/ — mutmut docs (Python mutation testing: incremental cache, per-test mutant selection, parallelism, CI badges).
13. https://stryker-mutator.io/ — Stryker (JS/TS mutation testing, 30+ mutators, dashboard reports, Apache-2.0).
14. https://rosettacode.org/wiki/Rosetta_Code — Rosetta Code (1,354 tasks, 1,005 languages, GFDL 1.3; no tests).
15. https://docs.github.com/en/search-github/searching-on-github/searching-code — GitHub code-search docs (`filename:`/`path:`/`language:`/`size:` qualifiers, default-branch-only, login requirement, size limits).
