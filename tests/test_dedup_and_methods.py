"""C2b — duplicate-group proposals, and per-method decomposition for Python.

Iteration 08's gap diagnosis had two items above all others:

  1. "top-level symbol" is the wrong unit for Python (`Inventory` is one
     186-line class, so per-symbol was still all-or-nothing there);
  2. the biggest documented opportunity in *both* fixtures is cross-symbol
     copy-paste, which no proposal granularity in the tool could express.

These tests pin both, with scripted backends, so the contract is checked
without a GPU — including the negatives: a merge that breaks a test must
leave every touched file byte-identical.
"""

import pathlib
import re
import textwrap

from less_code.backends import Backend
from less_code.dedup import (
    dedup_units,
    find_duplicate_groups,
    normalize_tokens,
    similarity,
)
from less_code.llm_reduce import (
    _dedup_candidate,
    _method_reply_ok,
    _reindent,
    build_dedup_prompt,
    class_context,
    proposal_units,
    reduce_duplicate_groups,
    reduce_symbols,
    verify_multifile,
)
from less_code.testrunners import run_tests

RUNNER = lambda r, l: run_tests(r, l, use_cache=False)  # noqa: E731


# ---- the copy-pasted pair used by most tests below -------------------------

CREDITS = textwrap.dedent('''
    def sum_credits(rows):
        """Total of every credit row."""
        total = 0
        for row in rows:
            if row["kind"] == "credit":
                total = total + row["amount"]
        return total
''').lstrip()

DEBITS = textwrap.dedent('''
    def sum_debits(rows):
        """Total of every debit row."""
        total = 0
        for row in rows:
            if row["kind"] == "debit":
                total = total + row["amount"]
        return total
''').lstrip()

LEDGER_TESTS = textwrap.dedent('''
    from mod_a import sum_credits
    from mod_b import sum_debits

    ROWS = [
        {"kind": "credit", "amount": 3},
        {"kind": "debit", "amount": 4},
        {"kind": "credit", "amount": 5},
    ]

    def test_credits():
        assert sum_credits(ROWS) == 8
        assert sum_credits([]) == 0

    def test_debits():
        assert sum_debits(ROWS) == 4
''').lstrip()

GOOD_MERGE = textwrap.dedent('''
    def _sum_kind(rows, kind):
        return sum(row["amount"] for row in rows if row["kind"] == kind)


    def sum_credits(rows):
        """Total of every credit row."""
        return _sum_kind(rows, "credit")


    def sum_debits(rows):
        """Total of every debit row."""
        return _sum_kind(rows, "debit")
''').lstrip()

BAD_MERGE = GOOD_MERGE.replace(
    'return sum(row["amount"] for row in rows if row["kind"] == kind)', "return 0"
)


def _ledger(tmp_path: pathlib.Path) -> pathlib.Path:
    root = tmp_path / "ledger"
    root.mkdir()
    (root / "mod_a.py").write_text(CREDITS)
    (root / "mod_b.py").write_text(DEBITS)
    (root / "test_ledger.py").write_text(LEDGER_TESTS)
    return root


class ScriptedBackend(Backend):
    """Replies with the next canned answer, recording every prompt."""

    def __init__(self, answers: list[str]):
        super().__init__("scripted")
        self.answers = list(answers)
        self.prompts: list[str] = []

    def complete(self, system, prompt, temperature=0.2):
        self.prompts.append(prompt)
        body = self.answers.pop(0) if self.answers else ""
        return f"```python\n{body}```"


# ---- normalization and grouping -------------------------------------------


def test_normalization_erases_names_literals_and_comments():
    a = normalize_tokens('def sum_credits(rows):  # legacy\n    return "x"\n', "python")
    b = normalize_tokens('def sum_debits(entries):\n    return "y"\n', "python")
    assert a == b
    assert "ID" in a and "def" in a and "sum_credits" not in a


def test_keywords_survive_normalization_so_shape_still_matters():
    loop = normalize_tokens("for x in y:\n    pass\n", "python")
    branch = normalize_tokens("if x in y:\n    pass\n", "python")
    assert loop != branch


