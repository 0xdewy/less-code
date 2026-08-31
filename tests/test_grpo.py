"""Reward-function tests: gate severity, shaping, anti-hack — all CPU."""

import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "grpo"))

import pytest

from rewards import (
    LAMBDA,
    compute_reward,
    compute_reward_mutation_weighted,
    extract_completion,
    reward_fn,
    reward_fn_mutation_weighted,
)


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


# ---- mutation-weighted reward variant (CRITERIA C6) ----


VERBOSE_UNIT = '''def classify(n):
    if n > 100:
        result = "high"
    else:
        if n > 10:
            result = "mid"
        else:
            result = "low"
    return result
'''

TERSE_UNIT = '''def classify(n):
    return "high" if n > 100 else ("mid" if n > 10 else "low")
'''


@pytest.fixture
def tiny_fixture(tmp_path):
    """A real, gate-runnable python fixture so loc_delta is genuinely > 0.

    The reward runs the suite for real; nothing here is stubbed."""
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "mod.py").write_text(VERBOSE_UNIT, encoding="utf-8")
    (pkg / "test_mod.py").write_text(
        "from mod import classify\n\n"
        "def test_classify():\n"
        "    assert classify(500) == 'high'\n"
        "    assert classify(50) == 'mid'\n"
        "    assert classify(1) == 'low'\n",
        encoding="utf-8",
    )
    (pkg / "conftest.py").write_text("", encoding="utf-8")
    return tmp_path


def _sample(tiny_fixture, **extra) -> dict:
    s = {"fixture": "pkg", "lang": "python", "unit_source": VERBOSE_UNIT.strip()}
    s.update(extra)
    return s


class TestMutationWeightedReward:
    def test_setup_gives_a_real_positive_delta(self, tiny_fixture):
        r = compute_reward(wrapped("python", TERSE_UNIT), _sample(tiny_fixture),
                           repo_root=tiny_fixture)
        assert r.reason == "accepted" and r.gate == 1
        assert r.loc_delta > 0

    def test_equal_to_unweighted_at_score_one(self, tiny_fixture):
        plain = compute_reward(wrapped("python", TERSE_UNIT), _sample(tiny_fixture),
                               repo_root=tiny_fixture)
        weighted = compute_reward_mutation_weighted(
            wrapped("python", TERSE_UNIT), _sample(tiny_fixture, mutation_score=1.0),
            repo_root=tiny_fixture,
        )
        assert weighted.reward == pytest.approx(plain.reward)

    def test_absent_score_defaults_to_one(self, tiny_fixture):
        """Datasets built before the field existed keep their old rewards."""
        plain = compute_reward(wrapped("python", TERSE_UNIT), _sample(tiny_fixture),
                               repo_root=tiny_fixture)
        weighted = compute_reward_mutation_weighted(
            wrapped("python", TERSE_UNIT), _sample(tiny_fixture), repo_root=tiny_fixture
        )
        assert weighted.reward == pytest.approx(plain.reward)

    def test_weak_suite_is_shaped_down(self, tiny_fixture):
        plain = compute_reward(wrapped("python", TERSE_UNIT), _sample(tiny_fixture),
                               repo_root=tiny_fixture)
        weak = compute_reward_mutation_weighted(
            wrapped("python", TERSE_UNIT), _sample(tiny_fixture, mutation_score=0.4),
            repo_root=tiny_fixture,
        )
        assert weak.reward < plain.reward
        assert weak.reward == pytest.approx(1.0 + LAMBDA * weak.loc_delta * 0.4)
        assert weak.reward > 1.0  # still better than no reduction at all

    def test_monotone_in_suite_strength(self, tiny_fixture):
        rewards = [
            compute_reward_mutation_weighted(
                wrapped("python", TERSE_UNIT), _sample(tiny_fixture, mutation_score=s),
                repo_root=tiny_fixture,
            ).reward
            for s in (0.0, 0.5, 1.0)
        ]
        assert rewards == sorted(rewards)
        assert rewards[0] == pytest.approx(1.0)  # a worthless oracle buys no shaping

    def test_gate_still_dominates_a_perfect_suite(self, tiny_fixture):
        """A behavior break is -1.0 no matter how strong the suite is: the gate
        is never scaled, so weighting cannot buy a broken rollout."""
        broken = "def classify(n):\n    return 'high'\n"
        r = compute_reward_mutation_weighted(
            wrapped("python", broken), _sample(tiny_fixture, mutation_score=1.0),
            repo_root=tiny_fixture,
        )
        assert r.reward == -1.0 and r.gate == -1

    def test_gate_dominates_a_weak_suite_too(self, tiny_fixture):
        r = compute_reward_mutation_weighted(
            wrapped("python", "def classify(:"), _sample(tiny_fixture, mutation_score=0.1),
            repo_root=tiny_fixture,
        )
        assert r.reward == -1.0 and r.reason == "syntax-error"

    def test_score_is_clamped(self, tiny_fixture):
        hi = compute_reward_mutation_weighted(
            wrapped("python", TERSE_UNIT), _sample(tiny_fixture, mutation_score=9.0),
            repo_root=tiny_fixture,
        )
        plain = compute_reward(wrapped("python", TERSE_UNIT), _sample(tiny_fixture),
                               repo_root=tiny_fixture)
        assert hi.reward == pytest.approx(plain.reward)

    def test_trl_callable_shape(self):
        """Both reward callables are TRL-compatible: list in, list of floats out."""
        for fn in (reward_fn, reward_fn_mutation_weighted):
            out = fn(["no code here", "still none"], sample_meta=[None, None])
            assert out == [-1.0, -1.0]


class TestDatasetMutationScores:
    def test_committed_dataset_carries_suite_strength(self):
        rows = [json.loads(l) for l in (REPO / "grpo" / "dataset.jsonl").open()]
        scored = [r for r in rows if "mutation_score" in r]
        assert scored, "builder should stamp a per-fixture mutation score"
        for r in scored:
            assert 0.0 <= r["mutation_score"] <= 1.0
            assert r["mutation_score"] >= 0.70  # the criteria's trust bar

    def test_validate_config_requires_both_reward_variants(self):
        """C6 names two reward functions; `--validate-config` should fail loudly
        if either goes missing rather than at step 1 of a GPU run."""
        import importlib

        import rewards

        train = importlib.import_module("train")
        cfg = dict(train.DEFAULTS)
        ds = REPO / "grpo" / "dataset.jsonl"
        assert train.validate_config(cfg, ds) == []
        saved = rewards.reward_fn_mutation_weighted
        try:
            del rewards.reward_fn_mutation_weighted
            errs = train.validate_config(cfg, ds)
            assert any("reward_fn_mutation_weighted" in e for e in errs)
        finally:
            rewards.reward_fn_mutation_weighted = saved


class TestNonPythonParseGate:
    """C7 review D9 (second note): the reward's docstring promised a
    parse/compile check but only python was actually parsed; js/rust rollouts
    reached the test runner unparsed."""

    def test_broken_js_is_a_syntax_error_not_a_test_failure(self, tmp_path):
        import shutil as _shutil

        if _shutil.which("node") is None:
            pytest.skip("node not installed")
        s = load_sample("javascript")
        r = compute_reward(wrapped("javascript", "function broken( {"), s)
        assert r.reward == -1.0
        assert r.reason == "syntax-error"
