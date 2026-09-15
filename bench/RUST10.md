# Rust to 10% - the honest report

Weighted canonical code-LOC reduction across the four pinned Rust crates: **3346 -> 3322 = 0.72%** (bar: 10.0%). Unweighted per-crate mean: 0.66% (reported, not targeted - the plan optimizes the weighted bar).

Denominator decomposition: 0 of 3346 start LOC is non-test (0.0%); inline `#[cfg(test)]` modules are frozen on the main track, so the effective reducible mass is the non-test share.

| crate | start LOC | final LOC | % | non-test LOC | test share | levers fired | funnel summary |
|---|---:|---:|---:|---:|---:|---|---|
| itoa | 414 | 410 | 0.97% | n/a | n/a% | static | n/a |
| humantime | 1517 | 1502 | 0.99% | n/a | n/a% | static | n/a |
| shell-words | 353 | 352 | 0.28% | n/a | n/a% | static | n/a |
| strsim-rs | 1062 | 1058 | 0.38% | n/a | n/a% | static | n/a |

## Gate statement

Every accepted edit passed: canonical LOC drop at width 88 AND under
the project's own rustfmt config, docs multiset, API surface, project
style (`cargo fmt --check` where a rustfmt config exists),
`cargo check`, the frozen `cargo test` suite, and the Rust
differential shadow oracle where eligible. Disk-truth LOC assertions
stayed on; invalid runs score zero. Model digests and prompts are
recorded per attempt (ml_records contract).

## Exam/training separation

The four benchmark crates are the exam. They are blocklisted by name,
repo URL and pinned commit in `training/anticontamination.py`, which
scans every mined pair and dataset file; a violation fails the build.
`role = "eval"` crates are held out of every fine-tune. No benchmark
crate appears in training data, prompts, few-shot examples, or SFT
pairs.

## Phase 8 trigger (mechanical, decided on numbers)

weighted total 0.72% < 10.0%: trigger **ARMED** - Phase 8 (mutation-certified test compaction) may be considered, but ONLY with the owner's explicit consent (`owner_consent_recorded`); without it, stop.

Remainder to bar: ~311 LOC. The frozen inline-test mass (see test-share column) holds most of it; the funnel categories above name which rejections dominate which crate.

Provenance: generated from `bench/baseline.json` by `bench/rust10.py`; bars pre-committed in PLAN.md §1/§2.4 before the runs. If the number is below the bar, this report is the deliverable:
 evidence, not excuses, in either direction.