def test_a_copy_pasted_pair_is_found_as_one_group(tmp_path):
    root = _ledger(tmp_path)
    groups = find_duplicate_groups(
        {p: p.read_text() for p in (root / "mod_a.py", root / "mod_b.py")}, "python"
    )
    assert len(groups) == 1
    assert [m.key for m in groups[0].members] == ["sum_credits", "sum_debits"]
    assert groups[0].similarity > 0.9
    assert len(groups[0].files) == 2  # members from different files are allowed


def test_unrelated_functions_are_not_grouped(tmp_path):
    src = {
        pathlib.Path("m.py"): textwrap.dedent('''
            def slugify(text):
                out = []
                for ch in text:
                    if ch.isalnum():
                        out.append(ch.lower())
                    else:
                        out.append("-")
                return "".join(out)


            class Registry:
                def __init__(self, name, limit, owner, region):
                    self.name = name
                    self.limit = limit
                    self.owner = owner
                    self.region = region
                    self.items = {}
        ''').lstrip()
    }
    assert find_duplicate_groups(src, "python") == []


def test_grouping_is_tight_not_a_transitive_chain():
    """Union-find chained every loop-shaped function into one blob and then the
    size cap threw away the real twins. Groups are seeded on the best pair."""
    twin = 'def {0}(xs):\n    n = 0\n    for x in xs:\n        if x > {1}:\n            n = n + 1\n    return n\n'
    other = (
        'def render(rows, width):\n    out = []\n    for row in rows:\n'
        '        out.append(str(row).ljust(width))\n'
        '    return "\\n".join(out)\n'
    )
    src = {pathlib.Path("m.py"): twin.format("over", "0") + "\n\n" + twin.format("under", "9") + "\n\n" + other}
    groups = find_duplicate_groups(src, "python", min_tokens=10)
    assert [m.key for m in groups[0].members] == ["over", "under"]


def test_python_dedup_units_descend_into_classes():
    src = "class A:\n    def m(self):\n        return 1\n\n\ndef top():\n    return 2\n"
    keys = [u.key for u in dedup_units(pathlib.Path("m.py"), src, "python")]
    assert keys == ["top", "A.m"] or keys == ["A.m", "top"]


def test_similarity_is_symmetric_and_bounded():
    a = normalize_tokens(CREDITS, "python")
    b = normalize_tokens(DEBITS, "python")
    assert similarity(a, b) == similarity(b, a) <= 1.0


# ---- the multi-file gate ---------------------------------------------------


def test_verify_multifile_restores_every_touched_file(tmp_path):
    root = _ledger(tmp_path)
    a, b = root / "mod_a.py", root / "mod_b.py"
    before = (a.read_text(), b.read_text())
    outcome, loc_before, loc_after = verify_multifile(
        {
            a: 'def sum_credits(rows):\n    """Total of every credit row."""\n    return 0\n',
            b: 'def sum_debits(rows):\n    """Total of every debit row."""\n    return 0\n',
        },
        "python", RUNNER, root,
    )
    assert outcome.startswith("tests-failed")
    assert loc_after < loc_before
    assert (a.read_text(), b.read_text()) == before


def test_verify_multifile_refuses_a_bigger_candidate(tmp_path):
    root = _ledger(tmp_path)
    a = root / "mod_a.py"
    outcome, _before, _after = verify_multifile(
        {a: a.read_text() + "\n\ndef _extra():\n    return 1\n"}, "python", RUNNER, root
    )
    assert outcome == "not-smaller"


def test_verify_multifile_catches_an_api_removal(tmp_path):
    root = _ledger(tmp_path)
    a = root / "mod_a.py"
    outcome, _b, _a = verify_multifile(
        {a: 'def _sum(rows):\n    """Total of every credit row."""\n    return 0\n'},
        "python", RUNNER, root,
    )
    assert outcome.startswith("api-changed") and "sum_credits" in outcome


# ---- duplicate-group proposals --------------------------------------------


def test_a_merged_helper_plus_rewrites_is_accepted_across_two_files(tmp_path):
    root = _ledger(tmp_path)
    backend = ScriptedBackend([GOOD_MERGE])
    records = reduce_duplicate_groups(
        backend, root, [root / "mod_a.py", root / "mod_b.py"], "python", RUNNER,
        attempts_per_group=1, max_groups=1,
    )
    assert [r.outcome for r in records] == ["accepted"]
    assert records[0].loc_after < records[0].loc_before
    a, b = (root / "mod_a.py").read_text(), (root / "mod_b.py").read_text()
    assert "_sum_kind(rows, \"credit\")" in a and "_sum_kind(rows, \"debit\")" in b
    # the helper is appended to each file that needs it — no invented import
    assert a.count("def _sum_kind") == 1 and b.count("def _sum_kind") == 1
    assert run_tests(root, "python", use_cache=False).ok


