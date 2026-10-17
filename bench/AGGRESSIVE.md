# Pinned corpus - aggressive profile

Coverage: 12/12 projects passed. Complete run.

Weighted post-formatter code LOC: **21892 → 19332** (11.69% reduction)

Profile: **aggressive** (`--aggressive`). Opt-in density layers removed 622 of those lines; the default profile alone reaches 8.85%.

| Project | Language | LOC | static % | total % | Tests/API/docs | Shadow oracle |
|---|---:|---:|---:|---:|---|---|
| boltons | python | 8967 → 7670 | 14.46% | 14.46% | pass | 119/285 fns, 9127 calls |
| more-itertools | python | 2642 → 2462 | 6.81% | 6.81% | pass | 75/88 fns, 7539 calls |
| yocto-queue | javascript | 64 → 51 | 20.31% | 20.31% | pass | n/a |
| p-limit | javascript | 491 → 482 | 1.83% | 1.83% | pass | n/a |
| itoa | rust | 414 → 410 | 0.97% | 0.97% | pass | 2/4 fns, 42 calls |
| humanize | python | 793 → 732 | 7.69% | 7.69% | pass | 16/16 fns, 5715 calls |
| pyupgrade | python | 5084 → 4141 | 18.55% | 18.55% | pass | 74/75 fns, 6135 calls |
| fastq | javascript | 378 → 358 | 5.29% | 5.29% | pass | n/a |
| picocolors | javascript | 127 → 119 | 6.3% | 6.3% | pass | n/a |
| humantime | rust | 1517 → 1498 | 1.25% | 1.25% | pass | 0/5 fns, 0 calls |
| shell-words | rust | 353 → 351 | 0.57% | 0.57% | pass | 0/1 fns, 0 calls |
| strsim-rs | rust | 1062 → 1058 | 0.38% | 0.38% | pass | 0/4 fns, 0 calls |

Every revision is pinned in `corpus.toml`; percentages are weighted by code LOC. Passing tests is not proof of semantic equivalence.
Projects with a final regression/validation audit: 2. Audit-based checkpoint selection is not independent held-out evaluation.
Unmeasured projects: 0; their LOC is unknown and excluded from the denominator.
Shadow oracle (Python): rewritten functions whose original body ran alongside the rewrite on every suite call, with identical results, exceptions, iterator items and argument mutation. Functions the suite never calls are counted but unverified.

Provenance (2026-10-17): `lc corpus --aggressive`, same commit and machine as
BASELINE.md. Every layer passed the same gates (frozen suite, API surface,
documentation, shadow oracle). Per-project aggressive yield: boltons -572
(aggressive rules -174, f-strings -35, in-package fallbacks -295 incl. the
vendored OrderedMultiDict copy in urlutils, notes -> comments -68),
pyupgrade -25, more-itertools -19, humanize -6. The fallback layer removed
documentation only inside deleted dead code (`docs_removed_with_dead_code`),
and dropped the "copy one boltons module out of the package" use case - why
it is opt-in.
