"""Rust rule library: AST semantic-preserving rewrites.

Mirrors `js_rules.py` and `rules.py`: tree-sitter patterns that produce
line-range replacements. All rules are conservative; the verify gate
is the source of truth.

Implemented rules:

  csv-interleave-to-join `for (i, x) in arr.iter().enumerate() { if i != 0 { out.push(SEP); }
                         out.push_str(PROC(x)); }` followed by a newline-push ->
                         `arr.iter().map(PROC).collect::<Vec<_>>().join(&SEP_STR) +
                         NEWLINE`. Catches `csv_escape_row`, `csv_escape_row_owned`,
                         and the inner loop in `csv_escape_rows`.

  inline-single-use-binding  `let x = EXPR; USE(x)` -> `USE(EXPR)` when the
                             untyped, immutable binding has exactly one use,
                             in the immediately following statement.

  unbrace-match-arm      `pat => { return x; }` -> `pat => return x,` for one
                         diverging, assignment or value statement.
"""

from __future__ import annotations

import re
from itertools import pairwise

import tree_sitter as ts
import tree_sitter_rust as tsr

RS_LANGUAGE = ts.Language(tsr.language())
RS_PARSER = ts.Parser(RS_LANGUAGE)


def test_spans(source: str) -> list[tuple[int, int]]:
    """Byte ranges of inline test modules/functions, including their attributes."""
    encoded = source.encode()
    spans = []

    def visit(node):
        previous = node.prev_named_sibling
        start = node.start_byte
        attributes = b""
        while previous is not None and previous.type == "attribute_item":
            start = previous.start_byte
            attributes += encoded[previous.start_byte : previous.end_byte]
            previous = previous.prev_named_sibling
        name = node.child_by_field_name("name")
        named_tests = (
            node.type == "mod_item"
            and name is not None
            and encoded[name.start_byte : name.end_byte] == b"tests"
        )
        if node.type in {"mod_item", "function_item"} and (
            named_tests or re.search(rb"\b(?:test|bench)\b", attributes)
        ):
            spans.append((start, node.end_byte))
            return
        for child in node.named_children:
            visit(child)

    visit(RS_PARSER.parse(encoded).root_node)
    return spans


def _node_text(src: bytes, node) -> str:
    return src[node.start_byte : node.end_byte].decode("utf-8")


def _identifier_nodes(src: bytes, node, name: str) -> list:
    found = []

    def visit(current):
        if current.type == "identifier" and _node_text(src, current) == name:
            found.append(current)
            return
        for child in current.named_children:
            visit(child)

    visit(node)
    return found


def _collect_inline_single_use_bindings(src: bytes, root):
    """Inline a short forwarding local into its sole, adjacent use.

    Mutable and typed declarations are excluded: mutation needs an identity,
    while an explicit type can supply coercion or inference context that the
    use site does not. Comments are named siblings and therefore interrupt the
    adjacent pair instead of being crossed by the rewrite.
    """
    out = []

    def visit(node):
        if node.type == "block":
            statements = list(node.named_children)
            for index, (declaration, following) in enumerate(pairwise(statements)):
                if declaration.type != "let_declaration":
                    continue
                declaration_text = _node_text(src, declaration)
                if re.match(r"\s*let\s+(?:mut|ref)\b", declaration_text):
                    continue
                if declaration.child_by_field_name("type") is not None:
                    continue
                pattern = declaration.child_by_field_name("pattern")
                value = declaration.child_by_field_name("value")
                if pattern is None or pattern.type != "identifier" or value is None:
                    continue
                name = _node_text(src, pattern)
                uses = _identifier_nodes(src, following, name)
                if len(uses) != 1:
                    continue
                if any(
                    _identifier_nodes(src, later, name)
                    for later in statements[index + 2 :]
                ):
                    continue
                use = uses[0]
                ancestor = use.parent
                deferred_or_conditional = False
                while ancestor is not None and ancestor != following:
                    if ancestor.type in {
                        "block",
                        "closure_expression",
                        "async_block",
                        "match_arm",
                        "while_expression",
                        "loop_expression",
                    }:
                        deferred_or_conditional = True
                        break
                    ancestor = ancestor.parent
                if deferred_or_conditional:
                    continue
                following_text = _node_text(src, following)
                offset = use.start_byte - following.start_byte
                value_text = _node_text(src, value)
                atomic = value.type in {
                    "identifier",
                    "scoped_identifier",
                    "field_expression",
                    "call_expression",
                    "try_expression",
                    "integer_literal",
                    "float_literal",
                    "string_literal",
                    "char_literal",
                    "boolean_literal",
                } or (use.parent is not None and use.parent.type == "arguments")
                inserted = value_text if atomic else f"({value_text})"
                replacement = (
                    following_text[:offset]
                    + inserted
                    + following_text[offset + len(name) :]
                )
                if (
                    use.parent is not None
                    and use.parent.type == "shorthand_field_initializer"
                ):
                    continue
                if (
                    value.start_point[0] == value.end_point[0]
                    and following.start_point[0] == following.end_point[0]
                    and following.start_point[1] + len(replacement) > 88
                ):
                    continue
                replacement_lines = replacement.splitlines()
                if len(replacement_lines) > 1:
                    prefix = " " * following.start_point[1]
                    replacement = "\n".join(
                        [replacement_lines[0]]
                        + [line.removeprefix(prefix) for line in replacement_lines[1:]]
                    )
                out.append(
                    (
                        declaration.start_point[0] + 1,
                        following.end_point[0] + 1,
                        replacement,
                        "inline-single-use-binding",
                    )
                )
                return
        for child in node.named_children:
            visit(child)

    visit(root)
    return out


