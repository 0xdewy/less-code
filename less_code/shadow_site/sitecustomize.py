"""Shadow-execution oracle, loaded as `sitecustomize` through PYTHONPATH.

Active only when `LC_SHADOW` names a spec file. For every rewritten function
in the spec, the *original* body is compiled into the rewritten module's own
namespace and installed as a shadow. Each call of the rewritten function then
also runs the original on tee'd iterators and deep copies of the other
arguments, and the outcomes are compared: return value or exception, elements
of a returned iterator (lazily), and mutation of the arguments. Mismatches are
recorded per function; the gate reads the report and rejects the layer.

Errors of the oracle itself are never mismatches: an argument that cannot be
copied, a value that cannot be compared, or a shadow that cannot be installed
count as *skipped*, so the oracle only ever makes the gate stricter.
"""

from __future__ import annotations

import atexit
import copy
import functools
import importlib.machinery
import itertools
import json
import math
import os
import sys
import threading
import types
import uuid
import weakref

_WEAK_TYPES = (
    weakref.ref,
    weakref.WeakKeyDictionary,
    weakref.WeakValueDictionary,
    weakref.WeakSet,
)

_SPEC_PATH = os.environ.get("LC_SHADOW")

# A bare `object()` is a sentinel: it has no state, and code compares it by
# identity (`x is _MARKER`). Copying one silently breaks every such check in
# the shadow, so plain objects are atomic for deep copies made here.
copy._deepcopy_dispatch[object] = lambda value, memo: value


def _faithful_copy(value):
    """Deep copy `value`, or raise when the copy does not reproduce it.

    `copy.deepcopy` reconstructs through `__reduce_ex__`, which mishandles
    some classes (a `list` subclass keeping its items elsewhere gets them
    twice). A copy that is not structurally the same as its source would make
    the original body misbehave for reasons that have nothing to do with the
    rewrite, so the call is skipped instead.
    """
    if _uncopyable(value):
        raise ValueError("no faithful deep copy for this value")
    try:
        if not isinstance(value, (str, bytes)) and len(value) > _MAX_SIZE:
            raise ValueError("argument too large to copy")
    except TypeError:
        pass  # unsized
    duplicate = copy.deepcopy(value)
    if _same(duplicate, value) is False:
        raise ValueError("unfaithful copy")
    return duplicate


_MAX_EXAMPLES = 3
_MAX_DEPTH = 24
#: Calls verified per function before shadowing stops; a hot function called
#: a million times with a large `self` would otherwise turn the suite
#: quadratic, and the first few hundred calls carry the evidence anyway.
_BUDGET = int(os.environ.get("LC_SHADOW_BUDGET", "400"))
_ITER_BUDGET = 10000
#: Arguments larger than this (by `len`) are not copied: three faithful
#: copies of a 50k-element container per call would dominate the suite.
_MAX_SIZE = 2000


def _repr(value) -> str:
    try:
        if type(value).__repr__ is object.__repr__ and hasattr(value, "__dict__"):
            text = f"{type(value).__name__}(**{vars(value)!r})"
        else:
            text = repr(value)
    except Exception:  # noqa: BLE001 - diagnostics only
        text = f"<unreprable {type(value).__name__}>"
    return text[:200]


def _uncopyable(value, depth: int = 0) -> bool:
    """Does `value` (or anything it holds) defeat a faithful deep copy?

    Weak references survive a deep copy but point at the *original*
    referents, so a copied structure keyed or linked by them is broken. A
    subclass of a builtin container with instance state is restored by
    `__reduce_ex__` as state *plus* items re-inserted through the subclass's
    own `append`/`__setitem__`, so it comes back subtly wrong (an LRU's
    linked list, a bidirectional dict's inverse) while still comparing equal.
    """
    if depth > 6:
        return False
    if isinstance(value, _WEAK_TYPES):
        return True
    if isinstance(value, types.MethodType) or (
        isinstance(value, types.FunctionType) and value.__closure__
    ):
        # a bound method or a closure reaches state the copy does not own:
        # the original body would then read and write the *real* objects
        return True
    if isinstance(value, functools.partial) and (value.args or value.keywords):
        return True
    if (
        isinstance(value, (list, dict, set))
        and type(value) not in (list, dict, set)
        and getattr(value, "__dict__", None)
    ):
        return True
    if isinstance(value, dict):
        items = list(value.items())[:64]
        return any(
            _uncopyable(k, depth + 1) or _uncopyable(v, depth + 1) for k, v in items
        )
    if isinstance(value, (list, tuple, set, frozenset)):
        return any(_uncopyable(v, depth + 1) for v in list(value)[:64])
    inner = getattr(value, "__dict__", None)
    if isinstance(inner, dict) and not isinstance(value, type):
        return _uncopyable(inner, depth + 1)
    return False


