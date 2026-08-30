"""Built-in mutation engine (Python AST + JS/Rust token swaps).

A mutant that fails to parse/compile counts as killed (industry convention:
cargo-mutants and Stryker both treat unviable mutants as caught).
"""

from __future__ import annotations

import ast
import random
from dataclasses import dataclass
from pathlib import Path

CMP_SWAP = {
    ast.Lt: ast.LtE, ast.LtE: ast.Lt, ast.Gt: ast.GtE, ast.GtE: ast.Gt,
    ast.Eq: ast.NotEq, ast.NotEq: ast.Eq, ast.Is: ast.IsNot, ast.IsNot: ast.Is,
    ast.In: ast.NotIn, ast.NotIn: ast.In,
}
BINOP_SWAP = {ast.Add: ast.Sub, ast.Sub: ast.Add}
BOOLOP_SWAP = {ast.And: ast.Or, ast.Or: ast.And}

JS_SWAPS = [
    ("===", "!=="), ("!==", "==="), ("==", "!="), ("!=", "=="),
    ("&&", "||"), ("||", "&&"), ("+", "-"), ("-", "+"),
    ("<", "<="), (">", ">="), ("<=", "<"), (">=", ">"),
]
RUST_SWAPS = [
    ("==", "!="), ("!=", "=="), ("&&", "||"), ("||", "&&"),
    ("+", "-"), ("<", "<="), (">", ">="), ("<=", "<"), (">=", ">"),
]


@dataclass
class Mutation:
    file: Path
    index: int
    description: str
    mutated_source: str


class _PyMutator(ast.NodeTransformer):
    def __init__(self, target_id: int) -> None:
        self.target_id = target_id
        self.count = 0

    def _maybe(self, node: ast.AST, replacement: ast.AST) -> ast.AST:
        if self.count == self.target_id:
            self.count += 1
            return ast.copy_location(replacement, node)
        self.count += 1
        return node

    def visit_Compare(self, node: ast.Compare) -> ast.AST:
        self.generic_visit(node)
        for i, op in enumerate(node.ops):
            if type(op) in CMP_SWAP:
                if self.count == self.target_id:
                    self.count += 1
                    node.ops[i] = ast.copy_location(CMP_SWAP[type(op)](), op)
                    return node
                self.count += 1
        return node

    def visit_BinOp(self, node: ast.BinOp) -> ast.AST:
        self.generic_visit(node)
        if type(node.op) in BINOP_SWAP:
            new = ast.BinOp(left=node.left, op=BINOP_SWAP[type(node.op)](), right=node.right)
            return self._maybe(node, new)
        return node

    def visit_BoolOp(self, node: ast.BoolOp) -> ast.AST:
        self.generic_visit(node)
        new = ast.BoolOp(op=BOOLOP_SWAP[type(node.op)](), values=node.values)
        return self._maybe(node, new)

    def visit_UnaryOp(self, node: ast.UnaryOp) -> ast.AST:
        self.generic_visit(node)
        if isinstance(node.op, ast.Not):
            return self._maybe(node, node.operand)
        return node

    def visit_Constant(self, node: ast.Constant) -> ast.AST:
        if node.value is True:
            return self._maybe(node, ast.Constant(value=False))
        if node.value is False:
            return self._maybe(node, ast.Constant(value=True))
        if isinstance(node.value, int) and not isinstance(node.value, bool):
            return self._maybe(node, ast.Constant(value=node.value + 1))
        return node


def _python_mutations(path: Path, source: str) -> list[Mutation]:
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    total = _count_sites(tree)
    mutants: list[Mutation] = []
    for i in range(total):
        mut = _PyMutator(i)
        try:
            new_tree = mut.visit(ast.parse(source))
            ast.fix_missing_locations(new_tree)
            mutants.append(Mutation(path, i, f"py-site-{i}", ast.unparse(new_tree)))
        except (SyntaxError, RecursionError):
            continue
    return mutants


def _count_sites(tree: ast.AST) -> int:
    counter = _PyMutator(10**9)
    counter.visit(tree)
    return counter.count


def _token_mutations(path: Path, source: str, swaps: list[tuple[str, str]]) -> list[Mutation]:
    # Mask longer symbols (===, <=, ->, +=, comments, ...) so short operators
    # inside them are never touched; masked sites cannot yield mutants.
    reserved = [
        "===", "!==", "<=", ">=", "=>", "->", "<<", ">>", "++", "--",
        "+=", "-=", "*=", "/=", "%=", "**", "&&=", "||=", "&=", "|=",
        "//", "/*", "*/",
    ]
    masked: list[tuple[int, int]] = []
    for sym in reserved:
        start = 0
        while (idx := source.find(sym, start)) != -1:
            masked.append((idx, idx + len(sym)))
            start = idx + len(sym)

    def free(idx: int, length: int, used: list[tuple[int, int]]) -> bool:
        end = idx + length
        return all(idx >= m1 or end <= m0 for m0, m1 in masked) and all(
            idx >= u1 or end <= u0 for u0, u1 in used
        )

    mutants: list[Mutation] = []
    used: list[tuple[int, int]] = []
    for old, new in swaps:
        start = 0
        while (idx := source.find(old, start)) != -1:
            end = idx + len(old)
            if free(idx, len(old), used):
                mutated = source[:idx] + new + source[end:]
                mutants.append(Mutation(path, len(mutants), f"{old}->{new}@{idx}", mutated))
                used.append((idx, end))
            start = idx + len(old)
    return mutants


def generate_mutations(path: Path, lang: str, max_mutants: int = 40, seed: int = 7) -> list[Mutation]:
    source = path.read_text(encoding="utf-8", errors="replace")
    if lang == "python":
        mutants = _python_mutations(path, source)
    elif lang in ("javascript", "typescript"):
        mutants = _token_mutations(path, source, JS_SWAPS)
    elif lang == "rust":
        mutants = _token_mutations(path, source, RUST_SWAPS)
    else:
        raise ValueError(f"unsupported language {lang}")
    if len(mutants) > max_mutants:
        rng = random.Random(seed)
        stride = len(mutants) / max_mutants
        mutants = [mutants[int(i * stride)] for i in range(max_mutants)]
    return mutants