def _try_interleave(src: bytes, block) -> tuple | None:
    """Detect `for (i, x) in arr.iter().enumerate() { if i != 0 { out.push(SEP); }
    out.push_str(EXPR); }` followed by `out.push(NEWLINE); out`.

    Only fires when the for body has exactly 2 stmts (the if-sep-push and the push_str),
    the loop variable is unused outside the body, and the post-statement is a single
    `out.push('\n')` followed by an identifier return.

    Returns (for_stmt, post_stmt, replacement) or None.
    """
    stmts = [c for c in block.children if c.is_named]
    # find for_expression (possibly wrapped in expression_statement) and its wrapper index
    for_stmt = None
    for_stmt_idx = None
    for i, s in enumerate(stmts):
        if s.type == "expression_statement":
            inner = s.child(0)
            if inner and inner.type == "for_expression":
                for_stmt = inner
                for_stmt_idx = i
                break
        elif s.type == "for_expression":
            for_stmt = s
            for_stmt_idx = i
            break
    if for_stmt is None:
        return None
    pat = for_stmt.child_by_field_name("pattern")
    if pat is None or pat.type != "tuple_pattern":
        return None
    items = [c for c in pat.children if c.is_named]
    if len(items) != 2:
        return None
    i_name = _node_text(src, items[0])
    x_name = _node_text(src, items[1])
    iterable = for_stmt.child_by_field_name("value")
    if iterable is None or iterable.type != "call_expression":
        return None
    # expect `arr.iter().enumerate()` -> call_expression with field_expression
    # function = `arr.iter().enumerate` (a field_expression with value = `arr.iter()`)
    outer_fexpr = iterable.child_by_field_name("function")
    if outer_fexpr is None or outer_fexpr.type != "field_expression":
        return None
    outer_method = outer_fexpr.child_by_field_name("field")
    if outer_method is None or _node_text(src, outer_method) != "enumerate":
        return None
    inner_call = outer_fexpr.child_by_field_name("value")
    if inner_call is None or inner_call.type != "call_expression":
        return None
    inner_fexpr = inner_call.child_by_field_name("function")
    if inner_fexpr is None or inner_fexpr.type != "field_expression":
        return None
    arr_node = inner_fexpr.child_by_field_name("value")
    arr_method = inner_fexpr.child_by_field_name("field")
    if arr_method is None or _node_text(src, arr_method) != "iter":
        return None
    if arr_node is None:
        return None
    arr_text = _node_text(src, arr_node)
    body = for_stmt.child_by_field_name("body")
    if body is None or body.type != "block":
        return None
    bstmts = [c for c in body.children if c.is_named]
    if len(bstmts) != 2:
        return None
    # stmt 0: if i != 0 { out.push(SEP); }
    s0, s1 = bstmts
    if s0.type != "expression_statement":
        return None
    if s0.child(0).type != "if_expression":
        return None
    if_expr = s0.child(0)
    cond = if_expr.child_by_field_name("condition")
    cons = if_expr.child_by_field_name("consequence")
    if cond is None or cond.type != "binary_expression":
        return None
    if (
        _node_text(src, cond.children[0]) != i_name
        or _node_text(src, cond.children[1]) != "!="
    ):
        return None
    if (
        cond.children[2].type not in ("number", "integer_literal")
        or _node_text(src, cond.children[2]) != "0"
    ):
        return None
    # consequence: `out.push(SEP)` (single expression)
    cstmts = [c for c in cons.children if c.is_named]
    if len(cstmts) != 1 or cstmts[0].type != "expression_statement":
        return None
    push_call = cstmts[0].child(0)
    if push_call.type != "call_expression":
        return None
    push_fexpr = push_call.child_by_field_name("function")
    if push_fexpr is None or push_fexpr.type != "field_expression":
        return None
    if _node_text(src, push_fexpr.child_by_field_name("field")) != "push":
        return None
    # stmt 1: out.push_str(EXPR)
    if s1.type != "expression_statement":
        return None
    push_str_call = s1.child(0)
    if push_str_call.type != "call_expression":
        return None
    push_str_fexpr = push_str_call.child_by_field_name("function")
    if push_str_fexpr is None or push_str_fexpr.type != "field_expression":
        return None
    if _node_text(src, push_str_fexpr.child_by_field_name("field")) != "push_str":
        return None
    push_str_arg = push_str_call.child_by_field_name("arguments")
    if push_str_arg is None:
        return None
    # the argument is &csv_escape_field(field, delim) — we need to extract
    # the function name (PROC_NAME) and the field-name placeholder (x_name)
    arg_node = next((c for c in push_str_arg.children if c.is_named), None)
    if arg_node is None:
        return None
    arg_text = _node_text(src, arg_node)
    if x_name not in arg_text:
        return None
    # we don't strictly need to parse — just substitute x_name -> placeholder
    # substitute loop var `x_name` -> `v` (closure arg) in the proc call;
    # Strip only the outer borrow accepted by `push_str`; ampersands inside the
    # expression may be operators or nested borrows and must remain untouched.
    proc_call = re.sub(rf"\b{x_name}\b", "v", arg_text).strip()
    if proc_call.startswith("&"):
        proc_call = proc_call[1:].lstrip()
    # post-loop: out.push('\n'); out — must be the two stmts immediately after for_stmt
    post = stmts[for_stmt_idx + 1 :]
    if len(post) != 2:
        return None
    p1, p2 = post
    if p1.type != "expression_statement":
        return None
    p1_inner = p1.child(0)
    if p1_inner.type != "call_expression":
        return None
    p1_fexpr = p1_inner.child_by_field_name("function")
    if p1_fexpr is None or p1_fexpr.type != "field_expression":
        return None
    p1_field = p1_fexpr.child_by_field_name("field")
    if p1_field is None or _node_text(src, p1_field) != "push":
        return None
    newline_args = p1_inner.child_by_field_name("arguments")
    if newline_args is None:
        return None
    newline_arg = next((c for c in newline_args.children if c.is_named), None)
    if newline_arg is None or newline_arg.type not in (
        "string_literal",
        "char_literal",
    ):
        return None
    newline_text = _node_text(src, newline_arg)
    # `p2` must be an identifier (the return)
    if p2.type != "identifier":
        return None
    out_name = _node_text(src, p2)
    push_recv_field = push_call.child_by_field_name("function")
    push_recv = (
        push_recv_field.child_by_field_name("value") if push_recv_field else None
    )
    if push_recv is None or _node_text(src, push_recv) != out_name:
        return None
    # Build replacement:
    #   let mut out = arr.iter().map(|PLACEHOLDER_FIELD| proc_call).collect::<Vec<_>>().join(&SEP_STR);
    #   out.push(NEWLINE);
    #   out
    sep_args = push_call.child_by_field_name("arguments")
    sep_arg = (
        next((c for c in sep_args.children if c.is_named), None) if sep_args else None
    )
    if sep_arg is None:
        return None
    sep_text = _node_text(src, sep_arg)
    # SEP is a char; join needs &str. Use delimiter.to_string().
    sep_join = f"&{sep_text}.to_string()"
    repl = (
        f"{out_name} = {arr_text}.iter()"
        f".map(|v| {proc_call})"
        f".collect::<Vec<_>>().join({sep_join});\n"
        f"{out_name}.push({newline_text});\n"
        f"{out_name}"
    )
    return for_stmt, p2, repl


