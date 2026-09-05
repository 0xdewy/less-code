import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from less_code.corpus import load_corpus
from less_code.documentation import documentation
from less_code.langdetect import is_test_file
from less_code.pipeline import shrink_project


@pytest.mark.parametrize(
    "failure", ["clone", "prepare", "baseline", "formatter", "none"]
)
def test_corpus_failure_accounting_and_model_forwarding(tmp_path, monkeypatch, failure):
    from less_code import corpus
    from less_code.loc import Loc
    from less_code.pipeline import ShrinkStats

    item = corpus.CorpusProject("demo", "unused", "a" * 40, "python", (), ("prepare",))
    monkeypatch.setattr(corpus, "load_corpus", lambda _: [item])

    def run(command, cwd, timeout):
        failed = (failure == "clone" and command[0] == "git") or (
            failure == "prepare" and command[0] == "prepare"
        )
        return subprocess.CompletedProcess(
            command, int(failed), "", "failed" if failed else ""
        )

    monkeypatch.setattr(corpus, "_run", run)
    monkeypatch.setattr(
        corpus, "map_project", lambda *_: SimpleNamespace(source_files=[])
    )
    monkeypatch.setattr(
        corpus,
        "count_tree",
        lambda *_args, **_kw: Loc(100, 0, 0, formatted=failure != "formatter"),
    )
    backend = object()

    def shrink(*args, **kwargs):
        assert kwargs["ml_backend"] is backend
        if failure in {"baseline", "formatter"}:
            return ShrinkStats("python")
        return ShrinkStats(
            "python",
            loc_start=100,
            loc_after_static=90,
            loc_final=80,
            tests_ok=True,
            api_ok=True,
            docs_ok=True,
            formatted_loc=True,
        )

    monkeypatch.setattr(corpus, "shrink_project", shrink)
    result = corpus.run_corpus(tmp_path / "unused", ml_backend=backend)
    aggregate = result["aggregate"]
    assert aggregate["valid"] == int(failure == "none")
    assert aggregate["unmeasured"] == int(failure in {"clone", "prepare", "formatter"})
    assert aggregate["loc_start"] == (
        0 if failure in {"clone", "prepare", "formatter"} else 100
    )
    assert aggregate["raw_pct"] == (20 if failure == "none" else 0)
    assert aggregate["static_pct"] == (10 if failure == "none" else 0)
    assert aggregate["audit_performed"] is False
    corpus.write_corpus_report(result, tmp_path / "report.json", tmp_path / "report.md")
    assert (
        f"({aggregate['raw_pct']}% reduction)" in (tmp_path / "report.md").read_text()
    )


@pytest.mark.parametrize(
    "error", [FileNotFoundError("missing"), subprocess.TimeoutExpired("git", 1)]
)
def test_corpus_setup_errors_become_failed_results(tmp_path, monkeypatch, error):
    from less_code import corpus

    def fail(*args, **kwargs):
        raise error

    monkeypatch.setattr(corpus.subprocess, "run", fail)
    result = corpus._run(("git",), tmp_path, 1)
    assert result.returncode != 0
    assert result.stderr


@pytest.mark.parametrize("audit_failure", ["baseline", "final", "none"])
def test_corpus_held_out_audit_scores_failures_zero(
    tmp_path, monkeypatch, audit_failure
):
    from less_code import corpus
    from less_code.loc import Loc
    from less_code.pipeline import ShrinkStats

    item = corpus.CorpusProject(
        "demo", "unused", "a" * 40, "python", (), audit=("audit",)
    )
    monkeypatch.setattr(corpus, "load_corpus", lambda _: [item])
    monkeypatch.setattr(
        corpus, "map_project", lambda *_: SimpleNamespace(source_files=[])
    )
    monkeypatch.setattr(
        corpus, "count_tree", lambda *_a, **_k: Loc(100, 0, 0, formatted=True)
    )
    audits = []
    searches = []

    def run(command, cwd, timeout):
        if command == ("audit",):
            audits.append(command)
            failed = (len(audits) == 1 and audit_failure == "baseline") or (
                len(audits) == 2 and audit_failure == "final"
            )
            return subprocess.CompletedProcess(command, int(failed), "AUDIT_ONLY", "")
        return subprocess.CompletedProcess(command, 0, "", "")

    def shrink(*args, **kwargs):
        assert "AUDIT_ONLY" not in str(kwargs)
        searches.append(True)
        return ShrinkStats(
            "python",
            loc_start=100,
            loc_after_static=90,
            loc_final=80,
            tests_ok=True,
            api_ok=True,
            docs_ok=True,
            formatted_loc=True,
        )

    monkeypatch.setattr(corpus, "_run", run)
    monkeypatch.setattr(corpus, "shrink_project", shrink)
    report = corpus.run_corpus(tmp_path / "unused")
    assert bool(searches) == (audit_failure != "baseline")
    assert report["aggregate"]["raw_pct"] == (20 if audit_failure == "none" else 0)
    assert report["aggregate"]["valid"] == int(audit_failure == "none")
    row = report["projects"][0]
    assert row["audit_ok"] == (
        None if audit_failure == "baseline" else audit_failure == "none"
    )


