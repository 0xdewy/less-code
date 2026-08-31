"""D3 subset — dead `pub` item removal for Rust, against a real synthetic crate.

The compiler is the oracle here: after the pass, `cargo test` must still be
green on a crate whose only remaining references are the ones we kept.
"""

import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

from less_code.static import (
    _rust_attached_start,
    _rust_remove_dead_pub,
    _rust_top_level_pub_items,
)

cargo = pytest.mark.skipif(shutil.which("cargo") is None, reason="cargo not installed")

LIB = textwrap.dedent(
    """\
    //! A tiny crate.

    /// Shout the input.
    ///
    /// Used by the integration test.
    pub fn shout(input: &str) -> String {
        input.to_uppercase()
    }

    /// Reverse the word order.
    ///
    /// Deprecated: nothing calls this any more.
    #[allow(dead_code)]
    pub fn reverse_words(input: &str) -> String {
        let mut words: Vec<&str> = input.split_whitespace().collect();
        words.reverse();
        words.join(" ")
    }

    /// A struct nobody constructs.
    pub struct Orphan {
        pub value: u32,
    }

    /// Kept: the lib's own code calls it.
    pub fn helper(n: u32) -> u32 {
        n + 1
    }

    pub fn bump(n: u32) -> u32 {
        helper(n)
    }

    /// A signature whose return type contains a `;` before the body brace.
    pub fn fixed(n: u8) -> [u8; 2] {
        [n, n]
    }
    """
)

TESTS = textwrap.dedent(
    """\
    use synthcrate::{bump, fixed, shout};

    #[test]
    fn it_works() {
        assert_eq!(shout("hi"), "HI");
        assert_eq!(bump(1), 2);
        assert_eq!(fixed(3), [3u8, 3u8]);
    }
    """
)

CARGO = textwrap.dedent(
    """\
    [package]
    name = "synthcrate"
    version = "0.1.0"
    edition = "2021"

    [lib]
    path = "src/lib.rs"
    """
)


@pytest.fixture
def crate(tmp_path: Path) -> Path:
    (tmp_path / "src").mkdir()
    (tmp_path / "tests").mkdir()
    (tmp_path / "Cargo.toml").write_text(CARGO)
    (tmp_path / "src" / "lib.rs").write_text(LIB)
    (tmp_path / "tests" / "it.rs").write_text(TESTS)
    return tmp_path


def run_pass(crate: Path):
    src = [crate / "src" / "lib.rs"]
    return _rust_remove_dead_pub(src, src + [crate / "tests" / "it.rs"])


def test_finds_top_level_pub_items():
    names = [n for n, _, _ in _rust_top_level_pub_items(LIB)]
    assert names == ["shout", "reverse_words", "Orphan", "helper", "bump", "fixed"]
    # `pub value: u32` is a field, not a top-level item: it must not be listed
    assert "value" not in names


def test_item_end_survives_a_semicolon_inside_the_return_type():
    spans = {n: (s, e) for n, s, e in _rust_top_level_pub_items(LIB)}
    start, end = spans["fixed"]
    assert LIB[start:end].rstrip().endswith("}")
    assert "[n, n]" in LIB[start:end]


def test_attached_doc_comments_and_attributes_are_included():
    spans = {n: (s, e) for n, s, e in _rust_top_level_pub_items(LIB)}
    start, end = spans["reverse_words"]
    text = LIB[start:end]
    assert text.startswith("/// Reverse the word order.")
    assert "#[allow(dead_code)]" in text
    # the doc block of the *previous* item must not be swallowed
    assert "Used by the integration test" not in text


def test_attached_start_lands_on_a_line_boundary():
    for _, start, _ in _rust_top_level_pub_items(LIB):
        assert start == 0 or LIB[start - 1] == "\n"
    assert _rust_attached_start("pub fn a() {}\n", 0) == 0


def test_removes_only_unreferenced_items(crate: Path):
    result = run_pass(crate)
    new = result.changed_files[str(crate / "src" / "lib.rs")]
    assert "reverse_words" not in new
    assert "struct Orphan" not in new
    assert "Deprecated: nothing calls this any more" not in new
    for kept in ("pub fn shout", "pub fn helper", "pub fn bump", "pub fn fixed"):
        assert kept in new
    assert result.loc_removed > 0


