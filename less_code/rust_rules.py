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
    encoded, spans = source.encode(), []

    def visit(node):
        previous, start, attributes = node.prev_named_sibling, node.start_byte, b""
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
                if (
                    declaration.type != "let_declaration"
                    or re.match(r"\s*let\s+(?:mut|ref)\b", _node_text(src, declaration))
                ) or declaration.child_by_field_name("type") is not None:
                    continue
                pattern = declaration.child_by_field_name("pattern")
                value = declaration.child_by_field_name("value")
                if pattern is None or pattern.type != "identifier" or value is None:
                    continue
                name = _node_text(src, pattern)
                uses = _identifier_nodes(src, following, name)
                if len(uses) != 1 or any(
                    _identifier_nodes(src, later, name)
                    for later in statements[index + 2 :]
                ):
                    continue
                use, deferred_or_conditional = uses[0], False
                # `use is following` when the following statement is nothing
                # but a bare tail expression (`x` with no semicolon): the
                # walk below starts at `use.parent`, which is then the
                # *enclosing* block, not a block wrapping the use inside
                # `following` - the very case this walk exists to catch. No
                # ancestor walk is needed at all here: there is nothing
                # between the declaration and the use for a block, closure,
                # or loop to hide behind.
                if use is not following:
                    ancestor = use.parent
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
                    or (
                        value.start_point[0] == value.end_point[0]
                        and following.start_point[0] == following.end_point[0]
                        and (following.start_point[1] + len(replacement) > 88)
                    )
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
    for_stmt, for_stmt_idx = None, None
    for i, s in enumerate(stmts):
        if s.type == "expression_statement":
            inner = s.child(0)
            if inner and inner.type == "for_expression":
                for_stmt, for_stmt_idx = inner, i
                break
        elif s.type == "for_expression":
            for_stmt, for_stmt_idx = s, i
            break
    if for_stmt is None:
        return None
    pat = for_stmt.child_by_field_name("pattern")
    if pat is None or pat.type != "tuple_pattern":
        return None
    items = [c for c in pat.children if c.is_named]
    if len(items) != 2:
        return None
    i_name, x_name = _node_text(src, items[0]), _node_text(src, items[1])
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
    if (
        arr_method is None or _node_text(src, arr_method) != "iter"
    ) or arr_node is None:
        return None
    arr_text, body = _node_text(src, arr_node), for_stmt.child_by_field_name("body")
    if body is None or body.type != "block":
        return None
    bstmts = [c for c in body.children if c.is_named]
    if len(bstmts) != 2:
        return None
    # stmt 0: if i != 0 { out.push(SEP); }
    s0, s1 = bstmts
    if s0.type != "expression_statement" or s0.child(0).type != "if_expression":
        return None
    if_expr = s0.child(0)
    cond = if_expr.child_by_field_name("condition")
    cons = if_expr.child_by_field_name("consequence")
    if (cond is None or cond.type != "binary_expression") or (
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
    # stmt 1: out.push_str(EXPR)
    if (
        (push_fexpr is None or push_fexpr.type != "field_expression")
        or _node_text(src, push_fexpr.child_by_field_name("field")) != "push"
    ) or s1.type != "expression_statement":
        return None
    push_str_call = s1.child(0)
    if push_str_call.type != "call_expression":
        return None
    push_str_fexpr = push_str_call.child_by_field_name("function")
    if (
        push_str_fexpr is None or push_str_fexpr.type != "field_expression"
    ) or _node_text(src, push_str_fexpr.child_by_field_name("field")) != "push_str":
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
    proc_call = proc_call[1:].lstrip() if proc_call.startswith("&") else proc_call
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
    # SEP is a char; join needs &str. Use delimiter.to_string().
    sep_join = f"&{_node_text(src, sep_arg)}.to_string()"
    return (
        for_stmt,
        p2,
        f"{out_name} = {arr_text}.iter().map(|v| {proc_call}).collect::<Vec<_>>().join({sep_join});\n{out_name}.push({newline_text});\n{out_name}",
    )


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
    pat, iter_n = fs.child_by_field_name("pattern"), fs.child_by_field_name("value")
    if (pat is None or pat.type != "identifier") or (
        iter_n is None or iter_n.type != "call_expression"
    ):
        return None
    fexpr = iter_n.child_by_field_name("function")
    if fexpr is None or fexpr.type != "field_expression":
        return None
    iter_method = fexpr.child_by_field_name("field")
    if iter_method is None or _node_text(src, iter_method) != "chars":
        return None
    s_name = _node_text(src, fexpr.child_by_field_name("value"))
    loop_var, body = _node_text(src, pat), fs.child_by_field_name("body")
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
    # top-level must be `==` not `||` (we'll flatten below)
    if (inner_cond is None or inner_cond.type != "binary_expression") or _node_text(
        src, inner_cond.children[1]
    ) != "||":
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
        if r.type in {"char_literal", "identifier"}:
            chars.append(_node_text(src, r))
        else:
            return None
    # consequence must be a single `FLAG = true;` assignment
    cstmts = [c for c in inner_cons.children if c.is_named]
    if len(cstmts) != 1:
        return None
    a = cstmts[0]
    a = (a.child(0) if a.child(0) else None) if a.type == "expression_statement" else a
    if (a is None or a.type != "assignment_expression") or _node_text(
        src, a.children[1]
    ) != "=":
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
    count_name, fs = m.group(1), for_stmt
    if fs.type == "expression_statement":
        fs = fs.child(0) if fs.child(0) else None
    if fs is None or fs.type != "for_expression":
        return None
    pat, iter_n = fs.child_by_field_name("pattern"), fs.child_by_field_name("value")
    # only fire when the loop var is unused
    if pat is None or (_node_text(src, pat) != "_" or iter_n is None):
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
    if (inner is None or inner.type != "assignment_expression") or _node_text(
        src, inner.children[1]
    ) != "=":
        return None
    lhs, rhs = inner.children[0], inner.children[2]
    # rhs: `count + 1` (binary_expression with +)
    if (
        lhs.type != "identifier" or _node_text(src, lhs) != count_name
    ) or rhs.type != "binary_expression":
        return None
    if _node_text(src, rhs.children[1]) != "+" or (
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
    total_name, fs = m.group(1), for_stmt
    if fs.type == "expression_statement":
        fs = fs.child(0) if fs.child(0) else None
    if fs is None or fs.type != "for_expression":
        return None
    pat, iter_n = fs.child_by_field_name("pattern"), fs.child_by_field_name("value")
    if pat is None:
        return None
    x_name, body = _node_text(src, pat), fs.child_by_field_name("body")
    if body is None or body.type != "block":
        return None
    bstmts = [c for c in body.children if c.is_named]
    if len(bstmts) != 1:
        return None
    inner = bstmts[0]
    if inner.type == "expression_statement":
        inner = inner.child(0) if inner.child(0) else None
    if inner is None or inner.type not in (
        "assignment_expression",
        "compound_assignment_expr",
    ):
        return None
    operator = _node_text(src, inner.children[1])
    if operator not in ("=", "+="):
        return None
    lhs, rhs = inner.children[0], inner.children[2]
    if lhs.type != "identifier" or _node_text(src, lhs) != total_name:
        return None
    if operator == "+=":
        rhs_call = rhs.children[2] if rhs.type == "binary_expression" else rhs
        if rhs_call.type != "call_expression":
            return None
        fn_field = rhs_call.child_by_field_name("function")
        if fn_field is None or fn_field.type != "identifier":
            return None
        fn_name = _node_text(src, fn_field)
        fn_args = rhs_call.child_by_field_name("arguments")
        if fn_args is None:
            return None
        fn_args_text = _node_text(src, fn_args).strip()
        if fn_args_text not in (f"({x_name})", x_name):
            return None
        iter_text = _node_text(src, iter_n)
        return let_stmt, fs, f"let {total_name} = {iter_text}.map({fn_name}).sum();"
    if (rhs.type != "binary_expression" or _node_text(src, rhs.children[1]) != "+") or (
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
    if fn_args_text not in (f"({x_name})", x_name):
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
                start, end = let_stmt.start_point[0] + 1, for_stmt.end_point[0] + 1
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
                start, end = let_stmt.start_point[0] + 1, for_stmt.end_point[0] + 1
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
                start, end = prev.start_point[0] + 1, nxt.end_point[0] + 1
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
                start, end = fs.start_point[0] + 1, p2.end_point[0] + 1
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
    S may span lines: rustfmt re-wraps the canonical candidate, so a
    width-pinned site simply fails the per-candidate LOC check and stays.
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
                    text, inner = _node_text(src, stmt).strip(), stmt
                    if stmt.type == "expression_statement" and stmt.named_children:
                        inner = stmt.named_children[0]
                    ends_with_semicolon = text.endswith(";")
                    keeps_type = (
                        not ends_with_semicolon
                        or inner.type in _ARM_DIVERGING
                        or inner.type in _ARM_UNIT
                    )
                    if keeps_type:
                        if "\n" in text:
                            prefix, body = " " * stmt.start_point[1], text.splitlines()
                            text = "\n".join(
                                [body[0]] + [ln.removeprefix(prefix) for ln in body[1:]]
                            )
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
    "vec-push-run",
    "string-concat-run",
    "nested-if-collapse",
    "let-else",
    "any-flag-loop",
    "for-push-to-collect",
    "use-merge",
    "match-early-return-to-question",
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
    if "vec-push-run" in selected:
        rewrites.extend(_collect_vec_push_run(src_bytes, tree.root_node))
    if "string-concat-run" in selected:
        rewrites.extend(_collect_string_concat_run(src_bytes, tree.root_node))
    if "nested-if-collapse" in selected:
        rewrites.extend(_collect_nested_if_collapse(src_bytes, tree.root_node))
    if "let-else" in selected:
        rewrites.extend(_collect_let_else(src_bytes, tree.root_node))
    if "any-flag-loop" in selected:
        rewrites.extend(_collect_any_flag(src_bytes, tree.root_node))
    if "for-push-to-collect" in selected:
        rewrites.extend(_collect_for_push_collect(src_bytes, tree.root_node))
    if "use-merge" in selected:
        rewrites.extend(_collect_use_merge(src_bytes, tree.root_node))
    if "match-early-return-to-question" in selected:
        rewrites.extend(_collect_match_question(src_bytes, tree.root_node))
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
        candidate_text = "\n".join(candidate_lines) + (
            "\n" if source.endswith("\n") else ""
        )
        # A syntactically broken candidate would make the canonical formatter
        # reject it and `measure()` fall back to raw LOC - the exact
        # formatter-fallback masking class. Parse the candidate ourselves
        # before any LOC is counted.
        try:
            parsed_candidate = RS_PARSER.parse(candidate_text.encode("utf-8"))
        except Exception:  # noqa: BLE001, S112 - parser bindings may raise
            continue
        if parsed_candidate.root_node.has_error:
            continue
        # Removing a binding can make rustfmt wrap its use site onto more
        # lines. Keep each candidate only when it independently lowers the
        # canonical metric, so a wide inline cannot consume savings from the
        # useful rewrites elsewhere in the same file.
        from .loc import measure

        before_text = "\n".join(lines) + ("\n" if source.endswith("\n") else "")
        if measure(candidate_text, "rust").code >= measure(before_text, "rust").code:
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
    return (source, []) if parsed.root_node.has_error else (new_source, applied)


# ---- Phase 3 rule families (census-gated at merge time) ----


def _collect_vec_push_run(src: bytes, root):
    """`let mut v = Vec::new(); v.push(A); v.push(B);` -> `let v = vec![A, B];`
    when v has no other pre-use mutation and the element type is inferable."""
    out = []

    def visit(node):
        if node.type == "block":
            statements = [c for c in node.named_children if c.type != "line_comment"]
            for index in range(len(statements) - 2):
                decl = statements[index]
                if decl.type != "let_declaration":
                    continue
                decl_text = _node_text(src, decl)
                if not re.match(
                    r"\s*let\s+mut\s+(\w+)\s*(?::\s*Vec<[^>]+>)?\s*=\s*Vec::new\(\)\s*;",
                    decl_text,
                ):
                    continue
                name = re.match(r"\s*let\s+mut\s+(\w+)", decl_text).group(1)
                pushes, ok = [], True
                for follow in statements[index + 1 : index + 3]:
                    inner = follow
                    if inner.type == "expression_statement":
                        inner = (
                            inner.named_children[0] if inner.named_children else None
                        )
                    if inner is None or inner.type != "call_expression":
                        ok = False
                        break
                    function = inner.child_by_field_name("function")
                    arguments = inner.child_by_field_name("arguments")
                    if function is None or function.type != "field_expression":
                        ok = False
                        break
                    receiver = function.child_by_field_name("value")
                    method = function.child_by_field_name("field")
                    if (
                        receiver is None
                        or _node_text(src, receiver) != name
                        or method is None
                        or _node_text(src, method) != "push"
                        or arguments is None
                    ):
                        ok = False
                        break
                    args = list(arguments.named_children)
                    if len(args) != 1:
                        ok = False
                        break
                    pushes.append(_node_text(src, args[0]))
                if not ok or len(pushes) != 2:
                    continue
                # no other mutation or use between the decl and the pushes
                between = statements[index + 1 : index + 3]
                later_uses = [
                    s
                    for s in statements[index + 3 :]
                    if re.search(rf"\b{name}\b", _node_text(src, s))
                ]
                if any(
                    re.search(
                        rf"\b{name}\.(push|pop|insert|clear|extend|append|retain|drain|truncate|split_off|resize|swap|dedup|sort|reverse)\b",
                        _node_text(src, s),
                    )
                    for s in later_uses
                ):
                    continue
                if any(
                    _node_text(src, s).strip().startswith(f"{name}.")
                    and not re.match(rf"\s*{name}\.push\(", _node_text(src, s))
                    for s in between
                ):
                    continue
                end, start = between[-1].end_point[0] + 1, decl.start_point[0] + 1
                replacement = f"let {name} = vec![{pushes[0]}, {pushes[1]}];"
                out.append((start, end, replacement, "vec-push-run"))
                return
        for child in node.named_children:
            visit(child)

    visit(root)
    return out


def _collect_string_concat_run(src: bytes, root):
    """`x.push_str("a"); x.push_str("b");` -> one push_str of the adjacent
    literals (adjacent string literals only)."""
    out = []

    def visit(node):
        if node.type == "block":
            statements = [c for c in node.named_children if c.type != "line_comment"]
            for index in range(len(statements) - 1):
                first, second, parts = statements[index], statements[index + 1], []
                for stmt in (first, second):
                    inner = stmt
                    if inner.type == "expression_statement":
                        inner = (
                            inner.named_children[0] if inner.named_children else None
                        )
                    if inner is None or inner.type != "call_expression":
                        parts = []
                        break
                    function = inner.child_by_field_name("function")
                    arguments = inner.child_by_field_name("arguments")
                    if (
                        function is None
                        or function.type != "field_expression"
                        or _node_text(
                            src, function.child_by_field_name("field") or function
                        )
                        != "push_str"
                        or arguments is None
                    ):
                        parts = []
                        break
                    args = list(arguments.named_children)
                    if len(args) != 1 or args[0].type != "string_literal":
                        parts = []
                        break
                    parts.append((_node_text(src, args[0]), inner))
                if len(parts) != 2:
                    continue
                (a_text, a_node), (b_text, b_node) = parts
                receiver_a = a_node.child_by_field_name("function").child_by_field_name(
                    "value"
                )
                receiver_b = b_node.child_by_field_name("function").child_by_field_name(
                    "value"
                )
                # adjacent string literals concatenate exactly like run-time
                # push_str when neither literal has a raw prefix or escapes
                # that re-split; keep it to plain "..." literals
                if _node_text(src, receiver_a) != _node_text(src, receiver_b) or not (
                    a_text.startswith('"') and b_text.startswith('"')
                ):
                    continue
                merged = a_text[:-1] + b_text[1:]
                replacement = f"{_node_text(src, receiver_a)}.push_str({merged});"
                out.append(
                    (
                        first.start_point[0] + 1,
                        second.end_point[0] + 1,
                        replacement,
                        "string-concat-run",
                    )
                )
                return
        for child in node.named_children:
            visit(child)

    visit(root)
    return out


def _collect_nested_if_collapse(src: bytes, root):
    """`if a { if b { X } else { Y } } else { Z }` -> guard merge `if a && b`
    when the canonical width permits (the unbrace-match-arm width lesson
    generalizes: rustfmt may re-split the merged condition)."""
    out = []

    def visit(node):
        if node.type == "if_expression":
            condition = node.child_by_field_name("condition")
            consequence = node.child_by_field_name("consequence")
            alternative = node.child_by_field_name("alternative")
            if (
                condition is None
                or consequence is None
                or consequence.type != "block"
                or len(consequence.named_children) != 1
            ):
                for child in node.named_children:
                    visit(child)
                return
            inner_stmt = consequence.named_children[0]
            if inner_stmt.type == "expression_statement":
                inner_stmt = (
                    inner_stmt.named_children[0] if inner_stmt.named_children else None
                )
            if inner_stmt is None or inner_stmt.type != "if_expression":
                for child in node.named_children:
                    visit(child)
                return
            inner_condition = inner_stmt.child_by_field_name("condition")
            inner_consequence = inner_stmt.child_by_field_name("consequence")
            inner_alternative = inner_stmt.child_by_field_name("alternative")
            # both branches must agree: an else on the outer requires an
            # else on the inner and vice versa
            # comments inside either block block the merge (named-sibling rule)
            if (alternative is None) != (inner_alternative is None) or any(
                c.type in {"line_comment", "block_comment"}
                for c in list(consequence.children) + list(inner_consequence.children)
            ):
                for child in node.named_children:
                    visit(child)
                return
            merged = (
                f"{_node_text(src, condition)} && ({_node_text(src, inner_condition)})"
                if any(op in _node_text(src, inner_condition) for op in ("&&", "||"))
                else f"{_node_text(src, condition)} && {_node_text(src, inner_condition)}"
            )
            consequence_text, alternative_text = _node_text(src, inner_consequence), ""
            if inner_alternative is not None:
                alt_block = (
                    next(
                        (
                            c
                            for c in inner_alternative.named_children
                            if c.type == "block"
                        ),
                        None,
                    )
                    if inner_alternative.type == "else_clause"
                    else inner_alternative
                )
                if alt_block is None:
                    for child in node.named_children:
                        visit(child)
                    return
                alternative_text = " else " + _node_text(src, alt_block)
            replacement = f"if {merged} {consequence_text}{alternative_text}"
            out.append(
                (
                    node.start_point[0] + 1,
                    node.end_point[0] + 1,
                    replacement,
                    "nested-if-collapse",
                )
            )
            return
        for child in node.named_children:
            visit(child)

    visit(root)
    return out


def _collect_let_else(src: bytes, root):
    """`if let PAT = e { BODY } else { diverging; }` -> `let PAT = e else
    { ... };` when the then-branch binds and simply proceeds."""
    out = []

    def visit(node):
        if node.type == "if_expression":
            condition = node.child_by_field_name("condition")
            consequence = node.child_by_field_name("consequence")
            alternative = node.child_by_field_name("alternative")
            if (
                condition is None
                or condition.type != "let_condition"
                or consequence is None
                or consequence.type != "block"
                or alternative is None
                or alternative.type != "else_clause"
            ):
                for child in node.named_children:
                    visit(child)
                return
            else_block = next(
                (c for c in alternative.named_children if c.type == "block"), None
            )
            if else_block is None:
                for child in node.named_children:
                    visit(child)
                return
            alt_inner = [
                c for c in else_block.named_children if c.type != "line_comment"
            ]
            if len(alt_inner) != 1:
                for child in node.named_children:
                    visit(child)
                return
            diverging = alt_inner[0]
            diverging_inner = diverging
            if diverging.type == "expression_statement":
                diverging_inner = (
                    diverging.named_children[0] if diverging.named_children else None
                )
            if diverging_inner is None or diverging_inner.type not in {
                "return_expression",
                "break_expression",
                "continue_expression",
            }:
                for child in node.named_children:
                    visit(child)
                return
            match = re.match(r"\s*let\s+(.*)", _node_text(src, condition), re.DOTALL)
            if not match:
                for child in node.named_children:
                    visit(child)
                return
            binding, body = match.group(1).strip(), _node_text(src, consequence)
            else_body = _node_text(src, else_block)
            replacement = f"let {binding} else {else_body};\n{body}"
            out.append(
                (
                    node.start_point[0] + 1,
                    node.end_point[0] + 1,
                    replacement,
                    "let-else",
                )
            )
            return
        for child in node.named_children:
            visit(child)

    visit(root)
    return out


def _try_any_flag(src: bytes, block):
    """`let mut found = false; for x in it { if cond(x) { found = true; } }`
    -> `let found = it.any(|x| cond(x));`

    The false init IS the identity of `any`, so empty-iteration behavior is
    identical. The loop variable must not be used after the loop, the body
    must be exactly one `if cond { found = true; }` (no else), and the flag
    must not be mutated anywhere else."""
    stmts = [c for c in block.named_children if c.type != "line_comment"]
    if len(stmts) < 2:
        return None
    let_stmt, loop_stmt = stmts[0], stmts[1]
    let_text = _node_text(src, let_stmt)
    match = re.match(r"\s*let\s+mut\s+(\w+)\s*(?::\s*bool\s*)?=\s*false\s*;", let_text)
    if not match:
        return None
    flag = match.group(1)
    # reads after the loop are fine (the tail expression usually reads it);
    # a re-assignment would not be preserved by the rewrite
    if any(re.search(rf"\b{flag}\s*=(?!=)", _node_text(src, s)) for s in stmts[2:]):
        return None
    fs = loop_stmt
    if fs.type == "expression_statement":
        fs = fs.named_children[0] if fs.named_children else None
    if fs is None or fs.type != "for_expression":
        return None
    pattern = fs.child_by_field_name("pattern")
    iterable, body = fs.child_by_field_name("value"), fs.child_by_field_name("body")
    if pattern is None or pattern.type != "identifier" or body is None:
        return None
    var = _node_text(src, pattern)
    # the loop variable must not be used after the loop
    if any(re.search(rf"\b{var}\b", _node_text(src, s)) for s in stmts[2:]):
        return None
    body_stmts = [c for c in body.named_children if c.type != "line_comment"]
    if len(body_stmts) != 1:
        return None
    inner = body_stmts[0]
    if inner.type == "expression_statement":
        inner = inner.named_children[0] if inner.named_children else None
    if (inner is None or inner.type != "if_expression") or inner.child_by_field_name(
        "alternative"
    ) is not None:
        return None
    condition = inner.child_by_field_name("condition")
    consequence = inner.child_by_field_name("consequence")
    if condition is None or consequence is None:
        return None
    consequence_stmts = [
        c for c in consequence.named_children if c.type != "line_comment"
    ]
    if len(consequence_stmts) != 1:
        return None
    assignment_text = _node_text(src, consequence_stmts[0])
    if not re.match(rf"\s*{flag}\s*=\s*true\s*;", assignment_text):
        return None
    cond_text, iter_text = _node_text(src, condition), _node_text(src, iterable)
    closure_var = var if var != "_" else "item"
    cond_subst = re.sub(rf"\b{var}\b", closure_var, cond_text)
    return (
        let_stmt.start_point[0] + 1,
        loop_stmt.end_point[0] + 1,
        f"let {flag} = {iter_text}.any(|{closure_var}| {cond_subst});",
        "any-flag-loop",
    )


def _collect_any_flag(src: bytes, root):
    out = []

    def visit(node):
        if node.type == "block":
            r = _try_any_flag(src, node)
            if r is not None:
                out.append(r)
                return
        for child in node.named_children:
            visit(child)

    visit(root)
    return out


_PURE_VEC_INIT = re.compile(r"\s*Vec::new\(\)\s*|\s*vec!\[\]\s*|\s*Vec::default\(\)\s*")

_DIVERGENT_IN_CLOSURE = {
    "return_expression",
    "break_expression",
    "continue_expression",
    "await_expression",
    "try_expression",
    "yield_expression",
    "loop_expression",
    "while_expression",
    "for_expression",
    "closure_expression",
    "async_block",
}

_VEC_MUTATORS = (
    "push",
    "push_str",
    "extend",
    "clear",
    "truncate",
    "retain",
    "append",
    "insert",
    "remove",
    "swap_remove",
    "pop",
    "drain",
    "resize",
    "reserve",
)


def _comments_only_in(span_node, keep_nodes) -> bool:
    """True when every comment inside span_node's byte range also lies
    inside one of keep_nodes (whose text the rewrite reuses verbatim).
    Anything else would detach or drop documentation."""
    keep = [(n.start_byte, n.end_byte) for n in keep_nodes if n is not None]
    stack = [span_node]
    while stack:
        node = stack.pop()
        if node.type in ("line_comment", "block_comment") and not any(
            start <= node.start_byte and node.end_byte <= end for start, end in keep
        ):
            return False
        stack.extend(node.children)
    return True


def _anchored_hunk(src: bytes, start_node, end_node, new_text: str, rule: str):
    """Whole-line hunk bounds that preserve in-line code around the node
    span: the leading indent is left for apply_rules' own padding, while
    any other same-line prefix/suffix rides inside the replacement."""
    line_start = src.rfind(b"\n", 0, start_node.start_byte) + 1
    line_end = src.find(b"\n", end_node.end_byte)
    if line_end == -1:
        line_end = len(src)
    prefix = src[line_start : start_node.start_byte].decode("utf-8", "replace")
    suffix = src[end_node.end_byte : line_end].decode("utf-8", "replace")
    stripped = prefix[len(prefix) - len(prefix.lstrip(" \t")) :]
    return (
        start_node.start_point[0] + 1,
        end_node.end_point[0] + 1,
        stripped + new_text + suffix,
        rule,
    )


_CLOSURE_BREAKERS = {
    "return_expression",
    "break_expression",
    "continue_expression",
    "await_expression",
    "try_expression",
    "yield_expression",
}


def _has_closure_breakers(node) -> bool:
    """True when moving this expression inside a closure would change
    control flow (early exits and awaits belong to the enclosing body)."""
    stack = [node]
    while stack:
        current = stack.pop()
        if current.type in _CLOSURE_BREAKERS:
            return True
        stack.extend(current.named_children)
    return False


def _pattern_binders(src: bytes, pattern) -> set[str]:
    """All identifier bindings in a (possibly destructuring) pattern."""
    binders = set()
    stack = [pattern]
    while stack:
        node = stack.pop()
        if node.type == "identifier":
            binders.add(_node_text(src, node))
        stack.extend(node.named_children)
    return binders


def _try_for_push_collect(src: bytes, block):
    """`let mut acc[: T] = Vec::new(); for P in ITER { acc.push(E) }` ->
    `let acc[: T] = ITER.into_iter().map(|P| E).collect();` when the pushed
    expression mentions the loop binder, performs no divergent/side-channel
    control flow, and the accumulator is only read afterwards."""
    statements = list(block.named_children)
    for index, (declaration, following) in enumerate(pairwise(statements)):
        if declaration.type != "let_declaration":
            continue
        pattern = declaration.child_by_field_name("pattern")
        value = declaration.child_by_field_name("value")
        annotation = declaration.child_by_field_name("type")
        if pattern is None or pattern.type != "identifier" or value is None:
            continue
        if not _PURE_VEC_INIT.fullmatch(_node_text(src, value)):
            continue
        name = _node_text(src, pattern)
        loop = following
        if loop.type == "expression_statement":
            loop = loop.named_children[0] if loop.named_children else None
        if loop is None or loop.type != "for_expression":
            continue
        loop_pattern = loop.child_by_field_name("pattern")
        iterable = loop.child_by_field_name("value")
        body = loop.child_by_field_name("body")
        if loop_pattern is None or iterable is None or body is None:
            continue
        if not _comments_only_in(declaration, [annotation]):
            continue
        body_stmts = list(body.named_children)
        if len(body_stmts) != 1:
            continue
        pushed = body_stmts[0]
        if pushed.type == "expression_statement":
            pushed = pushed.named_children[0] if pushed.named_children else None
        if pushed is None or pushed.type != "call_expression":
            continue
        function = pushed.child_by_field_name("function")
        arguments = pushed.child_by_field_name("arguments")
        if function is None or function.type != "field_expression" or arguments is None:
            continue
        receiver = function.child_by_field_name("value")
        method = function.child_by_field_name("field")
        args = arguments.named_children
        if (
            receiver is None
            or receiver.type != "identifier"
            or _node_text(src, receiver) != name
            or method is None
            or _node_text(src, method) != "push"
            or len(args) != 1
        ):
            continue
        if not _comments_only_in(following, [iterable, loop_pattern, args[0]]):
            continue
        pushed_text = _node_text(src, args[0])
        binders = _pattern_binders(src, loop_pattern)
        if not any(
            re.search(rf"\b{re.escape(binder)}\b", pushed_text)
            for binder in binders
            if binder != "_"
        ) and not (len(binders) == 1 and "_" in binders):
            continue
        if re.search(rf"\b{re.escape(name)}\b", pushed_text):
            continue
        refused, stack = False, [args[0]]
        while stack:
            node = stack.pop()
            if node.type in _DIVERGENT_IN_CLOSURE:
                refused = True
                break
            stack.extend(node.named_children)
        if refused:
            continue
        # comments between the declaration and the loop would be detached
        gap = src[declaration.end_byte : following.start_byte]
        if b"//" in gap or b"/*" in gap:
            continue
        # reads after the loop are fine; later mutation is not preserved
        escaped = re.escape(name)
        for later in statements[index + 2 :]:
            later_text = _node_text(src, later)
            if re.search(
                rf"\b{escaped}\s*\.\s*(?:{'|'.join(_VEC_MUTATORS)})\s*\(|\b{escaped}\s*=(?![=>])|&mut\s+{escaped}\b",
                later_text,
            ):
                break
        else:
            annotation_text = (
                f": {_node_text(src, annotation)}" if annotation is not None else ""
            )
            # into_iter() mirrors the for-loop desugar exactly: the same
            # head expression works for slices, ranges, maps, and existing
            # iterators alike (a bare .map() would not exist on slices).
            replacement = (
                f"let {name}{annotation_text} = "
                f"{_node_text(src, iterable)}.into_iter().map(|{_node_text(src, loop_pattern)}| "
                f"{pushed_text}).collect();"
            )
            return _anchored_hunk(
                src, declaration, following, replacement, "for-push-to-collect"
            )
    return None


def _collect_for_push_collect(src: bytes, root):
    out = []

    def visit(node):
        if node.type == "block":
            r = _try_for_push_collect(src, node)
            if r is not None:
                out.append(r)
                return
        for child in node.named_children:
            visit(child)

    visit(root)
    return out


def _use_head_alternatives(src: bytes, tree) -> tuple | None:
    """(head, [alternative texts]) for a use-tree node, or None when the
    shape is too exotic to merge. `use a;` contributes a `self`
    alternative so it merges with `use a::b;` into `use a::{self, b};`."""
    text = _node_text(src, tree)
    if tree.type == "identifier":
        return text, ["self"]
    if tree.type == "use_wildcard":
        inner = tree.named_children[0] if tree.named_children else None
        if inner is None:
            return None
        return _node_text(src, inner), ["*"]
    if tree.type == "use_as_clause":
        path = tree.named_children[0] if tree.named_children else None
        if path is None:
            return None
        head, _ = _use_head_alternatives(src, path) or (None, None)
        if head is None:
            return None
        return head, [text.split("::")[-1]]
    if tree.type == "scoped_use_list":
        path = tree.named_children[0] if tree.named_children else None
        items = tree.named_children[-1] if len(tree.named_children) > 1 else None
        if path is None or items is None or items.type != "use_list":
            return None
        return _node_text(src, path), [_node_text(src, c) for c in items.named_children]
    if tree.type == "scoped_identifier":
        segments, node = [], tree
        while node.type == "scoped_identifier" and len(node.named_children) == 2:
            segments.append(_node_text(src, node.named_children[1]))
            node = node.named_children[0]
        if node.type not in ("identifier", "crate", "self", "super"):
            return None
        segments.append(_node_text(src, node))
        segments.reverse()
        if len(segments) < 2:
            return None
        return "::".join(segments[:-1]), [segments[-1]]
    return None


def _collect_use_merge(src: bytes, root):
    """Merge adjacent same-prefix imports: `use a::b; use a::c;` ->
    `use a::{b, c};`. Runs form only across lines with pure-whitespace
    gaps (attributes and comments break the run), and duplicate
    alternatives collapse so the merge can never introduce E0252."""
    out = []

    def runs(parent):
        items = list(parent.named_children)
        index = 0
        while index < len(items):
            node = items[index]
            if node.type != "use_declaration":
                index += 1
                continue
            group = [node]
            index += 1
            while index < len(items) and items[index].type == "use_declaration":
                gap = src[items[index - 1].end_byte : items[index].start_byte]
                if b"#" in gap or b"/" in gap:
                    break
                group.append(items[index])
                index += 1
            if len(group) > 1:
                yield group

    def visit(node):
        for group in runs(node):
            parsed = []
            for item in group:
                children = [
                    c for c in item.named_children if c.type != "visibility_modifier"
                ]
                if len(children) != 1:
                    break
                vis = next(
                    (
                        _node_text(src, c)
                        for c in item.named_children
                        if c.type == "visibility_modifier"
                    ),
                    "",
                )
                split = _use_head_alternatives(src, children[0])
                if split is None:
                    break
                parsed.append((vis, split[0], split[1], item))
            else:
                # only contiguous same-key entries merge: a different import
                # sitting between two same-prefix ones must survive
                runs2, current = [], []
                for entry in parsed:
                    if current and current[-1][:2] != entry[:2]:
                        runs2.append(current)
                        current = []
                    current.append(entry)
                runs2.append(current)
                for run in runs2:
                    if len(run) < 2:
                        continue
                    vis, head = run[0][:2]
                    if not head:
                        continue
                    seen, merged = set(), []
                    for _, _, alts, _ in run:
                        for alt in alts:
                            if alt not in seen:
                                seen.add(alt)
                                merged.append(alt)
                    if len(merged) < 2:
                        continue
                    first, last = run[0][3], run[-1][3]
                    if not all(_comments_only_in(item, []) for _, _, _, item in run):
                        continue
                    prefix = f"{vis} " if vis else ""
                    out.append(
                        _anchored_hunk(
                            src,
                            first,
                            last,
                            f"{prefix}use {head}::{{{', '.join(merged)}}};",
                            "use-merge",
                        )
                    )
        for child in node.named_children:
            visit(child)

    visit(root)
    return out


_BINDING = r"[A-Za-z_][A-Za-z0-9_]*"
_OK_ERR_PATTERN = re.compile(rf"^(Ok|Err|Some)\(({_BINDING})\)$")


def _unwrap_value(node):
    """A match-arm value with statement wrappers removed. A trailing
    semicolon means the arm yields unit, never the expression, so blocks
    ending in `;` unwrap to nothing."""
    while node is not None and node.type == "expression_statement":
        node = node.named_children[0] if node.named_children else None
    if node is not None and node.type == "block":
        inner = [
            c
            for c in node.named_children
            if c.type not in ("line_comment", "block_comment")
        ]
        if len(inner) != 1 or inner[0].type == "expression_statement":
            return None
        node = inner[0]
    return node


def _try_match_question(src: bytes, node):
    """`match R { Ok(v) => v, Err(e) => return Err(e) }` -> `R?`, and the
    Option analogues (`None => return None` -> `?`, `None => return Err(X)`
    -> `ok_or_else`). Every non-success arm must diverge with `return`;
    guards are refused outright, so control flow cannot change shape."""
    scrutinee = node.child_by_field_name("value")
    body = node.child_by_field_name("body")
    if scrutinee is None or body is None or body.type != "match_block":
        return None
    if scrutinee.type not in ("identifier", "field_expression", "call_expression"):
        return None
    if _node_text(src, scrutinee).lstrip().startswith("&"):
        return None
    arms = [c for c in body.named_children if c.type == "match_arm"]
    if len(arms) != 2:
        return None
    parsed = []
    for arm in arms:
        pattern = arm.child_by_field_name("pattern")
        value = arm.child_by_field_name("value")
        if pattern is None or value is None:
            return None
        if any(c.type == "match_guard" for c in arm.named_children):
            return None
        match = _OK_ERR_PATTERN.fullmatch(_node_text(src, pattern).strip())
        if match is None:
            # only a bare None arm is allowed besides Ok/Err/Some
            if _node_text(src, pattern).strip() != "None":
                return None
            parsed.append((None, None, arm))
            continue
        parsed.append((match.group(1), match.group(2), arm))
    kinds = sorted(p[0] if p[0] is not None else "None" for p in parsed)
    success, failure = None, None
    for kind, binding, arm in parsed:
        if kind in ("Ok", "Some"):
            success = (kind, binding, arm)
        elif kind in ("Err", None):
            failure = (kind, binding, arm)
    if success is None or failure is None:
        return None
    if kinds not in (["Err", "Ok"], ["None", "Some"]):
        return None
    success_value = _unwrap_value(success[2].child_by_field_name("value"))
    if success_value is None or _node_text(src, success_value) != success[1]:
        return None
    failure_value = _unwrap_value(failure[2].child_by_field_name("value"))
    if failure_value is None or failure_value.type != "return_expression":
        return None
    returned = failure_value.named_children[0] if failure_value.named_children else None
    if returned is None:
        return None
    scrutinee_text = _node_text(src, scrutinee)
    if not _comments_only_in(node, [scrutinee]):
        return None
    if _has_closure_breakers(returned):
        return None
    if success[0] == "Ok":
        # only an Err-constructing payload maps onto `?`/`map_err`; a bare
        # `return foo(e)` returns a whole new result, not a mapped error
        if returned.type != "call_expression":
            return None
        function = returned.child_by_field_name("function")
        arguments = returned.child_by_field_name("arguments")
        if function is None or arguments is None:
            return None
        if (
            _node_text(src, function).strip().split("::")[-1] != "Err"
            or len(arguments.named_children) != 1
        ):
            return None
        inner = _node_text(src, arguments.named_children[0])
        binding = failure[1]
        if inner == binding or re.fullmatch(
            rf"{re.escape(binding)}\s*\.\s*into\s*\(\s*\)", inner
        ):
            replacement = f"{scrutinee_text}?"
        elif re.search(rf"\b{re.escape(binding)}\b", inner):
            replacement = f"{scrutinee_text}.map_err(|{binding}| {inner})?"
        else:
            # the original error is discarded; name the closure parameter
            # `_` so it cannot warn as unused
            replacement = f"{scrutinee_text}.map_err(|_| {inner})?"
        return _anchored_hunk(
            src, node, node, replacement, "match-early-return-to-question"
        )
    # Option scrutinee
    if returned.type != "identifier" or _node_text(src, returned) != "None":
        error_call = _node_text(src, returned)
        error_match = re.fullmatch(r"Err\((.*)\)", error_call, re.DOTALL)
        if error_match is None:
            return None
        payload = error_match.group(1).strip()
        if re.fullmatch(
            rf"{_BINDING}|-?\d+|\"(?:[^\"\\]|\\.)*\"|'.'"
            rf"|(?:[A-Za-z_][A-Za-z0-9_]*\s*\.\s*)+{_BINDING}",
            payload,
        ):
            # pure payloads evaluate eagerly with no observable difference
            replacement = f"{scrutinee_text}.ok_or({payload})?"
        else:
            replacement = f"{scrutinee_text}.ok_or_else(|| {payload})?"
    else:
        replacement = f"{scrutinee_text}?"
    return _anchored_hunk(
        src, node, node, replacement, "match-early-return-to-question"
    )


def _collect_match_question(src: bytes, root):
    out = []

    def visit(node):
        if node.type == "match_expression":
            r = _try_match_question(src, node)
            if r is not None:
                out.append(r)
                return
        for child in node.named_children:
            visit(child)

    visit(root)
    return out
