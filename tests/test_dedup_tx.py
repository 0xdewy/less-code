"""Phase 5: cross-function dedup transactions - all-or-nothing edits."""

from __future__ import annotations

import textwrap

from less_code.dedup_tx import (
    cascade_reject_tx,
    clone_groups,
    default_tx_gate,
    jaccard,
    shingles,
    validate_transaction,
)
from less_code.loc import TOKEN_RE


def _cloney(name: str, factor: int) -> str:
    return textwrap.dedent(
        f"""\
        fn loud_{name}(x: u64) -> u64 {{
            let mut total = 0;
            let mut cursor = x;
            while cursor > 0 {{
                let step = cursor % 8;
                total += step * {factor};
                cursor >>= 1;
                if step == 0 {{
                    total += 1;
                }}
            }}
            let mut scaled = total;
            scaled = scaled.wrapping_mul(31);
            scaled ^= scaled >> 2;
            scaled = scaled.wrapping_add(x);
            scaled %= 1_000_003;
            scaled
        }}

        """
    )


CLONEY = (
    _cloney("a", 3) + _cloney("b", 7) + "fn different(x: u64) -> u64 {\n    x + 1\n}\n"
)


def test_shingles_and_jaccard():
    a = shingles("one two three four five six seven eight nine ten")
    b = shingles("one two three four five six seven eight nine ten!")
    assert TOKEN_RE.findall("a b") == ["a", "b"]
    assert jaccard(a, b) >= 0.7
    assert jaccard(a, shingles("completely different words here now ok fine")) < 0.7


def test_clone_groups_find_planted_pair(tmp_path):
    lib = tmp_path / "lib.rs"
    groups = clone_groups({lib: CLONEY})
    assert groups, "the two loud_* loops are near-duplicates"
    names = set(groups[0]["functions"])
    assert "loud_a" in names and "loud_b" in names
    assert "different" not in names


def _tx(helper: str, sites: list[dict]) -> dict:
    return {"helper": helper, "call_sites": sites}


def test_validate_transaction_happy_path(tmp_path):
    lib = tmp_path / "lib.rs"
    sources = {lib: CLONEY}
    helper = "fn loud(factors: u64, x: u64) -> u64 {\n    let mut total = 0;\n    let mut cursor = x;\n    while cursor > 0 {\n        let step = cursor % 8;\n        total += step * factors;\n        cursor >>= 1;\n        if step == 0 {\n            total += 1;\n        }\n    }\n    let mut scaled = total;\n    scaled = scaled.wrapping_mul(31);\n    scaled ^= scaled >> 2;\n    scaled = scaled.wrapping_add(x);\n    scaled %= 1_000_003;\n    scaled\n}"
    proposal = _tx(
        helper,
        [
            {
                "path": "lib.rs",
                "anchor": "fn loud_a(x: u64) -> u64 {",
                "replacement_region": _cloney("a", 3).strip(),
                "replacement": "fn loud_a(x: u64) -> u64 {\n    loud(3, x)\n}",
            },
            {
                "path": "lib.rs",
                "anchor": "fn loud_b(x: u64) -> u64 {",
                "replacement_region": _cloney("b", 7).strip(),
                "replacement": "fn loud_b(x: u64) -> u64 {\n    loud(7, x)\n}",
            },
        ],
    )
    candidates, reason = validate_transaction(proposal, sources, tmp_path)
    assert reason == ""
    assert "fn loud(" in candidates[lib]
    assert "i * 3;" not in candidates[lib] or "loud(3, x)" in candidates[lib]


