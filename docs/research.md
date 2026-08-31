# Research Synthesis — Effective LOC Reduction Without Errors

Synthesized from [static.md](research/static.md) (18 sources), [rl.md](research/rl.md)
(20 sources), [data.md](research/data.md) (15 sources). Numbers in brackets cite
those reports' source lists.

## (a) What actually reduces LOC: the evidence

Ranked by measured/verified yield:

| Technique | Yield | Safety class |
|---|---|---|
| Unused file/export/dep deletion (knip, dead_code) | 6k–300k LOC on mature repos [static-1] | Provably-safe (compiler/type) or test-verified |
| Semantic dedup of copy-paste blocks | up to ~half of duplicated LOC [static-13,14] | Test-verified only (detectors do NOT refactor) |
| Dead/statement-level deletion w/ execution verify | +9.5% verified-deletion coverage (DELSCOUT) [rl-15] | Execution-verified |
| Import/variable lint cleanup (ruff, cargo fix) | small per fix [static-5,10] | Provably-safe tier |
| Formatting (biome, dprint, rustfmt) | ~0 real reduction [static-8,9] | Safe but cosmetic — exclude from metrics |
| Minification | shrinks size, −12pp downstream utility [rl-16] | Rejected — destroys readability |

**Key negative result**: LLM refactoring without verification breaks tests —
24.3% of LLM refactor failures come from context hallucination [rl-13].
Every LLM proposal must pass a frozen-test gate.

