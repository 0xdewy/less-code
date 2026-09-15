"""Differential oracle for Rust rewrites: run the original body alongside.

Rust analog of `shadow.py`, with a deliberately bounded v1 scope: only free
`function_item`s whose parameters and return type are all on a whitelist of
plain data types can be verified. Everything else a rewrite touched is
counted as unverified - unverifiable is never a mismatch, the oracle may
only reject more, never accept more.

Mechanism per checked function F: the pristine item is appended to the same
file inside `#[cfg(test)] mod __lc_shadow` as `__lc_orig_F` (`use super::*`
gives it the same types and the rewritten siblings - cumulative semantics,
same as the Python oracle). A generated test drives both bodies with
deterministic inputs and compares `format!("{:?}")` of the results; a
one-sided panic or value mismatch prints `__LC_SHADOW_MISMATCH` and fails.
The appended module is removed afterwards no matter the outcome.
"""

from __future__ import annotations

import itertools
import os
import re
import tempfile
import time
from pathlib import Path

from .shadow import ShadowReport
from .testrunners import run_tests

_MARK = "__LC_SHADOW_MISMATCH name=(\\S+) input=(.*)"
_VERIFIED_RE = "__LC_SHADOW_VERIFIED name=(\\S+) cases=(\\d+)"

_INT_TYPES = (
    "u8",
    "u16",
    "u32",
    "u64",
    "u128",
    "i8",
    "i16",
    "i32",
    "i64",
    "i128",
    "usize",
    "isize",
)

_STR_VALUES = [
    '""',
    '"0"',
    '" "',
    '"abc"',
    '"\\t\\n"',
    '"é🦉"',
    '"' + " " * 20 + '"',
    '"' + "a" * 100 + '"',
]

_CHAR_VALUES = ["'a'", "'Z'", "'0'", "' '", "'\\n'", "'µ'", "'🦉'"]

_DURATION_VALUES = [
    "Duration::ZERO",
    "Duration::from_secs(0)",
    "Duration::from_secs(1)",
    "Duration::from_millis(1500)",
    "Duration::from_secs(86_400)",
    "Duration::from_secs(u64::MAX)",
]


def _int_values(name: str) -> list[str]:
    return [
        f"0{name}",
        f"1{name}",
        f"2{name}",
        f"3{name}",
        f"{name}::MAX",
        f"{name}::MAX - 1",
    ]


# ---- eligibility: type whitelist, parsed from tree-sitter nodes ----


def _classify(node, src: bytes) -> tuple | None:
    """Whitelist descriptor for a type node, or None when refused."""
    kind = node.type
    text = src[node.start_byte : node.end_byte].decode()
    if kind == "primitive_type":
        if text in ("bool", "char"):
            return ("scalar", text)
        if text in _INT_TYPES:
            return ("scalar", text)
        if text == "str":
            return ("str",)
        return None  # f32, f64: NaN breaks exact comparison
    if kind == "reference_type":
        inner = None
        for child in node.named_children:
            if child.type == "lifetime":
                continue
            if child.type == "mutable_specifier":
                return None  # no &mut parameters
            if inner is not None:
                return None
            inner = _classify(child, src)
        if inner is None:
            return None
        return ("ref", inner, text)
    if kind == "type_identifier":
        if text == "String":
            return ("string",)
        if text == "Duration":
            return ("duration",)
        return None
    if kind == "scoped_type_identifier":
        last = node.named_children[-1] if node.named_children else None
        if last is not None:
            name = src[last.start_byte : last.end_byte].decode()
            if name in ("String", "Duration"):
                return ("string",) if name == "String" else ("duration",)
        return None
    if kind == "generic_type":
        base = node.child_by_field_name("type")
        args = node.child_by_field_name("arguments")
        if base is None or args is None:
            return None
        name = src[base.start_byte : base.end_byte].decode()
        arg_nodes = args.named_children
        if name == "Option" and len(arg_nodes) == 1:
            inner = _classify(arg_nodes[0], src)
            return None if inner is None else ("option", inner, text)
        if name == "Vec" and len(arg_nodes) == 1:
            inner = _classify(arg_nodes[0], src)
            return None if inner is None else ("vec", inner, text)
        if name == "Result" and len(arg_nodes) == 2:
            ok_t = _classify(arg_nodes[0], src)
            err_t = _classify(arg_nodes[1], src)
            if ok_t is None or err_t is None:
                return None
            return ("result", ok_t, err_t, text)
        return None
    if kind == "tuple_type":
        fields = [_classify(child, src) for child in node.named_children]
        if any(f is None for f in fields) or not fields or len(fields) > 3:
            return None
        return ("tuple", fields, text)
    if kind == "unit_type":
        return ("unit",)
    if kind == "array_type":
        element = node.child_by_field_name("element")
        length = node.child_by_field_name("length")
        inner = _classify(element, src) if element is not None else None
        if inner is None or length is None:
            return None
        try:
            count = int(src[length.start_byte : length.end_byte])
        except ValueError:
            return None
        if not 1 <= count <= 4:
            return None
        return ("array", inner, count, text)
    return None


def _param_ok(desc: tuple) -> bool:
    """Parameters exclude Result and () (return-only whitelist entries)."""
    kind = desc[0]
    if kind in ("result", "unit"):
        return False
    if kind == "ref":
        return _param_ok(desc[1])
    if kind in ("option", "vec"):
        return _param_ok(desc[1])
    if kind == "array":
        return _param_ok(desc[1])
    if kind == "tuple":
        return all(_param_ok(field) for field in desc[1])
    return True


