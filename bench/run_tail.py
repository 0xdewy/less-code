"""The compute tail (PLAN.md Phases 2-6), unattended.

After the ladder grid finishes: regenerate LADDER.md, pick the production
config (best rung x variant by accepted LOC), run the census, mine training
pairs, SFT, RFT round(s), held-out eval, and the promotion decision. Every
stage logs to training/tail/<stage>.log and tolerates earlier-stage failure
by recording it and stopping (the Iteration Protocol owns the verdict).

  setsid nohup uv run python bench/run_tail.py > training/tail.log 2>&1 &
"""

from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
TAIL = REPO / "training" / "tail"
RUST_CRATES = ("itoa", "humantime", "shell-words", "strsim-rs")
VARIANTS = ("v1", "v2")
RUNGS = ("qwen2.5-coder:7b", "qwen3:8b")


def log(message: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {message}", flush=True)


def sh(command: str, stage: str, timeout: int | None = None) -> bool:
    log(f"stage {stage}: {command}")
    result = subprocess.run(
        command,
        shell=True,
        cwd=REPO,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    (TAIL / f"{stage}.log").write_text(
        (result.stdout or "") + "\n=== stderr ===\n" + (result.stderr or "")
    )
    log(f"stage {stage}: exit {result.returncode}")
    if result.returncode != 0:
        log(f"stage {stage} FAILED; tail of stderr:")
        log((result.stderr or "")[-800:])
    return result.returncode == 0


def grid_complete() -> bool:
    return (
        subprocess.run(
            ["pgrep", "-f", "rust_ladder.py"],
            capture_output=True,
            check=False,
        ).returncode
        != 0
    )


def wait_for_grid() -> None:
    while not grid_complete():
        time.sleep(60)
    # the grid's last json lands after the process exits; give it a beat
    time.sleep(10)


def pick_production() -> tuple[str, str]:
    best = ("qwen2.5-coder:7b", "v1", -1)
    for rung in RUNGS:
        for variant in VARIANTS:
            path = REPO / "bench" / "ladder" / f"{rung}-{variant}-humantime.json"
            if not path.is_file():
                continue
            cell = json.loads(path.read_text())
            if cell.get("loc_delta", 0) > best[2]:
                best = (rung, variant, cell["loc_delta"])
    log(f"production config: {best[0]} x {best[1]} (humantime delta {best[2]})")
    return best[0], best[1]


def production_command(rung: str, variant: str) -> str:
    model = rung
    if variant == "v1":
        return f"python3 bench/ollama_file.py {model}"
    return f"python3 bench/ollama.py {model}"


def main() -> int:
    TAIL.mkdir(parents=True, exist_ok=True)
    log("waiting for the ladder grid to finish")
    wait_for_grid()
    log("grid complete")

    sh("uv run python bench/rust_ladder.py --report", "ladder-report")
    sh("uv run python bench/harvest_census.py", "census")

    rung, variant = pick_production()
    command = production_command(rung, variant)

    mine_ok = sh(
        f"uv run python training/mine.py --rung-command '{command}'"
        f" --limit 30 --attempts 3 --digest {rung}",
        "mine",
        timeout=14 * 3600,
    )
    if not mine_ok:
        log("mining failed or under-produced; SFT/RFT cannot start (recorded)")
        return 1
    if not sh("uv run python training/sft.py --validate-config", "sft-config"):
        return 1
    if not sh(
        "uv run python training/sft.py --pairs training/pairs/sft.jsonl",
        "sft",
        timeout=8 * 3600,
    ):
        return 1
    if not sh(
        "uv run python training/rft.py --model lc-sft --rounds 3",
        "rft",
        timeout=24 * 3600,
    ):
        return 1
    sh(
        "uv run python training/eval_proposer.py"
        f" --proposer base={command}"
        " --proposer sft=python3 bench/ollama_file.py lc-sft"
        " --budget 24",
        "eval",
        timeout=8 * 3600,
    )
    log("tail complete")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
