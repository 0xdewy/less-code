# Implementation plan: comma-collapse layer + consented deletion proposer

For the implementing agent. Read this whole file before writing code. Where this
plan says "pre-decided", do not relitigate — the decisions encode measurements
and failure modes already paid for. Estimated total: ~1,200 net new lines
including tests, across 3 new modules, over 6 phases.

## Measured context (why this plan is what it is)

Independent audit, 2026-09-11, on the pinned corpus + local real repos:

- Dead code by reference scan: ~0 everywhere (well-maintained code has none).
- Duplicates: ~250 LOC, boltons only.
- Magic trailing commas: 519 LOC on the Python corpus (pyupgrade 454,
  humanize 35, more-itertools 19, boltons 11) = +2.4pp on the 6.17% baseline.
- Line census: ~30% headers + ~20% jump/import + ~15-25% wrapped continuations
  are structurally irreducible. Deterministic semantic-preserving rewriting
  cannot double corpus yield; it plateaus near 9-10% with commas included.
- The recorded per-symbol model experiment added 26 lines on 21,854 LOC.
  Micro-rewrites are not worth their gate cost. Deletion proposals are the
  inverse ratio: one gate run per large, mechanically-applied edit.

Success is therefore defined on two tracks:
- Track A (deterministic): corpus 6.17% -> >= 8.5% via comma collapse.
- Track B (consented deletion): on application-style repos (see
  `bench/apps.toml`), accepted deletions of >= 15% of canonical LOC with all
  gates green and every deletion individually consented and reported.

## Ground rules

- Workspace: `/home/user/code/less-code`. Python 3.12+, `uv run` for everything.
- After every phase: `uv run pytest -q` (must stay green, currently 323),
  `uv run ruff check less_code/ tests/ bench/`,
  `uv run ruff format --check less_code/` — fix before moving on.
- NEVER weaken an existing gate, NEVER modify what `lc shrink` does to a tree
  except by adding the comma layer (Phase 1). `lc propose` is a separate
  command that never runs without an explicit flag.
- NEVER delete or edit files under `fixtures/`, `bench/*.json`, `bench/*.md`
  except where a phase says to regenerate them.
- New dependency budget: at most one — `coverage` (Phase 2), and only as an
  optional accelerator with a static fallback. No others, ever.
- No comments in code unless a docstring explains a non-obvious invariant
  (the existing codebase style: module docstring + docstrings on public
  functions, sparse otherwise).

---

## Phase 1 — comma-collapse layer (mechanical, ~120 lines)

Goal: removing a "magic" trailing comma is a semantically null source edit;
the canonical formatter then collapses the exploded block. Counted honestly
by the existing post-format metric. Reported as its OWN layer and stat so the
"semantic reduction" number stays comparable.

### 1.1 New module `less_code/comma.py`

Public function:

```python
def collapse_magic_commas(source: str) -> tuple[str, int]:
    """Remove trailing commas that pin a multi-line block open.

    Returns (new_source, commas_removed). Removals happen ONLY when:
      - the comma is the last non-whitespace, non-comment token on its line,
      - the matching closing bracket starts a later line,
      - nothing but whitespace sits between the comma and the line end
        (a trailing comment pins the comma; leave it alone).
    """
```

Implementation contract (tokenizer, not regex — a regex here will misfire on
brackets inside strings and f-strings; this is pre-decided):

1. `tokenize.generate_tokens` over the source. Track a stack of openers
   (`(`, `[`, `{`) from OP tokens; the stack entries record the LINE the
   opener started on.
2. When an OP token is a closer: pop the stack. If the token immediately
   before the closer (skipping NL, NEWLINE, COMMENT, and non-logical tokens)
   is a COMMA, AND that comma is on an earlier line than the closer, AND the
   text between the comma's end and the line end is whitespace only (check
   with the raw source slice), then mark the comma's exact
   `(start, end)` span for deletion.
3. Apply deletions right-to-left on the raw text. Return the count.

