# RL/GRPO for Code Reduction Survey

## GRPO mechanics

GRPO (Group Relative Policy Optimization) was introduced in DeepSeekMath as "a variant of Proximal Policy Optimization (PPO) that enhances mathematical reasoning abilities while concurrently optimizing the memory usage of PPO" [2]. The core difference from PPO: **no critic/value network**. Instead, for each prompt GRPO samples a group of G completions, scores each with a reward function (or model), and uses the group statistics as the baseline [2][3]:

- **Advantage computation**: `A_hat_i = (r_i - mean(r)) / std(r)`, applied to every token of completion i. The mean acts as a learned-free baseline; the std normalization is now optional in TRL (`scale_rewards=False` or `"batch"`), since Dr. GRPO ("Understanding R1-Zero-Like Training", 2503.20783) showed std scaling induces a question-difficulty bias, and Lite-PPO (2508.08221) recommends group-mean/global-std hybrid scaling [3].
- **Reward shaping**: rewards are typically sparse and verifiable (answer correctness, format). DeepSeek-R1 trained with rule-based accuracy + format rewards only, no neural reward model, to avoid reward hacking [1][3]. TRL's `GRPOTrainer` accepts plain Python reward callables (sync or async) that receive prompts/completions plus dataset columns, summed or weighted via `reward_weights` [3].
- **KL control**: original GRPO adds a per-token KL penalty to the frozen reference policy using Schulman's k3 unbiased approximator [2][3]. Modern practice drops it: TRL defaults `beta = 0.0` (KL term disabled), citing Open-Reasoner-Zero, Dr. GRPO, and DAPO, which found the KL term non-essential for GRPO stability [3]. The clipped surrogate objective (`num_iterations > 1`) still bounds policy-ratio drift, and token-level normalization (`loss_type="dapo"`) or constant normalization (`loss_type="dr_grpo"`) mitigate length bias [3].
- **Why critic-free fits small GPUs**: PPO for LLMs requires a value model comparable in size to the policy, plus its optimizer states — roughly doubling model-state memory and often requiring a separate reward model pass as well. GRPO needs only the policy (plus a frozen reference if `beta>0`, which can be skipped entirely at the default `beta=0`), replacing the critic with extra sampling [2][3]. This is why the DeepSeekMath authors explicitly framed GRPO as a memory optimization [2], and why TRL's own minimal example trains a 0.5B model with a single callable reward function [3].

Adoption evidence: Qwen2.5-Math applies GRPO as its final RL stage on top of an SFT model [4]; Qwen3's post-training is a four-stage pipeline — long-CoT cold start, reasoning RL with rule-based rewards, thinking-mode fusion, then general RL over 20+ tasks including coding and agentic skills [5]. DeepSeek-R1 demonstrated the same recipe produces emergent self-verification on verifiable tasks including coding competitions [1].

## RL for code: evidence it works

Execution- and test-based rewards are the dominant verified-reward design for code; multiple papers report concrete gains [7][11].

| Paper | Task | Reward | Result |
|---|---|---|---|
| CodeRL [6] | Program synthesis (APPS/MBPP) | Unit-test pass (sparse) + critic predicting functional correctness (dense) | New SOTA on APPS; zero-shot SOTA transfer on MBPP; critical re-sampling at inference |
| RLEF [7] | Competitive programming, multi-turn repair | Execution feedback (unit tests) fed back as env state; outcome reward | SOTA with 8B and 70B models; 10x fewer samples than independent sampling |
| SWEET-RL [8] | Multi-turn collaborative backend programming (ColBench) | Step-level rewards from a critic with training-time info | +6% absolute success/win rate vs SOTA multi-turn RL; Llama-3.1-8B matches/exceeds GPT-4o |
| ToRL [19] | Math with tool-integrated code execution | Answer correctness after executing model-written code | 43.3% on AIME24, +14% over RL without tools |
| RobustTests [18] | Code generation RLVR (CodeContests+) | Test-suite pass rate; stepwise dense pass-rate shaping | +3% absolute LiveCodeBench on Qwen3-32B; documents reward hacking under weak test coverage |
| DiDPO [17] | Long-horizon agentic coding | Compilation/test verifiable outcome reward, diff-level credit | +10% over comparable agentic RL baselines on Qwen2.5-7B-Coder |
| Qwen3 [5] | Coding/math reasoning + agentic tasks | Rule-based rewards (verifiable) | Four-stage RL pipeline yields frontier-competitive coding benchmarks (vs DeepSeek-R1, o3-mini) |
| DeepSeek-R1 [1] | Math/coding/STEM reasoning | Rule-based accuracy + format rewards | Reasoning behaviors (self-verification, strategy switching) emerge from pure RL; SOTA on verifiable tasks |
| Survey: RL for code LLMs [11] | Compiler opts, resource allocation, code gen | Taxonomy of RL applications | Systematizes RL-for-code literature |

