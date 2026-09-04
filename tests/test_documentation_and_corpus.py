from pathlib import Path

from less_code.corpus import load_corpus
from less_code.documentation import documentation
from less_code.langdetect import is_test_file
from less_code.pipeline import shrink_project


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
    assert len(projects) == 6
    assert {
        lang: sum(p.lang == lang for p in projects)
        for lang in {p.lang for p in projects}
    } == {
        "python": 2,
        "javascript": 2,
        "rust": 2,
    }
    assert all(len(project.commit) == 40 for project in projects)


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
                str(module): "def live():\n    # inserted\n    result = 1\n    return result\n"
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
    original = (
        "def live():\n"
        '    """Return the answer."""\n'
        "    return 1\n"
    )
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
