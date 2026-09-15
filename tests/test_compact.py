"""Phase 8: mutation-certified test compaction - the kill-superset veto."""

from __future__ import annotations

import textwrap
from pathlib import Path

from less_code.test_compact import (
    apply_proposal,
    compare_kill_sets,
    kill_set,
    mask_code,
    run_mutants,
    unmask_candidate,
)

PROBE = textwrap.dedent(
    """\
    pub fn add(a: u32, b: u32) -> u32 {
        a + b
    }

    #[cfg(test)]
    mod tests {
        use super::add;

        #[test]
        fn sums() {
            assert_eq!(add(2, 3), 5);
            assert_eq!(add(0, 0), 0);
        }
    }
    """
)


def _probe_root(tmp_path: Path, source: str) -> Path:
    root = tmp_path / "probe"
    (root / "src").mkdir(parents=True)
    (root / "Cargo.toml").write_text(
        '[package]\nname = "probe"\nversion = "0.1.0"\nedition = "2021"\n'
    )
    (root / "src" / "lib.rs").write_text(source)
    return root


def test_mask_code_inverts_masking_and_round_trips():
    masked, spans = mask_code(PROBE)
    assert "pub fn add" not in masked
    assert "/*__LC_FROZEN_0__*/" in masked
    assert "mod tests" in masked and "assert_eq!(add(2, 3), 5);" in masked
    assert unmask_candidate(masked, spans) == PROBE


def test_compare_kill_sets_superset_and_veto():
    original = {"m1": "Caught", "m2": "Caught", "m3": "Missed", "m4": "unviable"}
    equal = {"m1": "Caught", "m2": "Caught", "m3": "Missed", "m4": "Missed"}
    ok, reason = compare_kill_sets(original, equal)
    assert ok and reason == ""
    dropped = {"m1": "Caught", "m2": "Missed", "m3": "Missed"}
    ok, reason = compare_kill_sets(original, dropped)
    assert not ok and "dropped" in reason and "m2" in reason


def test_kill_drop_is_vetoed_and_equivalent_compaction_applies(tmp_path):
    root = _probe_root(tmp_path, PROBE)
    # candidate that KEEPS all kills with fewer lines: one strong assert
    assert "        assert_eq!(add(2, 3), 5);\n" in PROBE
    weaker_ok = PROBE.replace(
        "        assert_eq!(add(2, 3), 5);\n        assert_eq!(add(0, 0), 0);\n",
        "        assert_eq!(add(2, 3), 5);\n",
    )
    assert weaker_ok != PROBE
    result = apply_proposal(
        root,
        {
            "id": "probe:1",
            "path": "src/lib.rs",
            "candidate": weaker_ok,
            "loc_delta": 1,
        },
        timeout=600,
    )
    assert result["applied"], result
    assert (root / "src" / "lib.rs").read_text() == weaker_ok

    # candidate that DROPS a kill: assert_eq!(add(2, 2), 4) lets the
    # `+ -> *` mutant survive - the veto must revert the file
    root2 = _probe_root(tmp_path / "second", PROBE)
    weak = PROBE.replace(
        "        assert_eq!(add(2, 3), 5);\n        assert_eq!(add(0, 0), 0);\n",
        "        assert_eq!(add(2, 2), 4);\n",
    )
    assert weak != PROBE
    result = apply_proposal(
        root2,
        {
            "id": "probe:1",
            "path": "src/lib.rs",
            "candidate": weak,
            "loc_delta": 1,
        },
        timeout=600,
    )
    assert not result["applied"]
    assert "dropped" in result["reason"]
    assert (root2 / "src" / "lib.rs").read_text() == PROBE


def test_run_mutants_parses_statuses(tmp_path):
    root = _probe_root(tmp_path, PROBE)
    statuses = run_mutants(root, timeout=600)
    assert statuses and all(
        v in ("Caught", "Missed", "Timeout") for v in statuses.values()
    )
    assert kill_set(statuses), "the strong test must kill something"