Key transferable lessons: (a) execution feedback as environment state (not just reward) multiplies sample efficiency [7]; (b) sparse outcome rewards alone can train strong policies, but dense/shaped components (critic in [6], stepwise pass-rate in [18]) reduce variance; (c) weak test suites directly cause reward hacking and policy degradation [18]; (d) fine-grained credit assignment over code diffs beats outcome-only rewards in agentic settings [17].

## LLM-based code reduction/simplification evidence

What exists today for LOC reduction / refactoring via LLMs:

- **Refactoring agents (no RL)**: REFINE, a multi-agent evidence-guided refactoring pipeline over 450 Java files, cuts detected code smells by 68-73% using GPT-5.5/Gemini 3.1 Pro/Claude Opus 4.8 — but preservation checks still reveal behavior changes (assert modifications, public-method removal), and the authors require compilation + testing + human review before adoption [12]. RefactorAssist diagnoses why LLM refactorings break tests (24.3% context hallucination, 15.3% renaming errors, 13.7% feature addition, etc.) and recovers a 94.2% cumulative pass rate with static repair + test-log-guided agentic iteration [13].
- **Dead code removal**: DCE-LLM combines a small CodeBERT attribution-based line selector with fine-tuned LLM judgments and patches, achieving >94% F1 on unused/unreachable code detection, beating GPT-4o by 30% [14].
- **Execution-verified code deletion**: DELSCOUT is the closest published work to "LOC reduction as an optimization objective": it formulates redundant-code reduction as proposal scheduling — a ranker (0.5B-8B models) orders single-statement deletion candidates, an execution suite accepts the first passing candidate, and a verifier budget bounds trials; on MBPP it raises verified-deletion coverage by 9.5% relative [15]. Crucially this is inference-time search, not policy optimization — the rankers are not trained by RL [15].
- **Minification**: token-reducing code minification for SWE agents cuts input tokens by 42% at a 12-point resolution-rate cost, showing naive size reduction harms utility [16].
- **LLM-as-optimizer lineage**: OPRO shows LLMs can optimize by prompting with solution-value history (up to +8% GSM8K over human prompts), establishing "scored-candidate iteration" as an alternative to gradient/RL optimization [9]; DELSCOUT's rank-and-verify loop is essentially this pattern applied to deletion [15].

**Gap verification**: an arXiv API search for `all:GRPO AND all:"lines of code"` (executed 2026-08-30) returns no paper using GRPO — or any RL policy optimization — with an LOC-reduction reward; retrieved hits use "lines of code" only incidentally (e.g., describing one-line code changes) [source: arXiv API, see [20]]. GRPO-for-code publications optimize correctness/pass-rate on benchmarks [17][18][7], and code-reduction publications use non-RL search or fine-tuning [12][13][14][15]. **Conclusion: no published GRPO-for-LOC-reduction work was found — the gap is verified as of the search date.** The nearest neighbors are DELSCOUT (execution-verified deletion, no RL) [15] and RLVR code papers (RL, but correctness-only rewards) [18][17].

## Practical GRPO on 8GB VRAM

