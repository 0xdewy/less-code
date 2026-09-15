# Pinned corpus baseline

Coverage: 12/12 projects passed. Complete run.

Weighted post-formatter code LOC: **21890 → 20063** (8.35% reduction)

| Project | Language | LOC | static % | total % | inline-test share | Tests/API/docs | Shadow oracle |
|---|---:|---:|---:|---:|---|---|---|
| boltons | python | 8967 → 8302 | 7.42% | 7.42% | — | pass | 93/241 fns, 7292 calls |
| more-itertools | python | 2642 → 2480 | 6.13% | 6.13% | — | pass | 68/78 fns, 7093 calls |
| yocto-queue | javascript | 64 → 51 | 20.31% | 20.31% | — | pass | n/a |
| p-limit | javascript | 489 → 480 | 1.84% | 1.84% | — | pass | n/a |
| itoa | rust | 414 → 410 | 0.97% | 0.97% | 0% (414 non-test) | pass | n/a |
| humanize | python | 793 → 738 | 6.94% | 6.94% | — | pass | 15/15 fns, 5315 calls |
| pyupgrade | python | 5084 → 4213 | 17.13% | 17.13% | — | pass | 68/69 fns, 5314 calls |
| fastq | javascript | 378 → 358 | 5.29% | 5.29% | — | pass | n/a |
| picocolors | javascript | 127 → 119 | 6.3% | 6.3% | — | pass | n/a |
| humantime | rust | 1517 → 1502 | 0.99% | 0.99% | 45.6% (825 non-test) | pass | n/a |
| shell-words | rust | 353 → 352 | 0.28% | 0.28% | 41.4% (207 non-test) | pass | n/a |
| strsim-rs | rust | 1062 → 1058 | 0.38% | 0.38% | 50.5% (526 non-test) | pass | n/a |

Every revision is pinned in `corpus.toml`; percentages are weighted by code LOC. Passing tests is not proof of semantic equivalence.
Provenance (2026-09-14, Phase 0 of the Rust plan): the rust rows gained the inline-test share column, computed from `rust_rules.test_spans()` over the pinned clones (`measure()` per span, canonical): itoa 0/414 (its tests live in `tests/`, already excluded from source LOC), humantime 692/1517 = 45.6%, shell-words 146/353 = 41.4%, strsim-rs 536/1062 = 50.5%. No yield numbers changed: this phase measures the denominator, it does not shrink it.

The Rust rows carry 41-51% frozen inline-test LOC in their denominators (0% for itoa, whose tests live in `tests/`), so cross-language percentages understate Rust's non-test reduction.
Projects with a final regression/validation audit: 2. Audit-based checkpoint selection is not independent held-out evaluation.
Provenance: the Python rows with comma collapses (boltons, more-itertools,
humanize, pyupgrade) were re-measured on 2026-09-13 with the LOC-accounting
fixes; the other eight rows are carried from the same day's full run (their
code paths and numbers are unchanged). That run had already carried the
shadow-oracle set-fix: `deepcopy` re-orders set arguments, so the shadowed
original iterated a differently-ordered copy and the sequence comparison
recorded a spurious `iterator-item` mismatch in more-itertools'
`gray_product`, reverting the comma layer in earlier runs (8.45% twice); set
copies are now order-faithful (`set.copy()`), iterator mismatches get the
eager path's second-original-run nondeterminism check, and mismatch
diagnostics normalize set/frozenset reprs. Three honesty fixes are included.
First, the layer gate renders proposals under the project's own ruff config
(ruff.toml / .ruff.toml included): humanize sets `fix = true` in
`[tool.ruff]`, and under its own config the comma layer's line joins do not
survive, so that layer now commits nothing instead of silently counting them.
Second, the start measurement is re-based after the baseline test run:
humanize's test command (`uv --with-editable`) makes hatch-vcs generate
`src/humanize/_version.py` (+18 LOC) mid-run, which the frozen start-time
file list never saw; start and end now cover one file universe and any
universe drift after the baseline fails loud instead of printing a note. The
honest row is 793 -> 738 (6.94%), not the earlier 775 -> 685 (11.61%). Third,
`loc_final` is disk truth, re-measured over a fresh file map and asserted
equal to the tree at report time. The corpus lands at 8.35%, below the 8.5%
Track A target - the bar is truth, not the target.