def _try_contains_check(src: bytes, block):
    """Detect `let mut F = false; for c in X.chars() { if c == A || c == B || ... { F = true; } } if F { ... }`
    (or `if !F { ... }`) and emit a rewrite to `if X.contains([A, B, ...]) { ... }`.

    The block can have more stmts after the if (e.g. the rest of the function
    body); we look for the first 3 stmts matching the pattern.

    Returns (prev_let, last_if, replacement) or None.
    """
    stmts = [c for c in block.children if c.is_named]
    if len(stmts) < 3:
        return None
    prev, for_stmt, nxt = stmts[0], stmts[1], stmts[2]
    # prev: `let mut FLAG = false;` or `let mut FLAG: bool = false;`
    if prev.type != "let_declaration":
        return None
    prev_text = _node_text(src, prev)
    m = re.match(r"\s*let\s+mut\s+(\w+)\s*(?::\s*\w+\s*)?=\s*false\s*;", prev_text)
    if not m:
        return None
    flag_name = m.group(1)
    # for_stmt: `for c in X.chars() { if c == A || c == B || ... { FLAG = true; } }`
    # (possibly wrapped in expression_statement)
    fs = for_stmt
    if fs.type == "expression_statement":
        fs = fs.child(0) if fs.child(0) else None
    if fs is None or fs.type != "for_expression":
        return None
    pat = fs.child_by_field_name("pattern")
    iter_n = fs.child_by_field_name("value")
    if pat is None or pat.type != "identifier":
        return None
    if iter_n is None or iter_n.type != "call_expression":
        return None
    fexpr = iter_n.child_by_field_name("function")
    if fexpr is None or fexpr.type != "field_expression":
        return None
    iter_method = fexpr.child_by_field_name("field")
    if iter_method is None or _node_text(src, iter_method) != "chars":
        return None
    s_name = _node_text(src, fexpr.child_by_field_name("value"))
    loop_var = _node_text(src, pat)
    body = fs.child_by_field_name("body")
    if body is None or body.type != "block":
        return None
    bstmts = [c for c in body.children if c.is_named]
    if len(bstmts) != 1:
        return None
    inner = bstmts[0]
    if inner.type == "expression_statement":
        inner = inner.child(0) if inner.child(0) else None
    if inner is None or inner.type != "if_expression":
        return None
    inner_cond = inner.child_by_field_name("condition")
    inner_cons = inner.child_by_field_name("consequence")
    if inner_cond is None or inner_cond.type != "binary_expression":
        return None
    # top-level must be `==` not `||` (we'll flatten below)
    if _node_text(src, inner_cond.children[1]) != "||":
        return None

    def flatten_or(n):
        if n.type == "binary_expression" and _node_text(src, n.children[1]) == "||":
            yield from flatten_or(n.children[0])
            yield from flatten_or(n.children[2])
        else:
            yield n

    leaves = list(flatten_or(inner_cond))
    chars: list[str] = []
    for leaf in leaves:
        if leaf.type != "binary_expression":
            return None
        if _node_text(src, leaf.children[1]) != "==":
            return None
        l, r = leaf.children[0], leaf.children[2]
        if l.type != "identifier" or _node_text(src, l) != loop_var:
            return None
        # right side: char_literal OR identifier (e.g. a parameter like `delimiter`)
        if r.type == "char_literal" or r.type == "identifier":
            chars.append(_node_text(src, r))
        else:
            return None
    # consequence must be a single `FLAG = true;` assignment
    cstmts = [c for c in inner_cons.children if c.is_named]
    if len(cstmts) != 1:
        return None
    a = cstmts[0]
    if a.type == "expression_statement":
        a = a.child(0) if a.child(0) else None
    if a is None or a.type != "assignment_expression":
        return None
    if _node_text(src, a.children[1]) != "=":
        return None
    lhs = a.children[0]
    if lhs.type != "identifier" or _node_text(src, lhs) != flag_name:
        return None
    rhs = a.children[2]
    # rhs: either bare `true` identifier or boolean_literal
    if rhs.type == "boolean_literal":
        # the literal is "true" or "false"
        if _node_text(src, rhs.children[0]) != "true":
            return None
    elif rhs.type == "true":
        pass
    else:
        return None
    # next sibling: `if FLAG { ... }` or `if !FLAG { ... }`
    if nxt.type == "expression_statement":
        nxt = nxt.child(0) if nxt.child(0) else None
    if nxt is None or nxt.type != "if_expression":
        return None
    nxt_cond = nxt.child_by_field_name("condition")
    nxt_cons = nxt.child_by_field_name("consequence")
    if nxt_cond is None:
        return None
    negate = False
    if nxt_cond.type == "identifier":
        if _node_text(src, nxt_cond) != flag_name:
            return None
        negate = False
    elif nxt_cond.type == "unary_expression":
        if _node_text(src, nxt_cond.children[0]) != "!":
            return None
        inner_cond = nxt_cond.children[1]
        if inner_cond.type != "identifier" or _node_text(src, inner_cond) != flag_name:
            return None
        negate = True
    else:
        return None
    chars_str = "[" + ", ".join(chars) + "]"
    if negate:
        replacement = f"if !{s_name}.contains({chars_str}) {_node_text(src, nxt_cons)}"
    else:
        replacement = f"if {s_name}.contains({chars_str}) {_node_text(src, nxt_cons)}"
    return prev, nxt, replacement


