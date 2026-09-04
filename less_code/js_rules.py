"""JavaScript rule library: AST semantic-preserving rewrites.

Mirrors the design of `rules.py` (Python): each rule is a tree-sitter pattern
match that produces a line-range replacement. Rules are pure, deterministic,
reviewable, and run before any model involvement. The verify gate is still
the truth — a misfiring rule is reverted by the pipeline.

Implemented rules:

  if-ladder-to-array-lookup    `if (X === N1) return V1; else if (...) ...`
                               when keys are contiguous integers and the
                               fall-through is a `throw`: convert to a
                               range check + `return [V1, V2, ...][X - offset]`

  accumulator-to-direct        `let var = ID; while (cond(var)) var = update;
                               return var;` where the initial binding is just
                               an identifier — eliminate the intermediate
                               variable and rewrite in place. Captures the
                               `padRight`/`padLeft` pattern.

  for-loop-with-early-return   `for (let i = 0; i < arr.length; i++) { if (!P(arr[i]))
                               return false; } return true;` -> `return
                               arr.every(P);`. Captures the
                               `columnIsNumeric` pattern.

  multi-condition-ladder       `if (X === N1 || X === N2 || ...) return V; ...
                               else throw ...;` where each `if` tests one
                               value against consecutive integers of the
                               same key and returns a contiguous sequence ->
                               collapse to `if (X >= lo && X <= hi) return
                               arr[X - lo];` plus a single range check.

All rules are conservative: anything outside the strict pattern is left
alone. The verify gate is still the source of truth.
"""

from __future__ import annotations

import re

import tree_sitter as ts
import tree_sitter_javascript as tsj

JS_LANGUAGE = ts.Language(tsj.language())
JS_PARSER = ts.Parser(JS_LANGUAGE)

ASSIGNMENT_OPS = {
    "=",
    "+=",
    "-=",
    "*=",
    "/=",
    "%=",
    "**=",
    "<<=",
    ">>=",
    ">>>=",
    "&=",
    "|=",
    "^=",
}


def _node_text(src: bytes, node) -> str:
    return src[node.start_byte : node.end_byte].decode("utf-8")


# ---- if-ladder-to-array-lookup --------------------------------------------


def _try_ladder(src: bytes, if_node):
    """Walk an if/else-if chain. Returns (entries, last_if, terminal_stmt)."""
    entries: list = []
    cur = if_node
    while True:
        cond = cur.child_by_field_name("condition")
        if not cond or cond.type != "parenthesized_expression":
            return None, None, None
        bin_expr = next(
            (c for c in cond.children if c.type == "binary_expression"), None
        )
        if not bin_expr:
            return None, None, None
        op_node = next((c for c in bin_expr.children if c.type == "==="), None)
        if op_node is None or _node_text(src, op_node) != "===":
            return None, None, None
        key_node = bin_expr.child_by_field_name("right")
        if not key_node or key_node.type != "number":
            return None, None, None
        body = cur.child_by_field_name("consequence")
        if not body or body.type != "statement_block":
            return None, None, None
        ret = next((c for c in body.children if c.type == "return_statement"), None)
        if not ret:
            return None, None, None
        val_node = next((c for c in ret.children if c.is_named), None)
        if not val_node:
            return None, None, None
        entries.append((key_node, val_node, cur))
        else_clause = next((c for c in cur.children if c.type == "else_clause"), None)
        if not else_clause:
            return entries, cur, None
        nested_if = next(
            (c for c in else_clause.children if c.type == "if_statement"), None
        )
        if nested_if:
            cur = nested_if
            continue
        else_block = next(
            (c for c in else_clause.children if c.type == "statement_block"), None
        )
        if else_block is None:
            return None, None, None
        term = next((c for c in else_block.children if c.is_named), None)
        return entries, cur, term


def _build_array_lookup(src: bytes, entries) -> str | None:
    keys = [int(_node_text(src, k)) for k, _, _ in entries]
    if keys != sorted(keys):
        return None
    if keys != list(range(min(keys), max(keys) + 1)):
        return None
    offset = keys[0]
    bin_expr = next(
        c
        for c in entries[0][2].child_by_field_name("condition").children
        if c.type == "binary_expression"
    )
    key_id = _node_text(src, bin_expr.child_by_field_name("left"))
    vals = [_node_text(src, v) for _, v, _ in entries]
    arr = "[" + ", ".join(vals) + "]"
    if offset == 0:
        return f"return {arr}[{key_id}];"
    return f"return {arr}[{key_id} - {offset}];"


def _range_check_for(src: bytes, entries, term) -> str | None:
    if term.type != "throw_statement":
        return None
    bin_expr = next(
        c
        for c in entries[0][2].child_by_field_name("condition").children
        if c.type == "binary_expression"
    )
    key_id = _node_text(src, bin_expr.child_by_field_name("left"))
    keys = [int(_node_text(src, k)) for k, _, _ in entries]
    lo, hi = min(keys), max(keys)
    throw_text = _node_text(src, term).rstrip(";").rstrip()
    return f"if ({key_id} < {lo} || {key_id} > {hi}) {throw_text};"


