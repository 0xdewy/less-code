"""Orchestrator for `lc shrink`.

Layered pipeline, each layer verify-gated independently:

  L0  canonical formatter (not counted as reduction)
  L1  external static tools: ruff / snapshot-isolated clippy
  L1b language rule libraries: semantic-preserving rewrites
  L1c guard-block outlining: project-wide repeated guards

Each layer's edit set is applied, the frozen suite is run, and the edits
are kept iff the suite stays green AND code-LOC shrinks. A layer that
misfires is reverted (snapshot/restore), reported in `notes`, and never
takes another layer down with it.
"""

from __future__ import annotations

import contextlib
import json
import shutil
import signal
import subprocess
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from .api_check import api_surface, api_violations
from .documentation import documentation_layout
from .langdetect import map_project
from .loc import measure
from .static import static_pass
from .testrunners import run_tests, shadowed_imports


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
    ml_stats: dict[str, int] = field(default_factory=dict)

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
            "static_notes": self.static_notes,
            "layer_records": self.layer_records,
            "ml_stats": self.ml_stats,
        }


def pct(before: int, after: int) -> float:
    return round(100.0 * (before - after) / before, 2) if before else 0.0


def _tree_loc(files: list[Path], lang: str) -> int:
    total = 0
    for f in files:
        total += measure(f.read_text(encoding="utf-8", errors="replace"), lang).code
    return total


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


def _apply_outline(
    sources: dict[Path, str],
) -> tuple[dict[Path, str], list[str]]:
    from .outline import outline_guards

    changed, notes = outline_guards({str(path): text for path, text in sources.items()})
    return {path: changed.get(str(path), text) for path, text in sources.items()}, notes


def _ml_proposals(sources: dict[Path, str], lang: str, backend):
    """Collect immutable-snapshot proposals, ordered bottom-up per file."""
    from .ml_shrink import ProposedEdit, extract_symbols

    proposals = []
    notes = []
    considered = 0
    for path, text in sources.items():
        for symbol in extract_symbols(text, lang):
            considered += 1
            try:
                edit = backend.propose(lang, symbol)
            except Exception as exc:  # noqa: BLE001 - third-party backend boundary
                notes.append(f"{path.name}:{symbol['name']}: model error: {exc}")
                continue
            if isinstance(edit, ProposedEdit):
                proposals.append((path, symbol, edit))
    proposals.sort(key=lambda item: (str(item[0]), -item[1]["start_byte"]))
    return proposals, notes, considered


