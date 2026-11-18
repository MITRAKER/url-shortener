"""Tests for our NASA/JPL "Power of Ten" checker, plus the check that this
codebase itself follows the rules."""

from pathlib import Path
from textwrap import dedent

import pytest

from scripts.check_power_of_ten import check_file, check_paths, check_source, main

ROOT = Path(__file__).resolve().parent.parent


def rules(source: str) -> list[str]:
    return [v.rule for v in check_source(dedent(source))]


def test_this_codebase_follows_the_rules() -> None:
    assert check_paths([ROOT / "app", ROOT / "scripts", ROOT / "tests"]) == []


def test_clean_code_passes() -> None:
    assert (
        rules("""
        def add(a, b):
            for _ in range(3):
                a += b
            return a
    """)
        == []
    )


@pytest.mark.parametrize(
    "source",
    [
        # direct recursion
        """
        def fact(n):
            return 1 if n <= 1 else n * fact(n - 1)
        """,
        # indirect recursion: is_even -> is_odd -> is_even
        """
        def is_even(n):
            return n == 0 or is_odd(n - 1)

        def is_odd(n):
            return n != 0 and is_even(n - 1)
        """,
        # recursion through a method
        """
        class Tree:
            def size(self, node):
                return 1 + sum(self.size(c) for c in node.children)
        """,
        # a nested function calling itself
        """
        def outer():
            def walk(n):
                return walk(n - 1)
            return walk(3)
        """,
        # two nested functions calling each other
        """
        def parity(n):
            def even(k):
                return k == 0 or odd(k - 1)
            def odd(k):
                return k != 0 and even(k - 1)
            return even(n)
        """,
        # through the class name
        """
        class Tree:
            @staticmethod
            def depth(node):
                return 1 + max((Tree.depth(c) for c in node.children), default=0)
        """,
        # a constructor that builds more of its own class
        """
        class Node:
            def __init__(self, data):
                self.children = [Node(c) for c in data["children"]]
        """,
        # a method of a class nested in a function, and of a nested class
        """
        def make():
            class Walker:
                def walk(self, n):
                    return self.walk(n - 1)
            return Walker
        """,
        """
        class Outer:
            class Inner:
                def size(self, n):
                    return self.size(n - 1)
        """,
        # functions defined under `if` or `try` at module level
        """
        if True:
            def ping(n):
                return pong(n - 1)
            def pong(n):
                return ping(n - 1)
        """,
        # passing itself on instead of calling it
        """
        def depth(node):
            return 1 + max(map(depth, node["children"]), default=0)
        """,
        # a property that reads itself
        """
        class Box:
            @property
            def size(self):
                return self.size
        """,
        # async functions
        """
        async def countdown(n):
            await countdown(n - 1)
        """,
    ],
)
def test_recursion_is_rejected(source: str) -> None:
    assert "R1" in rules(source)


@pytest.mark.parametrize(
    "source",
    [
        # a method calling the builtin (or an imported function) of the same name
        """
        from time import sleep

        def make_clock():
            class FakeClock:
                def sleep(self, seconds):
                    sleep(seconds)
                def len(self, items):
                    return len(items)
            return FakeClock()
        """,
        # a method calling the module-level function of the same name
        """
        def save(data):
            return data

        class Store:
            def save(self, data):
                return save(data)
        """,
        # a parameter that hides a function of the same name
        """
        def helper():
            return 1

        def run(helper):
            return helper()
        """,
        # super() reaches the parent's method, not this one
        """
        class Base:
            def save(self):
                return 1

        class Child(Base):
            def save(self):
                return super().save()
        """,
        # mentioning a class is not constructing it
        """
        class Node:
            def __init__(self, other):
                self.ok = isinstance(other, Node)
        """,
        # `:=` in a comprehension creates a local variable of the function
        """
        def step(xs):
            found = [x for x in xs if (step := x)]
            return step(found)
        """,
        # so do names captured by `match` patterns
        """
        def handle(event):
            match event:
                case {"type": "retry", **handle}:
                    return handle(event)
                case [handle, *_]:
                    return handle(event)
        """,
    ],
)
def test_lookalikes_are_not_recursion(source: str) -> None:
    assert rules(source) == []


def test_same_method_name_on_another_object_is_not_recursion() -> None:
    assert (
        rules("""
        class Cache:
            def get(self, key):
                return self.client.get(key)
    """)
        == []
    )


@pytest.mark.parametrize(
    "source",
    [
        "while True:\n    pass\n",
        "while x < 10:\n    x += 1\n",
        "import itertools\nfor i in itertools.count():\n    pass\n",
        "from itertools import cycle\nfor i in cycle([1, 2]):\n    pass\n",
        "for line in iter(read, ''):\n    pass\n",
        "import itertools\nfor x in itertools.repeat(1):\n    pass\n",
        "from itertools import repeat\nfor x in repeat(1):\n    pass\n",
        "import itertools as it\nfor i in it.count():\n    pass\n",
        "for x in items:\n    items.append(x)\n",
        "for x in self.queue:\n    self.queue.extend([x])\n",
        "for x in items:\n    items += [x]\n",
    ],
)
def test_unbounded_loops_are_rejected(source: str) -> None:
    assert rules(source) == ["R2"]


