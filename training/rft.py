"""Rejection-sampling fine-tuning (RFT / STaR), the shipped RL (§6.4).

For round r = 1..3: sample k completions per training prompt at temperature
1.0 through the FULL Phase 1 cascade (the gate IS the filter - every "reward"
is a real gate pass, never a proxy), keep gate-passing completions with a
positive LOC delta, cap 4 keeps per prompt, and re-run SFT on the union of
mined + kept pairs. Stop early when the accept-rate gain < 2pp.

  uv run python training/rft.py --model qwen2.5-coder:7b --rounds 3
  uv run python training/rft.py --validate-config
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from less_code.langdetect import map_project
from less_code.ml_file import (
    FileBackend,
    default_gate,
    rewrite_tree,
)
from training.anticontamination import find_contamination
from training.mine import load_manifest

PAIRS = Path(__file__).resolve().parent / "pairs"
RFT_DIR = Path(__file__).resolve().parent / "rft"
K_SAMPLES = 8
MAX_KEEPS_PER_PROMPT = 4
ACCEPT_RATE_STOP_PP = 2.0
MAX_ROUNDS = 3


def validate_config() -> list[str]:
    errors: list[str] = []
    if not (PAIRS / "sft.jsonl").is_file():
        errors.append("no mined pairs: run training/mine.py first")
        return errors
    prompts = prompts_from_pairs()
    if not prompts:
        errors.append("no training prompts available from mined pairs")
    return errors


def prompts_from_pairs() -> list[dict]:
    """Unique (crate, path) prompt payloads from the mining run."""
    seen: dict[str, dict] = {}
    for line in (PAIRS / "sft.jsonl").open():
        if not line.strip():
            continue
        row = json.loads(line)
        user = json.loads(row["messages"][1]["content"])
        crate = row["meta"]["crate"]
        key = f"{crate}:{row['meta']['path']}"
        if key not in seen:
            seen[key] = {"crate": crate, "path": row["meta"]["path"], "user": user}
    return list(seen.values())


def sample_round(
    model: str,
    prompts: list[dict],
    round_no: int,
    timeout: int = 900,
) -> dict:
    """k samples per prompt through the full cascade; keeps gate-passers."""
    RFT_DIR.mkdir(parents=True, exist_ok=True)
    manifest = {c["name"]: c for c in load_manifest()}
    kept: list[dict] = []
    stats = {
        "round": round_no,
        "model": model,
        "prompts": 0,
        "samples": 0,
        "gate_passes": 0,
        "positive_deltas": 0,
        "kept": 0,
        "accept_rate": 0.0,
        "mean_loc_delta": 0.0,
    }
    deltas: list[int] = []
    by_crate: dict[str, list[dict]] = {}
    for prompt in prompts:
        by_crate.setdefault(prompt["crate"], []).append(prompt)
    for crate, crate_prompts in by_crate.items():
        item = manifest.get(crate)
        if item is None:
            continue  # a crate dropped from the manifest never becomes training data
        crate_dir = Path(__file__).resolve().parent / "crates" / crate
        if not crate_dir.exists():
            subprocess.run(
                ["git", "clone", "--quiet", item["repo"], str(crate_dir)],
                capture_output=True,
                timeout=300,
                check=False,
            )
        subprocess.run(
            ["git", "checkout", "--quiet", item["commit"]],
            cwd=crate_dir,
            capture_output=True,
            check=False,
        )
        project = map_project(crate_dir, "rust")
        targets = {
            p
            for p in project.source_files
            if f"{crate}:{p.relative_to(crate_dir)}"
            in {f"{prompt['crate']}:{prompt['path']}" for prompt in crate_prompts}
        }
        if not targets:
            continue
        pristine = {p: p.read_text() for p in project.source_files}
        gate = default_gate(crate_dir, pristine, timeout)
        backend = FileBackend(
            f"python3 bench/ollama_sample.py {model}",
            timeout=timeout,
        )
        # k samples per prompt: sweep the seed through the sampling adapter
        backend._extra_sequence = [
            {"seed": seed} for seed in range(1000, 1000 + K_SAMPLES * len(targets) * 4)
        ]
        stats["prompts"] += len(targets)
        file_stats, records = rewrite_tree(
            crate_dir,
            sorted(targets),
            backend,
            gate,
            attempts=K_SAMPLES,
        )
        stats["samples"] += file_stats.get("calls", 0)
        stats["gate_passes"] += file_stats.get("accepted_files", 0)
        del backend.captured
        for record in records:
            if not record["accepted"]:
                continue
            delta = record["loc_before"] - record["loc_after"]
            if delta <= 0:
                continue
            stats["positive_deltas"] += 1
            deltas.append(delta)
            keeps = [p for p in record["proposals"] if p.get("accepted")][
                :MAX_KEEPS_PER_PROMPT
            ]
            for keep in keeps:
                kept.append(
                    {
                        "crate": crate,
                        "path": record["path"],
                        "attempt": keep["attempt"],
                        "loc_delta": delta,
                        "round": round_no,
                        "digest": model,
                    }
                )
            stats["kept"] += len(keeps)
    if stats["samples"]:
        stats["accept_rate"] = round(100.0 * stats["gate_passes"] / stats["samples"], 2)
    if deltas:
        stats["mean_loc_delta"] = round(sum(deltas) / len(deltas), 2)
    # contamination guard over every kept pair's source crate name/path
    for entry in kept:
        if find_contamination(entry):
            raise SystemExit(f"contaminated RFT keep: {entry}")
    (RFT_DIR / f"round_{round_no}.json").write_text(json.dumps(stats, indent=2))
    (RFT_DIR / f"round_{round_no}_keeps.jsonl").write_text(
        "\n".join(json.dumps(k) for k in kept) + "\n"
    )
    return stats


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", default="qwen2.5-coder:7b")
    parser.add_argument("--rounds", type=int, choices=(1, 2, 3), default=MAX_ROUNDS)
    parser.add_argument("--validate-config", action="store_true")
    args = parser.parse_args()
    if args.validate_config:
        errors = validate_config()
        if errors:
            print("\n".join(errors), file=sys.stderr)
            return 1
        print("rft config ok: mined pairs present")
        return 0
    prompts = prompts_from_pairs()
    previous_rate: float | None = None
    for round_no in range(1, args.rounds + 1):
        stats = sample_round(args.model, prompts, round_no)
        print(json.dumps(stats, indent=2), file=sys.stderr)
        if previous_rate is not None and (
            stats["accept_rate"] - previous_rate < ACCEPT_RATE_STOP_PP
        ):
            print(
                f"accept-rate gain {stats['accept_rate'] - previous_rate:.2f}pp"
                f" < {ACCEPT_RATE_STOP_PP}pp: stopping early",
                file=sys.stderr,
            )
            break
        previous_rate = stats["accept_rate"]
        # policy improvement step = SFT on the union (mined + keeps)
        union = RFT_DIR / f"union_round_{round_no}.jsonl"
        with union.open("w") as out:
            for line in (PAIRS / "sft.jsonl").open():
                out.write(line if line.endswith("\n") else line + "\n")
            for line in (RFT_DIR / f"round_{round_no}_keeps.jsonl").open():
                out.write(line)
        print(f"union pairs: {union} - retrain with training/sft.py", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
