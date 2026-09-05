# Review of the bounded local-model experiment

Model: Qwen2.5-Coder 7B, digest
`dae161e27b0e90dd1856c8bb3209201fd6736d8eb66298e75ed87571486f4364`.
One attempt on at most eight eligible symbols per project; temperature 0,
seed 42, 16,384-token context, 2,048-token output limit. Recorded attempt and
gate time was 1,457.5 seconds (24.3 minutes), excluding static reduction/setup.

Four proposals passed the normal gates. Review rejected two. The raw results
remain in model-experiment.json and EXPERIMENT.md; no failed proposal has been
hidden.

| Project / symbol | Raw lines saved | Review |
|---|---:|---|
| boltons: `Table._add_horizontal_html_lines` | 7 | Reject: ignores the nesting-depth limit |
| p-limit: `Benchmarker.constructor` | 6 | No issue found: equivalent conditional expression and arithmetic inlining |
| fastq: `benchFastQPromise` | 2 | Reject under the per-symbol contract: forwards a previously discarded callback argument |
| humantime: `Parser.parse_first_char` | 4 | No issue found: removes blocks around single return expressions in match arms |

## Concrete counterexamples

For boltons at the pinned commit, construct an inner `Table([[1]],
headers=['inner'])` and an outer `Table([[inner]], headers=['outer'])`.
`outer.to_html(orientation='horizontal', max_depth=1)` contains **one** `<table`
tag in the original implementation and **two** after applying the recorded
proposal. This was executed against the pinned checkout. The proposal makes
recursive rendering unconditional and changes how the next depth is calculated.

For fastq, a promise resolving to `42` invokes the original success callback
with **zero arguments** and the proposal's callback with **one argument, 42**.
This was reproduced in Node. The existing benchmark callback ignores arguments,
so this is not evidence that the current benchmark loop crashes. It does violate
the reducer's explicit requirement to preserve a selected function for every
input; there is no declared relaxation allowing callback-contract changes.

The other two patches were inspected: p-limit preserves both arithmetic order
and conditional branches; humantime preserves all matched cases and returned
values. They also passed their native suites. This review is not a formal proof.
Six of the ten model lines retained by review are in p-limit's benchmark tooling,
not its shipped concurrency implementation.

## Scores and policy

The raw test-gated result is **406 / 21,854 lines removed (1.86%)**, versus
**371 / 21,854 (1.70%)** before the changes. Of the 35 additional lines,
16 came from static handling and 19 from the model.

For each project with a rejected model proposal, discard its entire model layer
and fall back to its previously gate-verified deterministic stage. Applying this
conservative review policy leaves **397 / 21,854 lines removed (1.82%)**:
**26 additional lines, 16 static and 10 model**. This score uses recorded,
previously verified static states; it is not a claim that a new independent
full-suite audit was run for every project. Static rewrites were not exhaustively
audited either.

All twelve projects passed their configured suites after repairing the
picocolors runner's color environment. Initial failures and the repair are
retained in JSON metadata. Transitive dependency versions are not fully locked;
the experiment is a single-runtime comparison, not a compatibility matrix.
Runtime versions: Python 3.12.13, Ruff 0.16.5, Node 26.1.0, Cargo 1.97.1.

The result supports the small static improvement, but does not justify training
on test-green model edits as positive examples. Half of those accepted edits
failed this review. Improve behavioral validation and candidate selection before
investing in training or simply increasing inference volume.

The counterexamples are now executable regression probes in `heldout.py`, wired
into the boltons and fastq manifest entries. They were added after this experiment
and never fed back to its model. Future runs use these stricter gates; the saved
experiment records correctly retain their original empty audit commands.