- **Model choice**: Qwen2.5-Coder-0.5B-Instruct or Qwen3-0.6B is the safe default — TRL's own GRPO quick-start trains Qwen2.5-0.5B-Instruct with a single-file script [3], and Qwen3's small dense models (0.6B/1.7B, Apache 2.0) were explicitly built through the same rule-reward RL pipeline and punch above their size (Qwen3-4B rivals Qwen2.5-72B-Instruct) [5]. The 1.5B Qwen2.5-Coder is the sweet spot for code quality and is feasible on 8GB with QLoRA (4-bit weights ~1GB; adapters + Adam states are small since only LoRA params train) [10]. The 3B model is viable but leaves little room for the generation KV-cache (GRPO holds `batch x num_generations` completions in memory simultaneously) [3] — reduce group size or completion length accordingly.
- **Memory budgeting**: QLoRA's recipe — 4-bit NF4 frozen base, backprop into low-rank adapters, double quantization, paged optimizers — finetuned 65B on 48GB [10], i.e., ~0.6GB/B-params all-in; scaling down, a 1.5B policy fits in ~1-1.5GB for model states, leaving the majority of 8GB for activations and the generation cache (estimate from [10] scaling; not a measured figure). Keep TRL's default `beta=0.0` to avoid loading a second (reference) model copy [3].
- **Batch/seq-len sizing**: keep `per_device_train_batch_size x num_generations` completions modest (e.g., G=8-16 total completions per step) and `max_completion_length` 512-1024 tokens for file-level edits; TRL requires `num_generations` to divide the generation batch and logs `frac_reward_zero_std` so you can spot uninformative groups (all-pass/all-fail) [3].
- **Throughput expectation**: generation dominates step time [3]. TRL reports ~1 day for the full DeepMath-103K GRPO run on Qwen2.5-0.5B across 8 GPUs [3]; a single 8GB GPU should be treated as a small-scale rig (order of a few hundred optimizer steps/hour for a 0.5B with short completions — estimate, not a sourced figure). `step_time` and `completions/*` metrics are logged for empirical tuning [3].
- **vLLM vs HF generate**: vLLM (colocate mode) is the standard accelerator but competes for training memory; TRL exposes `vllm_gpu_memory_utilization` and vLLM sleep mode for OOM cases [3]. On a single 8GB card, the memory-friendly alternative is transformers continuous batching (`use_transformers_continuous_batching=True`, `max_memory_percent` 0.3-0.4), which needs no extra processes and outperforms plain `generate()` at batch >= 32 [3]. TIS importance-sampling correction for the vLLM train/inference mismatch is on by default in TRL [3].
- **Config essentials**: `GRPOConfig(model=..., reward_funcs=[...], num_generations=8, max_completion_length=1024, beta=0.0, scale_rewards="batch", loss_type="dr_grpo")` plus `use_transformers_continuous_batching=True` for single-GPU memory safety [3]. `loss_type="dr_grpo"` matters here: GRPO's default per-sequence normalization carries a length bias, undesirable when the task itself changes output length [3].

## Reward design for LOC reduction

Concrete proposal — gated multiplicative reward:

```
r = gate x (1.0 + lambda x loc_delta) - hack_penalties
gate = 1 if all frozen tests pass else -1        # hard behavior gate
loc_delta = clip((loc_before - loc_after) / loc_before, 0, 0.9)
```

- **Hard test gate (`tests_pass_hard`)**: behavior preservation is non-negotiable, so the shaping term only applies when the full frozen suite passes; failing code gets a fixed negative reward. This mirrors rule-based reward design in R1 (accuracy reward) [1] and RLEF's unit-test outcomes [7]. Add a small format reward (valid code block / diff structure) as in R1's format reward and TRL's example [1][3], and optionally a tiny compile/parse reward for early gradient signal (dense shaping rationale from CodeRL and RobustTests [6][18]).
- **Frozen tests**: the policy must never see or generate the tests that judge it. RLEF executes hidden unit tests inside the environment [7]; RobustTests shows that insufficient or leaky test coverage directly causes reward hacking and policy degradation, motivating faulty-code-driven test synthesis to strengthen the suite [18]. Keep a held-out hidden test split to detect overfitting to the visible suite [18].
- **Reward-hacking modes and mitigations**:
  - *Deleting/weakening the tests themselves* → tests live outside the model's output span; reward function executes only the code block against the immutable suite (env-owned reward, as in RLEF/SWEET-RL environment design [7][8]).
  - *Empty or degenerate output* (`pass`-only file) → min-behavior checks: required public symbols/signatures must still exist and a held-out behavioral probe must pass; DELSCOUNT/DELSCOUT's principle that "execution retains authority over every committed deletion" is the template — every deletion is verified, and ordering bounds the damage of any single bad proposal [15].
  - *Gaming LOC vs real bloat* (one-char names, semicolon-joined lines) → reward chars AND tokens AND a complexity measure, not raw newline count; note that semantically valid minification exists but measurably harms downstream utility (-12pp), so penalize readability-destroying compression [16].
  - *Test overfitting* → hidden split + periodically regenerated tests [18].
  - *All-pass / all-fail groups* → zero advantage, wasted compute; filter prompts by `frac_reward_zero_std` and curriculum-difficulty the dataset so groups are mixed [3].