def _try_for_count(src: bytes, block):
    """Detect `let mut count = 0usize; for _ in iter { count = count + 1; }` -> `iter.count()`.
    Returns (let_stmt, for_stmt, replacement) or None.
    The block can have more stmts after the for-loop.
    """
    stmts = [c for c in block.children if c.is_named]
    if len(stmts) < 2:
        return None
    let_stmt, for_stmt = stmts[0], stmts[1]
    if let_stmt.type != "let_declaration":
        return None
    let_text = _node_text(src, let_stmt)
    # `let mut COUNT = 0usize;` or `let mut COUNT: usize = 0;` etc.
    m = re.match(r"\s*let\s+mut\s+(\w+)\s*(?::\s*\w+\s*)?=\s*0\w*\s*;", let_text)
    if not m:
        return None
    count_name = m.group(1)
    fs = for_stmt
    if fs.type == "expression_statement":
        fs = fs.child(0) if fs.child(0) else None
    if fs is None or fs.type != "for_expression":
        return None
    pat = fs.child_by_field_name("pattern")
    iter_n = fs.child_by_field_name("value")
    if pat is None:
        return None
    pat_text = _node_text(src, pat)
    if pat_text != "_":
        return None  # only fire when the loop var is unused
    if iter_n is None:
        return None
    body = fs.child_by_field_name("body")
    if body is None or body.type != "block":
        return None
    bstmts = [c for c in body.children if c.is_named]
    if len(bstmts) != 1:
        return None
    inner = bstmts[0]
    if inner.type == "expression_statement":
        inner = inner.child(0) if inner.child(0) else None
    if inner is None or inner.type != "assignment_expression":
        return None
    if _node_text(src, inner.children[1]) != "=":
        return None
    lhs, rhs = inner.children[0], inner.children[2]
    if lhs.type != "identifier" or _node_text(src, lhs) != count_name:
        return None
    # rhs: `count + 1` (binary_expression with +)
    if rhs.type != "binary_expression":
        return None
    if _node_text(src, rhs.children[1]) != "+":
        return None
    if (
        rhs.children[0].type != "identifier"
        or _node_text(src, rhs.children[0]) != count_name
    ):
        return None
    if rhs.children[2].type not in ("number", "integer_literal"):
        return None
    if _node_text(src, rhs.children[2]) != "1":
        return None
    iter_text = _node_text(src, iter_n)
    return let_stmt, for_stmt, f"let {count_name} = {iter_text}.count();"


