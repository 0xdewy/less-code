"""Orchestrator for `lc shrink`.

Layered pipeline, each layer verify-gated independently:

  L0  canonical formatter (not counted as reduction)
  L1  Ruff and language-specific static passes
  L1b language rule libraries: semantic-preserving rewrites
  L1c ruff and the rule library again, over the rewritten tree

Each layer's edit set is applied, the frozen suite is run, and the edits
are kept iff the suite stays green AND code-LOC shrinks. A layer that
misfires is reverted (snapshot/restore), reported in `notes`, and never
takes another layer down with it.
"""

from __future__ import annotations

import ast
import contextlib
import difflib
import json
import os
import shutil
import signal
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from .api_check import api_surface, api_violations
from .comma import collapse_magic_commas
from .documentation import documentation_layout
from .langdetect import map_project
from .loc import canonical_format, measure, project_measure, rustfmt_config
from .rust_shadow import changed_functions as rust_changed_functions
from .rust_shadow import run_rust_shadow
from .shadow import changed_functions, run_shadow
from .static import static_pass
from .testrunners import compile_feedback, run_tests, shadowed_imports


@dataclass
class ShrinkStats:
    lang: str
    files_considered: int = 0
    loc_start: int = 0
    loc_after_static: int = 0
    loc_final: int = 0
    static_notes: list[str] = field(default_factory=list)
    layer_records: list[dict] = field(default_factory=list)
    tests_ok: bool = False
    api_ok: bool = False
    docs_ok: bool = False
    formatted_loc: bool = False
    reduction_pct: float = 0.0
    api_baseline: str = "original"
    static_removed_symbols: list[str] = field(default_factory=list)
    comma_collapse_loc: int = 0
    ml_stats: dict[str, int] = field(default_factory=dict)
    ml_records: list[dict] = field(default_factory=list)
    ml_file_loc: int = 0
    ml_file_stats: dict[str, int] = field(default_factory=dict)
    ml_file_records: list[dict] = field(default_factory=list)
    audit_records: list[dict] = field(default_factory=list)
    oracle: dict = field(default_factory=dict)
    gate_tests_run: int | None = None
    root: Path | None = None  # not serialized: lets report time re-measure disk

    def to_json(self) -> dict:
        return {
            "lang": self.lang,
            "files_considered": self.files_considered,
            "loc_start": self.loc_start,
            "loc_after_static": self.loc_after_static,
            "loc_final": self.loc_final,
            "static_pct": pct(self.loc_start, self.loc_after_static),
            "hybrid_pct": pct(self.loc_start, self.loc_final),
            "llm_extra_pct": pct(self.loc_after_static, self.loc_final),
            "formatted_loc": self.formatted_loc,
            "tests_ok": self.tests_ok,
            "api_ok": self.api_ok,
            "docs_ok": self.docs_ok,
            "api_baseline": self.api_baseline,
            "static_removed_symbols": self.static_removed_symbols,
            "comma_collapse_loc": self.comma_collapse_loc,
            "static_notes": self.static_notes,
            "layer_records": self.layer_records,
            "ml_stats": self.ml_stats,
            "ml_records": self.ml_records,
            "ml_file_loc": self.ml_file_loc,
            "ml_file_stats": self.ml_file_stats,
            "ml_file_records": self.ml_file_records,
            "audit_records": self.audit_records,
            "oracle": self.oracle,
            "gate_tests_run": self.gate_tests_run,
        }


def pct(before: int, after: int) -> float:
    return round(100.0 * (before - after) / before, 2) if before else 0.0


def _tree_loc(files: list[Path], lang: str) -> int:
    total = 0
    for f in files:
        total += measure(f.read_text(encoding="utf-8", errors="replace"), lang).code
    return total