def _is_iterator(value) -> bool:
    return hasattr(value, "__next__") and not isinstance(value, (str, bytes))


def _same(a, b, depth: int = 0):
    """True / False / None (cannot tell). Structural, never identity."""
    if a is b:
        return True
    if depth > _MAX_DEPTH:
        return None
    if type(a) is not type(b):
        if isinstance(a, bool) or isinstance(b, bool):
            return False
        if isinstance(a, (int, float)) and isinstance(b, (int, float)):
            return a == b
        return False
    if isinstance(a, float):
        return (math.isnan(a) and math.isnan(b)) or a == b
    if isinstance(a, (int, complex, str, bytes, bytearray, type(None), range)):
        return a == b
    if isinstance(a, (list, tuple)):
        if len(a) != len(b):
            return False
        result = True
        for x, y in zip(a, b):
            same = _same(x, y, depth + 1)
            if same is False:
                return False
            if same is None:
                result = None
        return result
    if isinstance(a, dict):
        try:
            if a.keys() != b.keys():
                return False
        except Exception:  # noqa: BLE001
            return None
        result = True
        for key in a:
            same = _same(a[key], b[key], depth + 1)
            if same is False:
                return False
            if same is None:
                result = None
        return result
    if isinstance(a, (set, frozenset)):
        try:
            return bool(a == b)
        except Exception:  # noqa: BLE001
            return None
    if isinstance(a, (types.FunctionType, types.MethodType, type, types.ModuleType)):
        return getattr(a, "__qualname__", None) == getattr(b, "__qualname__", None)
    if _is_iterator(a):
        return None
    if isinstance(a, BaseException):
        return _same(a.args, b.args, depth + 1)
    custom_eq = type(a).__eq__ is not object.__eq__
    if custom_eq:
        try:
            equal = a == b
        except Exception:  # noqa: BLE001
            return None
        if isinstance(equal, bool):
            return equal
        return None
    d1, d2 = getattr(a, "__dict__", None), getattr(b, "__dict__", None)
    if isinstance(d1, dict) and isinstance(d2, dict):
        return _same(d1, d2, depth + 1)
    slots = getattr(type(a), "__slots__", None)
    if slots:
        names = [slots] if isinstance(slots, str) else list(slots)
        return _same(
            {n: getattr(a, n, None) for n in names},
            {n: getattr(b, n, None) for n in names},
            depth + 1,
        )
    if type(a).__repr__ is object.__repr__:
        return None  # a sentinel: its repr is an address, so nothing to compare
    try:
        return repr(a) == repr(b)
    except Exception:  # noqa: BLE001
        return None


def _where(a, b, depth: int = 0) -> str:
    """Path of the first structural difference, for diagnostics."""
    if depth > 8 or _same(a, b) is not False:
        return ""
    if isinstance(a, dict) and isinstance(b, dict):
        for key in a:
            if key not in b:
                return f"[{key!r}] (missing on one side)"
            if _same(a[key], b[key]) is False:
                return f"[{key!r}]" + _where(a[key], b[key], depth + 1)
        return " (keys)"
    if isinstance(a, (list, tuple)) and isinstance(b, (list, tuple)):
        if len(a) != len(b):
            return f" (len {len(a)} vs {len(b)})"
        for index, (x, y) in enumerate(zip(a, b)):
            if _same(x, y) is False:
                return f"[{index}]" + _where(x, y, depth + 1)
        return ""
    d1, d2 = getattr(a, "__dict__", None), getattr(b, "__dict__", None)
    if isinstance(d1, dict) and isinstance(d2, dict) and type(a) is type(b):
        for name in d1:
            if name not in d2:
                return f".{name} (missing on one side)"
            if _same(d1[name], d2[name]) is False:
                return f".{name}" + _where(d1[name], d2[name], depth + 1)
    return f" ({_repr(a)[:60]} vs {_repr(b)[:60]})"