def _eligible(item, src: bytes) -> tuple[bool, list[tuple], tuple | None]:
    """(eligible, param descriptors, return descriptor) for one function_item."""
    text = src[item.start_byte : item.end_byte]
    if re.search(rb"\b(?:unsafe|Self|async|extern)\b", text):
        return False, [], None
    type_parameters = item.child_by_field_name("type_parameters")
    if type_parameters is not None and any(
        child.type != "lifetime_parameter" for child in type_parameters.named_children
    ):
        return False, [], None
    if item.child_by_field_name("where_clause") is not None:
        return False, [], None
    params = []
    parameters = item.child_by_field_name("parameters")
    if parameters is None:
        return False, [], None
    for child in parameters.named_children:
        if child.type == "self_parameter":
            return False, [], None
        if child.type != "parameter":
            return False, [], None
        pattern = child.child_by_field_name("pattern")
        type_node = child.child_by_field_name("type")
        if pattern is None or pattern.type != "identifier" or type_node is None:
            return False, [], None
        desc = _classify(type_node, src)
        if desc is None or not _param_ok(desc):
            return False, [], None
        params.append(desc)
    return_node = item.child_by_field_name("return_type")
    ret = _classify(return_node, src) if return_node is not None else ("unit",)
    if ret is None:
        return False, [], None
    return True, params, ret


# ---- deterministic input generation ----


def _values(desc: tuple) -> list[str]:
    kind = desc[0]
    if kind == "local":
        # receivers and local-typed inputs re-construct instead of cloning:
        # the construction expression is pure, so re-evaluation is the copy
        return [desc[2]["construct"]]
    if kind == "scalar":
        if desc[1] == "bool":
            return ["true", "false"]
        if desc[1] == "char":
            return list(_CHAR_VALUES)
        return _int_values(desc[1])
    if kind == "str":
        return list(_STR_VALUES)
    if kind == "string":
        return [f"{value}.to_string()" for value in _STR_VALUES]
    if kind == "duration":
        return list(_DURATION_VALUES)
    if kind == "unit":
        return ["()"]
    if kind == "ref":
        if desc[1][0] == "str":
            return list(_STR_VALUES)  # a &str parameter receives literals
        return ["&" + value for value in _values(desc[1])[:6]]
    if kind == "option":
        return [f"None::<{desc[2]}>"] + [
            f"Some({value})" for value in _values(desc[1])[:3]
        ]
    if kind == "vec":
        base = _values(desc[1])
        return [
            "vec![]",
            f"vec![{base[0]}]",
            f"vec![{base[0]}, {base[1 % len(base)]}, {base[2 % len(base)]}]",
        ]
    if kind == "array":
        base = _values(desc[1])
        return [
            "[" + ", ".join(base[index % len(base)] for index in range(desc[2])) + "]",
            "["
            + ", ".join(base[(index + 1) % len(base)] for index in range(desc[2]))
            + "]",
        ]
    if kind == "tuple":
        per = [_values(field)[:3] for field in desc[1]]
        return [
            "(" + ", ".join(combination) + ")"
            for combination in itertools.product(*per)
        ]
    raise AssertionError(f"no input values for {desc}")


def _cases(param_descs: list[tuple], limit: int = 48) -> list[tuple[str, ...]]:
    """Deterministic case tuples: the full cartesian product when it fits the
    budget, otherwise the all-boundary case plus seeded-LCG picks."""
    if not param_descs:
        return []
    per = [_values(desc) for desc in param_descs]
    total = 1
    for values in per:
        total *= len(values)
    if total <= limit:
        return list(itertools.product(*per))
    state = 0x5EEDFACE

    def nxt() -> int:
        nonlocal state
        state = (state * 6364136223846793005 + 1442695040888963407) & ((1 << 64) - 1)
        return state >> 33

    out = [tuple(values[0] for values in per)]
    seen = {out[0]}
    while len(out) < limit:
        pick = tuple(values[nxt() % len(values)] for values in per)
        if pick not in seen:
            seen.add(pick)
            out.append(pick)
    return out


# ---- generated shadow module ----


def _function_index(source: str) -> dict[str, tuple[object, object]]:
    """All function_items of one file: name -> (node, tree). Ambiguous names
    (duplicates) map to (None, None) and are refused."""
    import tree_sitter as ts
    import tree_sitter_rust as grammar

    encoded = source.encode()
    root = ts.Parser(ts.Language(grammar.language())).parse(encoded).root_node
    if root.has_error:
        return {}
    index: dict[str, tuple[object, object]] = {}
    stack = [root]
    while stack:
        node = stack.pop()
        if node.type == "function_item":
            name = node.child_by_field_name("name")
            if name is not None:
                key = encoded[name.start_byte : name.end_byte].decode()
                if key in index:
                    index[key] = (None, None)
                else:
                    index[key] = (node, root)
        stack.extend(node.named_children)
    return index


