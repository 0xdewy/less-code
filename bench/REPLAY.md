# Pinned corpus baseline

Experiment: historical unsafe proposal replay; not new inference.

Coverage: 2/2 projects passed. Complete run.

Weighted post-formatter code LOC: **9317 → 9054** (2.82% reduction)

| Project | Language | LOC | static % | total % | Tests/API/docs |
|---|---:|---:|---:|---:|---|
| boltons | python | 8939 → 8676 | 2.94% | 2.94% | pass |
| fastq | javascript | 378 → 378 | 0.0% | 0.0% | pass |

Every revision is pinned in `corpus.toml`; percentages are weighted by code LOC. Passing tests is not proof of semantic equivalence.
Projects with a final regression/validation audit: 2. Audit-based checkpoint selection is not independent held-out evaluation.
Unmeasured projects: 0; their LOC is unknown and excluded from the denominator.
boltons: 1 initially accepted model edit(s) rolled back after audit; final LOC reflects the restored checkpoint.
fastq: 1 initially accepted model edit(s) rolled back after audit; final LOC reflects the restored checkpoint.
