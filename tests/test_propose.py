"""Deletion proposals: every never-propose rail gets its own test, plus
grouping closure, module promotion, mode gating, and the consented
apply/verify/revert loop."""

from __future__ import annotations

import ast
import json
import textwrap
from pathlib import Path

from less_code.propose import (
    Group,
    _api_clean,
    apply_group,
    apply_proposals,
    mine,
    propose_tree,
    verify_tree,
)


def _repo(root: Path, files: dict[str, str]) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    for name, text in files.items():
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(textwrap.dedent(text))
    return root


def _names(groups) -> set[str]:
    return {cand.name for group in groups for cand in group.candidates}


def test_rail_1_dynamic_discovery_blocks_the_trigger_subtree(tmp_path):
    root = _repo(
        tmp_path / "dyn",
        {
            "pkg/_main.py": (
                "import pkgutil\n\n\ndef _load():\n"
                "    return list(pkgutil.walk_packages())\n"
            ),
            "pkg/_plugins.py": "def _dead_plugin():\n    return 1\n",
            # outside the blocked subtree: still proposed
            "plain.py": (
                "def _dead_plain():\n    return 2\n\n\n"
                "def plain_live():\n    return 3\n"
            ),
            "test_plain.py": (
                "from plain import plain_live\n\n\ndef test_x():\n"
                "    assert plain_live()\n"
            ),
        },
    )
    groups = mine(root, "python", app=True)[1]
    assert _names(groups) == {"_dead_plain"}


def test_rail_1_pyupgrade_pattern_yields_zero_candidates(tmp_path):
    root = _repo(
        tmp_path / "pyupgrade-trap",
        {
            "_main.py": (
                "import pkgutil\n\n\ndef _find(plugin_name):\n"
                "    return [\n"
                "        name\n"
                "        for _, name, _ in pkgutil.iter_modules(__path__)\n"
                "        if name == plugin_name\n"
                "    ]\n"
            ),
            "_plugins/__init__.py": "def _dead_registry_entry():\n    return 1\n",
            "_data.py": "def _unreferenced_helper():\n    return 2\n",
            "test_main.py": "def test_nothing():\n    assert True\n",
        },
    )
    assert mine(root, "python")[1] == []
    assert mine(root, "python", app=True)[1] == []


def test_rail_2_string_dispatch_drops_methods_and_prefix_collisions(tmp_path):
    root = _repo(
        tmp_path / "dispatch",
        {
            "handlers.py": (
                "class Handlers:\n    def _sync_state(self):\n        return 1\n"
            ),
            "dispatch.py": (
                "def _handle_widgets(obj):\n"
                "    return getattr(obj, '_handler_registry', None)\n"
            ),
            "funcs.py": (
                "def _unrelated_helper():\n    return 2\n\n\n"
                "class _Other:\n    def _helper_method(self):\n        return 3\n"
            ),
            "test_x.py": "def test_x():\n    assert True\n",
        },
    )
    assert _names(mine(root, "python")[1]) == {"_Other", "_unrelated_helper"}


def test_rail_2_globals_alone_blocks_methods(tmp_path):
    root = _repo(
        tmp_path / "globals-dispatch",
        {
            "mod.py": (
                "def _pick(name):\n    return globals()[name]\n\n\n"
                "class C:\n    def _dead_method(self):\n        return 1\n\n\n"
                "def _standalone():\n    return 2\n"
            ),
            "test_x.py": "def test_x():\n    assert True\n",
        },
    )
    groups = mine(root, "python", app=True)[1]
    names = _names(groups)
    assert "_dead_method" not in names
    assert "_standalone" in names


def test_rail_3_decorated_definitions_are_never_proposed(tmp_path):
    root = _repo(
        tmp_path / "decorated",
        {
            "mod.py": (
                "def register(fn):\n    return fn\n\n\n"
                "@register\ndef _decorated():\n    return 1\n\n\n"
                "def _plain():\n    return 2\n"
            ),
            "test_x.py": "def test_x():\n    assert True\n",
        },
    )
    assert _names(mine(root, "python")[1]) == {"_plain"}


