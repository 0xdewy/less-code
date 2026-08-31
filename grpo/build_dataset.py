"""GRPO dataset builder — CPU only.

A sample = one reducible unit:
  prompt        : instruction + verbose code + frozen test spec (what the policy sees)
  tests         : the frozen suite (what the reward runs; policy never edits it)
  lang, loc     : metadata for reward normalization
  mutation_score: (with --mutants N) the fixture suite's mutant kill rate, the
                  weight `rewards.reward_fn_mutation_weighted` scales shaping by

Sources: the fixtures in this repo (smoke scale) and, for real training runs,
the mining mix from docs/research/data.md (repos filtered by tests-green +
mutation score, Exercism, CodeContests/CodeNet, CommitPackFT).

Usage:
  python grpo/build_dataset.py --fixtures fixtures --out grpo/dataset.jsonl
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import shutil
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from less_code.loc import count_source  # noqa: E402

INSTRUCTION = (
    "Rewrite the code with significantly fewer lines preserving behavior EXACTLY "
    "and the public API exactly (helpers must be underscore-prefixed). Respond "
    "with ONLY one fenced code block containing the complete rewritten code."
)


def python_units(source: str) -> list[tuple[str, int]]:
    units = []
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return units
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            seg = ast.get_source_segment(source, node) or ""
            if count_source(seg, "python").code >= 8:
                units.append((seg, node.end_lineno - node.lineno + 1))
    return units


JS_UNIT = re.compile(r"(?:^|\n)((?:export\s+)?(?:async\s+)?function\s+\w+[^{]*\{)", re.MULTILINE)
RUST_UNIT = re.compile(r"(?:^|\n)((?:pub(?:\([^)]*\))?\s+)?(?:async\s+)?fn\s+\w+[^{]*\{)", re.MULTILINE)


def _brace_units(source: str, pattern: re.Pattern) -> list[tuple[str, int]]:
    units = []
    for m in pattern.finditer(source):
        start = source.rfind("\n", 0, m.start(1)) + 1
        depth = 0
        i = source.find("{", m.start(1))
        if i == -1:
            continue
        while i < len(source):
            if source[i] == "{":
                depth += 1
            elif source[i] == "}":
                depth -= 1
                if depth == 0:
                    seg = source[start : i + 1]
                    if len(seg.splitlines()) >= 6:
                        units.append((seg, len(seg.splitlines())))
                    break
            i += 1
    return units


def units_for(source: str, lang: str) -> list[tuple[str, int]]:
    if lang == "python":
        return python_units(source)
    if lang in ("javascript", "typescript"):
        return _brace_units(source, JS_UNIT)
    if lang == "rust":
        return _brace_units(source, RUST_UNIT)
    return []


def green(root: Path, lang: str) -> bool:
    from less_code.testrunners import run_tests

    return run_tests(root, lang, timeout=600).ok


def mutation_score(fixture: Path, lang: str, max_mutants: int) -> float | None:
    """Suite strength for every sample from this fixture, used by
    `grpo.rewards.reward_fn_mutation_weighted` to scale the shaping term.

    Mutation testing REWRITES source files, so it runs on a throwaway copy and
    never touches the fixture. Returns None when disabled or unmeasurable; the
    reward then defaults the weight to 1.0."""
    if max_mutants <= 0:
        return None
    from less_code.audit import audit
    from less_code.langdetect import map_project

    with tempfile.TemporaryDirectory(prefix="grpo-mutscore-") as tmp:
        scratch = Path(tmp) / fixture.name
        shutil.copytree(fixture, scratch)
        try:
            project = map_project(scratch, lang)
            result = audit(scratch, lang, project.source_files, max_mutants=max_mutants)
        except Exception as exc:  # noqa: BLE001 - metadata only, never fatal
            print(f"  mutation score unavailable for {fixture.name}: {exc}", file=sys.stderr)
            return None
    if result.skipped_no_baseline or result.total == 0:
        return None
    return round(result.score, 4)


def build_from_fixture(
    fixture: Path, max_spec_chars: int = 30000, max_mutants: int = 0
) -> list[dict]:
    from less_code.langdetect import map_project

    fixture = fixture.resolve()
    project = map_project(fixture)
    if not green(fixture, project.lang):
        print(f"  SKIP {fixture.name}: baseline not green", file=sys.stderr)
        return []
    score = mutation_score(fixture, project.lang, max_mutants)
    spec = "\n\n".join(
        f.read_text(encoding="utf-8", errors="replace")[:max_spec_chars]
        for f in project.test_files
    )[:max_spec_chars]
    tag = project.lang
    samples: list[dict] = []
    for path in project.source_files:
        source = path.read_text(encoding="utf-8", errors="replace")
        for seg, lines in units_for(source, tag):
            sample = {
                "prompt": (
                    f"{INSTRUCTION}\n\nLanguage: {tag}\n\n"
                    f"The test suite the code must pass:\n```\n{spec}\n```\n\n"
                    f"```{tag}\n{seg}\n```"
                ),
                "tests_paths": [str(p.relative_to(fixture)) for p in project.test_files],
                "fixture": str(fixture.relative_to(REPO)),
                "lang": tag,
                "unit_lines": lines,
                "unit_loc": count_source(seg, tag).code,
                "unit_source": seg,
            }
            # Only emitted when actually measured, so a default-built dataset
            # stays byte-identical to the committed one.
            if score is not None:
                sample["mutation_score"] = score
            samples.append(sample)
    return samples


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--fixtures", type=Path, default=REPO / "fixtures")
    ap.add_argument("--out", type=Path, default=REPO / "grpo" / "dataset.jsonl")
    ap.add_argument("--min-samples", type=int, default=20)
    ap.add_argument(
        "--mutants",
        type=int,
        default=20,
        help="mutants per source file for the per-fixture mutation score written "
        "into each sample (0 = skip; the mutation-weighted reward then defaults "
        "the weight to 1.0). Costs one full suite run per mutant.",
    )
    args = ap.parse_args()

    all_samples: list[dict] = []
    for fixture in sorted(args.fixtures.iterdir()):
        if fixture.is_dir():
            made = build_from_fixture(fixture, max_mutants=args.mutants)
            print(f"{fixture.name}: {len(made)} samples")
            all_samples.extend(made)

    seen: set[str] = set()
    deduped = []
    for s in all_samples:
        key = s["unit_source"][:200]
        if key not in seen:
            seen.add(key)
            deduped.append(s)

    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("w", encoding="utf-8") as fh:
        for s in deduped:
            fh.write(json.dumps(s) + "\n")
    print(f"wrote {len(deduped)} samples -> {args.out}")
    if len(deduped) < args.min_samples:
        print(f"FAIL: expected >= {args.min_samples} samples", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