def _try_fold_sum(src: bytes, block):
    """Detect `let mut total = 0; for x in iter { total = total + f(x); }` -> `iter.map(f).sum()`.
    Returns (let_stmt, for_stmt, replacement) or None.
    The block can have more stmts after the for-loop.
    """
    stmts = [c for c in block.children if c.is_named]
    if len(stmts) < 2:
        return None
    let_stmt, for_stmt = stmts[0], stmts[1]
    if let_stmt.type != "let_declaration":
        return None
    let_text = _node_text(src, let_stmt)
    m = re.match(r"\s*let\s+mut\s+(\w+)\s*(?::\s*\w+\s*)?=\s*0\w*\s*;", let_text)
    if not m:
        return None
    total_name = m.group(1)
    fs = for_stmt
    if fs.type == "expression_statement":
        fs = fs.child(0) if fs.child(0) else None
    if fs is None or fs.type != "for_expression":
        return None
    pat = fs.child_by_field_name("pattern")
    iter_n = fs.child_by_field_name("value")
    if pat is None:
        return None
    x_name = _node_text(src, pat)
    body = fs.child_by_field_name("body")
    if body is None or body.type != "block":
        return None
    bstmts = [c for c in body.children if c.is_named]
    if len(bstmts) != 1:
        return None
    inner = bstmts[0]
    if inner.type == "expression_statement":
        inner = inner.child(0) if inner.child(0) else None
    if inner is None or inner.type != "assignment_expression":
        return None
    if _node_text(src, inner.children[1]) != "=":
        return None
    lhs, rhs = inner.children[0], inner.children[2]
    if lhs.type != "identifier" or _node_text(src, lhs) != total_name:
        return None
    if rhs.type != "binary_expression":
        return None
    if _node_text(src, rhs.children[1]) != "+":
        return None
    if (
        rhs.children[0].type != "identifier"
        or _node_text(src, rhs.children[0]) != total_name
    ):
        return None
    # rhs.children[2] is the function call expression (e.g. count_syllables_word(word))
    rhs_call = rhs.children[2]
    if rhs_call.type != "call_expression":
        return None
    fn_field = rhs_call.child_by_field_name("function")
    if fn_field is None or fn_field.type != "identifier":
        return None
    fn_name = _node_text(src, fn_field)
    fn_args = rhs_call.child_by_field_name("arguments")
    if fn_args is None:
        return None
    # the call's args should be just `x_name`
    fn_args_text = _node_text(src, fn_args).strip()
    if fn_args_text != f"({x_name})" and fn_args_text != x_name:
        return None
    iter_text = _node_text(src, iter_n)
    return let_stmt, for_stmt, f"let {total_name} = {iter_text}.map({fn_name}).sum();"