def test_rail_4_dunder_surface_is_never_proposed(tmp_path):
    root = _repo(
        tmp_path / "dunder",
        {
            "mod.py": (
                '__all__ = ["live"]\n\n\n'
                "class C:\n"
                "    def __deepcopy__(self, memo):\n        return None\n\n"
                "    def _dead_method(self):\n        return 1\n"
            ),
            "test_x.py": "from mod import live\n\n\ndef test_x():\n    assert True\n",
        },
    )
    assert _names(mine(root, "python", app=True)[1]) == {"C", "C._dead_method"}


def test_rail_5_entry_point_files_and_dirs_are_never_candidates(tmp_path):
    root = _repo(
        tmp_path / "entrypoints",
        {
            "mod.py": "def _used_by_noxfile():\n    return 1\n",
            "noxfile.py": "def session():\n    return _used_by_noxfile()\n",
            "scripts/tool.py": "def _dead_script_fn():\n    return 2\n",
            "test_x.py": "def test_x():\n    assert True\n",
        },
    )
    assert mine(root, "python", app=True)[1] == []


def test_rail_5_console_scripts_module_is_never_a_candidate(tmp_path):
    root = _repo(
        tmp_path / "console",
        {
            "pyproject.toml": '[project.scripts]\nmytool = "toolmod:main"\n',
            "toolmod.py": (
                "def main():\n    return 0\n\n\ndef _dead_tool_helper():\n    return 1\n"
            ),
            "test_x.py": "def test_x():\n    assert True\n",
        },
    )
    assert mine(root, "python", app=True)[1] == []


def test_rail_6_module_getattr_disqualifies_the_whole_module(tmp_path):
    root = _repo(
        tmp_path / "lazy",
        {
            "lazy.py": (
                "def __getattr__(name):\n    raise AttributeError(name)\n\n\n"
                "def _dead_lazy():\n    return 1\n"
            ),
            "open_mod.py": "def _dead_open():\n    return 2\n",
            "test_x.py": "def test_x():\n    assert True\n",
        },
    )
    assert _names(mine(root, "python")[1]) == {"open_mod.py"}


def test_rail_7_const_candidates_must_be_literal_assignments(tmp_path):
    root = _repo(
        tmp_path / "consts",
        {
            "mod.py": (
                "import os\n\n_CALL = os.getcwd()\n\n_LIT = 5\n\n_TUP = ('a', 1)\n"
            ),
            "test_x.py": "def test_x():\n    assert True\n",
        },
    )
    assert _names(mine(root, "python")[1]) == {"_LIT", "_TUP"}


def test_rail_8_test_referenced_symbols_are_live(tmp_path):
    root = _repo(
        tmp_path / "testref",
        {
            "mod.py": "def _used_in_tests():\n    return 1\n",
            "test_mod.py": (
                "from mod import _used_in_tests\n\n\ndef test_it():\n"
                "    assert _used_in_tests() == 1\n"
            ),
        },
    )
    assert mine(root, "python")[1] == []


def test_rail_9_library_mode_ignores_public_symbols(tmp_path):
    root = _repo(
        tmp_path / "lib",
        {
            "mod.py": "def public_api():\n    return 1\n",
            "test_x.py": "def test_x():\n    assert True\n",
        },
    )
    assert mine(root, "python")[1] == []


def test_rail_9_app_mode_requires_no_library_packaging(tmp_path):
    files = {"mod.py": "def public_api():\n    return 1\n"}
    bare = mine(_repo(tmp_path / "app", files), "python", app=True)[1]
    assert [c.kind for c in bare[0].candidates] == ["module"]
    marked = _repo(
        tmp_path / "marked",
        {**files, "pyproject.toml": '[project]\nname = "lib"\nversion = "1.0"\n'},
    )
    assert mine(marked, "python", app=True)[1] == []