class _Oracle:
    def __init__(self, report_dir: str) -> None:
        self.report_dir = report_dir
        self.stats: dict[str, dict] = {}
        self.local = threading.local()

    def entry(self, key: str) -> dict:
        return self.stats.setdefault(
            key,
            {"verified": 0, "skipped": 0, "mismatches": 0, "examples": []},
        )

    def mismatch(self, key: str, kind: str, new, old) -> None:
        entry = self.entry(key)
        entry["mismatches"] += 1
        if len(entry["examples"]) < _MAX_EXAMPLES:
            try:
                where = _where(new, old)
            except Exception:  # noqa: BLE001 - diagnostics only
                where = ""
            entry["examples"].append(
                {"kind": kind, "new": _repr(new), "old": _repr(old), "where": where}
            )

    def active(self) -> set:
        active = getattr(self.local, "active", None)
        if active is None:
            active = self.local.active = set()
        return active

    # ---- outcome comparison -------------------------------------------------

    def compare_iter(self, key: str, new_iter, old_iter):
        """Lazily compare two iterators, yielding the rewritten one's items."""
        compared = 0
        while True:
            if compared >= _ITER_BUDGET:
                yield from new_iter
                return
            compared += 1
            try:
                item = next(new_iter)
            except Exception as new_exc:
                try:
                    next(old_iter)
                except Exception as old_exc:  # noqa: BLE001
                    if type(new_exc) is not type(old_exc):
                        self.mismatch(key, "iterator-exception", new_exc, old_exc)
                    if isinstance(new_exc, StopIteration):
                        return
                    raise new_exc
                self.mismatch(key, "iterator-length", "<end>", "<more items>")
                if isinstance(new_exc, StopIteration):
                    return
                raise
            try:
                other = next(old_iter)
            except Exception as old_exc:  # noqa: BLE001
                self.mismatch(key, "iterator-length", item, old_exc)
                yield item
                yield from new_iter
                return
            same = _same(item, other)
            if same is False:
                self.mismatch(key, "iterator-item", item, other)
            elif same is None and _is_iterator(item) and _is_iterator(other):
                item = self.compare_iter(key, item, other)
            yield item

    def run(self, key: str, new, old, args, kwargs):
        active = self.active()
        if key in active or self.entry(key)["verified"] >= _BUDGET:
            return new(*args, **kwargs)
        # Prepare three views of the input: the caller's real objects (for
        # the rewrite), a deep copy (for the original) and a spare copy that
        # serves as the untouched baseline for mutation checks and as the
        # input of a second original run when results differ (nondeterminism).
        try:
            new_args, old_args, spare_args = [], [], []
            for value in args:
                if _is_iterator(value):
                    first, second, third = itertools.tee(value, 3)
                    new_args.append(first)
                    old_args.append(second)
                    spare_args.append(third)
                else:
                    new_args.append(value)
                    old_args.append(_faithful_copy(value))
                    spare_args.append(_faithful_copy(value))
            new_kwargs, old_kwargs, spare_kwargs = {}, {}, {}
            for name, value in kwargs.items():
                if _is_iterator(value):
                    first, second, third = itertools.tee(value, 3)
                    new_kwargs[name] = first
                    old_kwargs[name] = second
                    spare_kwargs[name] = third
                else:
                    new_kwargs[name] = value
                    old_kwargs[name] = _faithful_copy(value)
                    spare_kwargs[name] = _faithful_copy(value)
        except Exception:  # noqa: BLE001 - uncopyable argument
            self.entry(key)["skipped"] += 1
            return new(*args, **kwargs)
        active.add(key)
        try:
            new_result = new_exc = old_result = old_exc = None
            try:
                new_result = new(*new_args, **new_kwargs)
            except Exception as exc:  # noqa: BLE001
                new_exc = exc
            try:
                old_result = old(*old_args, **old_kwargs)
            except Exception as exc:  # noqa: BLE001
                old_exc = exc
            self._check_mutation(
                key,
                new_args,
                old_args,
                spare_args,
                new_kwargs,
                old_kwargs,
                spare_kwargs,
            )
            differs = None
            if (new_exc is None) != (old_exc is None):
                differs = ("exception", new_exc or new_result, old_exc or old_result)
            elif new_exc is not None:
                if (
                    type(new_exc) is not type(old_exc)
                    or _same(new_exc.args, old_exc.args) is False
                ):
                    differs = ("exception", new_exc, old_exc)
            elif _is_iterator(new_result) and _is_iterator(old_result):
                pass  # compared lazily below
            elif _same(new_result, old_result) is False:
                differs = ("result", new_result, old_result)
            if differs is not None:
                # nondeterministic on this input? then nothing can be concluded
                try:
                    again = old(*spare_args, **spare_kwargs)
                    again_exc = None
                except Exception as exc:  # noqa: BLE001
                    again, again_exc = None, exc
                if (
                    (again_exc is None) != (old_exc is None)
                    or (old_exc is None and _same(again, old_result) is False)
                    or (
                        old_exc is not None
                        and (
                            type(again_exc) is not type(old_exc)
                            or _same(again_exc.args, old_exc.args) is False
                        )
                    )
                ):
                    self.entry(key)["nondeterministic"] = (
                        self.entry(key).get("nondeterministic", 0) + 1
                    )
                else:
                    self.mismatch(key, *differs)
        finally:
            active.discard(key)
        self.entry(key)["verified"] += 1
        if new_exc is not None:
            raise new_exc
        if _is_iterator(new_result) and _is_iterator(old_result):
            return self.compare_iter(key, new_result, old_result)
        return new_result

    def _check_mutation(
        self, key, new_args, old_args, spare_args, new_kwargs, old_kwargs, spare_kwargs
    ):
        """Copy-against-copy on each side, so a copy that changed type
        (freezegun's FakeDate, proxies) never looks like a mutation."""
        pairs = [
            (str(i), n, o, s)
            for i, (n, o, s) in enumerate(zip(new_args, old_args, spare_args))
        ]
        pairs += [
            (name, new_kwargs[name], old_kwargs[name], spare_kwargs[name])
            for name in new_kwargs
        ]
        for label, real, old_copy, spare in pairs:
            if _is_iterator(real):
                continue
            try:
                after_new = _faithful_copy(real)
            except Exception:  # noqa: BLE001, S112 - uncopyable after-state
                continue
            new_mutated = _same(after_new, spare)
            old_mutated = _same(old_copy, spare)
            if new_mutated is None or old_mutated is None:
                continue
            if new_mutated and old_mutated:
                continue
            if _same(after_new, old_copy) is False:
                self.mismatch(key, f"argument-{label}-mutation", real, old_copy)

    # ---- installation ---------------------------------------------------------

    def install(self, module, functions: dict) -> None:
        for qualname, info in functions.items():
            key = f"{module.__file__}::{qualname}"
            try:
                self._install_one(module, qualname, info)
            except Exception as exc:  # noqa: BLE001 - never break the suite
                self.entry(key)["install_error"] = f"{type(exc).__name__}: {exc}"[:200]

    def _install_one(self, module, qualname: str, info: dict) -> None:
        key = f"{module.__file__}::{qualname}"
        parts = qualname.split(".")
        container = module
        for part in parts[:-1]:
            container = getattr(container, part)
        name = parts[-1]
        raw = vars(container)[name]
        wrap = None
        if isinstance(raw, property):
            wrap, raw = raw, raw.fget
        elif isinstance(raw, (classmethod, staticmethod)):
            wrap, raw = type(raw), raw.__func__
        if not isinstance(raw, types.FunctionType):
            self.entry(key)["install_error"] = (
                f"not a plain function: {type(raw).__name__}"
            )
            return
        namespace: dict = {}
        code = compile(info["source"], module.__file__ + "#original", "exec")
        exec(code, module.__dict__, namespace)  # noqa: S102 - the original body
        original = namespace[name]
        oracle = self

        @functools.wraps(raw)
        def shadowed(*args, **kwargs):
            return oracle.run(key, raw, original, args, kwargs)

        if isinstance(wrap, property):
            setattr(
                container, name, property(shadowed, wrap.fset, wrap.fdel, wrap.__doc__)
            )
        elif wrap is not None:
            setattr(container, name, wrap(shadowed))
        else:
            setattr(container, name, shadowed)
        self.entry(key)

    def write_report(self) -> None:
        try:
            os.makedirs(self.report_dir, exist_ok=True)
            path = os.path.join(
                self.report_dir, f"{os.getpid()}-{uuid.uuid4().hex}.json"
            )
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(self.stats, fh)
        except Exception:  # noqa: BLE001, S110 - reporting must never fail the suite
            pass


def _activate(spec_path: str) -> None:
    with open(spec_path, encoding="utf-8") as fh:
        spec = json.load(fh)
    functions = {
        os.path.realpath(path): value for path, value in spec["functions"].items()
    }
    oracle = _Oracle(spec["report_dir"])
    loader_class = importlib.machinery.SourceFileLoader
    original_exec = loader_class.exec_module

    def exec_module(self, module):
        original_exec(self, module)
        path = getattr(module, "__file__", None)
        if path:
            wanted = functions.get(os.path.realpath(path))
            if wanted:
                oracle.install(module, wanted)

    loader_class.exec_module = exec_module
    atexit.register(oracle.write_report)


if _SPEC_PATH and os.path.isfile(_SPEC_PATH):
    try:
        _activate(_SPEC_PATH)
    except Exception as exc:  # noqa: BLE001 - never break the suite
        print(f"lc shadow oracle inactive: {exc}", file=sys.stderr)