def changed_functions(
    pristine: dict[Path, str], current: dict[Path, str]
) -> dict[str, dict[str, dict]]:
    """`{file: {fn: {"source": original item, "params": ..., "return": ...}}}`
    for every rewritten function the v1 oracle can verify, plus "!unverified"
    entries for every rewritten function it refuses. Never a failure."""
    import tree_sitter as ts
    import tree_sitter_rust as grammar

    spec: dict[str, dict[str, dict]] = {}
    refused: list[str] = []
    for path, before_text in pristine.items():
        after_text = current.get(path)
        if after_text is None or after_text == before_text:
            continue
        parser = ts.Parser(ts.Language(grammar.language()))
        before_root = parser.parse(before_text.encode()).root_node
        after_root = parser.parse(after_text.encode()).root_node
        if before_root.has_error or after_root.has_error:
            continue
        old_index = _function_index(before_text)
        new_index = _function_index(after_text)
        functions: dict[str, dict] = {}
        before_src = before_text.encode()
        after_src = after_text.encode()
        for name, (node, tree) in old_index.items():
            if node is None:
                continue
            other = new_index.get(name)
            if other is None or (other[0], other[1]) == (None, None):
                continue
            other_node = other[0]
            if (
                before_src[node.start_byte : node.end_byte]
                == after_src[other_node.start_byte : other_node.end_byte]
            ):
                continue
            # impl methods are the v2 walk's business, never a v1 refusal
            if node.parent.type != "source_file":
                continue
            ok_before, params, ret = _eligible(node, before_src)
            ok_after, _, _ = _eligible(other_node, after_src)
            label = f"{Path(path).name}:{name}"
            if not ok_before or not ok_after:
                refused.append(label)
                continue
            functions[name] = {
                "source": before_src[node.start_byte : node.end_byte].decode(),
                "params": params,
                "return": ret,
            }
        # v2: methods on constructible local structs
        before_facts = _Facts(before_text)
        after_facts = _Facts(after_text)
        old_methods = _impl_method_index(before_text)
        new_methods = _impl_method_index(after_text)
        for (trait_text, type_name, fname), node in old_methods.items():
            other = new_methods.get((trait_text, type_name, fname))
            if other is None:
                continue
            if (
                before_src[node.start_byte : node.end_byte]
                == after_src[other.start_byte : other.end_byte]
            ):
                continue
            label = f"{Path(path).name}:{type_name}::{fname}"
            entry = (
                _v2_method_entry(before_facts, node, trait_text, type_name, fname)
                if before_facts.valid and after_facts.valid
                else None
            )
            new_entry = (
                _v2_method_entry(after_facts, other, trait_text, type_name, fname)
                if entry is not None
                else None
            )
            if entry is None or new_entry is None:
                refused.append(label)
                continue
            functions[f"{type_name}::{fname}"] = entry
        if functions:
            spec[str(path)] = functions
    if refused:
        spec["!unverified"] = {name: {} for name in sorted(refused)}
    return spec


def _renamed_original(item_text: str) -> str:
    import tree_sitter as ts
    import tree_sitter_rust as grammar

    encoded = item_text.encode()
    root = ts.Parser(ts.Language(grammar.language())).parse(encoded).root_node
    item = next(c for c in root.named_children if c.type == "function_item")
    name = item.child_by_field_name("name")
    offset = name.start_byte
    return item_text[:offset] + "__lc_orig_" + item_text[offset:]


def _rust_string_literal(text: str) -> str:
    """A Rust string literal for the input repr. NOT json.dumps: Rust has no
    backslash-u escapes (only brace form) and json.dumps happily emits lone
    surrogates for emoji - "incorrect unicode escape sequence"."""
    escaped = (
        text.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\n", "\\n")
        .replace("\r", "\\r")
        .replace("\t", "\\t")
    )
    return f'"{escaped}"'


def _shadow_test(name: str, info: dict) -> str:
    """One differential test, unrolled per input case.

    The generated code must compile inside a `#![no_std]` crate as well: bare
    `vec!`/`println!`/`format!` come from the std macro prelude and do not
    exist there. So the cases are unrolled (no `vec!`), the marker is written
    with the built-in `format_args!` through `write_fmt` (no `println!`), and
    `format!` resolves through the module-level `use std::format;` made
    available by `extern crate std;` in `_shadow_mod` - legal in a cfg(test)
    module of a no_std crate because the test harness links std either way.
    """
    params = info["params"]
    receiver = info.get("receiver")
    if receiver is not None:
        return _shadow_test_method(name, info)
    # unqualified: __lc_orig_* is a sibling item of the test inside
    # __lc_shadow; `super::` from here would be the crate root
    original_call = f"__lc_orig_{name}({{}})"
    rewritten_call = f"super::{name}({{}})"
    marker = (
        f'    __lc_mark(&format!("__LC_SHADOW_VERIFIED name={name} '
        'cases={}", verified));'
    )

    def body(call_original: str, call_rewritten: str, input_literal: str) -> list[str]:
        return [
            "        let original = std::panic::catch_unwind(",
            f"            std::panic::AssertUnwindSafe(|| {call_original}),",
            "        );",
            "        let rewritten = std::panic::catch_unwind(",
            f"            std::panic::AssertUnwindSafe(|| {call_rewritten}),",
            "        );",
            "        match (original, rewritten) {",
            "            (Ok(o), Ok(n)) => {",
            '                if format!("{:?}", o) != format!("{:?}", n) {',
            "                    __lc_mark(&format!(",
            '                        "__LC_SHADOW_MISMATCH name={} input={}",',
            f'                        "{name}",',
            f"                        {input_literal},",
            "                    ));",
            "                    panic!(",
            '                        "__LC_SHADOW_MISMATCH name={} input={}",',
            f'                        "{name}",',
            f"                        {input_literal},",
            "                    );",
            "                }",
            "            }",
            "            (Ok(_), Err(_)) | (Err(_), Ok(_)) => {",
            "                __lc_mark(&format!(",
            '                    "__LC_SHADOW_MISMATCH name={} input={}",',
            f'                    "{name}",',
            f"                    {input_literal},",
            "                ));",
            "                panic!(",
            '                    "__LC_SHADOW_MISMATCH name={} input={}",',
            f'                    "{name}",',
            f"                    {input_literal},",
            "                )",
            "            }",
            "            (Err(_), Err(_)) => {}",
            "        }",
        ]

    lines = [
        "#[test]",
        f"fn __lc_shadow_check_{name}() {{",
        "    let mut verified = 0usize;",
    ]
    if not params:
        lines.append("    {")
        lines += body(original_call.format(""), rewritten_call.format(""), '"no-args"')
        lines += ["        verified += 1;", "    }"]
    else:
        names = [f"p{index}" for index in range(len(params))]
        clone_args = ", ".join(f"{n}.clone()" for n in names)
        plain_args = ", ".join(names)
        for values in _cases(params):
            input_literal = _rust_string_literal("(" + ", ".join(values) + ")")
            lines.append("    {")
            lines += [
                f"        let {n} = {v};" for n, v in zip(names, values, strict=True)
            ]
            lines += body(
                original_call.format(clone_args),
                rewritten_call.format(plain_args),
                input_literal,
            )
            lines += ["        verified += 1;", "    }"]
    lines += [marker, "}"]
    return "\n".join(lines)