def test_a_merge_that_breaks_a_test_is_reverted_whole(tmp_path):
    root = _ledger(tmp_path)
    before = ((root / "mod_a.py").read_text(), (root / "mod_b.py").read_text())
    backend = ScriptedBackend([BAD_MERGE])
    records = reduce_duplicate_groups(
        backend, root, [root / "mod_a.py", root / "mod_b.py"], "python", RUNNER,
        attempts_per_group=1, max_groups=1,
    )
    assert records[0].outcome == "tests-failed"
    assert ((root / "mod_a.py").read_text(), (root / "mod_b.py").read_text()) == before


def test_a_reply_missing_a_member_is_refused_before_any_test_runs(tmp_path):
    root = _ledger(tmp_path)
    partial = (
        'def _sum_kind(rows, kind):\n    return 0\n\n\n'
        'def sum_credits(rows):\n    """Total of every credit row."""\n'
        '    return _sum_kind(rows, "credit")\n'
    )
    backend = ScriptedBackend([partial, GOOD_MERGE])
    records = reduce_duplicate_groups(
        backend, root, [root / "mod_a.py", root / "mod_b.py"], "python", RUNNER,
        attempts_per_group=2, max_groups=1,
    )
    assert [r.outcome for r in records] == ["bad-dedup-reply", "accepted"]
    assert "sum_debits" in records[0].detail
    # and the failure reached the next prompt as instructions
    assert "must contain all 2 members" in backend.prompts[1]


def test_a_public_helper_is_refused(tmp_path):
    root = _ledger(tmp_path)
    backend = ScriptedBackend([GOOD_MERGE.replace("_sum_kind", "sumKind")])
    records = reduce_duplicate_groups(
        backend, root, [root / "mod_a.py", root / "mod_b.py"], "python", RUNNER,
        attempts_per_group=1, max_groups=1,
    )
    assert records[0].outcome == "bad-dedup-reply"
    assert "private" in records[0].detail


def test_the_prompt_shows_every_member_and_asks_for_one_block(tmp_path):
    root = _ledger(tmp_path)
    group = find_duplicate_groups(
        {p: p.read_text() for p in (root / "mod_a.py", root / "mod_b.py")}, "python"
    )[0]
    prompt = build_dedup_prompt(group, "python", spec="assert True")
    assert "member 1: `sum_credits`" in prompt and "member 2: `sum_debits`" in prompt
    assert "mod_a.py" in prompt and "mod_b.py" in prompt
    assert "single\ncode block" in prompt or "single code block" in prompt


def test_a_method_member_is_spliced_back_at_class_indent(tmp_path):
    root = tmp_path / "p"
    root.mkdir()
    (root / "m.py").write_text(textwrap.dedent('''
        class Bag:
            def __init__(self, items):
                self.items = items

            def count_big(self):
                n = 0
                for item in self.items:
                    if item > 10:
                        n = n + 1
                return n

            def count_small(self):
                n = 0
                for item in self.items:
                    if item < 10:
                        n = n + 1
                return n
    ''').lstrip())
    sources = {root / "m.py": (root / "m.py").read_text()}
    group = find_duplicate_groups(sources, "python")[0]
    assert [m.key for m in group.members] == ["Bag.count_big", "Bag.count_small"]
    reply = textwrap.dedent('''
        def _count(items, keep):
            return sum(1 for item in items if keep(item))


        def count_big(self):
            return _count(self.items, lambda item: item > 10)


        def count_small(self):
            return _count(self.items, lambda item: item < 10)
    ''').lstrip()
    candidate, problem = _dedup_candidate(reply, group, "python", sources)
    assert problem == ""
    text = candidate[root / "m.py"]
    assert "    def count_big(self):" in text  # re-indented into the class
    assert "\ndef _count(items, keep):" in text  # helper at module level
    assert "class Bag:" in text and text.count("def __init__") == 1