- **Shaping scale**: with group-relative normalization, absolute reward scale is absorbed by mean/std, but Dr. GRPO's difficulty-bias finding argues for `scale_rewards="batch"` or `False` [3]. Keep `lambda` small (0.1-0.5) so the gate dominates early training and shaping only differentiates among behavior-preserving candidates (consistent with sparse-primary, dense-secondary designs in [6][18]).
- **Length-bias interaction**: because the objective itself rewards shorter outputs, use `loss_type="dr_grpo"` (constant normalization) so the loss does not additionally penalize long completions, and monitor `completions/mean_length` for collapse to degenerate short outputs [3]. A small KL (`beta` slightly > 0) or entropy bonus (`entropy_coef`, or adaptive entropy per Skywork-OR1) can prevent collapse to the empty-output attractor [3].
- **Fallback if RL is unstable on 8GB**: the DELSCOUT result shows non-RL rank-and-verify deletion already achieves verified coverage gains with 0.5B-8B rankers [15], and OPRO-style prompted iteration is a zero-training baseline worth beating [9].

## Sources

1. DeepSeek-R1: Incentivizing Reasoning Capability in LLMs via Reinforcement Learning — https://arxiv.org/abs/2501.12948
2. DeepSeekMath: Pushing the Limits of Mathematical Reasoning in Open Language Models (GRPO origin) — https://arxiv.org/abs/2402.03300
3. TRL GRPOTrainer documentation — https://huggingface.co/docs/trl/grpo_trainer
4. Qwen2.5-Math Technical Report — https://arxiv.org/abs/2409.12122
5. Qwen3: Think Deeper, Act Faster (blog) — https://qwenlm.github.io/blog/qwen3/
6. CodeRL: Mastering Code Generation through Pretrained Models and Deep RL — https://arxiv.org/abs/2207.01780
7. RLEF: Grounding Code LLMs in Execution Feedback with Reinforcement Learning — https://arxiv.org/abs/2410.02089
8. SWEET-RL: Training Multi-Turn LLM Agents on Collaborative Reasoning Tasks — https://arxiv.org/abs/2503.15478
9. Large Language Models as Optimizers (OPRO) — https://arxiv.org/abs/2309.03409
10. QLoRA: Efficient Finetuning of Quantized LLMs — https://arxiv.org/abs/2305.14314
11. Enhancing Code LLMs with Reinforcement Learning in Code Generation: A Survey — https://arxiv.org/abs/2412.20367
12. REFINE: A Multi-Agent LLM Approach for Evidence-Guided Code Refactoring — https://arxiv.org/abs/2608.23611
13. RefactorAssist: Agentic Refinement for Reliable Code Refactoring — https://arxiv.org/abs/2608.00924
14. DCE-LLM: Dead Code Elimination with Large Language Models — https://arxiv.org/abs/2506.11076
15. The Order Is the Guarantee: Verifier-Budgeted Code Deletion with Static-First Learned Proposals (DELSCOUT) — https://arxiv.org/abs/2608.04611
16. Reducing Token Usage of State-in-Context Agents using Minification — https://arxiv.org/abs/2606.01326
17. DiDPO: Diff-in-Diff Policy Optimization for Coding Agent Training — https://arxiv.org/abs/2608.07147
18. Robust Code RL via Faulty-Code-Driven Test case Synthesis and Dense Reward Shaping (RobustTests) — https://arxiv.org/abs/2608.24135
19. ToRL: Scaling Tool-Integrated RL — https://arxiv.org/abs/2503.23383
20. arXiv API gap-verification query (`all:GRPO AND all:"lines of code"`, 0 relevant results, consulted 2026-08-30) — http://export.arxiv.org/api/query?search_query=all:GRPO+AND+all:%22lines+of+code%22