def shrink_project(
    root: Path,
    lang: str | None = None,
    test_timeout: int = 600,
    ml_backend: object | None = None,
    test_command: list[str] | None = None,
) -> ShrinkStats:
    project = map_project(root, lang)
    stats = ShrinkStats(
        lang=project.lang,
        files_considered=len(project.source_files),
    )
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
    if not baseline.ok:
        stats.tests_ok = False
        stats.static_notes = [f"baseline tests failed: {baseline.output_tail[-300:]}"]
        return stats
    stats.tests_ok = True

    # Formatting is a measurement normalization, not a source rewrite. Every
    # `_tree_loc` call canonicalizes in memory; writing a foreign project's
    # formatter output can make its own style gate fail before reduction.
    pristine = _snapshot(project.source_files)
    docs_baseline = {
        path: documentation_layout(text, project.lang)
        for path, text in pristine.items()
    }
    api_original = api_surface(project.source_files, project.lang)
    _install_signal_restore(pristine)

    stats.loc_start = _tree_loc(project.source_files, project.lang)
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
        if project.lang != "python" or shutil.which("ruff") is None:
            return True
        config = root / "pyproject.toml"
        if not config.is_file():
            return True
        text = config.read_text(encoding="utf-8", errors="replace")
        if "[tool.ruff" not in text:
            return True
        relative = [str(path.relative_to(root)) for path in paths]
        commands = [["ruff", "check", *relative]]
        if "[tool.ruff.format]" in text:
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

    def _gate_layer(
        name: str,
        proposed_sources: dict[Path, str],
        notes: list[str],
        api_baseline=None,
    ) -> tuple[int, list[str], bool, str]:
        """Apply `proposed_sources`, run the suite, revert on red OR no-shrink.

        Reverts ONLY this layer's changes — earlier successful layers stay.
        """
        # compute the actual edit set (what changed vs current on disk)
        current = _snapshot(project.source_files)
        changes = {
            str(p): t for p, t in proposed_sources.items() if t != current.get(p)
        }
        if not changes:
            return (
                _tree_loc(project.source_files, project.lang),
                list(notes),
                True,
                "unchanged",
            )
        pre_loc = _tree_loc(project.source_files, project.lang)
        _write_changes(changes)
        loc_after = _tree_loc(project.source_files, project.lang)
        reason = ""
        if loc_after >= pre_loc:
            reason = "no canonical LOC reduction"
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
                reason = f"tests failed: {check.output_tail[-200:]}"
        if reason:
            _restore(current)
            layer_notes = list(notes) + [f"{name}: reverted by the gate ({reason})"]
            return pre_loc, layer_notes, False, reason
        return loc_after, list(notes), True, "accepted"

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
        for name, transform in (("rules", _apply_rules), ("outline", _apply_outline)):
            pre_loc = _tree_loc(project.source_files, project.lang)
            proposed, notes = transform(_snapshot(project.source_files))
            loc_after, notes, committed, _ = _gate_layer(
                name, proposed, notes, api_pre_layers
            )
            if name == "rules" and not committed:
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
            elif name == "outline" and not committed:
                notes.append("combined outline failed; narrowing file by file")
                for path, text in _snapshot(project.source_files).items():
                    trial, trial_notes = _apply_outline({path: text})
                    loc_after, narrowed_notes, _, _ = _gate_layer(
                        f"outline:{path.name}", trial, trial_notes, api_pre_layers
                    )
                    notes += narrowed_notes
            stats.static_notes += notes
            stats.layer_records.append(
                {
                    "layer": name,
                    "loc_before": pre_loc,
                    "loc_after": loc_after,
                    "committed": loc_after < pre_loc,
                    "notes": list(notes),
                }
            )

    if ml_backend is not None:
        pre_loc = _tree_loc(project.source_files, project.lang)
        proposals, notes, considered = _ml_proposals(
            _snapshot(project.source_files), project.lang, ml_backend
        )
        counters = Counter(
            symbols_considered=considered,
            proposed=len(proposals),
            accepted=0,
            accepted_loc=0,
        )
        from .ml_shrink import apply_edit, syntax_ok

        for path, symbol, edit in proposals:
            current = path.read_text(encoding="utf-8", errors="replace")
            try:
                candidate = apply_edit(current, symbol, edit)
            except ValueError as exc:
                counters["rejected_schema"] += 1
                notes.append(f"{path.name}:{symbol['name']}: {exc}")
                continue
            if not syntax_ok(candidate, project.lang):
                counters["rejected_syntax"] += 1
                notes.append(f"{path.name}:{symbol['name']}: invalid syntax")
                continue
            before = _tree_loc(project.source_files, project.lang)
            loc_after, candidate_notes, committed, reason = _gate_layer(
                f"ml:{path.name}:{symbol['name']}",
                {path: candidate},
                [],
                api_pre_layers,
            )
            notes += candidate_notes
            if committed:
                counters["accepted"] += 1
                counters["accepted_loc"] += before - loc_after
            else:
                category = next(
                    key
                    for prefix, key in (
                        ("no canonical", "loc"),
                        ("project lint", "style"),
                        ("documentation", "docs"),
                        ("API", "api"),
                        ("tests", "tests"),
                    )
                    if reason.startswith(prefix)
                )
                counters[f"rejected_{category}"] += 1
        stats.ml_stats = dict(counters)
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

    stats.loc_final = _tree_loc(project.source_files, project.lang)

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
    return stats


def write_report(stats: ShrinkStats, out: Path) -> Path:
    payload = {"reduce": stats.to_json()}
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return out
