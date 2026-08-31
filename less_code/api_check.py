"""Public-API surface extraction + preservation check (anti-reward-hacking).

The surface is what the gate refuses to let a rewrite change, so it has to be
finer-grained than "top-level names" (roadmap B2): class/impl methods with
their parameter lists *and defaults*, every JS export form, and Rust `pub`
methods and fields. stdlib only — a regex/AST pair per language, no
tree-sitter dependency; the extractor and the RL reward share this module.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path


def _signature(node: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    """`def(a,b=1,*rest,c=2,**kw)` — names and default values are both API."""
    a = node.args
    positional = a.posonlyargs + a.args
    defaults: list[str] = [""] * (len(positional) - len(a.defaults))
    defaults += [ast.unparse(d) for d in a.defaults]
    parts = [
        f"{arg.arg}={default}" if default else arg.arg
        for arg, default in zip(positional, defaults)
    ]
    if a.posonlyargs:
        parts.insert(len(a.posonlyargs), "/")
    if a.vararg:
        parts.append(f"*{a.vararg.arg}")
    elif a.kwonlyargs:
        parts.append("*")
    for arg, default in zip(a.kwonlyargs, a.kw_defaults):
        parts.append(f"{arg.arg}={ast.unparse(default)}" if default is not None else arg.arg)
    if a.kwarg:
        parts.append(f"**{a.kwarg.arg}")
    prefix = "async def" if isinstance(node, ast.AsyncFunctionDef) else "def"
    return f"{prefix}({','.join(parts)})"


def python_api(source: str) -> dict[str, str]:
    """Top-level functions/classes plus `Class.method` entries."""
    api: dict[str, str] = {}
    tree = ast.parse(source)
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            api[node.name] = _signature(node)
        elif isinstance(node, ast.ClassDef):
            bases = ",".join(ast.unparse(b) for b in node.bases)
            api[node.name] = f"class({bases})" if bases else "class"
            for child in node.body:
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    decorators = sorted(
                        ast.unparse(d).split("(")[0] for d in child.decorator_list
                    )
                    kind = "".join(f"@{d} " for d in decorators)
                    api[f"{node.name}.{child.name}"] = kind + _signature(child)
    return api


JS_EXPORT = re.compile(
    r"export\s+(?:default\s+)?(?:async\s+)?(?:function\s*\*?\s+(\w+)|class\s+(\w+)|const\s+(\w+)|let\s+(\w+)|var\s+(\w+))",
    re.MULTILINE,
)
JS_EXPORT_LIST = re.compile(r"export\s*\{([^}]*)\}", re.MULTILINE)
JS_EXPORT_DEFAULT = re.compile(r"export\s+default\s+(?!(?:async\s+)?(?:function|class)\b)", re.MULTILINE)
JS_MODULE_EXPORTS_PROP = re.compile(r"module\.exports\.(\w+)\s*=", re.MULTILINE)
JS_MODULE_EXPORTS_OBJ = re.compile(r"module\.exports\s*=\s*\{([^}]*)\}", re.MULTILINE)
JS_MODULE_EXPORTS_NAME = re.compile(r"module\.exports\s*=\s*(\w+)\s*;?\s*$", re.MULTILINE)
JS_EXPORTS_PROP = re.compile(r"(?<!module\.)\bexports\.(\w+)\s*=", re.MULTILINE)


def js_api(source: str) -> dict[str, str]:
    """ESM (`export`, `export {a, b as c}`, `export default`) and CJS
    (`module.exports = {…}`, `module.exports.x`, `exports.x`)."""
    api: dict[str, str] = {m: "export" for group in JS_EXPORT.findall(source) for m in group if m}
    for body in JS_EXPORT_LIST.findall(source):
        for item in body.split(","):
            item = item.strip()
            if not item:
                continue
            name = item.split(" as ")[-1].strip() if " as " in item else item
            api[name] = "export"
    if JS_EXPORT_DEFAULT.search(source) or re.search(
        r"export\s+default\s+(?:async\s+)?(?:function|class)\b", source
    ):
        api["default"] = "export-default"
    for name in JS_MODULE_EXPORTS_PROP.findall(source) + JS_EXPORTS_PROP.findall(source):
        api[name] = "export"
    for body in JS_MODULE_EXPORTS_OBJ.findall(source):
        for item in body.split(","):
            item = item.strip()
            if not item:
                continue
            api[item.split(":")[0].strip()] = "export"
    for name in JS_MODULE_EXPORTS_NAME.findall(source):
        api[name] = "export"
    return api


RUST_PUB = re.compile(
    r"^\s*pub(?:\([^)]*\))?\s+(?:async\s+)?(?:unsafe\s+)?(?:const\s+)?fn\s+(\w+)|"
    r"^\s*pub(?:\([^)]*\))?\s+(?:struct|enum|trait|mod|type|const|static|union)\s+(\w+)",
    re.MULTILINE,
)
RUST_IMPL = re.compile(r"^impl(?:<[^>]*>)?\s+([^{]+?)\s*\{", re.MULTILINE)
RUST_PUB_FN = re.compile(
    r"^\s+pub(?:\([^)]*\))?\s+(?:async\s+)?(?:unsafe\s+)?(?:const\s+)?fn\s+(\w+)\s*(?:<[^>]*>)?\s*\(([^)]*)\)",
    re.MULTILINE,
)
RUST_PUB_FIELD = re.compile(r"^\s+pub(?:\([^)]*\))?\s+(\w+)\s*:\s*([^,\n]+)", re.MULTILINE)
RUST_BLOCK = re.compile(r"^(?:#\[[^\]]*\]\s*)*(?:pub(?:\([^)]*\))?\s+)?(struct|enum|trait)\s+(\w+)[^;{]*\{", re.MULTILINE)


def _block_body(source: str, brace_at: int) -> str:
    depth, i = 0, brace_at
    while i < len(source):
        if source[i] == "{":
            depth += 1
        elif source[i] == "}":
            depth -= 1
            if depth == 0:
                return source[brace_at + 1 : i]
        i += 1
    return source[brace_at:]


def rust_api(source: str) -> dict[str, str]:
    """`pub` items, `pub` methods inside `impl` blocks (`Type::method` with
    its parameter list) and `pub` struct/enum fields (`Type.field`)."""
    api: dict[str, str] = {m: "pub" for group in RUST_PUB.findall(source) for m in group if m}
    for m in RUST_IMPL.finditer(source):
        target = re.sub(r"\s+", " ", m.group(1)).strip()
        name = target.split(" for ")[-1].split("<")[0].strip()
        body = _block_body(source, source.index("{", m.end() - 1))
        for fn in RUST_PUB_FN.finditer(body):
            params = re.sub(r"\s+", " ", fn.group(2)).strip()
            api[f"{name}::{fn.group(1)}"] = f"pub fn({params})"
    for m in RUST_BLOCK.finditer(source):
        body = _block_body(source, source.index("{", m.end() - 1))
        for field in RUST_PUB_FIELD.finditer(body):
            api[f"{m.group(2)}.{field.group(1)}"] = f"pub {field.group(2).strip()}"
    return api


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
        added_public = {
            k for k in set(new_api) - set(api)
            if not k.split(".")[-1].split("::")[-1].startswith("_")
        }
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