def _collect_for_count(src: bytes, root):
    out = []

    def visit(node):
        if node.type == "block":
            r = _try_for_count(src, node)
            if r is not None:
                let_stmt, for_stmt, repl = r
                start = let_stmt.start_point[0] + 1
                end = for_stmt.end_point[0] + 1
                out.append((start, end, repl, "for-count-to-method"))
                return
        for c in node.children:
            visit(c)

    visit(root)
    return out


def _collect_fold_sum(src: bytes, root):
    out = []

    def visit(node):
        if node.type == "block":
            r = _try_fold_sum(src, node)
            if r is not None:
                let_stmt, for_stmt, repl = r
                start = let_stmt.start_point[0] + 1
                end = for_stmt.end_point[0] + 1
                out.append((start, end, repl, "fold-add-to-sum"))
                return
        for c in node.children:
            visit(c)

    visit(root)
    return out


def _collect_contains_check(src: bytes, root):
    out = []

    def visit(node):
        if node.type == "block":
            r = _try_contains_check(src, node)
            if r is not None:
                prev, nxt, repl = r
                start = prev.start_point[0] + 1
                end = nxt.end_point[0] + 1
                out.append((start, end, repl, "contains-check-loop"))
                return
        for c in node.children:
            visit(c)

    visit(root)
    return out


def _collect_interleaves(src: bytes, root):
    out = []

    def visit(node):
        if node.type == "block":
            r = _try_interleave(src, node)
            if r is not None:
                fs, p2, repl = r
                start = fs.start_point[0] + 1
                end = p2.end_point[0] + 1
                out.append((start, end, repl, "csv-interleave-to-join"))
                return
        for c in node.children:
            visit(c)

    visit(root)
    return out


_ARM_DIVERGING = {"return_expression", "break_expression", "continue_expression"}
_ARM_UNIT = {"assignment_expression", "compound_assignment_expr"}


