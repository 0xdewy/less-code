# Benchmark experiment

**Review-filtered gain: 26 additional lines.** The table retains raw test-gated scores, including edits subsequently rejected in review.

| Project | Cohort | Before % | After static % | After model % | Comparable |
|---|---|---:|---:|---:|---|
| boltons | original | 2.94 | 2.94 | 3.02 | yes |
| more-itertools | original | 1.21 | 1.81 | 1.81 | yes |
| yocto-queue | original | 0.0 | 0.0 | 0.0 | yes |
| p-limit | original | 0.0 | 0.0 | 1.23 | yes |
| itoa | original | 0.0 | 0.0 | 0.0 | yes |
| humanize | expansion | 2.32 | 2.32 | 2.32 | yes |
| pyupgrade | expansion | 1.14 | 1.14 | 1.14 | yes |
| fastq | expansion | 0.0 | 0.0 | 0.53 | yes |
| picocolors | expansion | 0.0 | 0.0 | 0.0 | yes |
| humantime | expansion | 0.0 | 0.0 | 0.26 | yes |
| shell-words | expansion | 0.0 | 0.0 | 0.0 | yes |
| strsim-rs | original | 0.0 | 0.0 | 0.0 | yes |

Comparable projects: 12/12.
On the same 21854 starting canonical LOC: before 21483, after static 21467, after model 21448.
Additional accepted lines: 35 (16 static, 19 model).

original: 6/6 valid; 13620 → 13296 LOC (2.38% weighted); median project 0.61%.
expansion: 6/6 valid; 8234 → 8152 LOC (1.00% weighted); median project 0.40%.

Review-filtered comparable result: 21854 → 21457 LOC; 26 additional lines (16 static, 10 model).
Rejected model layers fall back to their previously verified static stage. This is a post-run review policy, not an automatic semantic proof. See MODEL_REVIEW.md for counterexamples.

Model outcomes: {'docs': 26, 'syntax': 7, 'accepted': 4, 'loc': 17, 'tests': 6, 'declaration': 15}.
Recorded model-attempt and gate time: 1457.5s.

These are gate-accepted reductions, not a proof of semantic equivalence. Failed projects remain visible and score zero. Unmeasured projects cannot contribute a known LOC denominator. Corpus expansion was selected before measuring yield; it is still mostly small libraries, not a representative sample of all software. Zero reduction can reflect missing candidate support (for example JavaScript arrow functions). Rust inline tests are frozen but still included in source-file LOC, so cross-language absolute LOC is not strictly production-only.