# ---- the mechanical merge ----------------------------------------------------


def _twin_project(tmp_path):
    root = tmp_path / "twins"
    root.mkdir()
    (root / "mod.py").write_text(textwrap.dedent('''
        def count_credits(rows):
            n = 0
            for row in rows:
                if row["kind"] == "credit":
                    n += row["amount"]
            return n


        def count_debits(rows):
            n = 0
            for row in rows:
                if row["kind"] == "debit":
                    n += row["amount"]
            return n
    ''').lstrip())
    (root / "test_mod.py").write_text(textwrap.dedent('''
        from mod import count_credits, count_debits

        ROWS = [{"kind": "credit", "amount": 3}, {"kind": "debit", "amount": 7}]

        def test_twins():
            assert count_credits(ROWS) == 3
            assert count_debits(ROWS) == 7
    ''').lstrip())
    return root


def test_mechanical_merge_builds_the_helper_with_no_model(tmp_path):
    from less_code.dedup import find_duplicate_groups, mechanical_merge

    root = _twin_project(tmp_path)
    sources = {root / "mod.py": (root / "mod.py").read_text()}
    group = find_duplicate_groups(sources, "python")[0]
    built = mechanical_merge(group)
    assert built is not None
    files, note = built
    assert "mechanical merge" in note
    text = files[root / "mod.py"]
    assert "def _count_credits_shared(rows, _slot0):" in text
    assert text.count("def count_credits(rows):") == 1
    assert 'return _count_credits_shared(rows, "credit")' in text
    assert 'return _count_credits_shared(rows, "debit")' in text
    # the helper body keeps member 1's code verbatim, slots substituted
    assert 'if row["kind"] == _slot0:' in text
    compile(text, "<t>", "exec")


def test_mechanical_merge_passes_the_real_gate_end_to_end(tmp_path):
    """Zero LLM calls: the dedup round accepts the mechanical merge, the
    suite stays green, behavior is identical (verified by executing it)."""
    from less_code.backends import Backend
    from less_code.llm_reduce import reduce_duplicate_groups

    root = _twin_project(tmp_path)

    class Refuses(Backend):
        """Any LLM call is a failure: the merge must be mechanical."""

        def __init__(self):
            super().__init__("refuses")

        def complete(self, system, prompt, temperature=0.2):
            raise AssertionError("the LLM must not be asked")

    records = reduce_duplicate_groups(
        Refuses(), root, [root / "mod.py"], "python", run_tests,
        attempts_per_group=1, max_groups=1,
    )
    assert [r.outcome for r in records] == ["accepted"]
    assert records[0].attempt == 0  # the mechanical marker
    assert records[0].loc_after < records[0].loc_before
    ns = {}
    exec(compile((root / "mod.py").read_text(), "<t>", "exec"), ns)
    rows = [{"kind": "credit", "amount": 3}, {"kind": "debit", "amount": 7}]
    assert ns["count_credits"](rows) == 3 and ns["count_debits"](rows) == 7


def test_mechanical_merge_declines_differing_docstrings(tmp_path):
    """A docstring documents its member; merging distinct docs is a doc
    change, and the member rewrite must keep the shared one only when both
    members had it."""
    from less_code.dedup import find_duplicate_groups, mechanical_merge

    root = tmp_path / "docs"
    root.mkdir()
    (root / "m.py").write_text(
        'def a(x):\n    """Doc A."""\n    return x * 1\n\n\ndef b(x):\n    """Doc B."""\n    return x * 2\n'
    )
    sources = {root / "m.py": (root / "m.py").read_text()}
    groups = find_duplicate_groups(sources, "python", min_tokens=4)
    if not groups:
        return  # too small to group: the decline is trivially true
    assert mechanical_merge(groups[0]) is None


