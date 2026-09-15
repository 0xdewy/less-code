# Verdict after the first model experiment

The result is not yet compelling. The review-filtered reduction was 1.82%,
versus 1.70% before these experiments. Of 26 additional lines removed, 16 came
from deterministic formatting integration and ten from the model. Six of those
ten model lines were benchmark tooling; only four were core library code.
Two of four initially accepted model proposals failed review. Approximately
24 minutes of recorded model-attempt/gate time is a poor return for that gain.
See [the original comparison](EXPERIMENT.md) and [review](MODEL_REVIEW.md).

## Implemented response

- Corpus audit failures now automatically restore a checkpoint: original source
  for a failed static layer, or audited static source for a failed model layer.
  Static failure skips model search. Audit output never enters retry prompts.
  Validator exceptions also restore the relevant checkpoint before propagating.
- Raw proposal acceptance remains in the evidence, with rollback flags and
  separate retained-edit/LOC counters. Returned trees undergo final tests and
  a final corpus audit. Invalid final trees still score zero.
- Python ranking counts executable statements instead of raw lines, so long
  docstrings do not dominate selection. The existing selection budget remains.
- Declaration checks handle decorated Python method indentation and ignore
  inter-token JS/Rust whitespace without ignoring literal values or punctuation.
  Rechecking the historical replies allows two of fifteen previously rejected
  declarations through this gate. This does **not** establish that those edits
  are correct or reduce canonical LOC.

## What this evidence does not establish

The completed [replay](REPLAY.md) passed on both fresh checkouts. Boltons
returned to its 8,676-line static checkpoint (263 lines saved); fastq returned
to 378 lines (zero saved). Both known unsafe edits passed the ordinary proposal
gate, failed the terminal audit, and were automatically rolled back. Final
tests and audits passed. The local suite passed 241 tests; targeted Ruff checks
and `git diff --check` also passed.

The rollback benchmark (`uv run python -m bench.replay`) replays the two known
unsafe replies against fresh pinned repositories, matching exact original
symbol text. It makes no inference calls and is a regression test, not a new
compression experiment. Its results are saved separately in REPLAY.md/replay.json;
the original twelve-project model results remain in model-experiment.json.

Audits used to select checkpoints are validation data, not independent held-out
evaluation. Known regression probes are not comprehensive behavioral oracles.
Neither the new ranking's model efficiency nor a higher full-corpus reduction
has been demonstrated. No training or additional model download was performed.

The next useful experiment is a separately frozen evaluation set with broader
behavioral checks and a fixed inference-time budget. Measure retained core-code
reductions separately from tooling reductions, plus review failures and time.
Do not train on test-gated acceptances: this experiment showed that those labels
contain real regressions. Larger structural simplifications remain an unproven
direction, not an implemented capability.

## Rust expansion cohort must be selected before yield is measured

Any proposed widening of the Rust cohort must be selected before measuring
yield on it, under the same policy the Python expansion cohort followed:
candidates qualify on realistic redundancy (compatibility shims kept for old
callers, feature-gated code paths, hand-rolled CLI plumbing), not on whether
a rule is known to fire. Hand-picking repositories because today's rule
library scores well on them converts a benchmark into a demonstration. The
frozen-test share of each candidate's denominator should be recorded
alongside any yield number for the same reason.

## Oracle v2: constructed self types

The bounded v1 oracle refuses methods, which is where humantime's logic
actually lives: all 5 functions its run changed take `&self`/`&mut self` (or
a `Formatter`), so the oracle verified nothing on that crate. The natural
extension is methods on constructible types: the shadow mod builds a value
via `Default`/`new()` (plus field-wise simple constructors), calls
`receiver.method(...)` on it for both bodies, and refuses anything whose
receiver type is not constructible without generics, traits, or unsafe.
Land this only after v1's mismatch-report shape has proven stable in use.

