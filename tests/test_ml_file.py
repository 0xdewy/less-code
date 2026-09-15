"""Phase 1: whole-file model rewrites - masking, FileBackend, cascade, loop."""

from __future__ import annotations

import json
import textwrap
from pathlib import Path

import pytest

from less_code.ml_file import (
    FileBackend,
    cascade_reject,
    default_gate,
    mask_tests,
    rewrite_file,
    rewrite_tree,
    unmask_candidates,
)

ORIG = textwrap.dedent(
    """\
    /// Adds x to itself; the doc comment must survive any rewrite.
    pub fn value(x: u64) -> u64 {
        let a = x;
        let b = x;
        a + b
    }

    fn _helper(x: u64) -> u64 {
        x
    }

    #[cfg(test)]
    mod tests {
        use super::value;

        #[test]
        fn doubles() {
            assert_eq!(value(3), 6);
        }
    }
    """
)


def _rust_root(tmp_path: Path, source: str = ORIG) -> Path:
    root = tmp_path / "tiny-rs"
    (root / "src").mkdir(parents=True)
    (root / "Cargo.toml").write_text(
        '[package]\nname = "tiny"\nversion = "0.1.0"\nedition = "2021"\n'
    )
    (root / "src" / "lib.rs").write_text(source)
    return root


def _replay_command(tmp_path: Path, frm: str, to: str) -> str:
    """A scripted no-model backend: apply one text replacement to whatever
    masked source arrives (the bench/ollama.py replay trick)."""
    script = tmp_path / "_replay_backend.py"
    if not script.exists():
        script.write_text(
            "import json, sys\n"
            "payload = json.load(sys.stdin)\n"
            "frm, to = json.loads(sys.argv[1]), json.loads(sys.argv[2])\n"
            "content = payload['source'].replace(frm, to)\n"
            "print(json.dumps({'path': payload['path'], 'content': content}))\n"
        )
    return (
        f"python3 {script} {json.dumps(json.dumps(frm))} {json.dumps(json.dumps(to))}"
    )


# ---- masking ----


def test_mask_unmask_round_trip():
    masked, spans = mask_tests(ORIG)
    assert len(spans) == 1
    assert "mod tests" not in masked
    assert "assert_eq!" not in masked
    assert "/*__LC_FROZEN_0__*/" in masked
    assert "pub fn value" in masked
    assert unmask_candidates(masked, spans) == ORIG


def test_mask_keeps_non_test_bytes_exact():
    masked, spans = mask_tests(ORIG)
    start, end, span_text = spans[0]
    assert ORIG.encode()[start:end].decode() == span_text
    head = masked[: masked.index("/*__LC_FROZEN_0__*/")]
    assert head == ORIG[: len(head)]
    tail = masked[masked.index("/*__LC_FROZEN_0__*/") + len("/*__LC_FROZEN_0__*/") :]
    assert ORIG.endswith(tail)


def test_unmask_rejects_deleted_reordered_duplicated_edited():
    masked, spans = mask_tests(ORIG)
    sentinel = "/*__LC_FROZEN_0__*/"
    assert unmask_candidates(masked.replace(sentinel, ""), spans) is None
    assert unmask_candidates(masked, spans) == ORIG
    two, two_spans = mask_tests("#[cfg(test)]\nmod tests {}\n\n#[test]\nfn t() {}\n")
    assert len(two_spans) == 2
    flipped = (
        two.replace("/*__LC_FROZEN_0__*/", "@@0@@")
        .replace("/*__LC_FROZEN_1__*/", "/*__LC_FROZEN_0__*/")
        .replace("@@0@@", "/*__LC_FROZEN_1__*/")
    )
    assert unmask_candidates(flipped, two_spans) is None
    assert (
        unmask_candidates(
            two.replace(
                "/*__LC_FROZEN_0__*/", "/*__LC_FROZEN_0__*/ /*__LC_FROZEN_0__*/"
            ),
            two_spans,
        )
        is None
    )
    assert (
        unmask_candidates(masked.replace(sentinel, "/*__LC_FROZEN_9__*/"), spans)
        is None
    )


