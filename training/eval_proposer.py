"""Held-out F3 evaluation and the promotion rule (PLAN.md §6.6).

Two or more proposers (base rung, SFT, RFT-round-r) run against the SAME
pristine `role = "eval"` crates at the SAME call budget. Metrics: accepted
LOC per call, accept rate, doc-eating rate (must be 0 BY CONSTRUCTION - the
host never accepts such output; the metric asserts it), wall time.

Promotion rule: the trained proposer runs on the four benchmark crates ONLY
after beating its base model on the held-out split at equal budget
(accepted-LOC/call >= base's). Otherwise the benchmark keeps the best ladder
rung and the trained model is recorded as a negative.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from less_code.langdetect import map_project
from less_code.loc import measure
from less_code.ml_file import FileBackend, default_gate, rewrite_tree
from training.mine import load_manifest

EVAL_MD = Path(__file__).resolve().parent / "EVAL.md"
CALL_BUDGET_PER_CRATE = 24


def validate_config() -> list[str]:
    errors: list[str] = []
    crates = [c for c in load_manifest() if c.get("role") == "eval"]
    if len(crates) < 5:
        errors.append(f"only {len(crates)} eval crates (need >= 5)")
    return errors


def eval_proposer(
    name: str,
    rung_command: str,
    crates: list[dict],
    budget: int = CALL_BUDGET_PER_CRATE,
    timeout: int = 900,
) -> dict:
    result: dict = {
        "proposer": name,
        "digest": name,
        "calls": 0,
        "accepted_files": 0,
        "accepted_loc": 0,
        "doc_eating_rejections": 0,
        "loc_before": 0,
        "loc_after": 0,
        "wall_s": 0.0,
    }
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="lc-eval-") as scratch:
        for item in crates:
            repo_dir = Path(scratch) / item["name"]
            subprocess.run(
                ["git", "clone", "--quiet", item["repo"], str(repo_dir)],
                capture_output=True,
                timeout=300,
                check=False,
            )
            subprocess.run(
                ["git", "checkout", "--quiet", item["commit"]],
                cwd=repo_dir,
                capture_output=True,
                check=False,
            )
            project = map_project(repo_dir, "rust")
            pristine = {p: p.read_text() for p in project.source_files}
            backend = FileBackend(rung_command, timeout=timeout, digest=name)
            gate = default_gate(repo_dir, pristine, timeout)
            stats, records = rewrite_tree(
                repo_dir,
                project.source_files,
                backend,
                gate,
                attempts=budget,
            )
            result["calls"] += stats.get("calls", 0)
            result["accepted_files"] += stats.get("accepted_files", 0)
            result["accepted_loc"] += stats.get("accepted_loc", 0)
            result["doc_eating_rejections"] += sum(
                1
                for r in records
                for p in r["proposals"]
                if p.get("category") == "docs"
            )
            result["loc_before"] += sum(
                measure(t, "rust").code for t in pristine.values()
            )
            result["loc_after"] += sum(
                measure(p.read_text(), "rust").code for p in project.source_files
            )
    result["wall_s"] = round(time.monotonic() - started, 1)
    result["accepted_loc_per_call"] = (
        round(result["accepted_loc"] / result["calls"], 3) if result["calls"] else 0.0
    )
    result["accept_rate_pct"] = (
        round(100.0 * result["accepted_files"] / result["calls"], 2)
        if result["calls"]
        else 0.0
    )
    return result


def promote(base: dict, candidate: dict) -> dict:
    """The promotion rule, computed on numbers (equal call budget)."""
    verdict = {
        "base": base["proposer"],
        "candidate": candidate["proposer"],
        "base_loc_per_call": base["accepted_loc_per_call"],
        "candidate_loc_per_call": candidate["accepted_loc_per_call"],
        "budget_equal": base["calls"] == candidate["calls"],
        "doc_eating_zero": candidate["doc_eating_rejections"] == 0
        and base["doc_eating_rejections"] == 0,
    }
    verdict["promoted"] = (
        verdict["budget_equal"]
        and candidate["accepted_loc_per_call"] >= base["accepted_loc_per_call"]
    )
    return verdict


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--proposer",
        action="append",
        default=None,
        help="name=rung-command; repeat for base, sft, rft proposers",
    )
    parser.add_argument("--budget", type=int, default=CALL_BUDGET_PER_CRATE)
    parser.add_argument("--validate-config", action="store_true")
    args = parser.parse_args()
    if args.validate_config:
        errors = validate_config()
        if not errors:
            print("eval config ok: eval split present")
        # fall through to shared error printing below
        if errors:
            print("\n".join(errors), file=sys.stderr)
            return 1
        print("eval config ok: eval split present")
        return 0
    if args.validate_config:
        return 0 if validate_config() == [] else 1
    if not args.proposer:
        parser.error("--proposer is required to run an evaluation")
    crates = [c for c in load_manifest() if c.get("role") == "eval"]
    proposers = []
    for spec in args.proposer:
        name, _, command = spec.partition("=")
        proposers.append((name, command))
    results = []
    for name, command in proposers:
        result = eval_proposer(name, command, crates, args.budget)
        results.append(result)
        print(json.dumps(result, indent=2), file=sys.stderr)
    verdict = promote(results[0], results[-1]) if len(results) >= 2 else None
    lines = [
        "# F3 evaluation - held-out proposers at equal budget",
        "",
        "| proposer | calls | accepted files | accepted LOC | LOC/call | accept % | doc-eating | wall |",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for r in results:
        lines.append(
            f"| {r['proposer']} | {r['calls']} | {r['accepted_files']} | "
            f"{r['accepted_loc']} | {r['accepted_loc_per_call']} | "
            f"{r['accept_rate_pct']}% | {r['doc_eating_rejections']} | {r['wall_s']}s |"
        )
    lines += ["", "## Promotion decision", ""]
    if verdict:
        lines += [
            f"- base: {verdict['base']} ({verdict['base_loc_per_call']} LOC/call)",
            f"- candidate: {verdict['candidate']} ({verdict['candidate_loc_per_call']} LOC/call)",
            f"- equal budget: {verdict['budget_equal']}; doc-eating zero: {verdict['doc_eating_zero']}",
            f"- **PROMOTED: {verdict['promoted']}** (candidate >= base at equal budget)",
        ]
    else:
        lines.append("- single proposer; no promotion decision possible")
    lines += [
        "",
        "The trained proposer touches the four benchmark crates only if",
        "PROMOTED is true; otherwise the best ladder rung stays and this file",
        "is the recorded negative (PLAN.md §6.6).",
    ]
    EVAL_MD.write_text("\n".join(lines) + "\n")
    print(f"eval: {EVAL_MD}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