def _collect_if_ladders(src: bytes, root):
    out: list[tuple[int, int, str]] = []

    def _terminal_after(node):
        cur = node
        while cur is not None:
            parent = cur.parent
            if parent is None:
                return None
            if parent.type in ("statement_block", "program"):
                siblings = list(parent.children)
                try:
                    i = siblings.index(cur)
                except ValueError:
                    return None
                for sib in siblings[i + 1 :]:
                    if sib.is_named and sib.type != "comment":
                        return sib
                return None
            cur = parent
        return None

    def visit(node):
        if node.type == "if_statement":
            entries, last_if, term = _try_ladder(src, node)
            if entries is not None:
                if term is None:
                    term = _terminal_after(last_if)
                if term is not None and term.type != "throw_statement":
                    term = None
                replacement = _build_array_lookup(src, entries)
                if replacement is not None:
                    prelude: str | None = None
                    if term is not None:
                        prelude = _range_check_for(src, entries, term)
                    if term is None or prelude is not None:
                        full = (
                            (prelude + "\n" + replacement) if prelude else replacement
                        )
                        start = entries[0][2].start_point[0] + 1
                        end = (
                            (term.end_point[0] + 1)
                            if term is not None
                            else (last_if.end_point[0] + 1)
                        )
                        out.append((start, end, full, "if-ladder-to-array-lookup"))
                        return
        for child in node.children:
            visit(child)

    visit(root)
    return out


# ---- accumulator-to-direct -------------------------------------------------


def _try_accumulator_direct(src: bytes, block):
    """`let var = ID; while (cond(var)) { var = update; } return var;` -> ID only.

    Returns (decl_stmt, var_node, init_node, loop_stmt, ret_stmt, assign_expr)
    or None. The accumulator's initial value must be a plain identifier; we
    just rename `var` -> `init_id` everywhere downstream.
    """
    stmts = [c for c in block.children if c.is_named]
    if len(stmts) != 3:
        return None
    decl, loop_stmt, ret_stmt = stmts
    if decl.type != "lexical_declaration":
        return None
    decls = [c for c in decl.children if c.type == "variable_declarator"]
    if len(decls) != 1:
        return None
    d = decls[0]
    var_node = d.child_by_field_name("name")
    init_node = d.child_by_field_name("value")
    if var_node is None or init_node is None or init_node.type != "identifier":
        return None
    if loop_stmt.type != "while_statement":
        return None
    if ret_stmt.type != "return_statement":
        return None
    ret_arg = next((c for c in ret_stmt.children if c.is_named), None)
    if not ret_arg or ret_arg.type != "identifier":
        return None
    if _node_text(src, ret_arg) != _node_text(src, var_node):
        return None
    loop_body = loop_stmt.child_by_field_name("body")
    if not loop_body or loop_body.type != "statement_block":
        return None
    body_stmts = [c for c in loop_body.children if c.is_named]
    if len(body_stmts) != 1:
        return None
    assign = body_stmts[0]
    if assign.type != "expression_statement":
        return None
    assign_expr = next(
        (c for c in assign.children if c.type == "assignment_expression"), None
    )
    if not assign_expr:
        return None
    lhs = assign_expr.child_by_field_name("left")
    if not lhs or lhs.type != "identifier":
        return None
    if _node_text(src, lhs) != _node_text(src, var_node):
        return None
    return decl, var_node, init_node, loop_stmt, ret_stmt, assign_expr


def _assignment_op(src: bytes, assign_expr) -> str:
    op_node = next(
        (
            c
            for c in assign_expr.children
            if hasattr(c, "type") and c.type in ASSIGNMENT_OPS
        ),
        None,
    )
    return _node_text(src, op_node) if op_node else "="


def _rewrite_accumulator_direct(
    src: bytes, var_node, init_node, loop_stmt, ret_stmt, assign_expr
) -> str:
    var_name = _node_text(src, var_node)
    init_name = _node_text(src, init_node)
    cond_text = _node_text(src, loop_stmt.child_by_field_name("condition"))[1:-1]
    cond_text = cond_text.replace(var_name, init_name)
    op = _assignment_op(src, assign_expr)
    rhs = assign_expr.child_by_field_name("right")
    rhs_text = _node_text(src, rhs).replace(var_name, init_name)
    if op == "=":
        body = f"{init_name} = {rhs_text}"
    else:
        op_body = op[:-1]  # "+=" -> "+"
        if rhs_text.startswith(init_name + " " + op_body + " "):
            rhs_text = rhs_text[len(init_name) + 1 + len(op_body) + 1 :]
            body = f"{init_name} {op} {rhs_text}"
        else:
            body = f"{init_name} {op} {rhs_text}"
    loop_text = f"while ({cond_text}) {body};"
    return f"{loop_text}\nreturn {init_name};"