def test_validate_transaction_rejects_bad_anchors_and_helpers(tmp_path):
    lib = tmp_path / "lib.rs"
    sources = {lib: CLONEY}
    helper = "fn loud() {}"
    good_region = _cloney("a", 3).strip()
    # non-unique anchor
    _, reason = validate_transaction(
        _tx(
            helper,
            [
                {
                    "path": "lib.rs",
                    "anchor": "fn ",
                    "replacement_region": good_region,
                    "replacement": "x",
                }
            ],
        ),
        sources,
        tmp_path,
    )
    assert "anchor not unique" in reason
    # non-unique region
    _, reason = validate_transaction(
        _tx(
            helper,
            [
                {
                    "path": "lib.rs",
                    "anchor": "fn loud_a",
                    "replacement_region": "total += 1;",
                    "replacement": "x",
                }
            ],
        ),
        sources,
        tmp_path,
    )
    assert "region not unique" in reason
    # unknown path
    _, reason = validate_transaction(
        _tx(
            helper,
            [
                {
                    "path": "nope.rs",
                    "anchor": "a",
                    "replacement_region": good_region,
                    "replacement": "x",
                }
            ],
        ),
        sources,
        tmp_path,
    )
    assert "unknown path" in reason
    # pub helper
    _, reason = validate_transaction(
        _tx(
            "pub fn loud() {}",
            [
                {
                    "path": "lib.rs",
                    "anchor": "fn loud_a",
                    "replacement_region": good_region,
                    "replacement": "x",
                }
            ],
        ),
        sources,
        tmp_path,
    )
    assert "private" in reason
    # generic helper
    _, reason = validate_transaction(
        _tx(
            "fn loud<T>(v: T) {}",
            [
                {
                    "path": "lib.rs",
                    "anchor": "fn loud_a",
                    "replacement_region": good_region,
                    "replacement": "x",
                }
            ],
        ),
        sources,
        tmp_path,
    )
    assert "generic" in reason
    # non-parsing helper
    _, reason = validate_transaction(
        _tx(
            "fn loud( {",
            [
                {
                    "path": "lib.rs",
                    "anchor": "fn loud_a",
                    "replacement_region": good_region,
                    "replacement": "x",
                }
            ],
        ),
        sources,
        tmp_path,
    )
    assert "does not parse" in reason


def test_cascade_reject_tx_symbol_and_loc_gates(tmp_path):
    lib = tmp_path / "lib.rs"
    shrunk = CLONEY.replace(
        "fn loud_a(x: u64) -> u64 {\n    let mut total = 0;\n    for i in 0..x {\n        total += i * 3;\n    }\n    total\n}",
        "fn loud_a(x: u64) -> u64 {\n    loud(3, x)\n}",
    )
    # no helper added -> symbol set changed
    assert cascade_reject_tx({lib: CLONEY}, {lib: shrunk}, "loud", tmp_path) != ""
    # helper added but LOC does not drop: helper duplicates body without
    # shrinking call sites
    with_helper = (
        CLONEY
        + "\nfn loud(f: u64, x: u64) -> u64 {\n    let mut total = 0;\n    for i in 0..x {\n        total += i * f;\n    }\n    total\n}\n"
    )
    assert cascade_reject_tx({lib: CLONEY}, {lib: with_helper}, "loud", tmp_path) == (
        "no canonical LOC reduction"
    )


def test_tx_gate_reverts_all_files_on_failure(tmp_path):
    root = tmp_path / "tiny-rs"
    (root / "src").mkdir(parents=True)
    (root / "Cargo.toml").write_text(
        '[package]\nname = "tiny"\nversion = "0.1.0"\nedition = "2021"\n'
    )
    one = root / "src" / "one.rs"
    two = root / "src" / "two.rs"
    lib = root / "src" / "lib.rs"
    one_text = "pub fn a(x: u64) -> u64 {\n    let t = x * 3;\n    t\n}\n"
    two_text = "pub fn b(x: u64) -> u64 {\n    let t = x * 3;\n    t\n}\n"
    one.write_text(one_text)
    two.write_text(two_text)
    lib.write_text("pub mod one;\npub mod two;\n")
    pristine = {one: one_text, two: two_text, lib: lib.read_text()}
    gate = default_tx_gate(root, pristine, test_timeout=300)
    broken_two = two_text.replace("let t = x * 3;\n    t", "let t: u64 = x as String")
    candidates = {
        one: one_text.replace("let t = x * 3;\n    t", "deuplicated(3, x)"),
        two: two_text.replace("let t = x * 3;\n    t", "let u = x;\n    u"),
        lib: lib.read_text()
        + "\nfn deuplicated(f: u64, x: u64) -> u64 {\n    let t = x * f;\n    t\n}\n",
    }
    candidates[one] = candidates[one].replace(
        "pub fn a(x: u64) -> u64 {\n    deuplicated(3, x)\n}",
        "pub fn a(x: u64) -> u64 {\n    deuplicated(3, x)\n}",
    )
    candidates = dict(candidates)
    candidates[one] = one_text.replace(
        "    let t = x * 3;\n    t\n", "    deuplicated(3, x)\n"
    )
    candidates[two] = broken_two
    _, _, accepted, reason = gate(candidates)
    assert not accepted and reason.startswith("compile failed")
    for path, text in pristine.items():
        assert path.read_text() == text, "all files must be byte-restored"