Edge cases that MUST pass (write them as tests):
- `foo(\n    a,\n    b,\n)` -> comma after `b` removed.
- `foo(\n    a,\n    b,  # keeps this\n)` -> NOT removed (trailing comment).
- `d = {\n    "k": v,\n}` -> removed.
- One-line calls `f(a, b,)` -> NOT touched (same line as closer).
- Nested: inner and outer magic commas both removed, independently.
- Syntax-invalid input: return it unchanged, 0 (wrap tokenize in try/except
  `tokenize.TokenError`, `IndentationError`, `SyntaxError`).
- After collapse + `canonical_format` (from `less_code.loc`), the result must
  `ast.parse` cleanly — assert this in the pipeline hook anyway.

Python only. Prettier re-adds JS trailing commas per its config (no-op) and
rustfmt does not explode trailing commas the way ruff format does. Do not
build this for JS/Rust; that was measured to be worthless.

### 1.2 Pipeline wiring

In `less_code/pipeline.py`:

- Import at top: `from .comma import collapse_magic_commas`.
- Add a transform function next to `_apply_ruff_again`:

```python
def _apply_comma_collapse(sources: dict[Path, str]) -> tuple[dict[Path, str], list[str]]:
    out, notes = dict(sources), []
    for path, text in sources.items():
        new_text, removed = collapse_magic_commas(text)
        if removed and new_text != text:
            out[path] = new_text
            notes.append(f"{path.name}: collapsed {removed} magic trailing comma(s)")
    return out, notes
```

- In the Python layer tuple inside `shrink_project` (currently
  `("rules", ...), ("ruff-again", ...), ("rules-again", ...)`), append
  `("comma-collapse", _apply_comma_collapse)` as the LAST deterministic layer.
  Ordering rationale (pre-decided): the comma edit changes line content the
  rules' span math reads; running it last avoids interleaving. It goes through
  the same `_gate_layer` as every other layer — LOC/style/docs/API/tests/oracle.
- `ShrinkStats`: add field `comma_collapse_loc: int = 0`. After the layer loop,
  set it to `loc_before - loc_after` of the comma layer record (0 if reverted).
  Add it to `to_json()`.

### 1.3 Docs and criteria (exact edits)

- `CRITERIA.md`: under "Out of scope / accepted trade-offs", the line
  "Formatting as a reduction strategy is excluded" stays, but append to it:
  "Exception: a magic trailing comma is a source edit, not formatting config;
  its collapse is gated like any rewrite and reported as a separate layer and
  stat (`comma_collapse_loc`), never inside the semantic reduction figure."
- `README.md`: add one bullet under "What it shrinks" and mention the separate
  reporting in Safety.

### 1.4 Tests (`tests/test_comma.py`)

Every edge case in 1.1, plus one pipeline-level test: a tmp project whose
source contains a magic-comma call, monkeypatch nothing, assert the file on
disk changed, `stats.comma_collapse_loc > 0`, suite green, and the collapse
appears as its own `layer_records` entry. Copy the test-construction pattern
from `tests/test_pipeline.py::_ml_project`.

### 1.5 Re-baseline

`uv run lc corpus --out bench/baseline.json --markdown bench/BASELINE.md`
(full 12-project run; budget ~60-90 min). Verify the python rows gain
roughly the comma numbers above (pyupgrade ~+8pp). Append a dated provenance
line to BASELINE.md like the existing one.

---

## Phase 2 — deletion candidate miner (deterministic, ~300 lines)

New module `less_code/propose.py`. This phase produces candidates only: a
JSON list of symbol groups that are provably unreferenced by anything the
repository can express, with evidence. NO edits happen yet.

### 2.1 Data model

```python
@dataclass
class Candidate:
    kind: str            # "function" | "method" | "class" | "const" | "module"
    name: str
    file: str            # repo-relative
    loc: int             # canonical LOC of the span (less_code.loc.measure)
    start_line: int      # 1-based, first decorator if decorated
    end_line: int
    covered_by_tests: bool | None   # None when coverage unavailable
    references: list[str]           # repo-relative files with a live reference
```

A proposal group is a set of Candidates forming a reference-closed component
(2.4). Groups carry a combined `loc` and the union of evidence.

### 2.2 Reference scanning — reuse, do not reinvent

