"""Reward for GRPO rollouts — gated LOC reduction (CPU; tests run on CPU).

    r = gate * (1 + lambda * loc_delta) - penalties
    gate          : +1 if frozen tests pass AND api preserved, else -1
    loc_delta     : clip((loc_before - loc_after)/loc_before, 0, 0.9)
    penalties     : minification/degenerate-output guards

Reward-hacking mitigations (research rl.md §Reward design):
- tests are FROZEN env state: policy output is the code unit only, never tests
- api surface must match exactly (underscor helpers allowed)
- reward 0-band candidates (not smaller) get gate but no shaping term
- min-behavior: parse/compile check + identical public symbols
"""

from __future__ import annotations

import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path

import sys

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from less_code.api_check import EXTRACTORS, api_violations  # noqa: E402
from less_code.loc import count_source  # noqa: E402
from less_code.testrunners import run_tests  # noqa: E402

LAMBDA = 0.5
MINIFICATION_PENALTY = 0.5


@dataclass
class RewardBreakdown:
    reward: float
    gate: int
    loc_delta: float
    penalty: float
    reason: str


def extract_completion(completion: str) -> str | None:
    import re

    blocks = re.findall(r"```[a-zA-Z0-9_+-]*\n(.*?)```", completion, re.DOTALL)
    if blocks:
        return blocks[-1].strip("\n")
    head = completion.strip()[:20]
    if any(completion.strip().startswith(s) for s in ("def ", "class ", "pub ", "fn ", "function ", "export ", "use ", "import ")):
        return completion.strip()
    return None


def _looks_minified(code: str) -> bool:
    lines = [l for l in code.splitlines() if l.strip()]
    if not lines:
        return False
    avg = sum(len(l) for l in lines) / len(lines)
    longest = max(len(l) for l in lines)
    return avg > 110 or longest > 250


def compute_reward(
    completion: str,
    sample: dict,
    repo_root: Path | None = None,
) -> RewardBreakdown:
    """Evaluate one rollout against its frozen fixture. Runs the suite on CPU."""
    repo_root = repo_root or REPO
    fixture = repo_root / sample["fixture"]
    lang = sample["lang"]
    unit = sample["unit_source"]
    loc_before = count_source(unit, lang).code

    code = extract_completion(completion)
    if code is None:
        return RewardBreakdown(-1.0, -1, 0.0, 0.0, "no-code-block")
    if _looks_minified(code):
        return RewardBreakdown(-1.0, -1, 0.0, MINIFICATION_PENALTY, "minified")
    try:
        if lang == "python":
            import ast

            ast.parse(code)
    except SyntaxError:
        return RewardBreakdown(-1.0, -1, 0.0, 0.0, "syntax-error")

    api_before = EXTRACTORS[lang](unit)
    api_after = EXTRACTORS[lang](code)
    if api_violations({sample["fixture"]: api_before}, {sample["fixture"]: api_after}):
        return RewardBreakdown(-1.0, -1, 0.0, 0.0, "api-changed")

    loc_after = count_source(code, lang).code
    loc_delta = max(0.0, min(0.9, (loc_before - loc_after) / max(loc_before, 1)))

    # splice the unit into a scratch copy of the fixture, run frozen tests
    tmp = Path(tempfile.mkdtemp(prefix="grpo-reward-"))
    try:
        scratch = tmp / "fixture"
        shutil.copytree(fixture, scratch)
        replaced = False
        for rel in (scratch.rglob("*")):
            if rel.is_file() and rel.suffix in (".py", ".js", ".mjs", ".rs"):
                text = rel.read_text(encoding="utf-8", errors="replace")
                if unit.strip() in text:
                    rel.write_text(text.replace(unit.strip(), code.strip(), 1), encoding="utf-8")
                    replaced = True
                    break
        if not replaced:
            return RewardBreakdown(-1.0, -1, 0.0, 0.0, "unit-not-found")
        result = run_tests(scratch, lang, timeout=300)
        if not result.ok:
            return RewardBreakdown(-1.0, -1, loc_delta, 0.0, "tests-failed")
        return RewardBreakdown(1.0 + LAMBDA * loc_delta, 1, loc_delta, 0.0, "accepted")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def reward_fn(completions: list[str], prompts: list[str] | None = None, **dataset_columns) -> list[float]:
    """TRL-compatible reward callable (dataset rows arrive via dataset_columns)."""
    samples = dataset_columns.get("sample_meta") or [None] * len(completions)
    out = []
    for completion, sample in zip(completions, samples):
        if sample is None:
            out.append(-1.0)
            continue
        out.append(compute_reward(completion, sample).reward)
    return out