def _shadow_mod(functions: dict[str, dict], marker_path: Path) -> str:
    path_literal = _rust_string_literal(str(marker_path))
    parts = [
        "#[cfg(test)]",
        "mod __lc_shadow {",
        "    #[allow(unused_extern_crates)]",
        "    extern crate std;",
        "    use super::*;",
        "    use std::format;",
        "    use std::io::Write as _;",
        "    fn __lc_mark(line: &str) {",
        "        if let Ok(mut file) = std::fs::OpenOptions::new()",
        "            .append(true)",
        "            .create(true)",
        f"            .open({path_literal})",
        "        {",
        "            let _ = file.write_all(line.as_bytes());",
        '            let _ = file.write_all(b"\n");',
        "        }",
        "    }",
    ]
    by_type: dict[str, list[tuple[str, dict]]] = {}
    for name, info in functions.items():
        if "receiver" in info:
            by_type.setdefault(info["receiver"]["type"], []).append((name, info))
        else:
            parts.append(_renamed_original(info["source"]))
    for type_name, entries in by_type.items():
        originals = [
            "    "
            + _renamed_method_original(
                info["source"], f"__lc_orig_{info['method_name']}"
            ).replace("\n", "\n    ")
            for _name, info in entries
        ]
        parts.append(f"    impl {type_name} {{")
        parts.extend(originals)
        parts.append("    }")
    for name, info in functions.items():
        parts.append(_shadow_test(name, info))
    parts.append("}")
    return "\n".join(parts) + "\n"


def _shadow_command(root: Path, test_command: list[str] | None) -> list[str]:
    """The project's own cargo test command narrowed to the shadow module."""
    cmd = (
        list(test_command)
        if test_command
        else [
            "cargo",
            "test",
            "--quiet",
        ]
    )
    if cmd[0] != "cargo" or "test" not in cmd[:2]:
        return cmd  # a wrapper script: run it whole, markers still surface
    tail = cmd[2:]
    if "--" in tail:
        index = tail.index("--")
        return cmd[:2] + tail[:index] + ["__lc_shadow"] + tail[index:] + ["--nocapture"]
    return cmd[:2] + tail + ["__lc_shadow", "--", "--nocapture"]


def _trace(report: ShadowReport, started: float) -> None:
    import os
    import sys

    if os.environ.get("LC_TRACE"):
        print(
            f"[rust-shadow] {report.functions} fns, {report.exercised} exercised,"
            f" {report.verified_calls} calls, {len(report.mismatches)} mismatch,"
            f" tests_ok={report.tests_ok}, {time.monotonic() - started:.0f}s"
            + "".join(
                f"\n[rust-shadow]   {m['file']}:{m['function']} x{m['count']}"
                for m in report.mismatches[:8]
            ),
            file=sys.stderr,
            flush=True,
        )


def run_rust_shadow(
    root: Path,
    spec: dict[str, dict[str, dict]],
    test_command: list[str] | None,
    timeout: int,
) -> ShadowReport:
    """Verify every eligible rewritten function against its original body.

    Refused functions ride along in the report as unverified (`functions`
    minus `exercised`); a shadow mod that fails to compile marks all of its
    functions unverified. Neither is ever a mismatch."""
    started = time.monotonic()
    spec = {path: dict(functions) for path, functions in spec.items()}
    refused = sorted(spec.pop("!unverified", {}))
    functions = {path: fns for path, fns in spec.items() if fns}
    total = sum(len(fns) for fns in functions.values())
    report = ShadowReport(tests_ok=True, functions=total + len(refused))
    if not total:
        _trace(report, started)
        return report
    originals: dict[Path, bytes] = {}
    fd, marker_name = tempfile.mkstemp(prefix="lc-rust-shadow-", suffix=".log")
    os.close(fd)
    marker_path = Path(marker_name)
    try:
        for path_str, fns in functions.items():
            path = Path(path_str)
            original_bytes = path.read_bytes()
            try:
                text = original_bytes.decode("utf-8")
            except UnicodeDecodeError:
                continue
            originals[path] = original_bytes
            if not text.endswith("\n"):
                text += "\n"
            path.write_text(text + _shadow_mod(fns, marker_path), encoding="utf-8")
        result = run_tests(
            root, "rust", timeout=timeout, command=_shadow_command(root, test_command)
        )
        report.tests_ok = result.ok
        report.output_tail = result.output_tail
        by_name: dict[str, str] = {}
        for path_str, fns in functions.items():
            for name in fns:
                by_name.setdefault(name, Path(path_str).name)
        # Markers are read from a file, not from output_tail: rustc warnings
        # can push stdout markers out of the 4000-char tail (the panic-driven
        # mismatch lines used to survive only because failures print last).
        marker_text = marker_path.read_text(encoding="utf-8", errors="replace")
        for name, value in re.findall(_VERIFIED_RE, marker_text):
            cases = int(value)
            report.verified_calls += cases
            if cases:
                report.exercised += 1
        for name, value in re.findall(_MARK, marker_text):
            report.mismatches.append(
                {
                    "function": name,
                    "file": by_name.get(name, "<unknown>"),
                    "count": 1,
                    "examples": [{"kind": "value", "input": value.strip()[:200]}],
                }
            )
    finally:
        for path, original_bytes in originals.items():
            path.write_bytes(original_bytes)
        marker_path.unlink(missing_ok=True)
    _trace(report, started)
    return report


