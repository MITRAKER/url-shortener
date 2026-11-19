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
        # a property setter that assigns its own property
        """
        class Temperature:
            @property
            def celsius(self):
                return self._celsius

            @celsius.setter
            def celsius(self, value):
                self.celsius = value
        """,
        # a property deleter that deletes its own property
        """
        class Cache:
            @property
            def data(self):
                return self._data

            @data.deleter
            def data(self):
                del self.data
        """,
        # `+=` reads the property before it assigns it
        """
        class Counter:
            @property
            def count(self):
                self.count += 1
                return self._count
        """,
        # every `self.x = ...` runs __setattr__, every `del self.x` runs __delattr__
        """
        class Record:
            def __setattr__(self, name, value):
                self.last = name
        """,
        """
        class Record:
            def __delattr__(self, name):
                del self.last
        """,
        # reading an attribute that nothing defines runs __getattr__
        """
        class Proxy:
            def __getattr__(self, name):
                return self.target
        """,
        # a base from this module, or `object`, does not define it either
        """
        class Base(object):
            pass

        class Proxy(Base):
            def __getattr__(self, name):
                return self.target
        """,
        # nor does setting another name, or an attribute of another object
        """
        class Proxy:
            def __init__(self, inner):
                object.__setattr__(self, "_target", inner)
                setattr(inner, "target", 1)
                inner.__dict__.update(target=1)

            def __getattr__(self, name):
                return self.target
        """,
        # ... nor names spelled out in update() or setdefault(), a read of
        # self.__dict__, or an annotation without a value
        """
        class Proxy:
            def __init__(self, inner):
                self.__dict__.update(_inner=inner)
                vars(self).update({"_hits": 0})
                self.__dict__.setdefault("_cache", {})
                self.__dict__["target"]: object
                self.seen = "target" in self.__dict__ and self.__dict__["target"]

            def __getattr__(self, name):
                return self.target
        """,
        # ... nor `super().target = v`, which fails, or a super() of another object
        """
        class Base:
            pass

        class Node(Base):
            def __init__(self, parent):
                try:
                    super().target = None
                except AttributeError:
                    super(Node, parent).__setattr__("target", self)

            def __getattr__(self, name):
                return self.target
        """,
        # ... nor a key that is not a name, or a call that fails
        """
        class Proxy:
            def __init__(self):
                self.__dict__.update({0: "zero"})
                vars(self)[1] = "one"
                try:
                    setattr(self, "target", None, strict=True)
                except TypeError:
                    pass
                try:
                    object.__setattr__(self, "target", None, strict=True)
                except TypeError:
                    pass
                try:
                    self.__setattr__("target", None, strict=True)
                except TypeError:
                    pass
                try:
                    self.__dict__.setdefault("target", default=None)
                except TypeError:
                    pass
                try:
                    self.__dict__.update({"target": None}, {})
                except TypeError:
                    pass
                try:
                    super(self).__setattr__("target", None)
                except TypeError:
                    pass

            def __getattr__(self, name):
                return self.target
        """,
        # ... nor a private name spelled out: inside a class, Python stores
        # `self.__target` as `self._Proxy__target`, and a string never changes
        """
        class Proxy:
            def __init__(self, target):
                self.__dict__.update(__target=target)

            def __getattr__(self, name):
                return getattr(self.__target, name)
        """,
        """
        class Base:
            def __init__(self, target):
                self.__target = target

        class Proxy(Base):
            def __getattr__(self, name):
                return getattr(self.__target, name)
        """,
        """
        class Proxy:
            def __getattr__(self, name):
                return getattr(self.__target, name)

        Proxy.__target = None  # outside the class: sets `__target` itself
        """,
        """
        class Proxy:
            global __target
            __target = None  # a global named `_Proxy__target`

            def __getattr__(self, name):
                return self.__target
        """,
        # ... nor an annotation without a value
        """
        class Proxy:
            def __getattr__(self, name):
                return self.target

        Proxy.target: str
        """,
        # ... nor a base of this module reached through other classes
        """
        class Shapes:
            class Base:
                sides = 0

        class Proxy(Shapes.Base):
            def __getattr__(self, name):
                return self.target
        """,
        """
        class A:
            class B:
                class C:
                    sides = 0

        class Proxy(A.B.C):
            def __getattr__(self, name):
                return self.target
        """,
        # ... when only one of two definitions of Shapes has Base, that one
        """
        import os

        if os.environ.get("FAST"):
            class Shapes:
                class Base:
                    sides = 0
        else:
            class Shapes:
                pass

        class Proxy(Shapes.Base):
            def __getattr__(self, name):
                return self.target
        """,
        # a private method calling itself (stored as `_Node__step`), a private
        # name in a class body, and a private nested class as a base
        """
        class Node:
            def __step(self, n):
                return self.__step(n - 1)
        """,
        """
        def build(n):
            class Node:
                def __helper(m):
                    return build(m - 1)

                value = __helper(n)

            return Node
        """,
        """
        class Outer:
            class __Inner:
                sides = 0

            def make(self):
                class Proxy(Outer.__Inner):
                    def __getattr__(self, name):
                        return self.target

                return Proxy
        """,
        # every read of an attribute runs __getattribute__
        """
        class Logged:
            def __getattribute__(self, name):
                return self.__dict__[name]
        """,
        # self(...) runs __call__
        """
        class Countdown:
            def __call__(self, n):
                return self(n - 1)
        """,
        # cls(...) runs __init__ and __new__
        """
        class Node:
            def __init__(self, n):
                self.child = Node.make(n - 1)

            @classmethod
            def make(cls, n):
                return cls(n)
        """,
        """
        class Node:
            def __new__(cls, n):
                return cls(n - 1)
        """,
        # self.step() may run a subclass's step, which calls back
        """
        class Base:
            def run(self):
                return self.step()

        class Child(Base):
            def step(self):
                return self.run()
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
        # a setter that reads its property runs the getter, not itself
        """
        class Temperature:
            @property
            def celsius(self):
                return self._celsius

            @celsius.setter
            def celsius(self, value):
                if value != self.celsius:
                    self._celsius = value

            @celsius.deleter
            def celsius(self):
                del self._celsius
        """,
        # __setattr__ and __delattr__ that hand the work to `object`, and an
        # assignment to an attribute of another object
        """
        class Record:
            def __init__(self, log):
                self.log = log
                self.last = None

            def __setattr__(self, name, value):
                if name != "log":
                    self.log.last = name
                super().__setattr__(name, value)

            def __delattr__(self, name):
                object.__delattr__(self, name)
        """,
        # __getattr__ reading attributes that exist: from `object`, a class
        # body, or an assignment in a base class's __init__
        """
        class Base:
            retries = 3

            def __init__(self, target):
                self.target = target

        class Proxy(Base):
            def __getattr__(self, name):
                if name in self.__dict__ or self.retries < 0:
                    return None
                return getattr(self.target, name)
        """,
        # ... or from a base or metaclass that this module does not define
        """
        import abc
        from plugins import Registry
        from shapes import Shape

        try:
            from shapes import Shapes
        except ImportError:
            class Shapes:
                class Base:
                    pass

        class Circle(Shape):
            def __getattr__(self, name):
                return self.radius

        class Square(Shapes.Base):
            def __getattr__(self, name):
                return self.side

        class Abstract(abc.ABC):
            def __getattr__(self, name):
                return self.impl

        class Settings(dict):
            pass

        class Env(Settings):
            def __getattr__(self, name):
                return self.get(name)

        class ApiError(Exception):
            def __getattr__(self, name):
                return self.args[0][name]

        class Plugin(metaclass=Registry):
            def __getattr__(self, name):
                return self.registry[name]
        """,
        # ... or set by name, with a constant or one known only at run time
        """
        class Proxy:
            def __init__(self, target, log):
                object.__setattr__(self, "_target", target)
                setattr(self, "_log", log)
                super().__setattr__("_hits", 0)
                super(Proxy, self).__setattr__("_misses", 0)
                self.__dict__["_name"] = "proxy"
                vars(self)["_size"] = 0

            def __getattr__(self, name):
                self._log.append(self._name)
                return getattr(self._target, name, self._hits + self._misses + self._size)

        class Lazy:
            def __init__(self, load):
                self.__dict__.update(_loader=load)
                vars(self).update({"_suffix": "!"})
                self.__dict__.setdefault("_cache", {})
                vars(self).setdefault("_seen", set())
                self.__dict__.setdefault("_misses")

            def __getattr__(self, name):
                self._seen.add(name)
                if self._misses is not None:
                    raise AttributeError(name)
                return self._cache.setdefault(name, self._loader(name) + self._suffix)
        """,
        # ... or private names, set the way Python stores them (a class name
        # loses its leading underscores; one made of underscores only mangles nothing)
        """
        class _Proxy:
            def __init__(self, target):
                self.__dict__.update(_Proxy__target=target)
                object.__setattr__(self, "_Proxy__hits", 0)
                self.__misses = 0

            def __getattr__(self, name):
                return getattr(self.__target, name, self.__fallback())

            def __fallback(self):
                self.__misses += 1
                return self.__hits + self.__misses

        class __:
            def __init__(self, target):
                self.__dict__.update(__target=target)

            def __getattr__(self, name):
                return getattr(self.__target, name)
        """,
        # ... or set on the class from outside its body, nested classes included
        """
        class Proxy:
            def __getattr__(self, name):
                return self.target

        Proxy.target = "set later"

        class Shapes:
            class Base:
                pass

        class Stored:
            target = "stored"

        Shapes.Base = Stored

        class Square(Shapes.Base):
            def __getattr__(self, name):
                return self.target
        """,
        # ... or reached through another class (Outer.Inner)
        """
        class Shapes:
            class Base:
                target = None

        class Proxy(Shapes.Base):
            def __getattr__(self, name):
                return self.target
        """,
        # ... but a nested class that may come from an import counts as outside
        """
        class Shapes:
            try:
                from shapes import Base
            except ImportError:
                class Base:
                    pass

        class Square(Shapes.Base):
            def __getattr__(self, name):
                return self.side
        """,
        """
        class Record:
            def __init__(self, fields):
                self.__dict__.update(fields)

            def __getattr__(self, name):
                return self.defaults[name]

        class Options:
            def __init__(self, **values):
                for key, value in values.items():
                    setattr(self, key, value)

            def __getattr__(self, name):
                return self.fallback

        class Snapshot:
            def __init__(self, fields):
                self.__dict__ = dict(fields)

            def __getattr__(self, name):
                return self.default

        class Defaults:
            def __init__(self, key, value):
                self.__dict__.setdefault(key, value)

            def __getattr__(self, name):
                return self.fallback

        class Loaded:
            def __init__(self, **values):
                vars(self).update(**values)

            def __getattr__(self, name):
                return self.fallback
        """,
        """
        class Logged:
            def __getattribute__(self, name):
                return object.__getattribute__(self, name)
        """,
        # self(...) and cls(...) from methods that __call__ and __init__ don't call
        """
        class Task:
            def __init__(self, step):
                self.step = step

            def __call__(self, n):
                return n + self.step

            def twice(self, n):
                return self(self(n))

            @classmethod
            def default(cls):
                return cls(1)
        """,
        # an override that calls super(), and a call that names its class
        """
        class Base:
            def run(self):
                return self.step() + Base.finish(self)

            def step(self):
                return 1

            def finish(self):
                return 0

        class Child(Base):
            def step(self):
                return super().step() + 1

            def finish(self):
                return self.run()
        """,
        # assigning on the class, or declaring without a value, runs no __setattr__
        """
        class Registry:
            def __setattr__(self, name, value):
                self.seen: bool
                Registry.register(name)

            @classmethod
            def register(cls, name):
                cls.last = name
        """,
        # replacing a method on its class does not call it
        """
        class Config:
            def load(self):
                Config.load = Config.cached
                return self.cached()

            def cached(self):
                return 1
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
