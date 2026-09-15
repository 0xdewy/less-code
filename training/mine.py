"""Mine accepted (prompt, completion) pairs through the full gate (§6.2).

Runs the Phase 2 production config (best rung x variant) over the training
cohort, and dumps:
  training/pairs/sft.jsonl     - ACCEPTED proposals as chat messages
  training/pairs/rejects.jsonl - rejects with category (RFT + funnel analysis)
  training/mining.json         - run summary: per-crate accept counts, digest

Every pair is anticontamination-scanned before it is written (§6.1): a hit
skips the pair, and the dataset test scans the files again. `--validate-config`
refuses to bless a dataset with < 50 accepted pairs (memorization guard).
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
import tomllib
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from less_code.langdetect import map_project
from less_code.loc import measure
from less_code.ml_file import (
    SYSTEM_PROMPT_FILE,
    FileBackend,
    default_gate,
    rewrite_tree,
)
from training.anticontamination import find_contamination

MANIFEST = Path(__file__).resolve().parent / "manifest.toml"
PAIRS_DIR = Path(__file__).resolve().parent / "pairs"
CRATE_CACHE = Path(__file__).resolve().parent / "crates"
MIN_PAIRS = 50
MAX_MINING_CRATES = 30


def load_manifest(path: Path = MANIFEST) -> list[dict]:
    if not path.is_file():
        return []
    data = tomllib.loads(path.read_text())
    return data.get("crate", [])


def validate_config(pairs_path: Path = PAIRS_DIR / "sft.jsonl") -> list[str]:
    errors: list[str] = []
    if not MANIFEST.is_file():
        errors.append(f"manifest missing: {MANIFEST} (run training/build_manifest.py)")
    elif len(load_manifest()) < 40:
        errors.append("manifest has fewer than 40 crates")
    if not pairs_path.is_file():
        errors.append(f"pairs file missing: {pairs_path} (run with --mine)")
    else:
        count = sum(1 for line in pairs_path.open() if line.strip())
        if count < MIN_PAIRS:
            errors.append(
                f"only {count} accepted pairs (< {MIN_PAIRS}); refusing to train"
            )
    return errors


class CapturingBackend:
    """Wraps the production FileBackend; records every payload/reply pair."""

    name = "mining"

    def __init__(self, inner: FileBackend):
        self.inner = inner
        self.captured: dict[str, list[dict]] = {}

    def rewrite(self, path: Path, masked_source: str, context: str, feedback):
        key = f"{path.parent.name}/{path.name}"
        started = time.monotonic()
        reply = self.inner.rewrite(path, masked_source, context, feedback)
        self.captured.setdefault(key, []).append(
            {
                "messages": {
                    "system": SYSTEM_PROMPT_FILE,
                    "user": {
                        "path": path.name,
                        "source": masked_source,
                        "context": context,
                        "feedback": feedback,
                    },
                },
                "reply": reply,
                "duration_s": round(time.monotonic() - started, 3),
            }
        )
        return reply

    @property
    def digest(self):
        return getattr(self.inner, "digest", None)

    @property
    def system(self):
        return self.inner.system


def pair_for(crate: str, capture: dict, proposal: dict, record: dict) -> dict | None:
    """The SFT pair for one accepted file-record, or None if not reconstructible."""
    key = f"{Path(record['path']).parent}/{Path(record['path']).name}"
    calls = capture.get(key, [])
    accepted_attempts = [p["attempt"] for p in record["proposals"] if p.get("accepted")]
    if not accepted_attempts:
        return None
    attempt = accepted_attempts[0]
    if attempt > len(calls):
        return None
    call = calls[attempt - 1]
    if not call["reply"]:
        return None
    messages = [
        {"role": "system", "content": call["messages"]["system"]},
        {"role": "user", "content": json.dumps(call["messages"]["user"])},
        {"role": "assistant", "content": call["reply"]},
    ]
    return {
        "messages": messages,
        "meta": {
            "crate": crate,
            "path": record["path"],
            "loc_before": record["loc_before"],
            "loc_after": record["loc_after"],
            "loc_delta": record["loc_before"] - record["loc_after"],
            "digest": record.get("digest"),
            "prompt_sha256": proposal.get("prompt_sha256"),
            "gate_full": True,
        },
    }


def mine(
    rung_command: str,
    limit: int = MAX_MINING_CRATES,
    attempts: int = 3,
    timeout: int = 900,
    digest: str | None = None,
) -> dict:
    crates = [c for c in load_manifest() if c.get("role") == "train"][:limit]
    PAIRS_DIR.mkdir(exist_ok=True)
    sft_path = PAIRS_DIR / "sft.jsonl"
    rejects_path = PAIRS_DIR / "rejects.jsonl"
    summary: dict = {"crates": {}, "digest": digest}
    pair_count = 0
    with sft_path.open("w") as sft, rejects_path.open("w") as rejects:
        for item in crates:
            crate = item["name"]
            crate_dir = CRATE_CACHE / crate
            if not crate_dir.exists():
                clone = subprocess.run(
                    ["git", "clone", "--quiet", item["repo"], str(crate_dir)],
                    capture_output=True,
                    text=True,
                    timeout=300,
                    check=False,
                )
                if clone.returncode:
                    subprocess.run(["rm", "-rf", str(crate_dir)], check=True)
                    continue
            subprocess.run(
                ["git", "checkout", "--quiet", item["commit"]],
                cwd=crate_dir,
                capture_output=True,
                check=False,
            )
            project = map_project(crate_dir, "rust")
            inner = FileBackend(rung_command, timeout=timeout, digest=digest)
            backend = CapturingBackend(inner)
            pristine = {p: p.read_text() for p in project.source_files}
            gate = default_gate(crate_dir, pristine, timeout)
            before = sum(measure(t, "rust").code for t in pristine.values())
            started = time.monotonic()
            stats, records = rewrite_tree(
                crate_dir, project.source_files, backend, gate, attempts
            )
            after = sum(
                measure(p.read_text(), "rust").code for p in project.source_files
            )
            accepted_here = 0
            rejected_here = 0
            for record in records:
                for proposal in record["proposals"]:
                    if proposal.get("accepted"):
                        pair = pair_for(crate, backend.captured, proposal, record)
                        if pair is None:
                            continue
                        contamination = find_contamination(pair)
                        if contamination:
                            print(
                                f"contaminated pair skipped ({crate}): {contamination}",
                                file=sys.stderr,
                            )
                            continue
                        sft.write(json.dumps(pair) + "\n")
                        accepted_here += 1
                    elif proposal.get("reason"):
                        rejects.write(
                            json.dumps(
                                {
                                    "crate": crate,
                                    "path": record["path"],
                                    "attempt": proposal["attempt"],
                                    "category": proposal["category"],
                                    "reason": proposal["reason"],
                                }
                            )
                            + "\n"
                        )
                        rejected_here += 1
            pair_count += accepted_here
            summary["crates"][crate] = {
                "accepted": accepted_here,
                "rejected": rejected_here,
                "loc_before": before,
                "loc_after": after,
                "wall_s": round(time.monotonic() - started, 1),
                "records": stats,
            }
            print(
                f"[{crate}] accepted={accepted_here} rejected={rejected_here} "
                f"{before} -> {after}",
                file=sys.stderr,
            )
    summary["accepted_pairs"] = pair_count
    (PAIRS_DIR / "mining.json").write_text(json.dumps(summary, indent=2))
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rung-command", default=None, help="production rung command")
    parser.add_argument("--limit", type=int, default=MAX_MINING_CRATES)
    parser.add_argument("--attempts", type=int, choices=(1, 2, 3), default=3)
    parser.add_argument("--timeout", type=int, default=900)
    parser.add_argument("--digest", default=None)
    parser.add_argument("--validate-config", action="store_true")
    args = parser.parse_args()
    if args.validate_config:
        errors = validate_config()
        if errors:
            print("\n".join(errors), file=sys.stderr)
            return 1
        print("config ok: manifest present, pairs >= 50")
        return 0
    if not args.rung_command:
        parser.error("--rung-command is required for mining")
    summary = mine(
        args.rung_command, args.limit, args.attempts, args.timeout, args.digest
    )
    print(
        json.dumps({"accepted_pairs": summary["accepted_pairs"]}, indent=2),
        file=sys.stderr,
    )
    if summary["accepted_pairs"] < MIN_PAIRS:
        print(
            f"mining produced {summary['accepted_pairs']} pairs (< {MIN_PAIRS});"
            " this negative rides the Iteration Protocol (PLAN.md §0.3):"
            " record digest, funnel and per-crate yields before any conclusion",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