def test_grouping_closes_transitively_and_breaks_on_live_edges(tmp_path):
    closed = _repo(
        tmp_path / "closed",
        {
            "mod.py": (
                "def _a():\n    return 1\n\n\n"
                "def _b():\n    return _a() + 1\n\n\n"
                "def live():\n    return 3\n"
            ),
            "test_mod.py": "from mod import live\n\n\ndef test_it():\n    assert live()\n",
        },
    )
    groups = mine(closed, "python")[1]
    assert len(groups) == 1
    assert _names(groups) == {"_a", "_b"}
    anchored = _repo(
        tmp_path / "anchored",
        {
            "mod.py": (
                "def _a():\n    return 1\n\n\n"
                "def _b():\n    return _a() + 1\n\n\n"
                "def main():\n    return _b()\n"
            ),
            "test_mod.py": "from mod import main\n\n\ndef test_it():\n    assert main() == 2\n",
        },
    )
    assert mine(anchored, "python")[1] == []


def test_module_promotion_and_import_mention_block(tmp_path):
    root = _repo(
        tmp_path / "promo",
        {
            "solo.py": "def _only_one():\n    return 1\n",
            "other.py": "def _dead_other():\n    return 2\n",
            "test_x.py": "import other\n\n\ndef test_x():\n    assert True\n",
        },
    )
    groups = mine(root, "python")[1]
    promoted = {cand.name: cand.kind for group in groups for cand in group.candidates}
    assert promoted == {"solo.py": "module", "_dead_other": "function"}


def test_apply_group_deletes_spans_with_attached_header_comments(tmp_path):
    root = _repo(
        tmp_path / "apply",
        {
            "mod.py": (
                "def live():\n    return 0\n\n"
                "# Does the thing.\n# More header.\n"
                "def _dead():\n    return 1\n\n\ndef live2():\n    return 2\n"
            ),
            "test_mod.py": "from mod import live, live2\n\n\ndef test_it():\n    assert live() + live2() == 2\n",
        },
    )
    project, groups = mine(root, "python")
    group = groups[0]
    assert _names([group]) == {"_dead"}
    touched, _ = apply_group(root, project, group)
    text = (root / "mod.py").read_text()
    assert "_dead" not in text
    assert "live2" in text
    assert touched == [root / "mod.py"]


def test_verify_tree_passes_on_pure_deletion_and_flags_docs_loss(tmp_path):
    root = _repo(
        tmp_path / "verify",
        {
            "mod.py": (
                "def _dead():\n    '''Documented dead code loses its docs.'''\n"
                "    return 1\n"
            ),
            "test_x.py": "def test_x():\n    assert True\n",
        },
    )
    project, groups = mine(root, "python")
    from less_code.pipeline import _snapshot

    before = _snapshot(project.source_files)
    apply_group(root, project, groups[0])
    report = verify_tree(root, project, before, groups[0], None, 600)
    assert report.docs_ok is False
    assert not report.ok


def test_api_clean_requires_every_violation_to_name_the_group():
    from less_code.propose import Candidate

    group = Group(
        id="1",
        candidates=[
            Candidate(
                kind="function",
                name="foo",
                file="mod.py",
                loc=2,
                start_line=1,
                end_line=2,
            )
        ],
    )
    assert _api_clean(["mod.py: missing=['foo']"], group)
    assert not _api_clean(["mod.py: missing=['bar']"], group)


def test_propose_then_apply_round_trip_and_stale_hash(tmp_path):
    root = _repo(
        tmp_path / "roundtrip",
        {
            "mod.py": "def _dead():\n    return 1\n\n\ndef live():\n    return 2\n",
            "test_mod.py": "from mod import live\n\n\ndef test_it():\n    assert live() == 2\n",
        },
    )
    out = root / "proposals.json"
    payload, code = propose_tree(root, coverage=False, out=out)
    assert code == 2
    assert payload["consent"]["tree_hash"]
    assert payload["groups"][0]["candidates"][0]["name"] == "_dead"
    assert out.is_file()
    first_run_count = len(payload["runs"])

    data, code = apply_proposals(root, ["1"], None, 600, out)
    assert code == 0
    assert data["groups"][0]["applied"] is True
    assert data["groups"][0]["gate"]["ok"] is True
    assert "_dead" not in (root / "mod.py").read_text()
    assert len(data["runs"]) == first_run_count + 1

    # a second proposal pass on the reduced tree finds nothing: exit 0
    payload2, code2 = propose_tree(root, coverage=False, out=out)
    assert code2 == 0 and payload2["groups"] == []

    # stale hash: proposals made before a tree change are rejected with 3
    root2 = _repo(tmp_path / "stale", dict(_repo_files()))
    out2 = root2 / "proposals.json"
    propose_tree(root2, coverage=False, out=out2)
    (root2 / "mod.py").write_text("def touched():\n    return 9\n")
    _, code3 = apply_proposals(root2, ["1"], None, 600, out2)
    assert code3 == 3
    assert "def touched" in (root2 / "mod.py").read_text()  # apply did nothing