def test_reference_from_a_test_file_keeps_the_item(crate: Path):
    (crate / "tests" / "it.rs").write_text(
        TESTS.replace("use synthcrate::{bump, fixed, shout};",
                      "use synthcrate::{bump, fixed, reverse_words, shout};")
        + '\n#[test]\nfn rw() { assert_eq!(reverse_words("a b"), "b a"); }\n'
    )
    new = run_pass(crate).changed_files[str(crate / "src" / "lib.rs")]
    assert "pub fn reverse_words" in new


def test_nothing_removed_when_everything_is_referenced(tmp_path: Path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "lib.rs").write_text("pub fn a() -> u32 { 1 }\n")
    (tmp_path / "t.rs").write_text("fn main() { let _ = a(); }\n")
    result = _rust_remove_dead_pub(
        [tmp_path / "src" / "lib.rs"], [tmp_path / "src" / "lib.rs", tmp_path / "t.rs"]
    )
    assert result.changed_files == {}


@cargo
def test_crate_still_compiles_and_tests_pass_after_removal(crate: Path):
    before = subprocess.run(
        ["cargo", "test", "--quiet"], cwd=crate, capture_output=True, text=True, timeout=600
    )
    assert before.returncode == 0, before.stdout + before.stderr

    for path, text in run_pass(crate).changed_files.items():
        Path(path).write_text(text)

    after = subprocess.run(
        ["cargo", "test", "--quiet"], cwd=crate, capture_output=True, text=True, timeout=600
    )
    assert after.returncode == 0, after.stdout + after.stderr
    assert "reverse_words" not in (crate / "src" / "lib.rs").read_text()


# ---- iteration 09: the clippy pedantic/complexity tier ---------------------


PEDANTIC_LIB = textwrap.dedent(
    """\
    /// Sum a slice the long way — `clippy::needless_range_loop` territory.
    pub fn total(values: &[u32]) -> u32 {
        let mut sum = 0;
        for i in 0..values.len() {
            sum += values[i];
        }
        sum
    }

    #[cfg(test)]
    mod tests {
        use super::*;

        #[test]
        fn it_sums() {
            assert_eq!(total(&[1, 2, 3]), 6);
        }
    }
    """
)


def _crate(tmp_path: Path, lib: str) -> Path:
    root = tmp_path / "crate"
    (root / "src").mkdir(parents=True)
    (root / "Cargo.toml").write_text(
        '[package]\nname = "peda"\nversion = "0.1.0"\nedition = "2021"\n'
    )
    (root / "src" / "lib.rs").write_text(lib)
    return root


def test_clippy_pedantic_is_optional_when_cargo_is_missing(tmp_path, monkeypatch):
    from less_code import static as static_mod

    monkeypatch.setattr(static_mod.shutil, "which", lambda _name: None)
    root = _crate(tmp_path, PEDANTIC_LIB)
    result = static_mod._rust_clippy_pedantic(root, [root / "src" / "lib.rs"])
    assert result.changed_files == {}
    assert "skipping clippy pedantic" in " ".join(result.notes)


@cargo
def test_clippy_pedantic_returns_edits_and_leaves_the_tree_untouched(tmp_path):
    from less_code.static import _rust_clippy_pedantic

    root = _crate(tmp_path, PEDANTIC_LIB)
    lib = root / "src" / "lib.rs"
    before = lib.read_text()
    result = _rust_clippy_pedantic(root, [lib])
    # whatever it decided, the tree is back the way it was: the edits travel
    # as `changed_files`, inside the pipeline's revert set
    assert lib.read_text() == before
    if result.changed_files:
        assert result.changed_files[str(lib)] != before


@cargo
def test_a_red_suite_drops_the_pedantic_tier_whole(tmp_path):
    from less_code.static import _rust_clippy_pedantic

    root = _crate(tmp_path, PEDANTIC_LIB)
    lib = root / "src" / "lib.rs"
    result = _rust_clippy_pedantic(
        root, [lib], runner=lambda _r, _l: _Red()
    )
    assert result.changed_files == {}
    assert any("reverted by the gate" in n for n in result.notes) or not any(
        "kept" in n for n in result.notes
    )


class _Red:
    ok = False
    output_tail = "boom"
