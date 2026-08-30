# Iteration 1 — Research phase

## Attempt
Dispatched 3 parallel research workers (static tools / RL-GRPO / training data),
each with web research and a fixed output contract; synthesized docs/research.md.

## Evidence
- docs/research/static.md (18 sources) — DONE line verified
- docs/research/rl.md (20 sources) — DONE line verified; arXiv gap check:
  no published GRPO-for-LOC-reduction exists → user's idea is novel
- docs/research/data.md (15 sources) — DONE line verified
- docs/research.md — synthesis w/ 4 required sections + architecture decision

## Verdict
- C1 research: PASS (observable: docs/research.md exists, sections a–d, 53 URLs
  across sub-reports, >= 15 cited)

## Gap diagnosis
None for C1. Architecture chosen: 4-layer hybrid (normalize → static-safe →
mutation-audit → verify-gated LLM → GRPO-on-top). Next: implement the tool (C2).
