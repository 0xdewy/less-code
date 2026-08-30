"""Public-API surface extraction + preservation check (anti-reward-hacking)."""

from __future__ import annotations

import ast
import re
from pathlib import Path


def python_api(source: str) -> dict[str, str]:
    api: dict[str, str] = {}
    tree = ast.parse(source)
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            args = [a.arg for a in node.args.posonlyargs + node.args.args + node.args.kwonlyargs]
            api[node.name] = f"def({','.join(args)})"
        elif isinstance(node, ast.ClassDef):
            api[node.name] = "class"
    return api


JS_EXPORT = re.compile(
    r"export\s+(?:default\s+)?(?:async\s+)?(?:function\s*\*?\s+(\w+)|class\s+(\w+)|const\s+(\w+)|let\s+(\w+)|var\s+(\w+))",
    re.MULTILINE,
)
RUST_PUB = re.compile(
    r"^\s*pub(?:\([^)]*\))?\s+(?:async\s+)?(?:unsafe\s+)?fn\s+(\w+)|"
    r"^\s*pub(?:\([^)]*\))?\s+(?:struct|enum|trait|mod|type)\s+(\w+)",
    re.MULTILINE,
)


def js_api(source: str) -> dict[str, str]:
    return {m: "export" for group in JS_EXPORT.findall(source) for m in group if m}


def rust_api(source: str) -> dict[str, str]:
    return {m: "pub" for group in RUST_PUB.findall(source) for m in group if m}


EXTRACTORS = {"python": python_api, "javascript": js_api, "typescript": js_api, "rust": rust_api}


def api_surface(files: list[Path], lang: str) -> dict[str, dict[str, str]]:
    extract = EXTRACTORS[lang]
    surface: dict[str, dict[str, str]] = {}
    for path in files:
        surface[str(path)] = extract(path.read_text(encoding="utf-8", errors="replace"))
    return surface


def api_violations(
    before: dict[str, dict[str, str]], after: dict[str, dict[str, str]]
) -> list[str]:
    """Files whose public API changed. Underscore-prefixed additions are
    allowed (internal helpers created during dedup); removals/changes of
    existing public symbols are violations."""
    problems: list[str] = []
    for file, api in before.items():
        new_api = after.get(file, {})
        missing = set(api) - set(new_api)
        changed = {
            k for k in set(api) & set(new_api) if api[k] != new_api[k]
        }
        added_public = {k for k in set(new_api) - set(api) if not k.startswith("_")}
        parts = []
        if missing:
            parts.append(f"missing={sorted(missing)}")
        if changed:
            parts.append(f"changed={sorted(changed)}")
        if added_public:
            parts.append(f"added={sorted(added_public)}")
        if parts:
            problems.append(f"{file}: {' '.join(parts)}")
    return problems