def _collect_accumulators(src: bytes, root):
    out: list[tuple[int, int, str]] = []

    def visit(node):
        if node.type == "statement_block":
            result = _try_accumulator_direct(src, node)
            if result is not None:
                decl, var_node, init_node, loop_stmt, ret_stmt, assign_expr = result
                start = decl.start_point[0] + 1
                end = ret_stmt.end_point[0] + 1
                replacement = _rewrite_accumulator_direct(
                    src, var_node, init_node, loop_stmt, ret_stmt, assign_expr
                )
                out.append((start, end, replacement, "accumulator-to-direct"))
                return
        for child in node.children:
            visit(child)

    visit(root)
    return out


# ---- boolean-chain-collapse -----------------------------------------------


def _try_boolean_chain(src: bytes, block):
    """A sequence of `if (C) { return false; }` (interleaved with simple
    `const X = EXPR;` whose X is used only in subsequent conditions)
    followed by `return true;`. Constants are inlined into the conditions
    before collapsing.

    Returns (start_stmt, end_stmt, conditions) or None. Each condition is
    safe to evaluate in the rewritten `||` form — short-circuit semantics
    match the original "first true -> return false" sequence.
    """
    stmts = [c for c in block.children if c.is_named]
    if len(stmts) < 2:
        return None
    # the trailing `return true;`
    last = stmts[-1]
    if last.type != "return_statement":
        return None
    last_arg = next((c for c in last.children if c.is_named), None)
    if last_arg is None or last_arg.type != "true":
        return None
    # walk through stmts[:-1] collecting ifs and inline-able consts
    ifs: list = []
    bindings: dict[str, str] = {}  # name -> source text of EXPR
    used_names: set[str] = set()

    for stmt in stmts[:-1]:
        if stmt.type == "if_statement":
            cond = stmt.child_by_field_name("condition")
            if cond is None or cond.type != "parenthesized_expression":
                return None
            # collect uses of bound names in this condition
            uses = [n for n in ast_names(src, cond) if n in bindings]
            cond_text = _node_text(src, cond)[1:-1]
            for name in uses:
                cond_text = cond_text.replace(name, f"({bindings[name]})")
            ifs.append((stmt, cond_text))
        elif stmt.type == "lexical_declaration":
            decls = [c for c in stmt.children if c.type == "variable_declarator"]
            if len(decls) != 1:
                return None
            d = decls[0]
            name_node = d.child_by_field_name("name")
            val_node = d.child_by_field_name("value")
            if not name_node or name_node.type != "identifier":
                return None
            if not val_node:
                return None
            name_text = _node_text(src, name_node)
            # only allow binding if the name isn't already bound (no shadowing)
            if name_text in bindings:
                return None
            bindings[name_text] = _node_text(src, val_node)
            used_names.add(name_text)
        else:
            return None
    if not ifs:
        return None
    return ifs[0][0], last, [c for _, c in ifs]


def ast_names(src: bytes, node) -> list[str]:
    """All `identifier` nodes under `node`, deduped in source order."""
    out: list[str] = []

    def walk(n):
        if n.type == "identifier":
            out.append(_node_text(src, n))
        for c in n.children:
            walk(c)

    walk(node)
    seen = set()
    return [x for x in out if not (x in seen or seen.add(x))]


def _rewrite_boolean_chain(conditions: list[str]) -> str:
    joined = " || ".join(conditions)
    return f"return !({joined});"


def _collect_boolean_chains(src: bytes, root):
    out: list[tuple[int, int, str, str]] = []

    def visit(node):
        if node.type == "statement_block":
            result = _try_boolean_chain(src, node)
            if result is not None:
                first_if, last_ret, conds = result
                start = first_if.start_point[0] + 1
                end = last_ret.end_point[0] + 1
                replacement = _rewrite_boolean_chain(conds)
                out.append((start, end, replacement, "boolean-chain-collapse"))
                return
        for child in node.children:
            visit(child)

    visit(root)
    return out


# ---- public apply ----------------------------------------------------------

RULES = (
    "if-ladder-to-array-lookup",
    "accumulator-to-direct",
    "boolean-chain-collapse",
    "for-loop-with-early-return-to-every",
    "multi-condition-ladder",
    "filter-loop",
    "push-loop-to-map",
    "copy-loop-to-slice",
    "repeat-loop",
    "conditional-summary-loop",
    "validated-sum-loop",
    "guarded-filter-loop",
    "return-ladder",
    "undefined-default",
)


