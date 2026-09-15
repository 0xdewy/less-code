"""Mutation-certified test compaction (PLAN.md Phase 8, conditional).

The 44-52% inline-test mass is the elephant in the rust denominators. There
is exactly one honest way to touch it: rewrite tests while PROVING they did
not get weaker. Protocol:

1. Baseline kill set: `cargo mutants` on the pristine tree, capped, with
   unmeasurable mutants excluded from BOTH sides.
2. Candidate: a model rewrite of `#[cfg(test)]` modules only (masking
   INVERTED: non-test code is sentineled frozen, the tests are visible).
   Gates: candidate tests pass on the PRISTINE source, and the candidate's
   kill set is a SUPERSET of the original's on the same mutant IDs. Any
   dropped kill vetoes - no averaging, no "close enough".
3. Consent flow exactly like `lc propose`: proposals land in
   `test-proposals.json`; nothing is applied without explicit `--apply IDS`;
   application re-runs every gate. Separate stat (`test_compaction_loc`),
   never inside the semantic figure.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import time
from pathlib import Path

from .ml_file import CONTENT_CAP, unmask_candidates
from .rust_rules import test_spans

MUTANTS_CAP = 150
PROPOSALS_NAME = "test-proposals.json"

SYSTEM_PROMPT_COMPACT = (
    "Rewrite this Rust file's TEST modules to use fewer canonical lines with"
    " the SAME testing power. The file's NON-test code is replaced by"
    " /*__LC_FROZEN_N__*/ sentinel lines: copy every sentinel line through"
    " EXACTLY as-is, in order, unchanged - the production code is frozen and"
    " your rewritten tests must still exercise it. Keep every test's asserted"
    " behavior: every assertion, every boundary case, every panic/error"
    " check. Merge repetitive tests with shared helpers only when each"
    " original case still runs. Do not delete assertions or cases. Do not"
    " touch non-test code. Whitespace tricks are useless: canonical"
    " formatting is applied before counting. Return exactly one JSON object"
    " with only `path` and `content`."
)


def mask_code(source: str) -> tuple[str, list[tuple[int, int, str]]]:
    """Masking INVERTED: every non-test top-level span is sentineled; the
    test modules stay visible for the model to compact."""
    encoded = source.encode()
    frozen = set(test_spans(source))
    parts: list[str] = []
    spans: list[tuple[int, int, str]] = []
    last = 0
    index = 0
    root_items = _top_level_items(source)
    for start, end in root_items:
        if any(fstart <= start < fend for fstart, fend in frozen):
            continue
        parts.append(encoded[last:start].decode("utf-8"))
        spans.append((start, end, encoded[start:end].decode("utf-8")))
        line_start = encoded.rfind(b"\n", 0, start) + 1
        indent = encoded[line_start:start].decode("utf-8")
        parts.append(indent + f"/*__LC_FROZEN_{index}__*/")
        last = end
        index += 1
    parts.append(encoded[last:].decode("utf-8"))
    return "".join(parts), spans


def _top_level_items(source: str) -> list[tuple[int, int]]:
    """Start/end byte spans of top-level items INCLUDING their doc comments
    and attributes (so a sentineled item cannot leak its docs)."""
    import tree_sitter as ts
    import tree_sitter_rust as grammar

    encoded = source.encode()
    root = ts.Parser(ts.Language(grammar.language())).parse(encoded).root_node
    if root.has_error:
        return []
    spans: list[tuple[int, int]] = []
    for child in root.named_children:
        if child.type in ("line_comment", "block_comment", "attribute_item"):
            continue
        start = child.start_byte
        previous = child.prev_named_sibling
        while previous is not None and previous.type in (
            "attribute_item",
            "line_comment",
            "block_comment",
        ):
            start = previous.start_byte
            previous = previous.prev_named_sibling
        spans.append((start, child.end_byte))
    return spans


def unmask_candidate(masked: str, spans: list[tuple[int, int, str]]) -> str | None:
    return unmask_candidates(masked, spans)


_STATUS_MAP = {
    "CaughtMutant": "Caught",
    "MissedMutant": "Missed",
    "TimeoutMutant": "Timeout",
}


def _snapshot_git(root: Path) -> bool:
    """cargo-mutants --in-place reverts mutations via git; guarantee a repo
    with a committed snapshot so the tree always comes back byte-exact."""

    def run(args: list[str]) -> bool:
        return (
            subprocess.run(
                args, cwd=root, capture_output=True, timeout=120, check=False
            ).returncode
            == 0
        )

    # cargo-mutants artifacts must never enter the snapshot: the post-run
    # `git checkout -- .` would otherwise resurrect STALE outcomes over the
    # fresh ones (found the hard way: the second run always looked all-Caught)
    gitignore = root / ".gitignore"
    wanted = ("mutants.out/", "target-mut-*/")
    existing = gitignore.read_text().splitlines() if gitignore.is_file() else []
    missing = [line for line in wanted if line not in existing]
    if missing:
        joined = (
            "\n".join(existing + [""] if existing else []) + "\n".join(missing) + "\n"
        )
        gitignore.write_text(joined)
    if not run(["git", "rev-parse", "--git-dir"]):
        if not (run(["git", "init", "-q"]) and run(["git", "add", "-A"])):
            return False
        return run(["git", "commit", "-qm", "lc: pre-mutants snapshot"])
    run(["git", "add", "-A"])
    # an already-committed clean tree is a valid snapshot
    if not run(["git", "commit", "-qm", "lc: pre-mutants snapshot"]):
        clean = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=root,
            capture_output=True,
            timeout=120,
            check=False,
        )
        return clean.returncode == 0 and not clean.stdout.strip()
    return True


def run_mutants(root: Path, timeout: int = 1800) -> dict[str, str] | None:
    """Run cargo-mutants; returns {mutant_id: status} or None on hard failure."""
    if not _snapshot_git(root):
        return None
    out_dir = root / "mutants.out"
    shutil.rmtree(out_dir, ignore_errors=True)  # a stale dir fails the run
    result = subprocess.run(
        ["cargo", "mutants", "--in-place"],
        cwd=root,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
        # a fresh target dir per call: shared build state let the previous
        # run's Caught verdict leak into this one
        env=dict(
            os.environ,
            CARGO_TARGET_DIR=str(
                root
                / f"target-mut-{hashlib.sha256(str(time.monotonic_ns()).encode()).hexdigest()[:8]}"
            ),
        ),
    )
    # cargo-mutants reverts its own mutations; enforce it
    subprocess.run(
        ["git", "checkout", "--", "."], cwd=root, capture_output=True, check=False
    )
    if result.returncode not in (0, 1, 2):
        return None
    outcomes_path = out_dir / "outcomes.json"
    if not outcomes_path.is_file():
        return None
    try:
        outcomes = json.loads(outcomes_path.read_text())["outcomes"]
    except (ValueError, KeyError):
        return None
    statuses: dict[str, str] = {}
    for entry in outcomes[:MUTANTS_CAP]:
        scenario = entry.get("scenario")
        if not isinstance(scenario, dict) or "Mutant" not in scenario:
            continue  # the baseline entry: no mutant to track
        name = scenario["Mutant"].get("name")
        status = _STATUS_MAP.get(str(entry.get("summary", "")))
        if name and status:
            statuses[name] = status
    return statuses


def kill_set(statuses: dict[str, str]) -> set[str]:
    """Mutants the suite killed ('Caught'); unmeasured excluded entirely."""
    return {name for name, status in statuses.items() if status == "Caught"}


def compare_kill_sets(
    original: dict[str, str], candidate: dict[str, str]
) -> tuple[bool, str]:
    """Superset check on the SAME measured mutant IDs only. Nothing measured
    is a veto, never a pass."""
    measured = {
        name
        for name in original.keys() & candidate.keys()
        if original[name] in ("Caught", "Missed", "Timeout")
        and candidate[name] in ("Caught", "Missed", "Timeout")
    }
    if not measured and original:
        return False, "nothing measured in the candidate tree"
    original_kills = {n for n in measured if original[n] == "Caught"}
    candidate_kills = {n for n in measured if candidate[n] == "Caught"}
    dropped = original_kills - candidate_kills
    if dropped:
        return (
            False,
            f"kill set dropped {len(dropped)} mutant(s): {sorted(dropped)[:3]}",
        )
    return True, ""


def propose_compaction(
    root: Path,
    files: list[Path],
    backend,
    attempts: int = 2,
    timeout: int = 1800,
) -> list[dict]:
    """Mine test-compaction proposals; nothing is applied. Each proposal
    carries per-module LOC delta and kill-set evidence."""
    proposals: list[dict] = []
    original_statuses = run_mutants(root, timeout)
    if original_statuses is None:
        return []
    baseline_kills = kill_set(original_statuses)
    for path in files:
        source = path.read_text(encoding="utf-8")
        masked, spans = mask_code(source)
        if not spans:
            continue
        for attempt in range(1, attempts + 1):
            try:
                reply = backend.rewrite(path, masked, SYSTEM_PROMPT_COMPACT, None)
            except Exception:  # noqa: BLE001 - external backend boundary
                break
            if reply is None:
                break
            if len(reply.encode()) > CONTENT_CAP:
                break
            candidate = unmask_candidate(reply, spans)
            if candidate is None:
                continue
            # gate 1: candidate tests pass on the PRISTINE source
            path.write_text(candidate, encoding="utf-8")
            from .testrunners import run_tests

            suite = run_tests(root, "rust", timeout=timeout, use_cache=False)
            if not suite.ok:
                path.write_text(source, encoding="utf-8")
                continue
            candidate_statuses = run_mutants(root, timeout)
            path.write_text(source, encoding="utf-8")  # never apply: consent flow
            if candidate_statuses is None:
                continue
            superset, evidence = compare_kill_sets(
                original_statuses, candidate_statuses
            )
            if not superset:
                continue
            from .loc import measure

            delta = measure(source, "rust").code - measure(candidate, "rust").code
            if delta <= 0:
                continue
            proposals.append(
                {
                    "id": f"{path.relative_to(root)}:{attempt}",
                    "path": str(path.relative_to(root)),
                    "loc_before": measure(source, "rust").code,
                    "loc_after": measure(candidate, "rust").code,
                    "loc_delta": delta,
                    "candidate": candidate,
                    "evidence": {
                        "baseline_kills": len(baseline_kills),
                        "candidate_kills": len(kill_set(candidate_statuses)),
                        "measured": len(
                            original_statuses.keys() & candidate_statuses.keys()
                        ),
                        "dropped": evidence,
                    },
                    "backend": getattr(backend, "name", type(backend).__name__),
                }
            )
            break
    return proposals


def write_proposals(root: Path, proposals: list[dict]) -> Path:
    out = root / PROPOSALS_NAME
    out.write_text(json.dumps({"test_compaction_proposals": proposals}, indent=2))
    return out


def load_proposals(root: Path) -> list[dict]:
    path = root / PROPOSALS_NAME
    if not path.is_file():
        raise SystemExit(f"no {PROPOSALS_NAME} in {root}; run lc compact-tests first")
    return json.loads(path.read_text())["test_compaction_proposals"]


def apply_proposal(root: Path, proposal: dict, timeout: int = 1800) -> dict:
    """Apply ONE consented proposal, re-verifying every gate. All-or-nothing."""
    from .testrunners import run_tests

    path = root / proposal["path"]
    original = path.read_text(encoding="utf-8")
    # the candidate's kill evidence is re-proven at apply time, never trusted
    # from the proposals file: pristine baseline first, then the candidate
    baseline_statuses = run_mutants(root, timeout)
    if baseline_statuses is None:
        return {
            "id": proposal["id"],
            "applied": False,
            "reason": "baseline mutants failed",
        }
    if not run_tests(root, "rust", timeout=timeout, use_cache=False).ok:
        return {
            "id": proposal["id"],
            "applied": False,
            "reason": "baseline tests failed",
        }
    path.write_text(proposal["candidate"], encoding="utf-8")
    if not run_tests(root, "rust", timeout=timeout, use_cache=False).ok:
        path.write_text(original, encoding="utf-8")
        return {"id": proposal["id"], "applied": False, "reason": "tests failed"}
    candidate_statuses = run_mutants(root, timeout)
    superset, evidence = compare_kill_sets(baseline_statuses, candidate_statuses or {})
    if not superset:
        path.write_text(original, encoding="utf-8")
        return {"id": proposal["id"], "applied": False, "reason": evidence}
    return {
        "id": proposal["id"],
        "applied": True,
        "loc_delta": proposal["loc_delta"],
    }