def test_span_region_edit_is_impossible_by_construction():
    """Whatever the model writes AROUND the sentinel, the span bytes come
    back verbatim from the host - the model never reproduces test code."""
    masked, spans = mask_tests(ORIG)
    sabotaged = masked.replace(
        "/*__LC_FROZEN_0__*/",
        "/*__LC_FROZEN_0__*/ // model may not touch what follows",
    )
    restored = unmask_candidates(sabotaged, spans)
    assert restored is not None
    assert "mod tests {" in restored
    assert "assert_eq!(value(3), 6);" in restored


# ---- FileBackend ----


def test_file_backend_scripted_round_trip(tmp_path):
    command = _replay_command(
        tmp_path, "    let a = x;\n    let b = x;\n    a + b\n", "    x + x\n"
    )
    backend = FileBackend(command)
    masked, _ = mask_tests(ORIG)
    out = backend.rewrite(Path("lib.rs"), masked, "", None)
    assert out is not None and "x + x" in out


def test_file_backend_abstain_fence_and_path_rules(tmp_path):
    null_script = tmp_path / "_null_backend.py"
    null_script.write_text("import sys\nsys.stdin.read()\nprint('null')\n")
    assert (
        FileBackend(f"python3 {null_script}").rewrite(Path("a.rs"), "x", "", None)
        is None
    )

    fenced = tmp_path / "_fenced_backend.py"
    fenced.write_text(
        "import json, sys\n"
        "sys.stdin.read()\n"
        "print('```json')\n"
        "print(json.dumps({'path': 'a.rs', 'content': 'fn x() {}'}))\n"
        "print('```')\n"
    )
    assert (
        FileBackend(f"python3 {fenced}").rewrite(Path("a.rs"), "x", "", None)
        == "fn x() {}"
    )

    wrong_path = tmp_path / "_wrong_path_backend.py"
    wrong_path.write_text(
        "import json, sys\n"
        "sys.stdin.read()\n"
        "print(json.dumps({'path': 'other.rs', 'content': 'x'}))\n"
    )
    with pytest.raises(RuntimeError):
        FileBackend(f"python3 {wrong_path}").rewrite(Path("a.rs"), "x", "", None)

    huge = tmp_path / "_huge_backend.py"
    huge.write_text(
        "import json, sys\n"
        "sys.stdin.read()\n"
        "print(json.dumps({'path': 'a.rs', 'content': 'x' * (97 * 1024)}))\n"
    )
    with pytest.raises(ValueError):
        FileBackend(f"python3 {huge}").rewrite(Path("a.rs"), "x", "", None)


# ---- cascade stages (pure text) ----


def joined(candidate_from: str, frm: str, to: str) -> str:
    assert frm in candidate_from
    return candidate_from.replace(frm, to)


def test_cascade_syntax_error():
    broken = joined(
        ORIG,
        "    let a = x;\n    let b = x;\n    a + b\n",
        "        a + (x\n",
    )
    assert cascade_reject(ORIG, broken, Path(".")) == "invalid syntax"


def test_cascade_symbol_drift_rename():
    renamed = ORIG.replace("fn _helper(", "fn _renamed_helper(")
    assert "symbol vanished" in cascade_reject(ORIG, renamed, Path("."))


def test_cascade_declaration_change():
    changed = ORIG.replace(
        "fn _helper(x: u64) -> u64", "fn _helper(x: u64, y: u64) -> u64"
    )
    assert "declaration changed" in cascade_reject(ORIG, changed, Path("."))


def test_cascade_docs_loss():
    lost = ORIG.replace(
        "/// Adds x to itself; the doc comment must survive any rewrite.\n", ""
    )
    assert cascade_reject(ORIG, lost, Path(".")) == "documentation changed"


def test_cascade_no_loc_drop():
    same = ORIG.replace("    a + b\n", "    a +\n    b\n")
    assert same != ORIG
    assert "no canonical LOC reduction" in cascade_reject(ORIG, same, Path("."))


def test_cascade_new_public_item_rejected():
    added = ORIG.replace(
        "fn _helper(x: u64) -> u64 {\n    x\n}\n",
        "fn _helper(x: u64) -> u64 {\n    x\n}\n\npub fn extra() {}\n",
    )
    assert added != ORIG
    assert "new public item" in cascade_reject(ORIG, added, Path("."))