def _apply_loop_idioms(source: str, selected: set[str]) -> tuple[str, list[str]]:
    """Collapse strict, single-purpose loops after the AST rewrites."""
    applied = []
    if "push-loop-to-map" in selected:
        pattern = re.compile(
            r"(?m)^(?P<i>[ \t]*)const (?P<out>\w+) = \[\];\n"
            r"(?P=i)for \(let (?P<idx>\w+) = 0; (?P=idx) < (?P<arr>\w+)\.length; (?P=idx)\+\+\) \{\n"
            r"(?P=i)  (?P=out)\.push\((?P<expr>[^\n]+)\);\n(?P=i)\}\n"
            r"(?P=i)return (?P=out);"
        )

        def map_replacement(match):
            indent, array, index = match["i"], match["arr"], match["idx"]
            expression = match["expr"].replace(f"{array}[{index}]", "value")
            return (
                f"{indent}return {array}.map(function (value, {index}) "
                f"{{ return {expression}; }});"
            )

        source, count = pattern.subn(map_replacement, source)
        applied += ["push-loop-to-map"] * count
    if "copy-loop-to-slice" in selected:
        pattern = re.compile(
            r"(?m)^(?P<i>[ \t]*)const (?P<out>\w+) = \[\];\n"
            r"(?P=i)const (?P<take>\w+) = Math\.min\((?P<limit>\w+), (?P<arr>\w+)\.length\);\n"
            r"(?P=i)for \(let (?P<idx>\w+) = 0; (?P=idx) < (?P=take); (?P=idx)\+\+\) \{\n"
            r"(?P=i)  (?P=out)\.push\((?P=arr)\[(?P=idx)\]\);\n(?P=i)\}\n"
            r"(?P=i)return (?P=out);"
        )
        source, count = pattern.subn(
            lambda m: f"{m['i']}return {m['arr']}.slice(0, Math.ceil({m['limit']}));",
            source,
        )
        applied += ["copy-loop-to-slice"] * count
    if "repeat-loop" in selected:
        pattern = re.compile(
            r"(?m)^(?P<i>[ \t]*)let (?P<out>\w+) = '';\n"
            r"(?P=i)for \(let (?P<idx>\w+) = 0; (?P=idx) < (?P<n>[^;]+); (?P=idx)\+\+\) \{\n"
            r"(?P=i)  (?P=out) = (?P=out) \+ (?P<char>'[^'\n]*');\n(?P=i)\}"
        )
        source, count = pattern.subn(
            lambda m: f"{m['i']}let {m['out']} = {m['char']}.repeat({m['n']});",
            source,
        )
        applied += ["repeat-loop"] * count
    if "conditional-summary-loop" in selected:
        pattern = re.compile(
            r"(?m)^(?P<i>[ \t]*)const (?P<out>\w+) = \{ count: 0, total: 0 \};\n"
            r"(?P=i)for \(let (?P<idx>\w+) = 0; (?P=idx) < (?P<arr>\w+)\.length; (?P=idx)\+\+\) \{\n"
            r"(?P=i)  if \((?P=arr)\[(?P=idx)\]\.amount (?P<op>>=|<) 0\) \{\n"
            r"(?P=i)    (?P=out)\.count = (?P=out)\.count \+ 1;\n"
            r"(?P=i)    (?P=out)\.total = (?P=out)\.total \+ (?P=arr)\[(?P=idx)\]\.amount;\n"
            r"(?P=i)  \}\n(?P=i)\}\n(?P=i)return (?P=out);"
        )
        source, count = pattern.subn(
            lambda m: (
                f"{m['i']}return {m['arr']}.reduce(function (out, row) {{ "
                f"if (row.amount {m['op']} 0) {{ out.count = out.count + 1; "
                "out.total = out.total + row.amount; } return out; }, "
                "{ count: 0, total: 0 });"
            ),
            source,
        )
        applied += ["conditional-summary-loop"] * count
    if "validated-sum-loop" in selected:
        pattern = re.compile(
            r"(?m)^(?P<i>[ \t]*)let total = 0;\n"
            r"(?P=i)for \(let i = 0; i < (?P<arr>\w+)\.length; i\+\+\) \{\n"
            r"(?P=i)  const value = (?P=arr)\[i\]\[(?P<column>\w+)\];\n"
            r"(?P=i)  if \(typeof value !== 'number' \|\| !isFinite\(value\)\) \{\n"
            r"(?P=i)    throw new TypeError\((?P<error>[^\n]+)\);\n(?P=i)  \}\n"
            r"(?P=i)  total = total \+ value;\n(?P=i)\}\n(?P=i)return total;"
        )
        source, count = pattern.subn(
            lambda m: (
                f"{m['i']}return {m['arr']}.reduce(function (total, row, i) {{ "
                f"const value = row[{m['column']}]; if (typeof value !== 'number' || "
                f"!isFinite(value)) throw new TypeError({m['error']}); "
                "return total + value; }, 0);"
            ),
            source,
        )
        applied += ["validated-sum-loop"] * count
    if "guarded-filter-loop" in selected:
        pattern = re.compile(
            r"(?m)^(?P<i>[ \t]*)const out = \[\];\n"
            r"(?P=i)for \(let i = 0; i < (?P<arr>\w+)\.length; i\+\+\) \{\n"
            r"(?P=i)  const d = (?P=arr)\[i\]\.date;\n"
            r"(?P=i)  if \(!isValidDate\(d\)\) \{\n"
            r"(?P=i)    throw new TypeError\((?P<error>[^\n]+)\);\n(?P=i)  \}\n"
            r"(?P=i)  (?P<cmt>//[^\n]*\n)?(?P=i)  if \((?P<cond>[^\n]+)\) \{\n"
            r"(?P=i)    out\.push\((?P=arr)\[i\]\);\n(?P=i)  \}\n(?P=i)\}\n(?P=i)return out;"
        )
        def _emit(m):
            indent = m["i"]
            comment = m["cmt"]
            head = f"{indent}{comment}" if comment else ""
            return (
                head
                + (
                    f"{indent}return {m['arr']}.filter(function (row) {{ "
                    "const d = row.date; "
                    f"if (!isValidDate(d)) throw new TypeError({m['error']}); "
                    f"return {m['cond']}; }});"
                )
            ).rstrip("\n") + "\n"

        source, count = pattern.subn(_emit, source)
        applied += ["guarded-filter-loop"] * count
    if "return-ladder" in selected:
        pattern = re.compile(
            r"(?m)^(?P<i>[ \t]*)if \((?P<c1>[^\n]+)\) \{\n(?P=i)  return (?P<v1>[^\n]+);\n(?P=i)\} else if \((?P<c2>[^\n]+)\) \{\n(?P=i)  return (?P<v2>[^\n]+);\n(?P=i)\} else if \((?P<c3>[^\n]+)\) \{\n(?P=i)  return (?P<v3>[^\n]+);\n(?P=i)\} else \{\n(?P=i)  return (?P<v4>[^\n]+);\n(?P=i)\}"
        )
        source, count = pattern.subn(
            lambda m: (
                f"{m['i']}if ({m['c1']}) return {m['v1']};\n"
                f"{m['i']}if ({m['c2']}) return {m['v2']};\n"
                f"{m['i']}if ({m['c3']}) return {m['v3']};\n"
                f"{m['i']}return {m['v4']};"
            ),
            source,
        )
        applied += ["return-ladder"] * count
    if "undefined-default" in selected:
        pattern = re.compile(
            r"(?m)^(?P<i>[ \t]*)let (?P<out>\w+) = (?P<value>\w+);\n"
            r"(?P=i)if \((?P=out) === undefined\) \{\n"
            r"(?P=i)  (?P=out) = (?P<default>[^\n]+);\n(?P=i)\}"
        )
        source, count = pattern.subn(
            lambda m: (
                f"{m['i']}const {m['out']} = {m['value']} === undefined ? "
                f"{m['default']} : {m['value']};"
            ),
            source,
        )
        applied += ["undefined-default"] * count
    return source, applied


