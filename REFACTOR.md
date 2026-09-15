# REFACTOR.md - superseded by the Rust plan

> **Status note (2026-09-14):** this file's delete-on-first-miss clause was
> REPEALED by PLAN.md §0.3 (the Iteration Protocol: >= 2 models x >= 2
> interface variants x >= 3 gate-feedback retries before any "it doesn't
> work" is legal, with funnel accounting). The feature itself is superseded
> by `lc rewrite` (`less_code/ml_file.py`), which keeps this file's good
> benchmark protocol (Phase 2 inherits it) and fixes the real defect the
> +0.18% run died of: the model had to reproduce headers and docs byte-exact,
> which the host now owns via test-span masking and body/file splicing. The
> design and outcome below are kept for the record.

# REFACTOR.md — model-proposed, gate-verified refactors for Rust

One feature, judged by benchmarks, deleted if it misses the bar.

OUTCOME (2026-09-13): bar missed (+0.18% vs >= 2%, qwen3:8b, all gates
green); feature deleted per the clause below. Recorded negative:
bench/refactor-results.json.

## Design (fixed — do not redesign during implementation)

`lc refactor <path>`: for each Rust source file (largest first), send the whole
file to a model backend (`--ml-cli`, same CliBackend contract as `lc shrink`),
get back the whole rewritten file, then verify through the existing gate stack.
Accept/reject is 100% deterministic — the model only proposes.

Action space is the whole file: stdlib/core idiom swaps, within-file dedup,
table-driving long ladders, branch simplification. Cross-file moves are out
of scope for v1 (not gateable cheaply).

## Non-goals

- No training, GRPO, SFT, or fine-tunes. No new dependencies.
- No Python target (headroom measured ~2%, negative result stands).
- No per-symbol interface, no shadow oracle (Python-only), no new gates.
- No comment/doc removal, no whitespace games — canonical LOC + docs gate
  already make both worthless; do not add new anti-cheat machinery.

## Prerequisites (hard)

1. PLAN.md Phase 6 closed: oracle set-ordering fix and the LOC-accounting
   fix (re-measure `loc_final` after clean-out; comma gate uses project
   formatter config; report-vs-disk assert). This feature's benchmark metric
   is only meaningful on the fixed counter.

## Implementation

New `less_code/refactor.py` (~300 lines) + `lc refactor` subcommand.

Loop per file (skip files that fail to parse):
1. Build prompt: system prompt (below) + full file source + sibling-module
   imports/uses + `cargo clippy` top findings for that file (bounded) +
   rejection feedback from prior attempt.
2. Backend returns `{"path": ..., "content": ...}` (32 KB cap, invalid
   JSON = abstain). NOTE: CliBackend is symbol-shaped (it validates
   `symbol_id` against the requested symbol) — add a small `FileBackend`
   in refactor.py that runs the command, strips code fences, parses the
   same JSON shape, and validates `path` matches the file just prompted.
   Do not modify CliBackend.
3. Verification cascade, cheapest first, first failure reverts and feeds
   the reason back (2 retries per file, then move on):
   a. syntax: tree-sitter parse of the candidate (instant, no semantics;
      standalone `rustc` on one file would spuriously fail on
      `use crate::...` cross-module paths — do not use it)
   b. canonical LOC strictly drops (same counter as pipeline; rustfmt-based)
   c. style: `rustfmt --check` clean after formatting
   d. API: `less_code.api_check` surface unchanged (tree-sitter, already
      rust-capable; `pub` items, signatures, `#[no_mangle]` exports)
   e. docs: comment and doc-comment text preserved (docs gate semantics)
   f. `cargo check` on the tree with the candidate applied (type/borrow
      errors; incremental, much cheaper than a test build)
   g. tests: `cargo test` on the tree with the candidate applied
4. Accepted: leave on disk, record `{file, loc_before, loc_after, attempts,
   duration}`. Rejected: restore, record reason category.

System prompt (exact):
  Rewrite this Rust file to use fewer canonical lines. Semantics must be
  identical. The public API must be byte-identical. Comments and doc
  comments must be preserved verbatim. Whitespace tricks are useless:
  canonical formatting is applied before counting. Prefer std/core idioms,
  removing duplication within the file, replacing hand-rolled logic with
  library calls, and collapsing repetitive ladders into data tables.
  If no safe reduction exists, return the file unchanged.

CLI: `lc refactor <path> [--ml-cli CMD] [--ml-timeout S] [--out REPORT]`;
exit codes mirror `lc shrink` (0 green gates, 1 gate failure); prints the
same summary JSON block with `refactor_loc` instead of layer stats.

Reuse, don't rebuild: `CliBackend` (ml_shrink.py), canonical LOC counter
(loc.py), api_check, test command resolution, report writer. The per-file
gate invocation should call the same primitives `_gate_layer` uses, scoped
to one file; do not fork gate logic.

## Tests

- Unit: prompt build; candidate apply/revert; each cascade stage rejecting
  a crafted bad candidate (syntax error, no-LOC-drop, rustfmt-dirty,
  API-changing, comment-stripping, test-breaking fixture).
- E2E on a small fixture crate: one hand-written model table (same replay
  trick as bench/ollama.py) drives accept and reject paths deterministically
  in CI without a model.
- Full suite + ruff stay green.

## Benchmark protocol (`bench/refactor_bench.py`, ~150 lines)

Rust corpus from corpus.toml (itoa, humantime, shell-words, strsim-rs),
pinned clones. Per project, three arms from identical fresh copies:
  A. `lc shrink` (control)
  B. A + `cargo clippy --fix --allow-dirty` then `lc shrink` idempotence
     check (free-competitor baseline)
  C. A + `lc refactor --ml-cli <backend>` (model + digest recorded, like
     model-experiment.json)
Arm B detail: after `cargo clippy --fix --allow-dirty`, run rustfmt and
measure canonical LOC the same way as the other arms.
Default arm-C backend (no API keys in this environment):
`python3 bench/ollama.py qwen3:8b` — record model name and digest.
Report per project and total: LOC A/B/C, refactor delta %, accept/reject
funnel categories, wall time, model cost if API. Write
`bench/refactor-results.json` + a BASELINE.md-style table.

## Success bar (decided now, not after seeing results)

- C ≥ A + 2% of corpus LOC, all gates green, and C > B.
- Every accepted diff passes cargo test; API surface unchanged.
If the bar is missed after one iteration of prompt/retry tuning: delete
`less_code/refactor.py`, the subcommand, and the bench script; keep only
the results JSON as the recorded negative. No partial keeps.

## Size estimate

~450 lines + tests. One module, one bench script, no config surface.