def _project_ruff_config(root: Path) -> Path | None:
    """The project's ruff config in ruff's own discovery order: ruff.toml and
    .ruff.toml override pyproject.toml. Evaluating a proposal under any other
    width counts line joins the project's own formatter re-splits — the comma
    layer's numbers would inflate by exactly those joins."""
    for name in ("ruff.toml", ".ruff.toml"):
        config = root / name
        if config.is_file():
            return config
    config = root / "pyproject.toml"
    if config.is_file() and "[tool.ruff" in config.read_text(
        encoding="utf-8", errors="replace"
    ):
        return config
    return None


def _disk_loc(root: Path, lang: str) -> tuple[int, list[Path]]:
    """LOC of the tree as it exists on disk right now, freshly mapped —
    the file set is NOT frozen at shrink start, so files the build or test
    commands create mid-run are counted (they sit on the disk we ship)."""
    fresh = map_project(root, lang)
    return _tree_loc(fresh.source_files, lang), fresh.source_files


def _snapshot(files: list[Path]) -> dict[Path, str]:
    return {p: p.read_text(encoding="utf-8", errors="replace") for p in files}


def _restore(snapshot: dict[Path, str]) -> None:
    for path, text in snapshot.items():
        path.write_text(text, encoding="utf-8")


def _write_changes(changes: dict[str, str]) -> None:
    for path_str, new_text in changes.items():
        Path(path_str).write_text(new_text, encoding="utf-8")


def _install_signal_restore(snapshot: dict[Path, str]) -> None:
    def _handler(signum, frame):  # pragma: no cover
        _restore(snapshot)
        raise SystemExit(130)

    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(ValueError, OSError):
            signal.signal(sig, _handler)


def _apply_rules(
    sources: dict[Path, str], only: set[str] | None = None
) -> tuple[dict[Path, str], list[str]]:
    from .rules import apply_rules

    out, notes = dict(sources), []
    for path, text in sources.items():
        new_text, applied = apply_rules(text, only)
        if applied and new_text != text:
            out[path] = new_text
            counts = {name: applied.count(name) for name in set(applied)}
            notes.append(f"{path.name}: rules {counts}")
    return out, notes


def _apply_ruff_again(
    sources: dict[Path, str],
) -> tuple[dict[Path, str], list[str]]:
    """The rule layers expose shapes ruff fixes (superfluous else after a
    return, a now-useless trailing return, an assignment before return)."""
    from .static import _ruff_fix

    out, notes = dict(sources), []
    for path, text in sources.items():
        new_text = _ruff_fix(text, path.name, unsafe=True)
        if new_text != text:
            out[path] = new_text
            notes.append(f"{path.name}: ruff --fix after rules")
    return out, notes


def _apply_comma_collapse(
    sources: dict[Path, str],
) -> tuple[dict[Path, str], list[str]]:
    out, notes = dict(sources), []
    for path, text in sources.items():
        new_text, removed = collapse_magic_commas(text)
        if removed and new_text != text:
            try:
                ast.parse(new_text)
            except SyntaxError:
                continue
            out[path] = new_text
            notes.append(f"{path.name}: collapsed {removed} magic trailing comma(s)")
    return out, notes