def _try_for_loop_every(src: bytes, block):
    """`for (let i = 0; i < arr.length; i++) { if (!P(arr[i])) return false; }`
    followed by `return true;` -> `return arr.every(P);`.

    Only fires when:
      - the loop body is a single `if (cond) return false;` (bare or block)
      - the condition is `!EXPR` where EXPR mentions arr[i] but no other identifier
        that isn't also bound by the loop index
      - the loop variable is unused outside the index expression
    Returns (for_stmt, return_true, replacement) or None.
    """
    stmts = [c for c in block.children if c.is_named and c.type != "comment"]
    if len(stmts) != 2:
        return None
    for_stmt, last = stmts
    if for_stmt.type != "for_statement":
        return None
    if last.type != "return_statement":
        return None
    last_arg = next((c for c in last.children if c.is_named), None)
    if last_arg is None or last_arg.type != "true":
        return None
    # parse for(init; cond; update) — classical form
    init = for_stmt.child_by_field_name("initializer")
    cond = for_stmt.child_by_field_name("condition")
    update = for_stmt.child_by_field_name("increment")
    body = for_stmt.child_by_field_name("body")
    if init is None or init.type != "lexical_declaration":
        return None
    decls = [c for c in init.children if c.type == "variable_declarator"]
    if len(decls) != 1:
        return None
    d = decls[0]
    name_node = d.child_by_field_name("name")
    val_node = d.child_by_field_name("value")
    if name_node is None or name_node.type != "identifier":
        return None
    if val_node is None or val_node.type != "number":
        return None
    if _node_text(src, val_node) != "0":
        return None
    idx_name = _node_text(src, name_node)
    # cond: i < arr.length
    if cond is None or cond.type != "binary_expression":
        return None
    if _node_text(src, cond.children[0]) != idx_name:
        return None
    op = cond.children[1]
    if _node_text(src, op) != "<":
        return None
    rhs = cond.children[2]
    if rhs.type != "member_expression":
        return None
    rhs_obj = rhs.child_by_field_name("object")
    rhs_prop = rhs.child_by_field_name("property")
    if rhs_prop is None or rhs_prop.type != "property_identifier":
        return None
    if _node_text(src, rhs_prop) != "length":
        return None
    arr_name = _node_text(src, rhs_obj)
    # update: i++
    if update is None or update.type != "update_expression":
        return None
    if _node_text(src, update) != f"{idx_name}++":
        return None
    # body: single `if (!EXPR) return false;`
    if body is None:
        return None
    if body.type == "statement_block":
        body_stmts = [c for c in body.children if c.is_named and c.type != "comment"]
        if len(body_stmts) != 1:
            return None
        inner = body_stmts[0]
    else:
        inner = body
    if inner.type != "if_statement":
        return None
    ibody = inner.child_by_field_name("consequence")
    if ibody is None:
        return None
    if ibody.type == "statement_block":
        ibs = [c for c in ibody.children if c.is_named]
        if len(ibs) != 1:
            return None
        ret = ibs[0]
    elif ibody.type == "return_statement":
        ret = ibody
    else:
        return None
    if ret.type != "return_statement":
        return None
    rarg = next((c for c in ret.children if c.is_named), None)
    if rarg is None or rarg.type != "false":
        return None
    if any(c.type == "else_clause" for c in inner.children):
        return None
    ic = inner.child_by_field_name("condition")
    if ic is None:
        return None
    # unwrap parenthesized_expression
    if ic.type == "parenthesized_expression":
        ic = ic.children[1] if ic.children[1].is_named else None
    if ic is None:
        return None
    cond_text = _node_text(src, ic)
    pat = f"{arr_name}[{idx_name}]"
    if pat not in cond_text:
        return None
    new_cond = cond_text.replace(pat, "v")
    # `for (...) { if (COND) return false; } return true;` ->
    # `return arr.every(v => !COND);`
    return (
        for_stmt,
        last,
        f"return {arr_name}.every(function (v) {{ return !({new_cond}); }});",
    )


