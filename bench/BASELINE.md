# Pinned corpus baseline

Coverage: 12/12 projects passed. Complete run.

Weighted post-formatter code LOC: **21872 → 20207** (7.61% reduction)

| Project | Language | LOC | static % | total % | Tests/API/docs | Shadow oracle |
|---|---:|---:|---:|---:|---|---|
| boltons | python | 8967 → 8138 | 9.25% | 9.25% | pass | 104/264 fns, 8256 calls |
| more-itertools | python | 2642 → 2476 | 6.28% | 6.28% | pass | 77/89 fns, 7626 calls |
| yocto-queue | javascript | 64 → 51 | 20.31% | 20.31% | pass | n/a |
| p-limit | javascript | 489 → 480 | 1.84% | 1.84% | pass | n/a |
| itoa | rust | 414 → 410 | 0.97% | 0.97% | pass | n/a |
| humanize | python | 775 → 719 | 7.23% | 7.23% | pass | 15/15 fns, 5315 calls |
| pyupgrade | python | 5084 → 4544 | 10.62% | 10.62% | pass | 76/78 fns, 5981 calls |
| fastq | javascript | 378 → 358 | 5.29% | 5.29% | pass | n/a |
| picocolors | javascript | 127 → 119 | 6.3% | 6.3% | pass | n/a |
| humantime | rust | 1517 → 1502 | 0.99% | 0.99% | pass | n/a |
| shell-words | rust | 353 → 352 | 0.28% | 0.28% | pass | n/a |
| strsim-rs | rust | 1062 → 1058 | 0.38% | 0.38% | pass | n/a |

Every revision is pinned in `corpus.toml`; percentages are weighted by code LOC. Passing tests is not proof of semantic equivalence.
Projects with a final regression/validation audit: 2. Audit-based checkpoint selection is not independent held-out evaluation.
Unmeasured projects: 0; their LOC is unknown and excluded from the denominator.
Shadow oracle (Python): rewritten functions whose original body ran alongside the rewrite on every suite call, with identical results, exceptions, iterator items and argument mutation. Functions the suite never calls are counted but unverified.