def shrink_project(
    root: Path,
    lang: str | None = None,
    test_timeout: int = 600,
    ml_backend: object | None = None,
    test_command: list[str] | None = None,
    ml_attempts: int = 3,
    ml_symbols: int = 64,
    final_validator: Callable[[], bool] | None = None,
    ml_file_backend: object | None = None,
    ml_file_attempts: int = 3,
) -> ShrinkStats:
    if not 1 <= ml_attempts <= 3:
        raise ValueError("ml_attempts must be between 1 and 3")
    if not 1 <= ml_symbols <= 64:
        raise ValueError("ml_symbols must be between 1 and 64")
    project = map_project(root, lang)
    stats = ShrinkStats(
        lang=project.lang,
        files_considered=len(project.source_files),
    )
    stats.root = root
    stats.static_notes += project.notes
    # Measured before any early return so a failed baseline still reports LOC.
    stats.loc_start = _tree_loc(project.source_files, project.lang)
    if project.lang == "python":
        shadowed = shadowed_imports(root)
        if shadowed:
            stats.tests_ok = False
            stats.static_notes = [
                "import-origin check failed: "
                + "; ".join(shadowed)
                + " — the gate would test code from outside this tree. Fix the"
                " host venv and re-run."
            ]
            return stats

    baseline = run_tests(root, project.lang, timeout=test_timeout, command=test_command)
    stats.gate_tests_run = baseline.tests_run
    if baseline.tests_run == 0:
        stats.static_notes.append(
            "WARNING: the gate ran 0 tests; reductions are backed by the "
            "API and documentation checks only"
        )
    if not baseline.ok:
        stats.tests_ok = False
        stats.static_notes.append(
            f"baseline tests failed: {baseline.output_tail[-300:]}"
        )
        stats.loc_final, _ = _disk_loc(root, project.lang)
        return stats
    stats.tests_ok = True

    # The baseline run can materialize files (editable installs, codegen).
    # Start and end must share one file universe, so re-base the start
    # measurement here or the reduction counts phantom lines.
    fresh = map_project(root, project.lang)
    if set(fresh.source_files) != set(project.source_files):
        added = sorted(set(fresh.source_files) - set(project.source_files))
        removed = sorted(set(project.source_files) - set(fresh.source_files))
        stats.loc_start = _tree_loc(fresh.source_files, fresh.lang)
        stats.files_considered = len(fresh.source_files)
        stats.static_notes.append(
            "baseline re-based: the test command materialized "
            f"{len(added)} file(s) (+{_tree_loc(added, fresh.lang)} LOC)"
            + (f"; {len(removed)} file(s) vanished" if removed else "")
        )
        project = fresh

    # Formatting is a measurement normalization, not a source rewrite. Every
    # `_tree_loc` call canonicalizes in memory; writing a foreign project's
    # formatter output can make its own style gate fail before reduction.
    pristine = _snapshot(project.source_files)
    canonical_in = {
        path: canonical_format(text, project.lang)[0] == text
        for path, text in pristine.items()
    }
    docs_baseline = {
        path: documentation_layout(text, project.lang)
        for path, text in pristine.items()
    }
    api_original = api_surface(project.source_files, project.lang)
    _install_signal_restore(pristine)

    stats.formatted_loc = bool(project.source_files) and all(
        measure(text, project.lang).formatted for text in pristine.values()
    )
    if not stats.formatted_loc:
        stats.loc_after_static = stats.loc_start
        stats.loc_final = stats.loc_start
        stats.api_ok = True
        stats.docs_ok = True
        stats.static_notes.append(
            "canonical formatter unavailable or rejected a source file; "
            "refusing to measure a reduction"
        )
        stats.loc_final, _ = _disk_loc(root, project.lang)
        return stats

    if final_validator is not None:
        baseline_ok = final_validator()
        stats.audit_records.append({"stage": "baseline", "passed": baseline_ok})
        if not baseline_ok:
            stats.loc_after_static = stats.loc_final = stats.loc_start
            stats.api_ok = stats.docs_ok = True
            stats.static_notes.append("baseline audit failed; no rewrites attempted")
            stats.loc_final, _ = _disk_loc(root, project.lang)
            return stats

    runner = lambda root_, lang_: run_tests(
        root_, lang_, timeout=test_timeout, command=test_command
    )

    def docs_preserved(sources: dict[Path, str]) -> bool:
        current = {
            path: documentation_layout(text, project.lang)
            for path, text in sources.items()
        }
        return current == docs_baseline

    def project_style_ok(paths: list[Path]) -> bool:
        """Run configured format/lint checks on the files a layer changed."""
        if project.lang == "rust":
            config_path, edition = rustfmt_config(root)
            if config_path is None:
                return True
            proc = subprocess.run(
                [
                    "rustfmt",
                    "--check",
                    "--edition",
                    edition,
                    "--config-path",
                    config_path,
                    *[str(path) for path in paths],
                ],
                cwd=root,
                capture_output=True,
                text=True,
                timeout=120,
                check=False,
            )
            return proc.returncode == 0
        if project.lang != "python" or shutil.which("ruff") is None:
            return True
        config = _project_ruff_config(root)
        if config is None:
            return True
        text = config.read_text(encoding="utf-8", errors="replace")
        relative = [str(path.relative_to(root)) for path in paths]
        # A project may set fix=true. Verification must never mutate the tree.
        commands = [["ruff", "check", "--no-fix", *relative]]
        if "[tool.ruff.format]" in text or (
            config.name != "pyproject.toml" and "[format]" in text
        ):
            commands.append(["ruff", "format", "--check", *relative])
        return all(
            subprocess.run(
                command,
                cwd=root,
                capture_output=True,
                text=True,
                timeout=120,
                check=False,
            ).returncode
            == 0
            for command in commands
        )

    def _project_rustfmt_drops(changes: dict[str, str], pre: dict[Path, str]) -> bool:
        """True when every changed rust file also shrinks under the project's
        OWN rustfmt config. A join that survives width 88 but re-splits under
        the project's rustfmt.toml must be rejected, never counted. `pre` is
        the pre-write snapshot: the comparison is candidate vs original."""
        return all(
            project_measure(text, root).code
            < project_measure(pre[Path(path)], root).code
            for path, text in changes.items()
            if Path(path) in pre
        )

    def _gate_layer(
        name: str,
        proposed_sources: dict[Path, str],
        notes: list[str],
        api_baseline=None,
    ) -> tuple[int, list[str], bool, str]:
        """Apply `proposed_sources`, run the suite, revert on red OR no-shrink.

        Reverts ONLY this layer's changes — earlier successful layers stay.

        Gates run cheap-first, expensive-last so a misfiring proposal costs
        only the cheapest check that catches it:

          1. LOC delta     - in-memory measure(), O(changed bytes)
          2. project style - subprocess (ruff/prettier/rustfmt), ~100 ms
          3. docs          - in-memory tokenize/AST, O(changed bytes)
          4. API surface   - in-memory regex/AST, O(changed bytes)
          5. tests         - subprocess, seconds; only the survivors get here

        Reorder at your peril: putting tests before docs / API / style
        wastes the frozen-suite runtime on proposals the cheap gates would
        have rejected in microseconds.
        """
        # compute the actual edit set (what changed vs current on disk)
        current = _snapshot(project.source_files)
        changes = {
            str(p): t for p, t in proposed_sources.items() if t != current.get(p)
        }
        config = _project_ruff_config(root)
        if (
            changes
            and project.lang == "python"
            and shutil.which("ruff")
            and config is not None
        ):
            # Render proposals in the target project's style before measuring
            # and checking them. Canonical metric formatting remains in memory.
            for path, text in changes.items():
                formatted = subprocess.run(
                    [
                        "ruff",
                        "format",
                        "--stdin-filename",
                        str(Path(path).relative_to(root)),
                        "-",
                    ],
                    cwd=root,
                    input=text,
                    capture_output=True,
                    text=True,
                    timeout=120,
                    check=False,
                )
                if formatted.returncode == 0:
                    changes[path] = formatted.stdout
        elif changes:
            # Clean in, clean out: a file that already matched its canonical
            # formatter is written back canonical, so the artifact matches the
            # metric and no rewrite ships formatter-churn noise.
            for path, text in changes.items():
                if canonical_in.get(Path(path)):
                    new_text, ok = canonical_format(text, project.lang)
                    if ok:
                        changes[path] = new_text
        if not changes:
            return (
                _tree_loc(project.source_files, project.lang),
                list(notes),
                True,
                "unchanged",
            )
        if project.lang == "rust":
            # A syntactically broken candidate would make the canonical
            # formatter reject it and `measure()` fall back to raw LOC - the
            # formatter-fallback masking class. Parse every changed file
            # before any LOC is counted (the comma layer's lesson, rust side).
            from .ml_shrink import syntax_ok

            broken = [
                path for path, text_ in changes.items() if not syntax_ok(text_, "rust")
            ]
            if broken:
                return (
                    _tree_loc(project.source_files, project.lang),
                    list(notes),
                    False,
                    f"invalid syntax: {broken[0]} does not parse",
                )
        pre_loc = _tree_loc(project.source_files, project.lang)
        _write_changes(changes)
        loc_after = _tree_loc(project.source_files, project.lang)
        reason = ""
        if loc_after >= pre_loc:
            reason = "no canonical LOC reduction"
        elif (
            project.lang == "rust"
            and rustfmt_config(root)[0] is not None
            and not _project_rustfmt_drops(changes, current)
        ):
            reason = "no canonical LOC reduction under the project's rustfmt config"
        elif not project_style_ok([Path(path) for path in changes]):
            reason = "project lint/format failed"
        elif not docs_preserved(_snapshot(project.source_files)):
            reason = "documentation changed"
        elif api_baseline:
            violations = api_violations(
                api_baseline, api_surface(project.source_files, project.lang)
            )
            if violations:
                reason = f"API changed: {violations[0]}"
        if not reason:
            check = runner(root, project.lang)
            if not check.ok:
                reason = f"tests failed: {compile_feedback(check.output_tail)}"
        if not reason and project.lang == "python":
            # Differential oracle: every rewritten function's original body
            # runs alongside it for the whole suite. Cumulative against the
            # pristine tree, so each layer is verified on top of the last.
            spec = changed_functions(pristine, _snapshot(project.source_files))
            if spec:
                report = run_shadow(root, spec, test_command, test_timeout)
                if not report.tests_ok:
                    reason = f"shadow run failed: {report.output_tail[-200:]}"
                elif report.mismatches:
                    first = report.mismatches[0]
                    reason = (
                        f"shadow oracle mismatch in {first['file']}:{first['function']}"
                        f" ({first['examples'][0]['kind'] if first['examples'] else 'n/a'})"
                    )
        if not reason and project.lang == "rust":
            # Differential oracle, Rust flavor: appended __lc_shadow mod runs
            # each rewritten function's original body beside it. Refused
            # functions are unverified, never a mismatch.
            spec = rust_changed_functions(pristine, _snapshot(project.source_files))
            if spec:
                report = run_rust_shadow(root, spec, test_command, test_timeout)
                if report.mismatches:
                    first = report.mismatches[0]
                    reason = (
                        f"rust shadow mismatch in {first['file']}:{first['function']}"
                    )
        if os.environ.get("LC_TRACE"):
            print(
                f"[gate] {name}: {len(changes)} file(s) {pre_loc}->{loc_after}"
                f" {reason or 'accepted'}"[:300],
                file=sys.stderr,
                flush=True,
            )
        if reason:
            _restore(current)
            layer_notes = list(notes) + [f"{name}: reverted by the gate ({reason})"]
            return pre_loc, layer_notes, False, reason
        return loc_after, list(notes), True, "accepted"

    def _salvage_hunks(name: str, path: Path, proposed: str, api_baseline):
        """Try bounded subsets of independent line edits, never weakening gates.

        All offsets refer to this checkpoint; retained subsets are reconstructed
        together so earlier accepted hunks cannot be lost through stale offsets.
        The gate may format the composed proposal in the project's style.
        """
        original = path.read_text(encoding="utf-8").splitlines(keepends=True)
        from .ml_shrink import syntax_ok

        revised = proposed.splitlines(keepends=True)
        edits = [
            (start, end, revised[new_start:new_end])
            for tag, start, end, new_start, new_end in difflib.SequenceMatcher(
                a=original, b=revised, autojunk=False
            ).get_opcodes()
            if tag != "equal"
        ]
        if len(edits) < 2:
            return []
        retained = []
        calls = 0
        notes = []

        def visit(batch):
            nonlocal calls
            if not batch or calls >= 32:
                return
            trial = list(original)
            for start, end, replacement in sorted(retained + batch, reverse=True):
                trial[start:end] = replacement
            calls += 1
            candidate = "".join(trial)
            if syntax_ok(candidate, project.lang):
                _, _, kept, reason = _gate_layer(
                    name, {path: candidate}, [], api_baseline
                )
            else:
                kept, reason = False, "invalid syntax"
            if kept:
                retained.extend(batch)
                notes.append(f"{name}: kept {len(batch)} hunk(s) in {path.name}")
            elif len(batch) > 1:
                middle = len(batch) // 2
                visit(batch[:middle])
                visit(batch[middle:])
            else:
                notes.append(f"{name}: rejected hunk in {path.name} ({reason})")

        middle = len(edits) // 2
        visit(edits[:middle])
        visit(edits[middle:])
        return notes

    def _salvage_file_batches(
        name: str,
        batches: list[list[tuple[Path, str]]],
        api_baseline=None,
    ) -> tuple[list[str], int]:
        """Bisect independent file edits, retaining every green subset."""
        salvage_notes: list[str] = []
        accepted = 0

        def visit(batch: list[tuple[Path, str]]) -> None:
            nonlocal accepted
            if not batch:
                return
            _, _, kept, reason = _gate_layer(name, dict(batch), [], api_baseline)
            if kept:
                accepted += len(batch)
                salvage_notes.append(f"{name}: kept subset of {len(batch)} file(s)")
            elif len(batch) == 1:
                salvage_notes.append(f"{name}: rejected {batch[0][0].name} ({reason})")
                salvage_notes.extend(_salvage_hunks(name, *batch[0], api_baseline))
            else:
                middle = len(batch) // 2
                visit(batch[:middle])
                visit(batch[middle:])

        for batch in batches:
            visit(batch)
        return salvage_notes, accepted

    # ---- L1 external static ----
    pre_loc = _tree_loc(project.source_files, project.lang)
    static = static_pass(
        root,
        project.lang,
        project.source_files,
        project.source_files + project.test_files,
        runner=runner,
    )
    changes = {Path(path): text for path, text in static.changed_files.items()}
    if changes:
        loc_after_static, gate_notes, committed, _ = _gate_layer(
            "external-static", changes, [], api_original
        )
        static.notes += gate_notes

        if not committed:
            items = sorted(changes.items())
            middle = len(items) // 2
            salvage_notes, _ = _salvage_file_batches(
                "external-static subset",
                [items[:middle], items[middle:]],
                api_original,
            )
            static.notes += salvage_notes
            loc_after_static = _tree_loc(project.source_files, project.lang)
    else:
        loc_after_static = pre_loc
    stats.static_notes += static.notes
    stats.loc_after_static = loc_after_static
    stats.layer_records.append(
        {
            "layer": "external-static",
            "loc_before": pre_loc,
            "loc_after": loc_after_static,
            "committed": loc_after_static < pre_loc,
            "notes": list(static.notes),
        }
    )

    # Every layer holds to the API as it existed before reduction.
    api_pre_layers = api_original
    api_after_static = api_surface(project.source_files, project.lang)
    stats.static_removed_symbols = sorted(
        name
        for file, api in api_original.items()
        for name in set(api) - set(api_after_static.get(file, {}))
    )

    if project.lang == "python":
        rewritten = False  # did rules change anything ruff has not seen?
        for name, transform in (
            ("rules", _apply_rules),
            ("ruff-again", _apply_ruff_again),
            ("rules-again", _apply_rules),
            ("comma-collapse", _apply_comma_collapse),
        ):
            if name.endswith("-again") and not rewritten:
                continue  # ruff already saw this exact tree in the static layer
            pre_loc = _tree_loc(project.source_files, project.lang)
            proposed, notes = transform(_snapshot(project.source_files))
            loc_after, notes, committed, _ = _gate_layer(
                name, proposed, notes, api_pre_layers
            )
            if name in ("rules", "rules-again") and not committed:
                from .rules import RULES

                notes.append("combined rules failed; narrowing rule by rule")
                for rule in RULES:
                    trial, trial_notes = _apply_rules(
                        _snapshot(project.source_files), {rule}
                    )
                    loc_after, narrowed_notes, rule_committed, _ = _gate_layer(
                        f"rules:{rule}", trial, trial_notes, api_pre_layers
                    )
                    notes += narrowed_notes
                    if not rule_committed:
                        current = _snapshot(project.source_files)
                        edits = sorted(
                            (path, text)
                            for path, text in trial.items()
                            if text != current[path]
                        )
                        middle = len(edits) // 2
                        salvage_notes, _ = _salvage_file_batches(
                            f"rules:{rule} subset",
                            [edits[:middle], edits[middle:]],
                            api_pre_layers,
                        )
                        notes += salvage_notes
                        loc_after = _tree_loc(project.source_files, project.lang)
            stats.static_notes += notes
            rewritten = rewritten or loc_after < pre_loc
            stats.layer_records.append(
                {
                    "layer": name,
                    "loc_before": pre_loc,
                    "loc_after": loc_after,
                    "committed": loc_after < pre_loc,
                    "notes": list(notes),
                }
            )

        comma_record = next(
            (
                record
                for record in stats.layer_records
                if record["layer"] == "comma-collapse"
            ),
            None,
        )
        if comma_record and comma_record["committed"]:
            stats.comma_collapse_loc = (
                comma_record["loc_before"] - comma_record["loc_after"]
            )

    # `loc_after_static` now means "after every deterministic rewrite":
    # external-static + rules both reduce without external state.
    # The ML layer below is the only non-deterministic step, and `loc_final`
    # is set AFTER it - so when an ML backend is wired, `loc_after_static <
    # loc_final` measures its contribution, which is what the field has
    # always purported to do (its old value just stopped counting too early).
    stats.loc_after_static = _tree_loc(project.source_files, project.lang)

    # Regression validation is a terminal gate, never model retry feedback.
    # Preserve the last audited checkpoint, not merely the last green suite.
    if final_validator is not None:
        try:
            static_ok = final_validator()
        except Exception:
            _restore(pristine)
            raise
        stats.audit_records.append({"stage": "static", "passed": static_ok})
        if not static_ok:
            _restore(pristine)
            stats.loc_after_static = stats.loc_start
            stats.layer_records.append(
                {
                    "layer": "static-audit-rollback",
                    "loc_after": stats.loc_start,
                    "committed": False,
                    "audit_rollback": True,
                }
            )
            stats.static_notes.append(
                "static audit failed; restored original; skipped model"
            )
            ml_backend = None
    static_checkpoint = _snapshot(project.source_files)

    if ml_backend is not None:
        from .ml_shrink import search

        pre_loc = _tree_loc(project.source_files, project.lang)
        stats.ml_stats, stats.ml_records, notes = search(
            root,
            project.lang,
            project.source_files,
            project.test_files,
            ml_backend,
            lambda name, path, candidate: _gate_layer(
                f"ml:{path.name}:{name}", {path: candidate}, [], api_pre_layers
            ),
            pre_loc,
            attempts=ml_attempts,
            max_symbols=ml_symbols,
        )
        final_ml_loc = _tree_loc(project.source_files, project.lang)
        stats.static_notes += notes
        stats.layer_records.append(
            {
                "layer": "ml",
                "loc_before": pre_loc,
                "loc_after": final_ml_loc,
                "committed": final_ml_loc < pre_loc,
                "notes": list(notes),
            }
        )

        if final_validator is not None:
            try:
                model_ok = final_validator()
            except Exception:
                _restore(static_checkpoint)
                raise
            stats.audit_records.append({"stage": "model", "passed": model_ok})
            if not model_ok:
                _restore(static_checkpoint)
                stats.static_notes.append(
                    "model audit failed; restored audited static checkpoint"
                )
                stats.ml_stats["rolled_back"] = stats.ml_stats.get("accepted", 0)
                stats.ml_stats["retained_loc"] = 0
                for record in stats.ml_records:
                    if record["accepted"]:
                        record["rolled_back"] = True
                stats.layer_records[-1].update(
                    committed=False,
                    loc_after=stats.loc_after_static,
                    audit_rollback=True,
                )

    if ml_file_backend is not None and project.lang == "rust":
        from .ml_file import default_gate, rewrite_tree

        pre_loc = _tree_loc(project.source_files, project.lang)
        file_checkpoint = _snapshot(project.source_files)
        gate = default_gate(root, file_checkpoint, test_timeout, test_command)
        stats.ml_file_stats, stats.ml_file_records = rewrite_tree(
            root,
            project.source_files,
            ml_file_backend,
            gate,
            attempts=ml_file_attempts,
        )
        final_file_loc = _tree_loc(project.source_files, project.lang)
        stats.ml_file_loc = pre_loc - final_file_loc
        stats.static_notes.append(
            f"ml-file: {stats.ml_file_stats.get('accepted_files', 0)} file(s),"
            f" -{stats.ml_file_loc} LOC"
        )
        stats.layer_records.append(
            {
                "layer": "ml-file",
                "loc_before": pre_loc,
                "loc_after": final_file_loc,
                "committed": final_file_loc < pre_loc,
                "notes": [
                    f"{r['path']}: {r['category']} ({r['reason'][:120]})"
                    if not r["accepted"]
                    else f"{r['path']}: accepted -{r['loc_before'] - r['loc_after']} LOC"
                    for r in stats.ml_file_records
                ],
            }
        )
        if final_validator is not None:
            try:
                file_ok = final_validator()
            except Exception:
                _restore(file_checkpoint)
                raise
            stats.audit_records.append({"stage": "ml-file", "passed": file_ok})
            if not file_ok:
                _restore(file_checkpoint)
                stats.static_notes.append(
                    "ml-file audit failed; restored pre-file-layer checkpoint"
                )
                stats.ml_file_stats["rolled_back"] = stats.ml_file_stats.get(
                    "accepted_files", 0
                )
                stats.ml_file_loc = 0
                stats.layer_records[-1].update(
                    committed=False,
                    loc_after=pre_loc,
                    audit_rollback=True,
                )

    stats.loc_final = _tree_loc(project.source_files, project.lang)
    if project.lang == "python":
        final_spec = changed_functions(pristine, _snapshot(project.source_files))
        stats.oracle = run_shadow(
            root, final_spec, test_command, test_timeout
        ).to_json()
    elif project.lang == "rust":
        final_spec = rust_changed_functions(pristine, _snapshot(project.source_files))
        stats.oracle = run_rust_shadow(
            root, final_spec, test_command, test_timeout
        ).to_json()

    # ---- API check ----
    api_after = api_surface(project.source_files, project.lang)
    violations = api_violations(api_pre_layers, api_after)
    stats.api_ok = not violations
    if violations:
        stats.static_notes.append(f"API violations: {violations[:3]}")

    final_check = run_tests(
        root, project.lang, timeout=test_timeout, command=test_command
    )
    stats.tests_ok = final_check.ok
    stats.docs_ok = docs_preserved(_snapshot(project.source_files))
    stats.loc_final, final_files = _disk_loc(root, project.lang)
    if set(final_files) != set(project.source_files):
        grew = sorted(set(final_files) - set(project.source_files))
        add_loc = _tree_loc(grew, project.lang) if grew else 0
        raise ValueError(
            "file universe drifted during reduction: the baseline and final"
            f" measurements must cover the same files, but {len(grew)}"
            f" file(s) appeared (+{add_loc} LOC)"
            " - materialize generated files before measuring"
        )
    if stats.ml_stats:
        stats.ml_stats["retained_loc"] = stats.loc_after_static - stats.loc_final
        stats.ml_stats["retained"] = sum(
            r["accepted"] and not r.get("rolled_back", False) for r in stats.ml_records
        )
    return stats


def write_report(stats: ShrinkStats, out: Path) -> Path:
    if stats.root is not None:
        disk, _ = _disk_loc(stats.root, stats.lang)
        if disk != stats.loc_final:
            raise ValueError(
                f"report-time disk truth ({disk} code LOC) does not match the"
                f" reported loc_final ({stats.loc_final}); the tree changed"
                " after measurement - re-run lc shrink"
            )
    payload = {"reduce": stats.to_json()}
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return out