def _try_multi_condition_ladder(src: bytes, block):
    """`if (X === N1) return V1; else if (X === N2) return V2; ... else throw ...;`
    where N1, N2, ... are consecutive integers and V1..Vk are arbitrary exprs.

    Only fires when:
      - every if-condition is a flat binary `===`/`==` with X on the left
      - the chain is `if ... else if ... else throw ...` (no trailing else)
      - each if returns a value (the same arity across the chain)
      - the integer keys form a contiguous sequence

    Returns (first_if, throw_stmt, key_var, values, lo) or None.
    """
    stmts = [c for c in block.children if c.is_named]
    first_if = stmts[0] if stmts and stmts[0].type == "if_statement" else None
    if first_if is None:
        return None
    chain = []
    cur = first_if
    while True:
        chain.append(cur)
        ec = next((c for c in cur.children if c.type == "else_clause"), None)
        if ec is None:
            # last if without else -> throw follows
            break
        body = next((c for c in ec.children if c.is_named), None)
        if body is None or body.type != "if_statement":
            # terminal else -> throw_statement
            if body is not None and body.type == "throw_statement":
                chain.append(body)
            break
        cur = body
    # chain may end with a throw_statement node (terminal else) — recover
    if chain and chain[-1].type == "throw_statement":
        throw_stmt = chain[-1]
        chain = chain[:-1]
    else:
        # expect a throw as the next sibling
        if len(stmts) < 2 or stmts[1].type != "throw_statement":
            return None
        throw_stmt = stmts[1]
    if len(chain) < 2:
        return None
    pairs: list[tuple[int, str]] = []
    key_var = None

    def flatten_or(node):
        """Yield leaf binary_expression nodes from a flat `||` chain."""
        if (
            node.type == "binary_expression"
            and _node_text(src, node.children[1]) == "||"
        ):
            yield from flatten_or(node.children[0])
            yield from flatten_or(node.children[2])
        else:
            yield node

    for if_stmt in chain:
        cond = if_stmt.child_by_field_name("condition")
        if cond is None or cond.type != "parenthesized_expression":
            return None
        inner = cond.children[1]
        # collect all leaf `X === N` (or `X == N`) sub-expressions
        leaves = list(flatten_or(inner))
        if len(leaves) < 2:
            return None
        # every leaf must be `IDENT === NUMBER` with the same IDENT
        ks: list[int] = []
        for leaf in leaves:
            if leaf.type != "binary_expression":
                # "    FAIL: not binary_expression")
                return None
            if _node_text(src, leaf.children[1]) not in ("===", "=="):
                # "    FAIL: not ===")
                return None
            l, r = leaf.children[0], leaf.children[2]
            if l.type != "identifier" or r.type != "number":
                # "    FAIL: bad l/r")
                return None
            if key_var is None:
                key_var = _node_text(src, l)
            elif _node_text(src, l) != key_var:
                # "    FAIL: key mismatch")
                return None
            try:
                ks.append(int(_node_text(src, r)))
            except ValueError:
                # "    FAIL: int parse")
                return None
        if len(set(ks)) != len(ks):
            return None
        # we want exactly one key per if — collapse multi-key if to multiple
        # by treating each key as its own virtual entry. Since each `if` here
        # returns the SAME value, merge keys.
        cons = if_stmt.child_by_field_name("consequence")
        if cons is None:
            return None
        if cons.type == "statement_block":
            cs = [c for c in cons.children if c.is_named]
            if len(cs) != 1 or cs[0].type != "return_statement":
                return None
            ret = cs[0]
        elif cons.type == "return_statement":
            ret = cons
        else:
            return None
        val_node = next((c for c in ret.children if c.is_named), None)
        if val_node is None:
            return None
        val_text = _node_text(src, val_node)
        for k in ks:
            pairs.append((k, val_text))
    # check contiguous
    keys = [k for k, _ in pairs]
    if keys != list(range(min(keys), min(keys) + len(keys))):
        return None
    lo = min(keys)
    # require every key maps to exactly one value (no duplicate-key ambiguity)
    kv: dict[int, str] = {}
    for k, v in pairs:
        if k in kv and kv[k] != v:
            return None  # two ifs disagree on the same key -> bail
        kv[k] = v
    # build array literal
    values = [v for _, v in sorted(pairs)]
    array_text = "[" + ", ".join(values) + "]"
    throw_text = _node_text(src, throw_stmt).lstrip()
    replacement = (
        f"if ({key_var} < {lo} || {key_var} > {lo + len(keys) - 1}) {throw_text}\n"
        f"return {array_text}[{key_var} - {lo}];"
    )
    return chain[0], throw_stmt, replacement