def _collect_arm_unbrace(src: bytes, root):
    """`pat => { S; }` -> `pat => S,` for one statement S.

    Braces around a single arm statement are syntax: the block scopes no
    binding. The arm keeps its type when S is diverging (`return`, `break`,
    `continue`: type `!`), an assignment (type `()`, which is what the
    block with a trailing semicolon evaluated to), or a final expression
    without a semicolon (the block's value is exactly that expression).
    Arms whose pattern spans lines, or whose block carries a comment, stay.
    """
    out = []

    def visit(node):
        if node.type == "match_arm":
            block = node.child_by_field_name("value")
            pattern_ok = (
                block is not None
                and block.type == "block"
                and node.start_point[0] == block.start_point[0]
                and not any(
                    c.type in {"line_comment", "block_comment"} for c in block.children
                )
            )
            if pattern_ok:
                stmts = block.named_children
                if len(stmts) == 1:
                    stmt = stmts[0]
                    text = _node_text(src, stmt).strip()
                    inner = stmt
                    if stmt.type == "expression_statement" and stmt.named_children:
                        inner = stmt.named_children[0]
                    ends_with_semicolon = text.endswith(";")
                    keeps_type = (
                        not ends_with_semicolon
                        or inner.type in _ARM_DIVERGING
                        or inner.type in _ARM_UNIT
                    )
                    single_line = stmt.start_point[0] == stmt.end_point[0]
                    if keeps_type and single_line:
                        head = src[node.start_byte : block.start_byte].decode("utf-8")
                        replacement = head + text.rstrip(";") + ","
                        out.append(
                            (
                                node.start_point[0] + 1,
                                block.end_point[0] + 1,
                                replacement,
                                "unbrace-match-arm",
                            )
                        )
                        return
        for child in node.children:
            visit(child)

    visit(root)
    return out


RULES = (
    "inline-single-use-binding",
    "csv-interleave-to-join",
    "contains-check-loop",
    "for-count-to-method",
    "fold-add-to-sum",
    "unbrace-match-arm",
)


def apply_rules(source: str, only: set[str] | None = None) -> tuple[str, list[str]]:
    selected = set(RULES) if only is None else (set(only) & set(RULES))
    if not selected:
        return source, []
    src_bytes = source.encode("utf-8")
    try:
        tree = RS_PARSER.parse(src_bytes)
    except Exception:  # noqa: BLE001 - parser bindings may raise implementation errors
        return source, []

    rewrites: list[tuple[int, int, str, str]] = []
    if "inline-single-use-binding" in selected:
        rewrites.extend(_collect_inline_single_use_bindings(src_bytes, tree.root_node))
    if "csv-interleave-to-join" in selected:
        rewrites.extend(_collect_interleaves(src_bytes, tree.root_node))
    if "contains-check-loop" in selected:
        rewrites.extend(_collect_contains_check(src_bytes, tree.root_node))
    if "for-count-to-method" in selected:
        rewrites.extend(_collect_for_count(src_bytes, tree.root_node))
    if "fold-add-to-sum" in selected:
        rewrites.extend(_collect_fold_sum(src_bytes, tree.root_node))
    if "unbrace-match-arm" in selected:
        rewrites.extend(_collect_arm_unbrace(src_bytes, tree.root_node))
    if not rewrites:
        return source, []

    frozen = [
        (src_bytes[:start].count(b"\n") + 1, src_bytes[:end].count(b"\n") + 1)
        for start, end in test_spans(source)
    ]
    rewrites = [
        r
        for r in rewrites
        if not any(r[0] <= end and r[1] >= start for start, end in frozen)
    ]

    lines = source.splitlines()
    applied: list[str] = []
    for start, end, replacement, rule_name in sorted(rewrites, key=lambda r: -r[0]):
        if start - 1 >= len(lines) or end - 1 >= len(lines):
            continue
        indent = len(lines[start - 1]) - len(lines[start - 1].lstrip())
        pad = lines[start - 1][:indent]
        new_block = [
            (pad + ln) if ln.strip() else "" for ln in replacement.splitlines()
        ]
        candidate_lines = lines[: start - 1] + new_block + lines[end:]
        # Removing a binding can make rustfmt wrap its use site onto more
        # lines. Keep each candidate only when it independently lowers the
        # canonical metric, so a wide inline cannot consume savings from the
        # useful rewrites elsewhere in the same file.
        from .loc import measure

        before_text = "\n".join(lines) + ("\n" if source.endswith("\n") else "")
        after_text = "\n".join(candidate_lines) + (
            "\n" if source.endswith("\n") else ""
        )
        after_loc = measure(after_text, "rust")
        if after_loc.code >= measure(before_text, "rust").code:
            continue
        lines = candidate_lines
        applied.append(rule_name)

    if not applied:
        return source, []
    new_source = "\n".join(lines)
    if source.endswith("\n"):
        new_source += "\n"

    try:
        parsed = RS_PARSER.parse(new_source.encode("utf-8"))
    except Exception:  # noqa: BLE001 - parser bindings may raise implementation errors
        return source, []
    if parsed.root_node.has_error:
        return source, []
    return new_source, applied
