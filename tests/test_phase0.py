"""Phase 0: rustfmt config/edition honesty, rust style gate, test-share."""

from __future__ import annotations

import textwrap
from pathlib import Path

from less_code.corpus import rust_inline_test_loc
from less_code.loc import (
    canonical_format,
    measure,
    project_canonical_format,
    project_measure,
    rustfmt_config,
)
from less_code.pipeline import shrink_project
from less_code.static import StaticResult

ORIG = textwrap.dedent(
    """\
    pub fn stats(x: u64, y: u64) -> (u64, u64, u64) {
        let bigger = if x > y { x } else { y };
        let smaller = if x > y { y } else { x };
        (bigger, smaller, bigger - smaller)
    }

    #[cfg(test)]
    mod tests {
        use super::stats;

        #[test]
        fn pairs() {
            assert_eq!(stats(3, 1), (3, 1, 2));
        }
    }
    """
)

JOINED = (
    "    let (big, small, diff) = if x > y { (x, y, x - y) } else { (y, x, y - x) };\n"
)
REPLACED = (
    "    let bigger = if x > y { x } else { y };\n"
    "    let smaller = if x > y { y } else { x };\n"
    "    (bigger, smaller, bigger - smaller)\n"
)


def _rust_root(tmp_path: Path, source: str) -> Path:
    root = tmp_path / "tiny-rs"
    (root / "src").mkdir(parents=True)
    (root / "Cargo.toml").write_text(
        '[package]\nname = "tiny"\nversion = "0.1.0"\nedition = "2021"\n'
    )
    (root / "src" / "lib.rs").write_text(source)
    return root


def _static_proposal(monkeypatch, root: Path, candidate: str):
    monkeypatch.setattr(
        "less_code.pipeline.static_pass",
        lambda *_a, **_k: StaticResult(
            changed_files={str(root / "src" / "lib.rs"): candidate}
        ),
    )


def test_rustfmt_config_discovery_and_edition(tmp_path):
    root = _rust_root(tmp_path, ORIG)
    config_path, edition = rustfmt_config(root)
    assert config_path is None
    assert edition == "2021"
    (root / "rustfmt.toml").write_text("max_width = 60\n")
    config_path, edition = rustfmt_config(root)
    assert config_path == str(root / "rustfmt.toml")
    (root / "Cargo.toml").write_text('[package]\nname = "tiny"\nversion = "0.1.0"\n')
    _, edition = rustfmt_config(root)
    assert edition == "2024"


def test_project_canonical_format_uses_project_width(tmp_path):
    root = _rust_root(tmp_path, ORIG)
    (root / "rustfmt.toml").write_text("max_width = 60\n")
    assert 60 < len(JOINED.strip()) <= 88
    candidate = ORIG.replace(REPLACED, JOINED + "    (big, small, diff)\n")
    assert candidate != ORIG
    assert measure(candidate, "rust").code < measure(ORIG, "rust").code
    joined, ok = project_canonical_format(candidate, root)
    assert ok and joined != candidate
    assert project_measure(candidate, root).code >= project_measure(ORIG, root).code


def test_fallback_masker_unbalanced_brace_rejected(tmp_path, monkeypatch):
    """rustfmt rejects the candidate, measure() falls back to raw text and
    would show a fake reduction - the gate must parse and reject instead."""
    root = _rust_root(tmp_path, ORIG)
    broken = ORIG.replace(
        "    (bigger, smaller, bigger - smaller)\n}\n",
        "    (bigger, smaller, bigger - smaller\n",
    )
    assert broken != ORIG
    assert canonical_format(broken, "rust")[1] is False
    assert measure(broken, "rust").code < measure(ORIG, "rust").code
    _static_proposal(monkeypatch, root, broken)
    stats = shrink_project(root, "rust")
    assert stats.loc_final == stats.loc_start
    assert (root / "src" / "lib.rs").read_text() == ORIG
    assert any("invalid syntax" in note for note in stats.static_notes)


def test_join_dying_under_project_rustfmt_is_rejected(tmp_path, monkeypatch):
    root = _rust_root(tmp_path, ORIG)
    (root / "rustfmt.toml").write_text("max_width = 60\n")
    candidate = ORIG.replace(REPLACED, JOINED + "    (big, small, diff)\n")
    assert measure(candidate, "rust").code < measure(ORIG, "rust").code
    _static_proposal(monkeypatch, root, candidate)
    stats = shrink_project(root, "rust")
    assert stats.loc_final == stats.loc_start
    assert (root / "src" / "lib.rs").read_text() == ORIG
    assert any("rustfmt config" in note for note in stats.static_notes)


def test_rust_style_gate_rejects_dirty_candidate(tmp_path, monkeypatch):
    root = _rust_root(tmp_path, ORIG)
    (root / "rustfmt.toml").write_text("max_width = 60\n")
    candidate = ORIG.replace(
        "    let bigger = if x > y { x } else { y };\n"
        "    let smaller = if x > y { y } else { x };\n",
        JOINED,
    )
    assert candidate != ORIG
    assert measure(candidate, "rust").code < measure(ORIG, "rust").code
    _static_proposal(monkeypatch, root, candidate)
    stats = shrink_project(root, "rust")
    assert stats.loc_final == stats.loc_start
    assert (root / "src" / "lib.rs").read_text() == ORIG


def test_project_without_rustfmt_config_skips_style_gate(tmp_path, monkeypatch):
    root = _rust_root(tmp_path, ORIG)
    good = ORIG.replace(
        "    let bigger = if x > y { x } else { y };\n"
        "    let smaller = if x > y { y } else { x };\n",
        "    let (bigger, smaller) = if x > y { (x, y) } else { (y, x) };\n",
    )
    assert measure(good, "rust").code < measure(ORIG, "rust").code
    _static_proposal(monkeypatch, root, good)
    stats = shrink_project(root, "rust")
    assert stats.loc_final < stats.loc_start
    assert stats.tests_ok


def test_corpus_rust_inline_test_share(tmp_path):
    root = _rust_root(tmp_path, ORIG)
    total = rust_inline_test_loc({root / "src" / "lib.rs": ORIG})
    full = measure(ORIG, "rust").code
    fn_only = measure(ORIG[: ORIG.index("#[cfg(test)]")], "rust").code
    assert total == full - fn_only
    assert 0 < total < full