Use the exact semantics of `less_code/static.py::_names_referenced_outside`
(word-boundary regex over file contents, definition-site lines excluded) but
scan a WIDER corpus — this is the critical difference:

- Reference corpus = EVERY file in the repo: all `.py` including tests, plus
  `.md`, `.rst`, `.cfg`, `.toml`, `.ini`, `.txt`, `.yml`, `.yaml`, `.json`
  read as text. A symbol mentioned in a docstring or config is REFERENCED
  (conservative in the safe direction: fewer proposals, never wrong ones).
- Candidate corpus = `map_project(root).source_files` only.

Extract `_names_referenced_outside` into a shared helper (move it to a new
`less_code/refs.py` or keep it in static.py and import; pick ONE, update
static.py's callers, keep its behavior byte-identical — its unit tests must
not change).

### 2.3 Never-propose rails (hard rules, each is a test)

A candidate is DISCARDED if any apply. These are all real traps; several were
found the hard way in this repo's history:

1. **Dynamic discovery**: if ANY scanned `.py` contains
   `pkgutil`, `import_module`, `walk_packages`, `iter_modules`,
   `entry_points`, or `__subclasses__`, then no candidate may live in that
   file's directory subtree (its package root is dynamically discovered —
   pyupgrade's plugin registry looks unreferenced but is loaded by
   filesystem). Corpus regression fixture: a tmp repo reproducing the
   pyupgrade `_data.py` + `_plugins/` pattern must produce ZERO candidates.
2. **String-dispatched names**: if a source file contains a string literal
   matching `_[a-z_]+` used near `getattr(`, or f-string/format concatenation
   feeding `getattr`/`globals()`, drop every private symbol whose name could
   be produced by those patterns. Conservative shortcut (pre-decided): if
   `getattr` or `globals()` appears anywhere in the repo, drop ALL `method`
   candidates and any function whose name shares a 6+ char prefix with a
   string literal in the repo.
3. **Decorated definitions**: any decorator disqualifies (registration:
   `@app.route`, `@pytest.fixture`, plugin registries).
4. **Dunder and protocol surface**: `__`-prefixed anything is never proposed.
5. **Entry points**: files named in `static.py::_ENTRY_POINT_FILES`, plus
   `scripts/`, `bin/` dirs, plus every `console_scripts` / `scripts` entry
   parsed out of `pyproject.toml`/`setup.cfg` — their referenced names are
   live, and files under them are never candidates.
6. **`__all__`, `__version__`, module `__getattr__`**: a module defining
   `__getattr__` or `__dir__` at top level gets no candidates at all.
7. **Assignment with side effects**: const candidates must be
   `ast.Assign` with only `ast.Name` targets and a literal or tuple-of-literal
   value (`ast.Constant`, `ast.Tuple` of Constants). Anything else executes.
8. **Test-referenced**: referenced by any test file -> live (already covered
   by the wider scan; restated because it is the most common false positive).
9. **Non-private symbols**: only proposed when `--app` mode (2.5) is on AND
   the repo has no `[project]`/`setup.py` library packaging marker, OR the
   symbol is `_`-private. Libraries keep their public API; that is the
   product's contract.

### 2.4 Component grouping

Symbols reference symbols. Deleting `_a` that is only called by dead `_b`
requires deleting both. Algorithm (pre-decided, simple):

1. Nodes: all candidates from 2.2/2.3 + all non-candidate top-level symbols
   (live nodes).
2. Edge `s -> t` if symbol `s`'s OWN definition body (source segment between
   its start/end lines) references name `t` (word-boundary).
3. A candidate group = maximal set of candidates where every edge from a
   group member goes to another group member, and NO live node has an edge
   into the group. Compute by: start from each candidate with zero in-edges
   from live nodes; flood-fill over candidate-to-candidate edges.
4. Whole unused MODULES: after grouping, a module whose every top-level
   symbol landed in candidate groups (and which passes rails 1/5) becomes a
   single `module` candidate spanning the file.

### 2.5 Modes

- Default (library mode): only `_private` symbols. This is what runs on the
  corpus; expected to find almost nothing (measured) — that is correct
  behavior, not failure.
