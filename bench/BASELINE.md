# Pinned corpus baseline

Coverage: 12/12 projects passed. Complete run.

Weighted post-formatter code LOC: **21892 → 19954** (8.85% reduction)

| Project | Language | LOC | static % | total % | Tests/API/docs | Shadow oracle |
|---|---:|---:|---:|---:|---|---|
| boltons | python | 8967 → 8242 | 8.09% | 8.09% | pass | 93/241 fns, 7292 calls |
| more-itertools | python | 2642 → 2481 | 6.09% | 6.09% | pass | 67/77 fns, 7084 calls |
| yocto-queue | javascript | 64 → 51 | 20.31% | 20.31% | pass | n/a |
| p-limit | javascript | 491 → 482 | 1.83% | 1.83% | pass | n/a |
| itoa | rust | 414 → 410 | 0.97% | 0.97% | pass | 2/4 fns, 42 calls |
| humanize | python | 793 → 738 | 6.94% | 6.94% | pass | 15/15 fns, 5315 calls |
| pyupgrade | python | 5084 → 4166 | 18.06% | 18.06% | pass | 68/69 fns, 5314 calls |
| fastq | javascript | 378 → 358 | 5.29% | 5.29% | pass | n/a |
| picocolors | javascript | 127 → 119 | 6.3% | 6.3% | pass | n/a |
| humantime | rust | 1517 → 1498 | 1.25% | 1.25% | pass | 0/5 fns, 0 calls |
| shell-words | rust | 353 → 351 | 0.57% | 0.57% | pass | 0/1 fns, 0 calls |
| strsim-rs | rust | 1062 → 1058 | 0.38% | 0.38% | pass | 0/4 fns, 0 calls |

Every revision is pinned in `corpus.toml`; percentages are weighted by code LOC. Passing tests is not proof of semantic equivalence.
Projects with a final regression/validation audit: 2. Audit-based checkpoint selection is not independent held-out evaluation.
Unmeasured projects: 0; their LOC is unknown and excluded from the denominator.
Shadow oracle (Python): rewritten functions whose original body ran alongside the rewrite on every suite call, with identical results, exceptions, iterator items and argument mutation. Functions the suite never calls are counted but unverified.

Provenance (2026-10-17): full re-run on commit 0bab66e plus the default-profile
rules `merge-imports` and `search-loop-to-next`. The same machine measured the
unchanged 0bab66e code first: **21892 → 20060 (8.37%)**, every row valid
(boltons 7.42%, more-itertools 6.13%, pyupgrade 17.13%, humantime 1.25%,
shell-words 0.57%; other rows as above). This run: 8.85%. merge-imports is
declined by more-itertools' own lint config (E401) and is gate-reverted there;
more-itertools lands one line above the previous run because rule-by-rule
narrowing salvaged pack-assignments in a different order.

The opt-in `--aggressive` profile is recorded separately in
[AGGRESSIVE.md](AGGRESSIVE.md) and never blended into this figure.