def test_cascade_api_removed_pub():
    """A pub fn removal is caught earlier by declaration drift; the API
    stage's own net is struct/field surface, which fn-symbol drift cannot
    see (extract_symbols is function_item-only)."""
    shrunk = ORIG.replace(
        "    let a = x;\n    let b = x;\n    a + b\n",
        "    x + x\n",
    )
    assert cascade_reject(ORIG, shrunk, Path(".")) == ""
    with_struct = shrunk.replace(
        "fn _helper(",
        "pub struct Config {\n    pub size: u64\n}\n\nfn _helper(",
    )
    field_gone = with_struct.replace(
        "pub struct Config {\n    pub size: u64\n}",
        "pub struct Config;",
    )
    assert cascade_reject(with_struct, field_gone, Path(".")).startswith("API changed")


_STATS_FN = textwrap.dedent(
    """\
pub fn stats(x: u64, y: u64) -> u64 {{
    let s1 = x + 1;
    let s2 = y + 2;
    let s3 = s1 + s2;
    let s4 = s3 * 3;
    let s5 = s4 + 4;
    {body}
}}
"""
)


def test_cascade_join_that_re_splits_at_project_width(tmp_path):
    """The join survives width 88 but dies under the project's own 60 - the
    gate helper rejects it (never silently counted)."""
    root = tmp_path
    (root / "rustfmt.toml").write_text("max_width = 60\n")
    orig = _STATS_FN.format(body="s1 + s2 + s3 + s4 + s5")
    long_join = "    let long_combined_result_value = x + 1 + y + 2 + (x + 1 + y + 2) * 3 + 12345;\n"
    assert 60 < len(long_join.strip()) <= 88
    candidate = orig.replace(
        "    let s1 = x + 1;\n    let s2 = y + 2;\n",
        long_join,
    )
    from less_code.loc import measure, project_measure

    assert measure(candidate, "rust").code < measure(orig, "rust").code
    assert project_measure(candidate, root).code >= project_measure(orig, root).code
    assert "no canonical LOC reduction" in cascade_reject(orig, candidate, root)


def test_cascade_fmt_dirty_rejected(tmp_path):
    """The candidate reduces under BOTH metrics, but its raw bytes are not
    clean under the project config - the artifact would ship formatter churn."""
    root = tmp_path
    (root / "rustfmt.toml").write_text("max_width = 60\n")
    orig = _STATS_FN.format(body="s1 + s2 + s3 + s4 + s5")
    long_join = "    let long_combined_result_value = x + 1 + y + 2 + (x + 1 + y + 2) * 3 + 12345;\n"
    candidate = orig.replace(
        "    let s1 = x + 1;\n    let s2 = y + 2;\n    let s3 = s1 + s2;\n",
        long_join,
    )
    from less_code.loc import measure, project_measure

    assert measure(candidate, "rust").code < measure(orig, "rust").code
    assert project_measure(candidate, root).code < project_measure(orig, root).code
    assert cascade_reject(orig, candidate, root) == "project lint/format failed"


def test_gate_compile_failure_reverts(tmp_path):
    root = _rust_root(tmp_path)
    pristine = {root / "src" / "lib.rs": ORIG}
    gate = default_gate(root, pristine, test_timeout=300)
    broken = ORIG.replace("    a + b\n", "    a + (x as String)\n")
    _, _, accepted, reason = gate(root / "src" / "lib.rs", broken)
    assert not accepted and reason.startswith("compile failed")
    assert (root / "src" / "lib.rs").read_text() == ORIG


def test_gate_test_failure_reverts(tmp_path):
    root = _rust_root(tmp_path)
    pristine = {root / "src" / "lib.rs": ORIG}
    gate = default_gate(root, pristine, test_timeout=300)
    wrong = ORIG.replace("    a + b\n", "    a + b + 1\n")
    _, _, accepted, reason = gate(root / "src" / "lib.rs", wrong)
    assert not accepted and reason.startswith("tests failed")
    assert (root / "src" / "lib.rs").read_text() == ORIG