# ---- oracle v2: methods on constructible local structs ----
#
# humantime's logic is 100% `&self` methods the v1 oracle refused (FOLLOWUP
# "Oracle v2"). v2 extends eligibility to inherent-impl methods whose self
# type is a same-file struct that is constructible WITHOUT generics: via
# `Default`, `T::new(...)` with whitelist-arg signatures, or a field-wise
# literal when every field is a whitelist type. Receivers are re-constructed
# per case (never `Clone`: `Drop` impls and interior-mutability fields are
# refused outright), and Debug must exist for the `format!("{:?}")`
# comparison. Everything refused stays unverified - never a mismatch.
# `&mut self` remains refused in v2 (state carry-over is the v3 frontier).

_MUTABILITY_TOKENS = re.compile(
    r"\b(?:Cell|RefCell|Mutex|RwLock|Rc|Arc|UnsafeCell|AtomicBool|AtomicPtr"
    r"|AtomicU8|AtomicU16|AtomicU32|AtomicU64|AtomicUsize|AtomicI8|AtomicI16"
    r"|AtomicI32|AtomicI64|AtomicIsize)\b"
)


def _alias_map(source: str) -> dict[str, str]:
    """`use core::time::Duration as StdDuration;` -> StdDuration ->
    core::time::Duration; plain `use std::time::Duration;` -> Duration ->
    std::time::Duration. Values are matched on their leaf segment."""
    import tree_sitter as ts
    import tree_sitter_rust as grammar

    encoded = source.encode()
    root = ts.Parser(ts.Language(grammar.language())).parse(encoded).root_node
    if root.has_error:
        return {}
    aliases: dict[str, str] = {}
    stack = [root]
    while stack:
        node = stack.pop()
        if node.type == "use_declaration":
            arg = node.named_children[0] if node.named_children else None
            if arg is not None and arg.type == "scoped_identifier":
                full = encoded[arg.start_byte : arg.end_byte].decode()
                alias_node = arg.child_by_field_name("alias")
                if alias_node is not None:
                    head = encoded[arg.start_byte : alias_node.start_byte].decode()
                    aliases[
                        encoded[alias_node.start_byte : alias_node.end_byte].decode()
                    ] = head.strip()
                else:
                    last = arg.named_children[-1]
                    if last.type == "identifier":
                        aliases[encoded[last.start_byte : last.end_byte].decode()] = (
                            full
                        )
        stack.extend(node.named_children)
    return aliases


def _std_leaf(text: str, aliases: dict[str, str]) -> tuple | None:
    leaf = text.split("::")[-1]
    resolved = aliases.get(leaf, leaf)
    leaf = resolved.split("::")[-1]
    if leaf == "Duration":
        return ("duration",)
    if leaf == "String":
        return ("string",)
    return None