- `--app`: allow non-private symbols when the target has no library
  packaging (`pyproject.toml` `[project]` with anything but scripts, no
  `setup.py`). This is the mode for Track B.

### 2.6 Coverage overlay (optional accelerator)

If the target's interpreter can `import coverage` (try
`<interpreter> -c "import coverage"`; the interpreter is the one running the
test command — for a custom `--test-command` just try `sys.executable` and
skip if unsure): run the frozen gate command under
`coverage run --parallel-mode`, parse `coverage combine`'s data with the
`coverage` API (or `coverage json`), mark each candidate
`covered_by_tests`. UNCOVERED candidates are ranked FIRST in the report.
Never let coverage alone qualify a candidate — rails + gate still apply.
If coverage is unavailable: `covered_by_tests = None`, continue.

Do NOT add `coverage` to the project dependencies; shell out and degrade.

### 2.7 Unit tests (`tests/test_propose.py`)

One test per rail (build tiny tmp repos), plus: grouping transitively closes;
module promotion; library mode ignores public; app mode requires no
library packaging; pyupgrade-pattern fixture yields zero.

---

## Phase 3 — application, verification, consent (~250 lines)

Still inside `less_code/propose.py`. This is the part to get exactly right.

### 3.1 Mechanical application of a group

For each candidate, delete source lines `[start_line, end_line]` using the
kill-line algorithm ALREADY PROVEN in `static.py::_python_remove_dead`:
consume the blank lines directly above, plus a comment block tightly attached
above those blanks, but stop at the first blank line that separates it from a
live neighbor. Extract that logic into a shared helper rather than copying it
(static.py's behavior must remain identical — its tests are the contract).
Modules: delete the whole file (leave directories; remove empty `__init__`
never).

After applying a group: `ast.parse` every touched file. Any SyntaxError ->
abort the group (report why), do not partially apply.

### 3.2 Verification — same primitives, own loop

Write `verify_tree(root, project, test_command, timeout) -> GateReport` in
`propose.py` reusing the exact primitives and ORDER that
`pipeline.py::_gate_layer` uses (read it first): docs multiset
(`documentation_layout`), tests (`run_tests`), then — Python only — the
shadow oracle (`changed_functions` + `run_shadow`) IF any surviving function
was modified. Deletion-only groups delete and do not modify survivors, so the
oracle will find nothing to shadow; that is expected and fine (pre-decided:
a deleted function cannot be shadowed; the suite is its gate, and rails
already excluded everything the suite exercises when coverage was available).

The API gate CHANGES MEANING here — this is the one deliberate contract
change in the plan:

- Run `api_violations(before, after)` as usual. For deletion groups, the
  violations list IS the "what you would lose" disclosure, not an abort.
- A group is `api_clean` iff every violation corresponds to a symbol IN the
  group (deleting `_x` removes `_x`; fine). A violation naming something NOT
  in the group (re-export chains, `from m import *` re-exports) disqualifies
  the group — that means live surface depends on it.

### 3.3 Consent protocol (never silent, never interactive-by-default)

- `lc propose <path>` computes groups, verifies NOTHING yet, writes
  `proposals.json` and prints a markdown table (name, kind, file, LOC,
  covered_by_tests, api delta) + the exact command to apply.
- `lc propose <path> --apply 1,3,7` applies groups by id: for each, apply ->
  verify -> on failure revert that group (snapshot/restore the tree per
  group; reuse the `_snapshot`/`_restore` pattern) and record the gate
  reason; on success keep and record `GateReport`.
- `--apply all` exists. `--yes` does not exist. Deletions are consented by
  explicit group id, always. A proposal file older than the tree's current
  hash (`testrunners.tree_hash`) is rejected: "re-run lc propose" (prevents
  stale-span applications — same class of bug the ML layer's stale-span
  tests guard against; read `tests/test_pipeline.py` for those patterns).
- Always operate on `--copy-to` semantics internally: `lc propose` makes a
  sibling copy `<path>-proposals` unless `--in-place` is passed. Originals
  are sacred.

### 3.4 Report

`proposals.json`: per group — id, candidates, loc, evidence (references
empty, covered_by_tests), gate result, api delta, applied bool. A top-level
`consent` block records command line, tree hash, timestamp. This file is the
audit trail; never overwrite it — append `runs: []`.

---

## Phase 4 — CLI + README (~100 lines)

`less_code/cli.py`: add `propose` subcommand wiring Phase 2-3. Flags:
`--lang`, `--app`, `--apply`, `--copy-to/--in-place`, `--out proposals.json`,
`--test-command`, `--timeout`, `--coverage/--no-coverage`.

Exit codes: 0 = nothing to do / applied cleanly; 1 = gate failure on every
requested group; 2 = unapplied proposals awaiting consent; 3 = stale tree
hash. README: new section "Deletion proposals" — three paragraphs: what it
proposes, that nothing is ever deleted without explicit group ids, and the
rails list summarized.

---

## Phase 5 — model-assisted grouping (optional, only if Phases 2-4 land early)

Reuse `less_code/ml_shrink.py::CliBackend` (read its protocol first). The
model's ONLY allowed outputs, validated by the host against the candidate
set: merge groups, split groups, rank by putative coherence, write a one-line
rationale per group. The model can NEVER add a target — any name not in the
host's candidate set invalidates the whole response (discard + note). Prompt
contract:

- stdin JSON: `{"mode": "group", "candidates": [...], "source_excerpts":
  {...}}` (excerpts capped at 12k chars total).
- stdout JSON: `{"groups": [[candidate-name, ...], ...]}` or `null`.
- Same abstain/timeout/malformed handling as the existing ML layer.

If in doubt, SKIP this phase entirely — it is an accelerator, not a feature.
Do not spend more than a day on it.

---

## Phase 6 — benchmarks, docs, criteria

1. `bench/apps.toml`: a manifest of application-style targets for Track B.
   Start with local dirs (`financial_planner`, `abi_reconstructor`) using
   their known test commands:
   financial_planner: `/data/codex-scratch/user/opencode/fp-venv/bin/python -m pytest -x -q`
   (recreate the venv if gone: `uv venv` + `uv pip install -r requirements.txt pytest pandas`).
   These are not pinned git repos; record path + a tree hash instead of a
   commit. Acceptance for Track B: financial_planner, `--app` mode,
   >= 15% canonical LOC removed across consented groups, gates green,
   `uv run` of its own pytest on the result tree green.
2. Re-run the full corpus (as in 1.5). Track A target: >= 8.5% weighted.
3. `CRITERIA.md`: add C7 (comma layer: separate stat, gated, python-only,
   corpus re-baselined) and C8 (propose: rails tested, consent required,
   stale-hash rejection, api-delta disclosure, originals sacred).
4. `bench/STRATEGY.md`: append a dated section summarizing the measured
   audit this plan is based on (numbers above) so the next person inherits
   the evidence, not just the conclusion.

## What NOT to build (measured dead ends — do not revisit)

- More dead-code detection depth for libraries: measured ~0.
- A JS or Rust comma layer: formatters undo it or never explode it.
- Per-symbol micro-rewrite model search: recorded experiment yielded 26 LOC.
- Duplicate-consolidation codegen (merge clones into one helper): deferred;
  it needs authoring new shared code, which is a different risk class than
  deletion. Revisit only after Track B proves the consent flow.
- Any reachability claim from coverage alone: coverage ranks, rails + gate
  decide.

## Final acceptance checklist

- [ ] `uv run pytest -q` green; ruff clean; no new dependencies beyond optional `coverage` shelling.
- [ ] `lc shrink` output on any fixture byte-identical to today EXCEPT comma
      collapse (verify with the fixtures before/after Phase 1).
- [ ] Comma layer: separate stat + layer record; corpus >= 8.5%.
- [ ] `lc propose` on the pyupgrade-trap fixture proposes nothing.
- [ ] `lc propose --apply` without propose-fresh hash exits 3.
- [ ] financial_planner demo: >= 15% deleted, green gates, consent recorded.
- [ ] README/CRITERIA/STRATEGY updated; BASELINE.md regenerated with
      provenance note.