def test_gate_oracle_mismatch_reverts(tmp_path):
    root = _rust_root(
        tmp_path,
        textwrap.dedent(
            """\
            pub fn add(x: u32, y: u32) -> u32 {
                x.saturating_add(y)
            }

            #[cfg(test)]
            mod tests {
                use super::add;

                #[test]
                fn small() {
                    assert_eq!(add(2, 3), 5);
                }
            }
            """
        ),
    )
    pristine = {root / "src" / "lib.rs": root.joinpath("src/lib.rs").read_text()}
    gate = default_gate(root, pristine, test_timeout=300)
    overflow = pristine[root / "src" / "lib.rs"].replace(
        "    x.saturating_add(y)\n", "    x + y\n"
    )
    _, _, accepted, reason = gate(root / "src" / "lib.rs", overflow)
    assert not accepted and reason.startswith("rust shadow mismatch")
    assert (root / "src" / "lib.rs").read_text() == pristine[root / "src" / "lib.rs"]


# ---- loop ----


class RecordingBackend:
    name = "recording"
    system = "S"

    def __init__(self, replies):
        self.replies = replies
        self.payloads = []

    def rewrite(self, path, masked_source, context, feedback):
        self.payloads.append(
            {
                "masked": masked_source,
                "feedback": feedback,
                "path": path.name,
                "context": context,
            }
        )
        return self.replies.pop(0)


def test_feedback_loop_second_attempt_sees_reason(tmp_path):
    root = _rust_root(tmp_path)
    masked, _ = mask_tests(ORIG)
    backend = RecordingBackend(
        [
            masked,
            masked.replace(
                "    let a = x;\n    let b = x;\n    a + b\n", "    x + x\n"
            ),
        ]
    )
    gate = default_gate(root, {root / "src" / "lib.rs": ORIG}, test_timeout=300)
    record = rewrite_file(root, root / "src" / "lib.rs", backend, gate, attempts=3)
    assert record["accepted"]
    assert backend.payloads[0]["feedback"] is None
    assert "no canonical LOC reduction" in backend.payloads[1]["feedback"]["reason"]
    assert record["loc_after"] < record["loc_before"]
    assert "x + x" in (root / "src" / "lib.rs").read_text()


def test_loop_stops_on_backend_error_and_abstain(tmp_path):
    root = _rust_root(tmp_path)

    class Boom:
        name = "boom"
        system = "S"

        def rewrite(self, *a, **k):
            raise RuntimeError("backend down")

    record = rewrite_file(root, root / "src" / "lib.rs", Boom(), None, attempts=3)
    assert not record["accepted"]
    assert record["category"] == "backend"
    assert record["attempts"] == 1

    class Abstain:
        name = "abstain"
        system = "S"

        def rewrite(self, *a, **k):
            return None

    record = rewrite_file(root, root / "src" / "lib.rs", Abstain(), None, attempts=3)
    assert record["category"] == "abstained"
    assert record["attempts"] == 1


def test_sentinel_violation_feeds_back(tmp_path):
    root = _rust_root(tmp_path)
    masked, _ = mask_tests(ORIG)
    backend = RecordingBackend([masked.replace("/*__LC_FROZEN_0__*/", "")])
    record = rewrite_file(root, root / "src" / "lib.rs", backend, None, attempts=1)
    assert record["category"] == "sentinel"
    assert "sentinel" in record["reason"]


# ---- E2E on a real crate ----


def test_e2e_rewrite_tree_accept_and_reject(tmp_path):
    root = _rust_root(tmp_path)
    good_command = _replay_command(
        tmp_path, "    let a = x;\n    let b = x;\n    a + b\n", "    x + x\n"
    )
    backend = FileBackend(good_command)
    stats, records = rewrite_tree(
        root,
        [root / "src" / "lib.rs"],
        backend,
        default_gate(root, {root / "src" / "lib.rs": ORIG}, test_timeout=300),
        attempts=3,
    )
    assert stats["accepted_files"] == 1
    assert records[0]["accepted"]
    assert "x + x" in (root / "src" / "lib.rs").read_text()
    assert "let a = x;" not in (root / "src" / "lib.rs").read_text()

    root2 = _rust_root(tmp_path / "second", ORIG)
    (tmp_path / "second").mkdir(exist_ok=True)
    root2 = tmp_path / "second" / "tiny-rs"
    backend2 = FileBackend(_replay_command(tmp_path, "MISSING", "NOTHING"))
    stats2, records2 = rewrite_tree(
        root2,
        [root2 / "src" / "lib.rs"],
        backend2,
        default_gate(root2, {root2 / "src" / "lib.rs": ORIG}, test_timeout=300),
        attempts=2,
    )
    assert stats2["accepted_files"] == 0
    assert not records2[0]["accepted"]
    assert records2[0]["proposals"][0]["reason"] == "no canonical LOC reduction"