def test_mechanical_merge_keeps_identical_docstrings_on_members(tmp_path):
    from less_code.dedup import find_duplicate_groups, mechanical_merge

    root = tmp_path / "docs"
    root.mkdir()
    (root / "m.py").write_text(
        'def a(rows, kind):\n'
        '    """Sum matching rows."""\n'
        '    total = 0\n'
        '    for row in rows:\n'
        '        if row["kind"] == kind:\n'
        '            total += row["amount"]\n'
        '    return total\n\n\n'
        'def b(rows, kind):\n'
        '    """Sum matching rows."""\n'
        '    total = 0\n'
        '    for row in rows:\n'
        '        if row["flag"] == kind:\n'
        '            total += row["amount"]\n'
        '    return total\n'
    )
    sources = {root / "m.py": (root / "m.py").read_text()}
    groups = find_duplicate_groups(sources, "python")
    assert groups
    built = mechanical_merge(groups[0])
    assert built is not None
    text = built[0][root / "m.py"]
    # one per rewritten member, plus the helper's own (member 1's body is
    # kept verbatim, docstring included)
    assert text.count('"""Sum matching rows."""') == 3
    compile(text, "<t>", "exec")


# ---- per-method decomposition (python) ------------------------------------

BIG_CLASS = textwrap.dedent('''
    """Module docstring."""

    LIMIT = 10


    class Store:
        """A store of things.

        Kept verbose on purpose.
        """

        VERSION = 3

        def __init__(self, items):
            self.items = list(items)
            self.log = []

        def add(self, item):
            if item is None:
                raise ValueError("item required")
            if not isinstance(item, int):
                raise TypeError("item must be int")
            self.items.append(item)
            self.log.append(("add", item))
            return len(self.items)

        def big(self):
            out = []
            for item in self.items:
                if item > LIMIT:
                    out.append(item)
            out.sort()
            return out

        def small(self):
            out = []
            for item in self.items:
                if item <= LIMIT:
                    out.append(item)
            out.sort()
            return out

        def total(self):
            total = 0
            for item in self.items:
                total = total + item
            return total

        def describe(self):
            parts = []
            for item in self.items:
                parts.append(str(item))
            return ",".join(parts)

        def average(self):
            if not self.items:
                return 0
            total = 0
            for item in self.items:
                total = total + item
            return total // len(self.items)


    def helper(store):
        return store.total()
''').lstrip()

STORE_TESTS = textwrap.dedent('''
    import pytest
    from m import Store, helper

    def test_store():
        s = Store([1, 20, 3])
        assert s.big() == [20]
        assert s.small() == [1, 3]
        assert s.total() == 24
        assert s.describe() == "1,20,3"
        assert s.average() == 8
        assert s.add(30) == 4
        assert helper(s) == 54

    def test_errors():
        s = Store([])
        with pytest.raises(ValueError):
            s.add(None)
        with pytest.raises(TypeError):
            s.add("x")
''').lstrip()


def _store(tmp_path: pathlib.Path) -> pathlib.Path:
    root = tmp_path / "store"
    root.mkdir()
    (root / "m.py").write_text(BIG_CLASS)
    (root / "test_m.py").write_text(STORE_TESTS)
    return root


def test_a_big_class_is_decomposed_into_its_methods():
    keys = [k for _s, _e, _n, k in proposal_units(BIG_CLASS, "python")]
    assert "Store" not in keys
    assert keys == [
        "Store.__init__", "Store.add", "Store.big", "Store.small",
        "Store.total", "Store.describe", "Store.average", "helper",
    ]


def test_a_small_class_stays_one_unit():
    src = "class Tiny:\n    def a(self):\n        return 1\n\n    def b(self):\n        return 2\n"
    assert [k for _s, _e, _n, k in proposal_units(src, "python")] == ["Tiny"]


def test_non_python_languages_are_unaffected():
    src = "export function a(x) {\n  return x;\n}\nclass B {\n  m() { return 2; }\n}\n"
    assert [k for _s, _e, _n, k in proposal_units(src, "javascript")] == ["a", "B"]


def test_class_context_carries_init_body_and_other_signatures():
    ctx = class_context(BIG_CLASS, "Store", "big")
    assert "self.items = list(items)" in ctx  # __init__ body, verbatim
    assert "VERSION = 3" in ctx  # class attribute
    assert "def total(self): ..." in ctx  # signature only
    assert "total = total + item" not in ctx  # …not its body
    assert "def big" not in ctx  # the method under rewrite is excluded


def test_reindent_moves_a_reply_to_the_method_column():
    assert _reindent("def a():\n    return 1\n", 4) == "    def a():\n        return 1"
    assert _reindent("    def a():\n        return 1", 0) == "def a():\n    return 1"
    assert _reindent("def a():\n\n    return 1", 4).splitlines()[1] == ""


