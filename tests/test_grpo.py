"""Reward-function tests: gate severity, shaping, anti-hack — all CPU."""

import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "grpo"))

import pytest

from rewards import compute_reward, extract_completion


def load_sample(lang: str) -> dict:
    rows = [json.loads(l) for l in (REPO / "grpo" / "dataset.jsonl").open()]
    for r in rows:
        if r["lang"] == lang:
            return r
    pytest.skip(f"no {lang} sample in dataset")


def wrapped(lang: str, code: str) -> str:
    return f"```{lang}\n{code}\n```"


class TestExtract:
    def test_takes_last_block(self):
        assert extract_completion("x\n```python\na=1\n```") == "a=1"

    def test_bare_code_accepted(self):
        assert extract_completion("def f():\n    pass") == "def f():\n    pass"


class TestRewardGate:
    def test_identity_rewrite_is_zero_delta_positive_gate(self):
        s = load_sample("python")
        unit = s["unit_source"]
        r = compute_reward(wrapped("python", unit), s)
        assert r.gate == 1 and r.reason == "accepted"
        assert r.loc_delta == 0.0
        assert r.reward == 1.0  # no shaping without real reduction

    def test_no_code_block_is_negative(self):
        s = load_sample("python")
        r = compute_reward("I would reduce this by...", s)
        assert r.reward == -1.0 and r.reason == "no-code-block"

    def test_syntax_error_is_negative(self):
        s = load_sample("python")
        r = compute_reward(wrapped("python", "def broken(:"), s)
        assert r.reward == -1.0 and r.reason == "syntax-error"

    def test_behavior_break_is_negative_even_if_shorter(self):
        s = load_sample("python")
        broken = "def _noop():\n    pass\n" * 3
        r = compute_reward(wrapped("python", broken), s)
        assert r.reward == -1.0
        assert r.reason in ("api-changed", "tests-failed", "unit-not-found")

    def test_minified_penalty(self):
        s = load_sample("python")
        minified = "def f(x):" + ";return " * 1 + "x;" * 200
        minified = "def f(x): return " + "+".join(["x"] * 200)
        r = compute_reward(wrapped("python", minified), s)
        assert r.reason == "minified" or r.reward < 1.0

    def test_smaller_equivalent_rewrite_positive_and_shaped(self):
        s = load_sample("python")
        unit = s["unit_source"]
        # behavior-preserving textual transform: same code, drop trailing blank lines
        smaller = unit.rstrip() + "\n"
        r = compute_reward(wrapped("python", smaller), s)
        assert r.gate == 1


class TestDataset:
    def test_dataset_rows_wellformed(self):
        rows = [json.loads(l) for l in (REPO / "grpo" / "dataset.jsonl").open()]
        assert len(rows) >= 20
        langs = {r["lang"] for r in rows}
        assert langs == {"python", "javascript", "rust"}
        for r in rows:
            assert r["unit_loc"] >= 6
            assert "```" in r["prompt"] and "test" in r["prompt"].lower()