class _Facts:
    """Everything the v2 classifier needs about one file, parsed once."""

    def __init__(self, source: str):
        import tree_sitter as ts
        import tree_sitter_rust as grammar

        self.parser = ts.Parser(ts.Language(grammar.language()))
        encoded = source.encode()
        self.src = encoded
        root = self.parser.parse(encoded).root_node
        self.valid = not root.has_error
        if not self.valid:
            return
        self.aliases = _alias_map(source)
        self.raw: dict[str, dict] = {}
        self.default_impls: set[str] = set()
        self.drop_impls: set[str] = set()
        self.new_ctors: dict[str, list[tuple]] = {}
        self.debug_impls: set[str] = set()
        self.structs: dict[str, dict | None] = {}
        self._collect(root)

    def _collect(self, root) -> None:
        stack = [root]
        while stack:
            node = stack.pop()
            if node.type == "struct_item":
                self._collect_struct(node)
            elif node.type == "impl_item":
                self._collect_impl(node)
            stack.extend(node.named_children)
        for name in list(self.raw):
            self._descriptor(name, frozenset())

    def _derives(self, node) -> set[str]:
        derives: set[str] = set()
        previous = node.prev_named_sibling
        while previous is not None and previous.type == "attribute_item":
            match = re.search(
                rb"derive\s*\(([^)]*)\)",
                self.src[previous.start_byte : previous.end_byte],
            )
            if match:
                derives |= {
                    part.strip().split(b"::")[-1].decode()
                    for part in match.group(1).split(b",")
                    if part.strip()
                }
            previous = previous.prev_named_sibling
        return derives

    def _collect_struct(self, node) -> None:
        name_node = node.child_by_field_name("name")
        body = node.child_by_field_name("body")
        type_params = node.child_by_field_name("type_parameters")
        if name_node is None or body is None or type_params is not None:
            return
        name = self._text(name_node)
        fields = []
        for field in body.named_children:
            if body.type == "ordered_field_declaration_list":
                # tuple struct: the field IS the (possibly pub-wrapped) type
                if field.type == "visibility_modifier":
                    continue
                if field.type in ("visibility_modifier",):
                    continue
                type_node = field
                if field.type == "field_declaration":
                    typed = [
                        c
                        for c in field.named_children
                        if c.type != "visibility_modifier"
                    ]
                    type_node = typed[-1] if typed else None
                if type_node is None:
                    fields = []
                    break
                fields.append((str(len(fields)), self._text(type_node)))
                continue
            type_node = field.child_by_field_name("type")
            if type_node is None:
                fields = []
                break
            name_field = field.child_by_field_name("name")
            label = (
                self._text(name_field) if name_field is not None else str(len(fields))
            )
            fields.append((label, self._text(type_node)))
        self.raw[name] = {
            "fields": fields,
            "tuple": body.type == "ordered_field_declaration_list",
            "derives": self._derives(node),
        }

    def _collect_impl(self, node) -> None:
        type_node = node.child_by_field_name("type")
        trait_node = node.child_by_field_name("trait")
        if type_node is None:
            return
        type_name = self._text(type_node)
        if trait_node is not None:
            trait_name = self._text(trait_node)
            if trait_name.split("::")[-1] == "Default":
                self.default_impls.add(type_name)
            if trait_name.split("::")[-1] == "Debug":
                self.debug_impls.add(type_name)
            if trait_name.split("::")[-1] == "Drop":
                self.drop_impls.add(type_name)
            return
        if "<" in type_name:
            return  # generic inherent impls stay refused in v2
        for child in node.named_children:
            if child.type == "declaration_list":
                for item in child.named_children:
                    if item.type == "function_item":
                        self._collect_new_ctor(item, type_name)

    def _collect_new_ctor(self, item, type_name: str) -> None:
        name_node = item.child_by_field_name("name")
        if name_node is None or self._text(name_node) != "new":
            return
        if item.child_by_field_name("type_parameters") is not None:
            return
        params = self._param_texts(item)
        if params is None:
            return
        ret = self._return_text(item)
        if ret in (None, type_name, "Self"):
            self.new_ctors.setdefault(type_name, []).append(params)

    def _text(self, node) -> str:
        return self.src[node.start_byte : node.end_byte].decode()

    def _param_texts(self, item) -> list[tuple] | None:
        """(pattern, type text) pairs excluding the self parameter."""
        parameters = item.child_by_field_name("parameters")
        if parameters is None:
            return None
        params = []
        for child in parameters.named_children:
            if child.type == "self_parameter":
                continue
            if child.type != "parameter":
                return None
            pattern = child.child_by_field_name("pattern")
            type_node = child.child_by_field_name("type")
            if pattern is None or pattern.type != "identifier" or type_node is None:
                return None
            params.append((self._text(pattern), self._text(type_node)))
        return params

    def _return_text(self, item) -> str | None:
        return_node = item.child_by_field_name("return_type")
        if return_node is None:
            return None
        text = self._text(return_node)
        return text[2:].strip() if text.startswith("->") else text.strip()

    # ---- classification ----

    _WRAP = "struct __LcWrap {{ f: {} }}"

    def classify(
        self, type_text: str, visiting: frozenset[str] = frozenset()
    ) -> tuple | None:
        """Whitelist descriptor for a raw type string, local structs included."""
        if _MUTABILITY_TOKENS.search(type_text):
            return None
        wrapped = self._WRAP.format(type_text)
        encoded = wrapped.encode()
        root = self.parser.parse(encoded).root_node
        if root.has_error:
            return None
        item = root.named_children[0]
        body = item.child_by_field_name("body")
        field = body.named_children[0] if body.named_children else None
        node = field.child_by_field_name("type") if field else None
        if node is None:
            return None
        return self._classify_node(node, encoded, visiting)

    def _classify_node(
        self, node, src: bytes, visiting: frozenset[str]
    ) -> tuple | None:
        kind = node.type
        text = src[node.start_byte : node.end_byte].decode()
        if kind == "type_identifier":
            if text in self.raw:
                if text in visiting:
                    return None
                descriptor = self._descriptor(text, visiting | {text})
                if descriptor is None:
                    return None
                return ("local", text, descriptor)
            return _std_leaf(text, self.aliases)
        if kind == "scoped_type_identifier":
            return _std_leaf(text, self.aliases)
        if kind == "primitive_type":
            return _classify(node, src)
        if kind == "reference_type":
            inner = None
            for child in node.named_children:
                if child.type == "lifetime":
                    continue
                if child.type == "mutable_specifier":
                    return None
                if inner is not None:
                    return None
                inner = child
            if inner is None:
                return None
            sub = self._classify_node(inner, src, visiting)
            return None if sub is None else ("ref", sub, text)
        if kind == "generic_type":
            base = node.child_by_field_name("type")
            args = node.child_by_field_name("arguments")
            if base is None or args is None:
                return None
            leaf = self._text(base).split("::")[-1]
            inner_nodes = args.named_children
            if leaf == "Option" and len(inner_nodes) == 1:
                sub = self._classify_node(inner_nodes[0], src, visiting)
                return None if sub is None else ("option", sub, text)
            if leaf == "Vec" and len(inner_nodes) == 1:
                sub = self._classify_node(inner_nodes[0], src, visiting)
                return None if sub is None else ("vec", sub, text)
            return None  # Box/Result-in-struct/etc. stay refused in v2
        if kind == "tuple_type":
            fields = [
                self._classify_node(n, src, visiting) for n in node.named_children
            ]
            if not fields or any(f is None for f in fields) or len(fields) > 3:
                return None
            return ("tuple", fields, text)
        if kind == "unit_type":
            return ("unit",)
        if kind == "array_type":
            element = node.child_by_field_name("element")
            length = node.child_by_field_name("length")
            if element is None or length is None:
                return None
            inner = self._classify_node(element, src, visiting)
            try:
                count = int(self._text(length))
            except ValueError:
                return None
            if inner is None or not 1 <= count <= 4:
                return None
            return ("array", inner, count, text)
        return None

        return None

    def _descriptor(self, name: str, visiting: frozenset[str]) -> dict | None:
        if name in self.structs:
            return self.structs[name]
        info = self.raw.get(name)
        if info is None or name in visiting:
            self.structs[name] = None
            return None
        self.structs[name] = None  # re-entrancy guard
        if name in self.drop_impls:
            return None
        fields = []
        for label, type_text in info["fields"]:
            desc = self.classify(type_text, visiting | {name})
            if desc is None or not _param_ok(desc):
                return None
            fields.append((label, type_text, desc))
        debug = "Debug" in info["derives"] or name in self.debug_impls
        default = "Default" in info["derives"] or name in self.default_impls
        construct = self._construct_expr(name, fields, info["tuple"], default)
        if construct is None or not debug:
            return None
        descriptor = {
            "fields": fields,
            "tuple": info["tuple"],
            "construct": construct,
            "variants": self._construct_variants(
                name, fields, info["tuple"], construct
            ),
        }
        self.structs[name] = descriptor
        return descriptor

    def _construct_variants(
        self, name: str, fields: list, tuple_struct: bool, construct: str
    ) -> list[str]:
        """Default construction plus one variation per field and an
        all-fields-varied construction, so a zero-arg method is exercised
        beyond the all-zero receiver (where `x + k` == `x - k`)."""

        def render(values: list[str]) -> str:
            if tuple_struct:
                return f"{name}({', '.join(values)})"
            pairs = ", ".join(
                f"{label}: {value}"
                for (label, _t, _d), value in zip(fields, values, strict=True)
            )
            return f"{name} {{{pairs}}}"

        first = [_first_value(d) for _l, _t, d in fields]
        variants = [construct]
        for index in range(len(fields)):
            values = list(first)
            options = _values(fields[index][2])
            if len(options) > 1:
                values[index] = options[1]
                variant = render(values)
                if variant not in variants:
                    variants.append(variant)
        if len(fields) > 1:
            values = [
                _values(d)[1] if len(_values(d)) > 1 else _first_value(d)
                for _l, _t, d in fields
            ]
            variant = render(values)
            if variant not in variants:
                variants.append(variant)
        return variants

    def _construct_expr(
        self, name: str, fields: list, tuple_struct: bool, default: bool
    ) -> str | None:
        if default:
            return f"{name}::default()"
        if name in self.new_ctors:
            args = []
            for _pattern, type_text in self.new_ctors[name]:
                desc = self.classify(type_text)
                if desc is None or not _param_ok(desc):
                    return None
                args.append(_first_value(desc))
            return f"{name}::new({', '.join(args)})"
        if not fields:
            return None  # no way to distinguish an empty tuple struct's intent
        if tuple_struct:
            return f"{name}({', '.join(_first_value(d) for _, _, d in fields)})"
        pairs = ", ".join(f"{label}: {_first_value(d)}" for label, _, d in fields)
        return f"{name} {{{pairs}}}"


