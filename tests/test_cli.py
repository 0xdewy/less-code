"""`--copy-to` and `--test-command`: the escape hatches for trying `lc shrink`
against a real repository without touching it, and against one whose tests
need a specific interpreter or a scoped command (its own venv, a package
inside a monorepo)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from less_code.cli import main


def _python_project(root: Path) -> None:
    (root / "pkg").mkdir()
    (root / "pkg" / "__init__.py").write_text("")
    (root / "pkg" / "lib.py").write_text(
        "def add(a, b):\n    result = a + b\n    return result\n"
    )
    (root / "test_lib.py").write_text(
        "from pkg.lib import add\n\ndef test_add():\n    assert add(1, 2) == 3\n"
    )


def test_copy_to_shrinks_a_copy_and_leaves_the_original_untouched(tmp_path):
    src = tmp_path / "project"
    src.mkdir()
    _python_project(src)
    original = (src / "pkg" / "lib.py").read_text()
    dest = tmp_path / "copy"
    out = tmp_path / "report.json"

    rc = main(
        [
            "shrink",
            str(src),
            "--copy-to",
            str(dest),
            "--out",
            str(out),
        ]
    )

    assert rc == 0
    assert (src / "pkg" / "lib.py").read_text() == original  # untouched
    assert (dest / "pkg" / "lib.py").read_text() != original  # the copy shrank
    assert "return a + b" in (dest / "pkg" / "lib.py").read_text()
    report = json.loads(out.read_text())["reduce"]
    assert report["tests_ok"] and report["loc_final"] < report["loc_start"]


def test_copy_to_refuses_a_nonempty_destination(tmp_path):
    src = tmp_path / "project"
    src.mkdir()
    _python_project(src)
    dest = tmp_path / "copy"
    dest.mkdir()
    (dest / "existing.txt").write_text("do not clobber me")

    with pytest.raises(SystemExit, match="exists and is not empty"):
        main(["shrink", str(src), "--copy-to", str(dest)])

    assert (dest / "existing.txt").read_text() == "do not clobber me"


def test_test_command_is_shell_parsed_and_overrides_the_default(tmp_path):
    src = tmp_path / "project"
    src.mkdir()
    _python_project(src)
    dest = tmp_path / "copy"
    out = tmp_path / "report.json"
    # a custom interpreter invocation, exactly the shape a project's own
    # venv or a monorepo's scoped test runner needs
    command = f"{sys.executable} -m pytest -x -q --no-header"

    rc = main(
        [
            "shrink",
            str(src),
            "--copy-to",
            str(dest),
            "--test-command",
            command,
            "--out",
            str(out),
        ]
    )

    assert rc == 0
    report = json.loads(out.read_text())["reduce"]
    assert report["tests_ok"]


def test_test_command_failure_is_reported_not_swallowed(tmp_path):
    src = tmp_path / "project"
    src.mkdir()
    _python_project(src)
    dest = tmp_path / "copy"
    out = tmp_path / "report.json"

    rc = main(
        [
            "shrink",
            str(src),
            "--copy-to",
            str(dest),
            "--test-command",
            "false",  # always exits 1: simulates a broken custom command
            "--out",
            str(out),
        ]
    )

    assert rc != 0
    report = json.loads(out.read_text())["reduce"]
    assert not report["tests_ok"]


def _project_with_dead_code(root: Path) -> None:
    (root / "pkg").mkdir(parents=True)
    (root / "pkg" / "__init__.py").write_text("")
    (root / "pkg" / "lib.py").write_text(
        "def add(a, b):\n    result = a + b\n    return result\n\n\n"
        "def _dead_util():\n    return 42\n"
    )
    (root / "test_lib.py").write_text(
        "from pkg.lib import add\n\ndef test_add():\n    assert add(1, 2) == 3\n"
    )


def test_propose_awaits_consent_then_applies_on_a_sibling_copy(tmp_path):
    src = tmp_path / "proj"
    _project_with_dead_code(src)
    original = (src / "pkg" / "lib.py").read_text()

    rc = main(["propose", str(src)])
    assert rc == 2  # unapplied proposals awaiting consent
    sibling = tmp_path / "proj-proposals"
    assert sibling.is_dir()
    assert (src / "pkg" / "lib.py").read_text() == original  # sacred
    proposals = sibling / "proposals.json"
    assert proposals.is_file()
    import json

    data = json.loads(proposals.read_text())
    assert data["groups"][0]["candidates"][0]["name"] == "_dead_util"
    assert data["consent"]["tree_hash"]

    rc = main(["propose", str(src), "--apply", "1"])
    assert rc == 0
    assert "_dead_util" not in (sibling / "pkg" / "lib.py").read_text()
    assert (src / "pkg" / "lib.py").read_text() == original  # still sacred


def test_propose_with_no_candidates_is_exit_zero(tmp_path):
    src = tmp_path / "clean"
    src.mkdir()
    _python_project(src)

    rc = main(["propose", str(src)])

    assert rc == 0
    import json

    proposals = tmp_path / "clean-proposals" / "proposals.json"
    assert proposals.is_file()
    assert json.loads(proposals.read_text())["groups"] == []


def test_propose_apply_rejects_a_stale_tree_with_exit_three(tmp_path):
    src = tmp_path / "stale"
    _project_with_dead_code(src)
    assert main(["propose", str(src)]) == 2
    sibling = tmp_path / "stale-proposals"
    (sibling / "pkg" / "lib.py").write_text("def touched():\n    return 1\n")

    rc = main(["propose", str(src), "--apply", "1"])

    assert rc == 3
    assert "def touched" in (sibling / "pkg" / "lib.py").read_text()