**Key insight (user's hypothesis, refined)**: a test suite is only a trustworthy
gate if it actually pins behavior. Coverage proves reach, not checking
[static-16]. **Mutation score is the right oracle** for "tests good enough to
shrink against" [static-15,16,18]. This upgrades the user's "code with great
testing" from a vibe to a measurable filter: mutation score ≥ 60–70%.

## (b) RL/GRPO for code reduction: state of the art and the gap

- GRPO: critic-free PPO variant, group-relative advantage, rule-based verifiable
  rewards, KL now typically off (beta=0) [rl-2,3]. Fits small GPUs because no
  value network — TRL trains 0.5B models in a single-file script [rl-3].
- RL-for-code works with execution/test rewards: CodeRL, RLEF (10x sample
  efficiency), SWEET-RL, ToRL, Qwen3's 4-stage RL [rl-5..8].
- **Verified gap**: arXiv search `GRPO AND "lines of code"` returns nothing —
  no published RL policy optimization with an LOC-reduction reward [rl-20].
  Nearest: DELSCOUT (execution-verified deletion, non-RL ranker) [rl-15].
  → The user's idea is novel and the evidence base supports feasibility.
- Reward design for LOC reduction (adopted): `r = gate × (1 + λ·loc_delta) −
  penalties`, gate = frozen tests pass, penalties for degenerate outputs,
  reward hacking mitigated via frozen/hidden tests, symbol-presence checks,
  multi-metric size (LOC + tokens + complexity) [rl-§Reward design].
- 8GB VRAM recipe: Qwen2.5-Coder-0.5B or 1.5B + QLoRA, G=8–16 completions,
  completion len 512–1024, `loss_type="dr_grpo"`, beta=0 [rl-3,10].

## (c) Training data with strong tests

Full tables in [data.md](research/data.md). Decision:

1. **Core (~60%)**: mined repos filtered by clone→green→mutation-score≥60%
   (GitHub queries for committed coverage/mutation CI; candidate lists per
   language: dtolnay micro-crates, trove-classifiers, kind-of, micromatch,
   cargo-mutants itself, exercism tracks [data-33..37]).
2. **Curriculum (~10%)**: Exercism exercises (MIT, hermetic, exemplar+tests) [data-10].
3. **Shorter-oracle pairs (~15%)**: CodeContests/CodeNet — multiple accepted
   solutions per problem ranked by code_size prove a shorter passing version
   exists [data-8,9].
4. **Weak supervision (~10%)**: CommitPackFT refactor/simplify commits [data-4].
5. **Held-out eval (never trained)**: BigCodeBench, HumanEval+/MBPP+,
   HumanEvalPack (Rust/JS variants), QuixBugs as reward-hacking probe [data-2,5,6,7].

## (d) Chosen architecture: the `less-code` hybrid pipeline

```
                     ┌─────────────────────────────────────────────┐
 target repo ──────► │ L0 normalize (format-only, not counted)     │
                     ├─────────────────────────────────────────────┤
                     │ L1 static provably-safe deletions           │  cheap, deterministic
                     │   ruff --fix(safe) / cargo fix / knip /     │
                     │   vulture@100% / machete                    │
                     ├─────────────────────────────────────────────┤
                     │ L1.5 audit: mutation score of test suite    │  trust oracle
                     │   (built-in mutator, cargo-mutants/mutmut)  │
                     ├─────────────────────────────────────────────┤
                     │ L2 LLM semantic reduction, verify-gated:    │  the big wins
                     │   per-file rewrite → frozen tests → symbol  │
                     │   checks → accept iff green ∧ LOC↓ ; else   │
                     │   revert. Greedy multi-attempt per file.    │
                     ├─────────────────────────────────────────────┤
                     │ L3 GRPO-trained reducer (Qwen2.5-Coder      │  learned policy
                     │   QLoRA): reward = test-gate × LOC delta;   │
                     │   L2's accept loop IS the reward fn.        │
                     └─────────────────────────────────────────────┘
                          ► report: LOC before/after per layer, tests green,
                            mutation score, accepted/rejected candidates
```

**Why this combination (each layer justified by evidence):**
- Static first: milliseconds, provably safe, removes the noise LLMs waste
  tokens on [static-9,10,13].
- Mutation gate before trusting tests: weak tests make the L2 gate a rubber
  stamp and cause reward hacking in L3 [rl-18].
- Verify-gated LLM loop: the accept-if-green-else-revert pattern is exactly
  DELSCOUT's "execution retains authority" principle [rl-15], generalized from
  deletion to whole-file semantic reduction.
- GRPO on top: the user's proposal, made concrete; L2's verifier is reused as
  the reward function so training and inference optimize the same objective.

**Rejected alternatives**: minification (harms utility [rl-16]); zero-shot LLM
refactor without gates (24% breakage [rl-13]); static-only (plateaus at
provably-unused/token-identical [static-13,14]); pure RL without static layer
(wastes samples on trivially-removable code).

## Consolidated source list (≥15)

static.md [1..18]: knip.dev, cargo-machete, cargo-udeps, rustc lints (warn +
allowed-by-default), clippy docs, eslint CLI, biomejs.dev, dprint.dev, ruff,
vulture, pyflakes, jscpd, PMD CPD, mutmut, stryker-mutator.io (×2),
cargo-mutants.
rl.md [1..20]: DeepSeek-R1 (2501.12948), DeepSeekMath (2402.03300), TRL
GRPOTrainer docs, Qwen2.5-Math, Qwen3 blog, CodeRL (2207.01780), RLEF
(2410.02089), SWEET-RL (2503.15478), OPRO (2309.03409), QLoRA (2305.14314),
RL-code survey (2412.20367), REFINE, RefactorAssist, DCE-LLM (2506.11076),
DELSCOUT, token-minification, DiDPO, RobustTests, ToRL (2503.23383), arXiv
gap-verification query.
data.md [1..15]: The Stack v2, BigCodeBench (site + dataset card), CommitPackFT,
HumanEvalPack, EvalPlus, QuixBugs, Project CodeNet, code_contests,
exercism/python, mutants.rs, mutmut, stryker, Rosetta Code, GitHub code-search
docs.

The bare names above are expanded to followable URLs below, so this synthesis
is self-contained (C7 review D8: this document previously carried zero URLs).
### Numbered URL list


**`docs/research/static.md` — 18 sources**

1. <https://knip.dev/>
2. <https://github.com/bnjbvr/cargo-machete>
3. <https://github.com/est31/cargo-udeps>
4. <https://doc.rust-lang.org/rustc/lints/listing/warn-by-default.html>
5. <https://doc.rust-lang.org/rustc/lints/listing/allowed-by-default.html>
6. <https://doc.rust-lang.org/clippy/>
7. <https://eslint.org/docs/latest/use/command-line-interface>
8. <https://biomejs.dev/formatter/>
9. <https://dprint.dev/>
10. <https://docs.astral.sh/ruff/linter/>
11. <https://github.com/jendrikseipp/vulture>
12. <https://github.com/PyCQA/pyflakes>
13. <https://github.com/kucherenko/jscpd>
14. <https://docs.pmd-code.org/latest/pmd_userdocs_cpd.html>
15. <https://mutmut.readthedocs.io/en/latest/>
16. <https://stryker-mutator.io/docs/>
17. <https://stryker-mutator.io/docs/stryker-js/introduction/>
18. <https://github.com/sourcefrog/cargo-mutants>

**`docs/research/rl.md` — 20 sources**

1. <https://arxiv.org/abs/2501.12948>
2. <https://arxiv.org/abs/2402.03300>
3. <https://huggingface.co/docs/trl/grpo_trainer>
4. <https://arxiv.org/abs/2409.12122>
5. <https://qwenlm.github.io/blog/qwen3/>
6. <https://arxiv.org/abs/2207.01780>
7. <https://arxiv.org/abs/2410.02089>
8. <https://arxiv.org/abs/2503.15478>
9. <https://arxiv.org/abs/2309.03409>
10. <https://arxiv.org/abs/2305.14314>
11. <https://arxiv.org/abs/2412.20367>
12. <https://arxiv.org/abs/2608.23611>
13. <https://arxiv.org/abs/2608.00924>
14. <https://arxiv.org/abs/2506.11076>
15. <https://arxiv.org/abs/2608.04611>
16. <https://arxiv.org/abs/2606.01326>
17. <https://arxiv.org/abs/2608.07147>
18. <https://arxiv.org/abs/2608.24135>
19. <https://arxiv.org/abs/2503.23383>
20. <http://export.arxiv.org/api/query?search_query=all:GRPO+AND+all:%22lines+of+code%22>

**`docs/research/data.md` — 15 sources**

1. <https://huggingface.co/datasets/bigcode/the-stack-v2>
2. <https://bigcode-bench.github.io>
3. <https://huggingface.co/datasets/bigcode/bigcodebench>
4. <https://huggingface.co/datasets/bigcode/commitpackft>
5. <https://huggingface.co/datasets/bigcode/humanevalpack>
6. <https://evalplus.github.io/leaderboard.html>
7. <https://github.com/jkoppel/QuixBugs>
8. <https://github.com/IBM/Project_CodeNet>
9. <https://github.com/google-deepmind/code_contests>
10. <https://github.com/exercism/python>
11. <https://mutants.rs/>
12. <https://mutmut.readthedocs.io/en/latest/>
13. <https://stryker-mutator.io/>
14. <https://rosettacode.org/wiki/Rosetta_Code>
15. <https://docs.github.com/en/search-github/searching-on-github/searching-code>

Total: 53 entries, 52 distinct URLs (1 cited in two reports).