def _first_value(desc: tuple) -> str:
    return _values(desc)[0]


def _impl_method_index(source: str) -> dict[tuple[str, str, str], object]:
    """(trait text or '', impl type text, fn name) -> function_item node.

    Every impl method is indexed - inherent, trait, generic - because every
    rewritten function must land either in the verified set or under
    '!unverified'. Which impls v2 can actually verify is decided later."""
    import tree_sitter as ts
    import tree_sitter_rust as grammar

    encoded = source.encode()
    root = ts.Parser(ts.Language(grammar.language())).parse(encoded).root_node
    if root.has_error:
        return {}
    index: dict[tuple[str, str, str], object] = {}
    stack = [root]
    while stack:
        node = stack.pop()
        if node.type == "impl_item":
            trait_node = node.child_by_field_name("trait")
            type_node = node.child_by_field_name("type")
            if type_node is None:
                continue
            type_text = encoded[type_node.start_byte : type_node.end_byte].decode()
            trait_text = (
                encoded[trait_node.start_byte : trait_node.end_byte].decode()
                if trait_node is not None
                else ""
            )
            for child in node.named_children:
                if child.type == "declaration_list":
                    for item in child.named_children:
                        if item.type == "function_item":
                            name = item.child_by_field_name("name")
                            if name is not None:
                                key = (
                                    trait_text,
                                    type_text,
                                    encoded[name.start_byte : name.end_byte].decode(),
                                )
                                index.setdefault(key, item)
            continue  # impls are fully handled here, never descended again
        stack.extend(node.named_children)
    return index