@pytest.mark.parametrize(
    "source",
    [
        "import itertools\nfor x in itertools.repeat(1, 3):\n    pass\n",
        "from itertools import repeat\nfor x in repeat(1, times=3):\n    pass\n",
        "for x in list(items):\n    items.append(x)\n",  # walks a copy
        "for ch in text:\n    text += ch\n",  # a str: += makes a new one
    ],
)
def test_bounded_loops_pass(source: str) -> None:
    assert rules(source) == []


def test_long_functions_are_rejected() -> None:
    body = "\n".join(f"    x{i} = {i}" for i in range(60))
    assert rules(f"def long():\n{body}\n") == ["R4"]  # 61 lines
    short_body = "\n".join(f"    x{i} = {i}" for i in range(59))
    assert rules(f"def ok():\n{short_body}\n") == []  # exactly 60 lines


@pytest.mark.parametrize(
    "source",
    [
        "def f():\n    global counter\n    counter = 1\n",
        "def f():\n    x = 1\n    def g():\n        nonlocal x\n        x = 2\n",
    ],
)
def test_widened_scope_is_rejected(source: str) -> None:
    assert rules(source) == ["R6"]


@pytest.mark.parametrize(
    "source",
    [
        "eval('1 + 1')",
        "exec('x = 1')",
        "compile('x', 'f', 'exec')",
        "__import__('os')",
        "import importlib\nimportlib.import_module('os')",
        "from importlib import import_module\nimport_module('os')",
        "import importlib as il\nil.import_module('os')",
    ],
)
def test_dynamic_code_is_rejected(source: str) -> None:
    assert rules(source) == ["R8"]


@pytest.mark.parametrize(
    "source",
    [
        "import contextlib\nwith contextlib.suppress(Exception):\n    run()\n",
        "from contextlib import suppress\nwith suppress(BaseException):\n    run()\n",
        "from contextlib import suppress as quiet\nwith quiet(KeyError, Exception):\n    run()\n",
    ],
)
def test_suppressing_every_exception_is_rejected(source: str) -> None:
    assert rules(source) == ["R7"]


def test_suppressing_a_specific_exception_passes() -> None:
    assert rules("from contextlib import suppress\nwith suppress(KeyError):\n    run()\n") == []


def test_waiver_needs_the_right_rule_and_a_reason() -> None:
    assert rules("while True:  # p10-waiver: R2 server loop until shutdown\n    pass\n") == []
    assert rules("while True:  # p10-waiver: R2\n    pass\n") == ["R2"]  # no reason
    assert rules("while True:  # p10-waiver: R1 wrong rule\n    pass\n") == ["R2"]


def test_one_line_can_waive_two_rules() -> None:
    body = "\n".join(f"    x{i} = {i}" for i in range(60))
    waivers = "# p10-waiver: R1 depth is capped  # p10-waiver: R4 generated table"
    assert rules(f"def walk(n):  {waivers}\n{body}\n    return walk(n - 1)\n") == []


@pytest.mark.parametrize("separator", ["\x0c", "\x1c", "\u2028"])
def test_waivers_stay_on_their_line(separator: str) -> None:
    # str.splitlines() treats these as line breaks, Python's tokenizer does not.
    source = f"x = '{separator}'\ny = 1  # p10-waiver: R2 not this line\nwhile True:\n    pass\n"
    assert [(v.rule, v.line) for v in check_source(source)] == [("R2", 3)]


def test_syntax_errors_are_reported() -> None:
    assert rules("def broken(:\n") == ["SYNTAX"]


def test_files_are_decoded_like_python_does(tmp_path: Path) -> None:
    bom = tmp_path / "bom.py"
    bom.write_bytes(b"\xef\xbb\xbfx = 1\n")
    latin = tmp_path / "latin.py"
    latin.write_bytes(b"# -*- coding: latin-1 -*-\nname = '\xe9'\n")
    broken = tmp_path / "broken.py"
    broken.write_bytes(b"x = '\xff'\n")

    assert check_file(bom) == []
    assert check_file(latin) == []
    assert [v.rule for v in check_file(broken)] == ["SYNTAX"]


def test_violations_are_formatted_like_compiler_errors() -> None:
    (violation,) = check_source("while True:\n    pass\n", "x.py")
    assert str(violation).startswith("x.py:1: R2 ")


def test_cli_exit_codes(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    good = tmp_path / "good.py"
    good.write_text("x = 1\n")
    bad = tmp_path / "bad.py"
    bad.write_text("while True:\n    pass\n")

    assert main([str(good)]) == 0
    assert main([str(tmp_path)]) == 1
    assert "bad.py:1: R2" in capsys.readouterr().out
    assert main([str(tmp_path / "missing")]) == 2
    assert main([]) == 2