def _repo_files():
    return {
        "mod.py": "def _dead():\n    return 1\n\n\ndef live():\n    return 2\n",
        "test_mod.py": "from mod import live\n\n\ndef test_it():\n    assert live() == 2\n",
    }


def test_proposals_file_is_an_append_only_audit_trail(tmp_path):
    root = _repo(tmp_path / "audit", _repo_files())
    out = root / "proposals.json"
    propose_tree(root, coverage=False, out=out)
    original = json.loads(out.read_text())
    apply_proposals(root, ["1"], None, 600, out)
    after = json.loads(out.read_text())
    assert after["consent"] == original["consent"]
    assert len(after["runs"]) == 1
    assert after["runs"][0]["results"][0]["applied"] is True


def test_coverage_overlay_degrades_to_none_without_coverage(tmp_path):
    root = _repo(tmp_path / "cov", _repo_files())
    payload, code = propose_tree(root, coverage=True, out=root / "proposals.json")
    assert code == 2
    assert payload["groups"][0]["evidence"]["covered_by_tests"] is None
    assert all(
        cand["covered_by_tests"] is None for cand in payload["groups"][0]["candidates"]
    )


def test_adjacent_deletions_remap_offsets_between_sequential_groups(tmp_path):
    """Boltons-demo regression: consecutive adjacent deletions applied
    top-down in consent order. Every group's recorded span is remapped
    through the lines earlier groups removed; every group applies."""
    body = "".join(
        f"def _dead_{number}():\n    return {number}\n\n\n" for number in range(1, 5)
    )
    root = _repo(
        tmp_path / "adjacent",
        {
            "mod.py": body + "def live():\n    return 0\n",
            "test_mod.py": "from mod import live\n\n\ndef test_it():\n    assert live() == 0\n",
        },
    )
    out = root / "proposals.json"
    payload, code = propose_tree(root, coverage=False, out=out)
    assert code == 2
    assert len(payload["groups"]) == 4
    data, code = apply_proposals(root, ["1", "2", "3", "4"], None, 600, out)
    assert code == 0
    assert all(group["applied"] for group in data["groups"])
    text = (root / "mod.py").read_text()
    assert "_dead_" not in text
    assert "def live" in text
    ast.parse(text)


def test_group_spanning_a_deleted_region_still_applies(tmp_path):
    """A group whose members sit on both sides of another group's deletion:
    the intervening removal shifts the later member's recorded lines, and
    the remap - not a spurious stale-span revert - must absorb it."""
    root = _repo(
        tmp_path / "spanning",
        {
            "mod.py": (
                "def _top():\n    return _bottom() + 1\n\n\n"
                "def _middle():\n    return 5\n\n\n"
                "def _bottom():\n    return 2\n\n\n"
                "def live():\n    return 0\n"
            ),
            "test_mod.py": "from mod import live\n\n\ndef test_it():\n    assert live() == 0\n",
        },
    )
    out = root / "proposals.json"
    payload, code = propose_tree(root, coverage=False, out=out)
    assert code == 2
    names = {
        group["id"]: {cand["name"] for cand in group["candidates"]}
        for group in payload["groups"]
    }
    assert names["1"] == {"_top", "_bottom"}
    assert names["2"] == {"_middle"}
    data, code = apply_proposals(root, ["1", "2"], None, 600, out)
    assert code == 0
    assert all(group["applied"] for group in data["groups"])
    text = (root / "mod.py").read_text()
    for gone in ("_top", "_middle", "_bottom"):
        assert gone not in text
    assert "def live" in text
    ast.parse(text)