def _v2_method_entry(
    facts: _Facts, item, trait_text: str, type_name: str, fname: str
) -> dict | None:
    """Spec entry for one `&self` method on a constructible local struct,
    or None when v2 must refuse (every refusal stays unverified)."""
    text = facts._text(item)
    if trait_text or re.search(r"\b(?:unsafe|async|extern)\b", text):
        return None
    if item.child_by_field_name("type_parameters") is not None:
        return None
    if item.child_by_field_name("where_clause") is not None:
        return None
    parameters = item.child_by_field_name("parameters")
    if parameters is None:
        return None
    self_param = next(
        (c for c in parameters.named_children if c.type == "self_parameter"), None
    )
    if self_param is None or facts._text(self_param) != "&self":
        return None
    params = facts._param_texts(item)
    if params is None:
        return None
    descs = []
    for _pattern, type_text in params:
        desc = facts.classify(type_text)
        if desc is None or not _param_ok(desc):
            return None
        descs.append(desc)
    ret_text = facts._return_text(item)
    ret = facts.classify(ret_text) if ret_text else ("unit",)
    if ret is None:
        return None
    struct = facts.structs.get(type_name)
    if struct is None:
        return None
    return {
        "source": text,
        "params": descs,
        "return": ret,
        "receiver": {
            "type": type_name,
            "construct": struct["construct"],
            "variants": struct.get("variants", [struct["construct"]]),
        },
        "method_name": fname,
    }


def _renamed_method_original(item_text: str, orig_name: str) -> str:
    import tree_sitter as ts
    import tree_sitter_rust as grammar

    encoded = item_text.encode()
    root = ts.Parser(ts.Language(grammar.language())).parse(encoded).root_node
    item = next(c for c in root.named_children if c.type == "function_item")
    name = item.child_by_field_name("name")
    return item_text[: name.start_byte] + orig_name + item_text[name.end_byte :]


def _shadow_test_method(name: str, info: dict) -> str:
    """Differential test for one `&self` method: two freshly constructed
    receivers per case, the original body riding in a test-only impl block."""

    receiver = info["receiver"]
    construct = receiver["construct"]
    method = info["method_name"]
    params = info["params"]

    original_call = f"__lc_recv_a.__lc_orig_{method}({{}})"
    rewritten_call = f"__lc_recv_b.{method}({{}})"
    marker = (
        f'    __lc_mark(&format!("__LC_SHADOW_VERIFIED name={name} '
        'cases={}", verified));'
    )

    def body(call_original: str, call_rewritten: str, args_original: str) -> list[str]:
        lines = [
            "        let __lc_recv_a = " + construct + ";",
            "        let __lc_recv_b = " + construct + ";",
            "        let original = std::panic::catch_unwind(",
            f"            std::panic::AssertUnwindSafe(|| {call_original}),",
            "        );",
            "        let rewritten = std::panic::catch_unwind(",
            f"            std::panic::AssertUnwindSafe(|| {call_rewritten}),",
            "        );",
            "        match (original, rewritten) {",
            "            (Ok(o), Ok(n)) => {",
            '                if format!("{:?}", o) != format!("{:?}", n) {',
            "                    __lc_mark(&format!(",
            '                        "__LC_SHADOW_MISMATCH name={} input={}",',
            f'                        "{name}",',
            "                        " + args_original + ",",
            "                    ));",
            "                    panic!(",
            '                        "__LC_SHADOW_MISMATCH name={} input={}",',
            f'                        "{name}",',
            "                        " + args_original + ",",
            "                    );",
            "                }",
            "            }",
            "            (Ok(_), Err(_)) | (Err(_), Ok(_)) => {",
            "                __lc_mark(&format!(",
            '                    "__LC_SHADOW_MISMATCH name={} input={}",',
            f'                    "{name}",',
            "                    " + args_original + ",",
            "                ));",
            "                panic!(",
            '                    "__LC_SHADOW_MISMATCH name={} input={}",',
            f'                    "{name}",',
            "                    " + args_original + ",",
            "                )",
            "            }",
            "            (Err(_), Err(_)) => {}",
            "        }",
        ]
        return lines

    lines = [
        "#[test]",
        f"fn __lc_shadow_check_{name.replace('::', '_')}() {{",
        "    let mut verified = 0usize;",
    ]
    if not params:
        for construct_variant in info["receiver"].get("variants", [construct]):
            lines.append("    {")
            case_construct = construct_variant
            original_recv = f"__lc_recv_a.__lc_orig_{method}()"
            rewritten_recv = f"__lc_recv_b.{method}()"
            block_lines = body(
                original_recv, rewritten_recv, _rust_string_literal("(no args)")
            )
            block_lines[0] = "        let __lc_recv_a = " + case_construct + ";"
            block_lines[1] = "        let __lc_recv_b = " + case_construct + ";"
            lines += block_lines
            lines += ["        verified += 1;", "    }"]
    else:
        names = [f"p{index}" for index in range(len(params))]
        # local-typed arguments re-construct for the original call instead of
        # cloning: the construction expression is pure, so re-evaluation is
        # the deep copy (PLAN.md §4.1)
        original_args = ", ".join(
            desc[2]["construct"] if desc[0] == "local" else f"{n}.clone()"
            for n, desc in zip(names, params, strict=True)
        )
        plain_args = ", ".join(names)
        for values in _cases(params):
            input_literal = _rust_string_literal("(" + ", ".join(values) + ")")
            lines.append("    {")
            lines += [
                f"        let {n} = {v};" for n, v in zip(names, values, strict=True)
            ]
            lines += body(
                original_call.format(original_args),
                rewritten_call.format(plain_args),
                input_literal,
            )
            lines += ["        verified += 1;", "    }"]
    lines += [marker, "}"]
    return "\n".join(lines)