def test_documentation_extracts_comments_and_docstrings():
    source = '# note\ndef f():\n    """Useful docs."""\n    return 1\n'
    docs = documentation(source, "python")
    assert docs[("comment", "# note")] == 1
    assert docs[("docstring", "Useful docs.")] == 1


def test_root_javascript_test_file_is_an_oracle():
    assert is_test_file(Path("test.js"))
    assert is_test_file(Path("spec.mjs"))


def test_corpus_is_pinned_and_language_balanced():
    projects = load_corpus(Path("bench/corpus.toml"))
    assert len(projects) == 12
    assert sum(p.cohort == "original" for p in projects) == 6
    assert sum(p.cohort == "expansion" for p in projects) == 6
    assert {
        lang: sum(p.lang == lang for p in projects)
        for lang in {p.lang for p in projects}
    } == {
        "python": 4,
        "javascript": 4,
        "rust": 4,
    }
    assert all(len(project.commit) == 40 for project in projects)


def test_comparison_does_not_count_changed_denominators_as_improvement():
    from bench.compare import compare

    row = {
        "name": "a",
        "cohort": "original",
        "commit": "a" * 40,
        "valid": True,
        "loc_start": 100,
        "loc_final": 90,
        "loc_after_static": 90,
        "raw_pct": 10,
        "static_pct": 10,
        "test_command": [],
        "audit_command": [],
    }
    before = {"projects": [row, dict(row, name="b")]}
    after = {
        "projects": [
            dict(row, loc_final=80, raw_pct=20),
            dict(row, name="b", loc_start=200),
        ]
    }
    report = compare(before, after)
    assert "Comparable projects: 1/2" in report
    assert "Additional accepted lines: 10" in report
    assert "NO: baseline/gate/measurement mismatch" in report
    after["projects"][0]["test_command"] = ["echo", "weaker gate"]
    assert "Comparable projects: 0/2" in compare(before, after)


def test_callback_probe_rejects_forwarding_a_previously_discarded_value():
    from bench.heldout import check_fastq

    before = "function benchFastQPromise(done) { qPromise.push(42).then(function () { done() }, done) }"
    after = "function benchFastQPromise(done) { qPromise.push(42).then(done, done) }"
    assert check_fastq(before).returncode == 0
    assert check_fastq(after).returncode != 0


def test_pipeline_reverts_documentation_loss(tmp_path):
    module = tmp_path / "library.py"
    module.write_text(
        "def live():\n"
        "    return 1\n\n\n"
        "def unused():\n"
        '    """Still documentation."""\n'
        "    return 2\n"
    )
    (tmp_path / "test_library.py").write_text(
        "from library import live\n\ndef test_live():\n    assert live() == 1\n"
    )
    stats = shrink_project(tmp_path, "python")
    assert stats.docs_ok
    assert '"""Still documentation."""' in module.read_text()


def test_pipeline_rejects_added_documentation(tmp_path, monkeypatch):
    """The docs gate enforces the multiset contract: a comment or docstring
    that did not exist in the source cannot be introduced by a layer."""
    module = tmp_path / "library.py"
    original = "def live():\n    result = 1\n    return result\n"
    module.write_text(original)
    (tmp_path / "test_library.py").write_text(
        "from library import live\n\ndef test_live():\n    assert live() == 1\n"
    )

    from less_code import pipeline
    from less_code.static import StaticResult

    monkeypatch.setattr(
        pipeline,
        "static_pass",
        lambda *_args, **_kwargs: StaticResult(
            changed_files={
                str(
                    module
                ): "def live():\n    # inserted\n    result = 1\n    return result\n"
            },
            loc_removed=1,
        ),
    )
    monkeypatch.setattr(pipeline, "_apply_rules", lambda sources: (sources, []))
    monkeypatch.setattr(pipeline, "_apply_outline", lambda sources: (sources, []))
    shrink_project(tmp_path, "python")
    assert module.read_text() == original


def test_pipeline_rejects_removed_documentation(tmp_path, monkeypatch):
    """The docs gate enforces the multiset contract: a pre-existing comment
    or docstring cannot be deleted by a layer."""
    module = tmp_path / "library.py"
    original = 'def live():\n    """Return the answer."""\n    return 1\n'
    module.write_text(original)
    (tmp_path / "test_library.py").write_text(
        "from library import live\n\ndef test_live():\n    assert live() == 1\n"
    )

    from less_code import pipeline
    from less_code.static import StaticResult

    monkeypatch.setattr(
        pipeline,
        "static_pass",
        lambda *_args, **_kwargs: StaticResult(
            changed_files={str(module): "def live():\n    return 1\n"},
            loc_removed=1,
        ),
    )
    monkeypatch.setattr(pipeline, "_apply_rules", lambda sources: (sources, []))
    monkeypatch.setattr(pipeline, "_apply_outline", lambda sources: (sources, []))
    shrink_project(tmp_path, "python")
    assert module.read_text() == original