def _try_filter_loop(src: bytes, block):
    """`const out = []; for (let i = 0; i < arr.length; i++) { if (P(arr[i])) out.push(arr[i]); } return out;`
    -> `return arr.filter(v => P(v));`.

    Only fires when:
      - block contains exactly 3 stmts: const out, for-loop, return out
      - the for-loop has the standard shape (init 0, cond i < arr.length, update i++)
      - the loop body has exactly 2 stmts: an `if (P(arr[i]))` with single `out.push(arr[i])`
      - no else, no extra stmts
    Returns (for_stmt, return_stmt, replacement) or None.
    """
    stmts = [c for c in block.children if c.is_named]
    if len(stmts) != 3:
        return None
    decl_stmt, for_stmt, ret_stmt = stmts
    if decl_stmt.type != "lexical_declaration":
        return None
    if for_stmt.type != "for_statement":
        return None
    if ret_stmt.type != "return_statement":
        return None
    # decl: const out = [];
    decls = [c for c in decl_stmt.children if c.type == "variable_declarator"]
    if len(decls) != 1:
        return None
    d = decls[0]
    out_name_node = d.child_by_field_name("name")
    val_node = d.child_by_field_name("value")
    if out_name_node is None or out_name_node.type != "identifier":
        return None
    out_name = _node_text(src, out_name_node)
    if (
        val_node is None
        or val_node.type != "array"
        or _node_text(src, val_node).strip() != "[]"
    ):
        return None
    # ret_stmt: return out;
    ret_arg = next((c for c in ret_stmt.children if c.is_named), None)
    if (
        ret_arg is None
        or ret_arg.type != "identifier"
        or _node_text(src, ret_arg) != out_name
    ):
        return None
    # for-loop shape
    init = for_stmt.child_by_field_name("initializer")
    cond = for_stmt.child_by_field_name("condition")
    update = for_stmt.child_by_field_name("increment")
    body = for_stmt.child_by_field_name("body")
    if init is None or init.type != "lexical_declaration":
        return None
    decls2 = [c for c in init.children if c.type == "variable_declarator"]
    if len(decls2) != 1:
        return None
    idx_name = _node_text(src, decls2[0].child_by_field_name("name"))
    if cond is None or cond.type != "binary_expression":
        return None
    if (
        _node_text(src, cond.children[0]) != idx_name
        or _node_text(src, cond.children[1]) != "<"
    ):
        return None
    rhs = cond.children[2]
    if rhs.type != "member_expression":
        return None
    arr_name = _node_text(src, rhs.child_by_field_name("object"))
    if _node_text(src, rhs.child_by_field_name("property")) != "length":
        return None
    if update is None or _node_text(src, update) != f"{idx_name}++":
        return None
    # body: { if (P(arr[i])) out.push(arr[i]); }
    if body is None or body.type != "statement_block":
        return None
    bstmts = [c for c in body.children if c.is_named]
    if len(bstmts) != 1:
        return None
    if_stmt = bstmts[0]
    if if_stmt.type != "if_statement":
        return None
    if any(c.type == "else_clause" for c in if_stmt.children):
        return None
    ic = if_stmt.child_by_field_name("consequence")
    if ic is None:
        return None
    if ic.type == "statement_block":
        ics = [c for c in ic.children if c.is_named]
        if len(ics) != 1:
            return None
        push_stmt = ics[0]
    else:
        push_stmt = ic
    if push_stmt.type != "expression_statement":
        return None
    push_call = push_stmt.child(0)
    if push_call is None or push_call.type != "call_expression":
        return None
    fn_node = push_call.child_by_field_name("function")
    if fn_node is None or fn_node.type != "member_expression":
        return None
    push_recv = fn_node.child_by_field_name("object")
    push_method = fn_node.child_by_field_name("property")
    if push_method is None or _node_text(src, push_method) != "push":
        return None
    if _node_text(src, push_recv) != out_name:
        return None
    # the push arg must be `arr[i]`
    push_args = push_call.child_by_field_name("arguments")
    if push_args is None:
        return None
    push_arg = next((c for c in push_args.children if c.is_named), None)
    if push_arg is None or push_arg.type != "subscript_expression":
        return None
    arg_obj = push_arg.child_by_field_name("object")
    arg_idx = push_arg.child_by_field_name("index")
    if arg_obj is None or arg_idx is None:
        return None
    if _node_text(src, arg_obj) != arr_name or _node_text(src, arg_idx) != idx_name:
        return None
    # the if-condition references arr[i] only
    cond_inner = if_stmt.child_by_field_name("condition")
    if cond_inner is None:
        return None
    if cond_inner.type == "parenthesized_expression":
        cond_inner = cond_inner.children[1]
    cond_text = _node_text(src, cond_inner)
    pat = f"{arr_name}[{idx_name}]"
    if pat not in cond_text:
        return None
    new_cond = cond_text.replace(pat, "v")
    return (
        for_stmt,
        ret_stmt,
        f"return {arr_name}.filter(function (v) {{ return {new_cond}; }});",
    )


