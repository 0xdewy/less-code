"""Host-anchored, per-symbol model proposals for the final shrink pass."""

from __future__ import annotations

import ast
import json
import re
import subprocess
import textwrap
from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class ProposedEdit:
    """A model may choose a symbol and replace that symbol's complete text."""

    symbol_id: str
    replacement: str


class ModelBackend(Protocol):
    name: str

    def propose(self, lang: str, symbol: dict) -> ProposedEdit | None: ...


class CliBackend:
    """Run a command once per symbol; invalid output means no proposal."""

    name = "cli"

    def __init__(self, command: str, timeout: float = 60.0):
        self.command = command
        self.timeout = timeout

    def propose(self, lang: str, symbol: dict) -> ProposedEdit | None:
        try:
            proc = subprocess.run(
                self.command,
                shell=True,
                input=build_prompt(lang, symbol),
                capture_output=True,
                text=True,
                timeout=self.timeout,
                check=False,
            )
        except (subprocess.SubprocessError, OSError):
            return None
        if proc.returncode:
            return None
        output = proc.stdout.strip()
        if output.startswith("```"):
            output = re.sub(r"^\s*```(?:json)?\s*", "", output)
            output = re.sub(r"\s*```\s*$", "", output)
        try:
            data = json.loads(output)
            edit = ProposedEdit(data["symbol_id"], data["replacement"])
        except (json.JSONDecodeError, KeyError, TypeError):
            return None
        return edit if edit.symbol_id == symbol["id"] else None


def _symbol(source: str, name: str, start: int, end: int) -> dict:
    encoded = source.encode()
    return {
        "id": f"{name}@{start}:{end}",
        "name": name,
        "start_byte": start,
        "end_byte": end,
        "text": encoded[start:end].decode(),
    }


def _extract_symbols_python(source: str) -> list[dict]:
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    out = []
    lines = source.splitlines()
    offsets = [0]
    for line in source.splitlines(keepends=True):
        offsets.append(offsets[-1] + len(line.encode()))

    def span(node) -> tuple[int, int]:
        first = node.decorator_list[0].lineno if node.decorator_list else node.lineno
        line = lines[first - 1]
        indent = len(line.encode()) - len(line.lstrip().encode())
        return (
            offsets[first - 1] + indent,
            offsets[node.end_lineno - 1] + node.end_col_offset,
        )

    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            out.append(_symbol(source, node.name, *span(node)))
        elif isinstance(node, ast.ClassDef):
            for child in node.body:
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    out.append(
                        _symbol(
                            source,
                            f"{node.name}.{child.name}",
                            *span(child),
                        )
                    )
    return out


def _extract_symbols_tree_sitter(source: str, lang: str) -> list[dict]:
    import tree_sitter as ts

    if lang in ("javascript", "typescript"):
        import tree_sitter_javascript as grammar

        kinds = {
            "function_declaration",
            "generator_function_declaration",
            "method_definition",
        }
    else:
        import tree_sitter_rust as grammar

        kinds = {"function_item"}
    encoded = source.encode()
    root = ts.Parser(ts.Language(grammar.language())).parse(encoded).root_node
    out = []

    def walk(node, owner: str = "") -> None:
        next_owner = owner
        if node.type in {"class_declaration", "impl_item"}:
            name = node.child_by_field_name("name") or node.child_by_field_name("type")
            if name is not None:
                next_owner = encoded[name.start_byte : name.end_byte].decode()
        if node.type in kinds:
            name_node = node.child_by_field_name("name")
            if name_node is not None:
                name = encoded[name_node.start_byte : name_node.end_byte].decode()
                qualified = f"{owner}.{name}" if owner else name
                out.append(
                    _symbol(
                        source,
                        qualified,
                        node.start_byte,
                        node.end_byte,
                    )
                )
            return
        for child in node.children:
            walk(child, next_owner)

    walk(root)
    return out


def extract_symbols(source: str, lang: str) -> list[dict]:
    if lang == "python":
        return _extract_symbols_python(source)
    return _extract_symbols_tree_sitter(source, lang)


def apply_edit(source: str, symbol: dict, edit: ProposedEdit) -> str:
    """Replace the host-selected symbol; model-supplied positions do not exist."""
    if edit.symbol_id != symbol["id"]:
        raise ValueError("proposal does not match the requested symbol")
    if not isinstance(edit.replacement, str):
        raise ValueError("replacement must be text")
    encoded = source.encode()
    start, end = symbol["start_byte"], symbol["end_byte"]
    if not (0 <= start < end <= len(encoded)) or not edit.replacement.strip():
        raise ValueError("invalid symbol replacement")
    line_start = encoded.rfind(b"\n", 0, start) + 1
    indent = encoded[line_start:start]
    if indent.strip():
        indent = b""
    replacement_lines = textwrap.dedent(edit.replacement).strip("\n").splitlines()
    replacement = "\n".join(
        line if index == 0 or not line else indent.decode() + line
        for index, line in enumerate(replacement_lines)
    ).encode()
    return (encoded[:start] + replacement + encoded[end:]).decode()


def syntax_ok(source: str, lang: str) -> bool:
    if lang == "python":
        try:
            ast.parse(source)
            return True
        except SyntaxError:
            return False
    try:
        import tree_sitter as ts

        if lang in ("javascript", "typescript"):
            import tree_sitter_javascript as grammar
        else:
            import tree_sitter_rust as grammar
        root = (
            ts.Parser(ts.Language(grammar.language())).parse(source.encode()).root_node
        )
        return not root.has_error
    except Exception:  # noqa: BLE001 - parser bindings may raise implementation errors
        return False


SYSTEM_PROMPT = (
    "Rewrite the supplied symbol with less code while preserving behavior for "
    "every input, its complete signature, errors, and documentation. Prefer "
    "standard-library idioms. Return null when uncertain. Otherwise return "
    "exactly one JSON object with only `symbol_id` and `replacement`; "
    "`replacement` must contain the complete symbol including its declaration."
)


def build_prompt(lang: str, symbol: dict) -> str:
    return json.dumps(
        {
            "system": SYSTEM_PROMPT,
            "language": lang,
            "symbol_id": symbol["id"],
            "symbol_name": symbol["name"],
            "source": symbol["text"],
        },
        indent=2,
    )