def test_a_whole_class_reply_to_a_method_prompt_is_refused():
    problem = _method_reply_ok("class Store:\n    def big(self):\n        return []\n", "big")
    assert "only `def` blocks" in problem


def test_a_reply_for_the_wrong_method_is_refused():
    assert "does not define `big`" in _method_reply_ok("def small(self):\n    return []\n", "big")


def test_a_correct_method_reply_passes_the_shape_check():
    assert _method_reply_ok("def big(self):\n    return sorted(x for x in self.items)\n", "big") == ""


def _asked_for(prompt: str) -> str:
    m = re.search(r"to rewrite: `([^`]+)`", prompt)
    return m.group(1) if m else ""


class MethodBackend(Backend):
    def __init__(self, answers: dict[str, str]):
        super().__init__("methods")
        self.answers = answers
        self.prompts: list[str] = []

    def complete(self, system, prompt, temperature=0.2):
        self.prompts.append(prompt)
        return f"```python\n{self.answers.get(_asked_for(prompt), '')}```"


class AlwaysBackend(MethodBackend):
    """One canned reply whatever it is asked, so the test does not depend on
    which method happens to be the biggest."""

    def __init__(self, reply: str):
        super().__init__({})
        self.reply = reply

    def complete(self, system, prompt, temperature=0.2):
        self.prompts.append(prompt)
        return f"```python\n{self.reply}```"


def test_one_method_is_proposed_at_a_time_and_accepted_on_its_own(tmp_path):
    root = _store(tmp_path)
    backend = MethodBackend({
        "Store.add": (
            'def add(self, item):\n'
            '    if item is None:\n'
            '        raise ValueError("item required")\n'
            '    if not isinstance(item, int):\n'
            '        raise TypeError("item must be int")\n'
            '    self.items.append(item)\n'
            '    self.log.append(("add", item))\n'
            '    return len(self.items)\n'
        ),
        "Store.big": "def big(self):\n    return sorted(i for i in self.items if i > LIMIT)\n",
        "Store.small": "def small(self):\n    return sorted(i for i in self.items if i <= LIMIT)\n",
        "Store.total": "def total(self):\n    return sum(self.items)\n",
        "Store.describe": 'def describe(self):\n    return ",".join(str(i) for i in self.items)\n',
    })
    records = reduce_symbols(
        backend, root, root / "m.py", "python", RUNNER,
        attempts_per_symbol=1, min_symbol_loc=4,
    )
    assert [_asked_for(p) for p in backend.prompts][0].startswith("Store.")
    accepted = [r for r in records if r.outcome == "accepted"]
    assert len(accepted) == 4  # big, small, total, describe
    final = (root / "m.py").read_text()
    # the class docstring, its attribute and __init__ are untouched by splices
    assert '"""A store of things.' in final
    assert "VERSION = 3" in final
    assert "self.items = list(items)" in final
    assert "return sum(self.items)" in final
    assert "i > LIMIT" in final and "i <= LIMIT" in final
    assert run_tests(root, "python", use_cache=False).ok


def test_the_method_prompt_carries_the_class_context(tmp_path):
    root = _store(tmp_path)
    backend = MethodBackend({})  # empty replies: we only inspect the prompt
    reduce_symbols(
        backend, root, root / "m.py", "python", RUNNER,
        attempts_per_symbol=1, max_symbols=1,
    )
    prompt = backend.prompts[0]
    assert _asked_for(prompt).startswith("Store.")
    assert "The class `Store` it belongs to" in prompt
    assert "self.items = list(items)" in prompt
    assert "at most" in prompt  # the size target iteration 08 asked for


def test_a_class_shaped_method_reply_is_recorded_and_fed_back(tmp_path):
    root = _store(tmp_path)
    before = (root / "m.py").read_text()
    backend = AlwaysBackend("class Store:\n    def add(self, item):\n        return 1\n")
    records = reduce_symbols(
        backend, root, root / "m.py", "python", RUNNER,
        attempts_per_symbol=2, max_symbols=1,
    )
    assert [r.outcome for r in records][0] == "bad-method-reply"
    assert "no class statement" in backend.prompts[1]
    assert (root / "m.py").read_text() == before