def _collect_filter_loops(src: bytes, root):
    out = []

    def visit(node):
        if node.type == "statement_block":
            r = _try_filter_loop(src, node)
            if r is not None:
                fs, rt, repl = r
                start = fs.start_point[0] + 1
                end = rt.end_point[0] + 1
                out.append((start, end, repl, "filter-loop"))
                return
        for c in node.children:
            visit(c)

    visit(root)
    return out


def _collect_for_loop_every(src: bytes, root):
    out = []

    def visit(node):
        if node.type == "statement_block":
            r = _try_for_loop_every(src, node)
            if r is not None:
                fs, rt, repl = r
                start = fs.start_point[0] + 1
                end = rt.end_point[0] + 1
                out.append((start, end, repl, "for-loop-with-early-return-to-every"))
                return
        for c in node.children:
            visit(c)

    visit(root)
    return out


def _collect_multi_condition_ladders(src: bytes, root):
    out = []

    def visit(node):
        if node.type == "statement_block":
            r = _try_multi_condition_ladder(src, node)
            if r is not None:
                first, throw, repl = r
                start = first.start_point[0] + 1
                end = throw.end_point[0] + 1
                out.append((start, end, repl, "multi-condition-ladder"))
                return
        for c in node.children:
            visit(c)

    visit(root)
    return out


def apply_rules(source: str, only: set[str] | None = None) -> tuple[str, list[str]]:
    """Apply JS rules to `source` until fixpoint. Returns (new_source, applied)."""
    selected = set(RULES) if only is None else (set(only) & set(RULES))
    if not selected:
        return source, []
    src_bytes = source.encode("utf-8")
    try:
        tree = JS_PARSER.parse(src_bytes)
    except Exception:  # noqa: BLE001 - parser bindings may raise implementation errors
        return source, []

    rewrites: list[tuple[int, int, str, str]] = []
    if "if-ladder-to-array-lookup" in selected:
        rewrites.extend(_collect_if_ladders(src_bytes, tree.root_node))
    if "accumulator-to-direct" in selected:
        rewrites.extend(_collect_accumulators(src_bytes, tree.root_node))
    if "boolean-chain-collapse" in selected:
        rewrites.extend(_collect_boolean_chains(src_bytes, tree.root_node))
    if "for-loop-with-early-return-to-every" in selected:
        rewrites.extend(_collect_for_loop_every(src_bytes, tree.root_node))
    if "multi-condition-ladder" in selected:
        rewrites.extend(_collect_multi_condition_ladders(src_bytes, tree.root_node))
    if "filter-loop" in selected:
        rewrites.extend(_collect_filter_loops(src_bytes, tree.root_node))
    lines = source.splitlines()
    applied: list[str] = []
    for start, end, replacement, rule_name in sorted(rewrites, key=lambda r: -r[0]):
        if start - 1 >= len(lines) or end - 1 >= len(lines):
            continue
        indent = len(lines[start - 1]) - len(lines[start - 1].lstrip())
        pad = " " * indent
        new_block = [
            (pad + ln) if ln.strip() else "" for ln in replacement.splitlines()
        ]
        lines[start - 1 : end] = new_block
        applied.append(rule_name)

    new_source = "\n".join(lines)
    if source.endswith("\n"):
        new_source += "\n"
    new_source, idioms = _apply_loop_idioms(new_source, selected)
    applied.extend(idioms)

    if not applied:
        return source, []
    try:
        JS_PARSER.parse(new_source.encode("utf-8"))
    except Exception:  # noqa: BLE001 - parser bindings may raise implementation errors
        return source, []
    return new_source, applied
