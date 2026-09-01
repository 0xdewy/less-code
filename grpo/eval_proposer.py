"""Proposer-swap evaluation (roadmap F3): does a trained model beat its base
INSIDE the same search, at the same budget?

The honest test of training is not loss curves — it is marginal accepted LOC
per LLM call in the real pipeline. This script runs `lc reduce` twice on the
same pristine target with two proposer models and prints the comparison:

    uv run python grpo/eval_proposer.py --target fixtures/py \
        --model-a qwen2.5-coder:7b --model-b /path/to/merged-sft-model \
        --calls 30

Both runs use the same seed-ordering (deterministic scheduling), same gates,
same budget. The printed table is the claim: yield %, accepted count, accept
rate, files reached, doc-eating rate (docs-lost outcomes / calls).

Note on adapters: ollama serves GGUF models, so a LoRA adapter must be merged
and converted first (mergekit or transformers merge + llama.cpp quantize).
For a quick CPU-side signal, --model-b may point at any ollama-served model.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import tempfile
import time
from collections import Counter
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def one_run(target: Path, model: str, calls: int, out: Path, extra: list[str]) -> dict:
    cmd = [
        str(REPO / ".venv" / "bin" / "lc"), "reduce", str(target),
        "--model", model, "--attempts", "2",
        "--max-llm-calls", str(calls), "--num-ctx", "16384",
        "--out", str(out), *extra,
    ]
    env = {"PATH": f"{REPO / '.venv' / 'bin'}:/usr/local/bin:/usr/bin:/bin",
           "HOME": str(Path.home())}
    t0 = time.time()
    proc = subprocess.run(cmd, capture_output=True, text=True,
                          cwd=str(REPO), timeout=7200, env=env)
    if proc.returncode != 0:
        print(proc.stdout[-2000:])
        print(proc.stderr[-2000:])
        raise SystemExit(f"reduce failed for {model}")
    r = json.loads(out.read_text())["reduce"]
    outcomes = Counter(a["outcome"].split(":")[0] for a in r["attempts"])
    real_calls = sum(v for k, v in outcomes.items() if k != "budget-exhausted")
    return {
        "model": model,
        "hybrid_pct": r["hybrid_pct"],
        "llm_extra_pct": r["llm_extra_pct"],
        "accepted": outcomes.get("accepted", 0),
        "docs_lost": outcomes.get("docs-lost", 0),
        "accept_rate": round(outcomes.get("accepted", 0) / max(1, real_calls), 3),
        "files": len({a["file"].rsplit(":", 1)[0] for a in r["attempts"]}),
        "tests_ok": r["tests_ok"],
        "api_ok": r["api_ok"],
        "seconds": round(time.time() - t0),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--target", type=Path, required=True)
    ap.add_argument("--model-a", required=True, help="baseline proposer")
    ap.add_argument("--model-b", required=True, help="trained proposer")
    ap.add_argument("--calls", type=int, default=30)
    ap.add_argument("--extra", nargs="*", default=[],
                    help="extra args for both runs, e.g. --extra --trust t.json")
    args = ap.parse_args()

    rows = []
    with tempfile.TemporaryDirectory(prefix="prop-eval-") as td:
        td = Path(td)
        # reduce mutates its target: two pristine copies, one per proposer
        for tag, model in (("a", args.model_a), ("b", args.model_b)):
            work = td / tag
            subprocess.run(["cp", "-a", str(args.target), str(work)], check=True)
            rows.append(one_run(work, model, args.calls, td / f"{tag}.json", args.extra))

    print(f"\n| {'model':32s} | yield% | llm% | accepted | accept-rate | docs-lost | files | ok | s |")
    print("|---|---|---|---|---|---|---|---|---|")
    for r in rows:
        print(
            f"| {r['model'][:32]:32s} | {r['hybrid_pct']} | {r['llm_extra_pct']} | "
            f"{r['accepted']} | {r['accept_rate']} | {r['docs_lost']} | "
            f"{r['files']} | {r['tests_ok'] and r['api_ok']} | {r['seconds']} |"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