def test_rewrite_project_standalone(tmp_path):
    from less_code.ml_file import rewrite_project

    root = _rust_root(tmp_path)
    backend = FileBackend(
        _replay_command(
            tmp_path, "    let a = x;\n    let b = x;\n    a + b\n", "    x + x\n"
        )
    )
    stats, records, tests_ok = rewrite_project(
        root, backend, attempts=3, test_timeout=300
    )
    assert tests_ok
    assert stats["accepted_files"] == 1
    assert stats["loc_after"] < stats["loc_before"]
    assert records[0]["accepted"]


def test_shrink_pipeline_ml_file_layer(tmp_path):
    """`lc shrink --ml-file-cli`: the file layer runs after the deterministic
    layers, is reported as its OWN stat, and passes the frozen suite."""
    from less_code.pipeline import shrink_project

    root = _rust_root(
        tmp_path,
        textwrap.dedent(
            """\
            /// Adds x to itself, after a bump; docs must survive.
            pub fn value(x: u64) -> u64 {
                let mut a = x;
                let mut b = x;
                a = a + 1;
                a + b
            }

            #[cfg(test)]
            mod tests {
                use super::value;

                #[test]
                fn bumps() {
                    assert_eq!(value(3), 7);
                }
            }
            """
        ),
    )
    backend = FileBackend(
        _replay_command(
            tmp_path,
            "    let mut a = x;\n    let mut b = x;\n    a = a + 1;\n    a + b\n",
            "    x + x + 1\n",
        )
    )
    stats = shrink_project(root, "rust", ml_file_backend=backend, ml_file_attempts=2)
    assert stats.ml_file_loc > 0
    assert stats.ml_file_stats.get("accepted_files") == 1
    layer = next(r for r in stats.layer_records if r["layer"] == "ml-file")
    assert layer["committed"]
    assert stats.loc_final == stats.loc_after_static - stats.ml_file_loc
    assert "x + x + 1" in (root / "src" / "lib.rs").read_text()
    assert "docs must survive" in (root / "src" / "lib.rs").read_text()
    assert stats.tests_ok


def test_item_loop_keeps_earlier_accept_when_later_item_fails(tmp_path):
    """Regression: the per-item loop's gate must restore the ACCEPTED state,
    not the driver-start pristine - a later failed candidate must never wipe
    an earlier accepted rewrite in the same file."""
    from bench.rust_ladder import rewrite_file_items

    root = _rust_root(
        tmp_path,
        textwrap.dedent(
            """\
            pub fn first(x: u64) -> u64 {
                let a = x;
                let b = x;
                a + b
            }

            pub fn second(x: u64) -> u64 {
                let c = x;
                let d = x;
                c + d
            }

            #[cfg(test)]
            mod tests {
                use super::{first, second};

                #[test]
                fn both() {
                    assert_eq!(first(3), 6);
                    assert_eq!(second(3), 6);
                }
            }
            """
        ),
    )
    lib = root / "src" / "lib.rs"

    replies = iter(["x + x", "0"])
    state = {"n": 0}

    class ScriptedItems:
        name = "scripted-items"
        system = "s"
        digest = "t"

        def rewrite_body(self, symbol_id, symbol_name, masked, context, feedback):
            state["n"] += 1
            return next(replies)

    shared = {lib: lib.read_text()}
    gate = default_gate(root, shared, test_timeout=300)
    record = rewrite_file_items(root, lib, ScriptedItems(), gate, attempts=1, max_items=2)
    text = lib.read_text()
    rewritten = text.count("    x + x\n}")
    assert rewritten == 1, "exactly the gate-passing item's rewrite lands"
    assert text.count("let a = x;") + text.count("let c = x;") == 1, (
        "the failed item must be fully reverted while the accepted one stays"
    )
    assert record["accepted"]
