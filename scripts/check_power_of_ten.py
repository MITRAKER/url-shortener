"""Check Python code against the NASA/JPL "Power of Ten" rules that general
linters do not cover. (Ruff and mypy cover the others, see docs/CODING_STANDARD.md.)

    R1  no recursion, direct or indirect (within one module)
    R2  every loop has a fixed upper bound: no `while`, no endless iterators,
        no `for` loop that grows the list it walks over
    R4  no function longer than 60 lines
    R6  no `global` or `nonlocal` (data lives in the smallest possible scope)
    R7  no silently discarded errors: no `contextlib.suppress(Exception)`
    R8  no dynamic code: eval, exec, compile, __import__, importlib.import_module

Usage:
    python scripts/check_power_of_ten.py app scripts tests

A line may break a rule only with a waiver that names the rule and gives a reason:
    while True:  # p10-waiver: R2 server loop, runs until the process stops

Like every static analyzer for a dynamic language, this is an approximation.
For R1 it builds a call graph of one module, resolving names with Python's own
scoping rules (local, enclosing functions, module; class bodies are skipped).
It follows plain names, whether called or passed on (`map(f, xs)`),
`ClassName.m`, `super().m`, and `ClassName(...)`, which runs `__init__` and
`__new__`. Through `self` and `cls` it also follows what Python runs for you,
as the class or any subclass in the module defines it:
    self.m, cls.m     m, or an override of m in a subclass
    reading self.x    a property getter and `__getattribute__`; also
                      `__getattr__`, unless x is in `dir(object)` or is
                      `__dict__`, `__module__` or `__weakref__`, or the class
                      or a base binds x in its body, code anywhere in the
                      module sets it on one of them (`Class.x = v`,
                      `Outer.Inner.x = v`, `cls.x = v`,
                      `setattr(Class, "x", v)`), or a method sets it on
                      `self`: `self.x = v`, `setattr(self, "x", v)`,
                      `object.__setattr__(self, "x", v)`, or through
                      `self.__dict__` or `vars(self)`: `["x"] = v`,
                      `.setdefault("x", v)`, `.update(x=v)`
    self.x = v        a property setter and `__setattr__` (`+=` reads first)
    del self.x        a property deleter and `__delattr__`
    self(...)         `__call__`
    cls(...)          `__init__` and `__new__`
Names are compared the way Python stores them: inside class `C`, `__x` is
`_C__x`, as an attribute (`self.__x`) and as a plain name in every scope,
while a string such as `setattr(self, "__x", v)` is kept as it is written.
Because `self` and `cls` stand for every subclass in the module, a factory
such as `Base.make()`, which runs `cls(...)`, is reported when a subclass's
`__init__` calls it, though `cls` is `Base` there. Code like that needs a
waiver: `# p10-waiver: R1 <reason>`.

It only sees classes defined in the module. It cannot follow calls through
other objects (`obj.m()`, `type(self)(...)`, `self.__class__(...)`), through
other modules (base classes defined there included), through `getattr`,
`setattr` or metaclasses, or through descriptors made any other way than by
decorating a method (such as `x = property(get, put)`). Nor does it follow the
special methods that operators, statements and built-in functions call:
`len(self)` (`__len__`), `self == other` (`__eq__`), `self[k]` (`__getitem__`,
`__missing__`), `str(self)`, `repr(self)` and f-strings (`__str__`,
`__repr__`, `__format__`), `iter(self)` and `for x in self` (`__iter__`),
`bool(self)` and `if self:` (`__bool__`), `with self:` (`__enter__`), nor
`__getattr__` after a property's getter raises AttributeError, which Python
also falls back to. It resolves `super().m` to the bases of the class that
calls it, not to the next class in the instance's MRO. And it assumes that an
instance may have any attribute when its class or a base has a metaclass, or a
base other than `object` that is not written as the name of a class statement
in the module or as `Outer.Inner` with `Inner` a class statement in `Outer`'s
own body (an import, `module.Base`, an alias such as `Shape = Base`, an
`Inner` that `Outer` inherits, or `Generic[T]`), or sets attributes by names
known only at run time (`setattr(self, key, v)`, `self.__dict__.update(d)`, a
new `self.__dict__`): a `__getattr__` there that reads a missing attribute is
not reported. Nor does it follow the order of statements: `Class.x = v` counts
wherever it is, even for a class statement of the same name that runs after
it; and `Outer.Inner = v` makes a base written `Outer.Inner` unknown, as it
may run first.
"""

import ast
import re
import sys
import tokenize
from collections import defaultdict
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

MAX_FUNCTION_LINES = 60
WAIVER = re.compile(r"#\s*p10-waiver:\s*(R\d+)\s+\S")
# The line endings Python's tokenizer counts. str.splitlines() also splits on
# form feeds and other characters, which would put waivers on the wrong line.
LINE_BREAK = re.compile(r"\r\n|\r|\n")
DYNAMIC_CODE = frozenset(
    {"eval", "exec", "compile", "__import__", "importlib.import_module", "importlib.__import__"}
)
ENDLESS_ITERATORS = frozenset({"itertools.count", "itertools.cycle"})
REPEAT_WITH_TIMES_ARGS = 2  # itertools.repeat(x, 3) stops; itertools.repeat(x) never does
ITER_WITH_SENTINEL_ARGS = 2  # iter(read, "") calls read() until it returns ""
SUPER_WITH_OBJECT_ARGS = 2  # super(C, x): the object comes second
GROWING_METHODS = frozenset({"append", "extend", "insert"})
CATCH_ALL = frozenset({"Exception", "BaseException"})

FunctionNode = ast.FunctionDef | ast.AsyncFunctionDef
Comprehension = ast.ListComp | ast.SetComp | ast.DictComp | ast.GeneratorExp
# The scopes around a point in the code, innermost first, ending with the module.
Chain = tuple[ast.AST, ...]


@dataclass(frozen=True, order=True)
class Violation:
    path: str
    line: int
    rule: str
    message: str

    def __str__(self) -> str:
        return f"{self.path}:{self.line}: {self.rule} {self.message}"


def _import_names(tree: ast.Module) -> dict[str, str]:
    """What each imported name stands for: `import itertools as it` gives
    {"it": "itertools"}, `from importlib import import_module` gives
    {"import_module": "importlib.import_module"}."""
    names: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                top = alias.name.split(".")[0]
                names[alias.asname or top] = alias.name if alias.asname else top
        elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
            names.update({a.asname or a.name: f"{node.module}.{a.name}" for a in node.names})
    return names


def _qualified(expr: ast.expr, imports: dict[str, str]) -> str | None:
    """The full name an expression refers to: `it.count` -> "itertools.count"."""
    parts: list[str] = []
    node = expr
    for _ in range(sum(1 for _ in ast.walk(expr))):  # one step per `.attribute`
        if not isinstance(node, ast.Attribute):
            break
        parts.append(node.attr)
        node = node.value
    if not isinstance(node, ast.Name):
        return None
    return ".".join([imports.get(node.id, node.id), *reversed(parts)])


# ----------------------------------------------------------------- R1 ----

# What a use of a name or attribute does. Reading a property runs its getter,
# assigning it runs its setter, and `del` runs its deleter.
Access = Literal["load", "store", "delete"]
# Methods that Python passes the class to, not an instance, with no decorator.
IMPLICIT_CLASSMETHODS = frozenset({"__new__", "__init_subclass__", "__class_getitem__"})
# Attributes every instance has, from `object` or from its class: reading one
# never falls through to `__getattr__`.
ALWAYS_THERE = frozenset(dir(object)) | {"__dict__", "__module__", "__weakref__"}
# Stands for every name, among the attributes a class sets on `self`, when it
# sets some by names known only at run time: `setattr(self, key, value)`.
ANY_NAME = "*"


@dataclass(frozen=True)
class Instance:
    """`self` or `cls` inside a method of `cls`. At run time, `self` is an
    instance of that class or of a subclass, and `cls` is one of those classes."""

    cls: ast.ClassDef
    is_class: bool = False  # `cls` of a classmethod or of `__new__`: a class


# What a name can be bound to. None means something we cannot follow, such as
# a parameter, an assignment or an import.
Target = FunctionNode | ast.ClassDef | Instance | None


@dataclass(frozen=True)
class Reference:
    """A name or attribute used inside a function: a possible call."""

    owner: FunctionNode  # the function whose execution uses it
    chain: Chain
    node: ast.Name | ast.Attribute
    access: Access


def _qualified_name(name: str, chain: Chain) -> str:
    outer = [s.name for s in reversed(chain) if isinstance(s, FunctionNode | ast.ClassDef)]
    return ".".join([*outer, name])


def _has_decorator(node: FunctionNode, name: str) -> bool:
    return any(
        (isinstance(d, ast.Name) and d.id == name)
        or (isinstance(d, ast.Attribute) and d.attr == name)
        for d in node.decorator_list
    )


def _parameters(node: FunctionNode | ast.Lambda, owner: ast.AST) -> Iterator[tuple[str, Target]]:
    """Parameter names, with `self`/`cls` of a method bound to its class."""
    args = node.args
    positional = [*args.posonlyargs, *args.args]
    if (
        positional
        and isinstance(node, FunctionNode)
        and isinstance(owner, ast.ClassDef)
        and not _has_decorator(node, "staticmethod")
    ):
        takes_class = node.name in IMPLICIT_CLASSMETHODS or _has_decorator(node, "classmethod")
        yield positional[0].arg, Instance(owner, is_class=takes_class)
        positional = positional[1:]
    others = [*positional, *args.kwonlyargs, args.vararg, args.kwarg]
    yield from ((arg.arg, None) for arg in others if arg is not None)


def _accessor(target: Target) -> Access:
    """Which use of an attribute runs `target`: a `@x.setter` function runs when
    it is assigned, a `@x.deleter` one when it is deleted, the rest when it is read."""
    decorators = target.decorator_list if isinstance(target, FunctionNode) else []
    kinds = {d.attr for d in decorators if isinstance(d, ast.Attribute)}
    if "setter" in kinds:
        return "store"
    return "delete" if "deleter" in kinds else "load"


def _run_by(targets: list[Target], access: Access) -> list[Target]:
    return [target for target in targets if _accessor(target) == access]


def _hook(access: Access) -> str:
    """The method Python runs on every read, assignment or `del` of an attribute."""
    if access == "store":
        return "__setattr__"
    return "__delattr__" if access == "delete" else "__getattribute__"


def _closure(
    start: ast.ClassDef, edges: dict[ast.ClassDef, list[ast.ClassDef]]
) -> list[ast.ClassDef]:
    """`start`, then every class reachable through `edges`, breadth-first."""
    found = [start]
    # Bounded (rule 2): each round takes the next class, and none is added twice.
    for index in range(len(edges)):
        if index >= len(found):
            break
        found += [cls for cls in edges[found[index]] if cls not in found]
    return found


def _subclasses(
    parents: dict[ast.ClassDef, list[ast.ClassDef]],
) -> dict[ast.ClassDef, list[ast.ClassDef]]:
    """The classes that name each class as a base."""
    children: dict[ast.ClassDef, list[ast.ClassDef]] = {cls: [] for cls in parents}
    for cls, bases in parents.items():
        for base in bases:
            children[base].append(cls)
    return children


def _evaluated_outside(node: FunctionNode | ast.Lambda) -> list[ast.AST]:
    """Parts of a def that run in the enclosing scope, when the def runs."""
    args = node.args
    parts: list[ast.AST] = [*args.defaults, *(d for d in args.kw_defaults if d is not None)]
    if isinstance(node, FunctionNode):
        every_arg = [*args.posonlyargs, *args.args, *args.kwonlyargs, args.vararg, args.kwarg]
        parts += [a.annotation for a in every_arg if a is not None and a.annotation is not None]
        parts += [*node.decorator_list, *([node.returns] if node.returns else [])]
    return parts


def _bound_names(node: ast.AST) -> list[str]:
    """Names a non-scope node binds in the current scope (except Name targets)."""
    if isinstance(node, ast.Import | ast.ImportFrom):
        return [a.asname or a.name.split(".")[0] for a in node.names]
    if isinstance(node, ast.ExceptHandler | ast.MatchAs | ast.MatchStar) and node.name:
        return [node.name]
    if isinstance(node, ast.MatchMapping) and node.rest:
        return [node.rest]
    return []


def _mangled(name: str, cls: ast.ClassDef | None) -> str:
    """`name` as Python stores it when the code that names it is inside the
    body of `cls`: a private name such as `__x` becomes `_Cls__x`. A string
    is never changed: `setattr(self, "__x", v)` sets `__x` itself."""
    owner = cls.name.lstrip("_") if cls is not None else ""
    if not owner or not name.startswith("__") or name.endswith("__"):
        return name
    return f"_{owner}{name}"


def _enclosing_class(chain: Chain) -> ast.ClassDef | None:
    """The innermost class whose body contains this point, at any depth."""
    return next((scope for scope in chain if isinstance(scope, ast.ClassDef)), None)


def _name_of(key: ast.expr | None) -> str | None:
    """The attribute name that `key` gives: ANY_NAME if it is known only at
    run time, None if it is a constant that is not a name (`d[0] = v`)."""
    if isinstance(key, ast.Constant):
        return key.value if isinstance(key.value, str) else None
    return ANY_NAME


def _update_names(args: list[ast.expr], keywords: list[ast.keyword]) -> list[str | None]:
    """The keys that `d.update(*args, **keywords)` may set: the names of its
    keyword arguments and the constant keys of a dict display. Anything else
    (`**values`, or a mapping built at run time) may set any key: ANY_NAME."""
    names: list[str | None] = [ANY_NAME if kw.arg is None else kw.arg for kw in keywords]
    for arg in args:
        keys = arg.keys if isinstance(arg, ast.Dict) else [None]
        names += [_name_of(key) for key in keys]
    return names


def _namespace_owner(node: ast.expr) -> ast.expr | None:
    """`obj` when `node` is `obj.__dict__` or `vars(obj)`: the dict that holds
    the attributes of `obj`."""
    match node:
        case (
            ast.Attribute(value=obj, attr="__dict__")
            | ast.Call(func=ast.Name(id="vars"), args=[obj])
        ):
            return obj
    return None


def _attribute_writes(node: ast.AST) -> list[tuple[ast.expr, str]]:
    """The object and the name of each attribute set without `obj.name = v`:
    `setattr(obj, name, v)`, `object.__setattr__(obj, name, v)`,
    `obj.__setattr__(name, v)` (`obj` may be `super()`), and through
    `obj.__dict__` or `vars(obj)`: `[name] = v`, `.setdefault(name, v)` and
    `.update(...)`. A name known only at run time is ANY_NAME; a call that
    fails (a keyword argument where none is taken, or two mappings for
    `update`) or a key that is not a string sets no attribute. `**options`
    and `*mappings` may be empty, or one mapping in all: they don't fail."""
    writes: list[tuple[ast.expr | None, str | None]] = []
    match node:
        case (
            ast.Call(func=ast.Name(id="setattr"), args=[obj, key, _], keywords=[])
            | ast.Call(func=ast.Attribute(attr="__setattr__"), args=[obj, key, _], keywords=[])
            | ast.Call(
                func=ast.Attribute(value=obj, attr="__setattr__"), args=[key, _], keywords=[]
            )
        ):
            writes = [(obj, _name_of(key))]
        case ast.Subscript(value=namespace, slice=key, ctx=ast.Store()):
            writes = [(_namespace_owner(namespace), _name_of(key))]
        case ast.Call(
            func=ast.Attribute(value=namespace, attr="setdefault"),
            args=[key] | [key, _],
            keywords=keywords,
        ) if all(keyword.arg is None for keyword in keywords):
            writes = [(_namespace_owner(namespace), _name_of(key))]
        case ast.Call(
            func=ast.Attribute(value=namespace, attr="update"), args=args, keywords=keywords
        ) if len(args) <= 1 or any(isinstance(arg, ast.Starred) for arg in args):
            owner = _namespace_owner(namespace)
            writes = [(owner, name) for name in _update_names(args, keywords)]
    return [(obj, name) for obj, name in writes if obj is not None and name is not None]


class _Scopes:
    """One pass over a module that records which names each scope binds, and
    which names and attributes each function uses."""

    def __init__(self, tree: ast.Module) -> None:
        self.bindings: defaultdict[ast.AST, defaultdict[str, list[Target]]] = defaultdict(
            lambda: defaultdict(list)
        )
        self.declared: defaultdict[ast.AST, set[str]] = defaultdict(set)  # global/nonlocal
        # Attributes set on a class from outside its body, such as `Class.x = v`.
        self.class_stores: dict[ast.ClassDef, set[str]] = {}
        self.class_chains: dict[ast.ClassDef, Chain] = {}
        self.names: dict[FunctionNode, str] = {}  # every function, by qualified name
        self.references: list[Reference] = []
        # Attributes set by name, as `setattr(obj, "name", v)`: object, name, where.
        self.writes: list[tuple[ast.expr, str, Chain]] = []
        # `obj.name = v` anywhere, in a function or not: `Class.x = v` adds to a class.
        self.attribute_stores: list[tuple[ast.Attribute, Chain]] = []
        nodes = list(ast.walk(tree))
        self.calls = {n.func for n in nodes if isinstance(n, ast.Call)}
        # `x.a += 1` reads x.a, then assigns it; `x.a: int` with no value does neither.
        self.updated = {n.target for n in nodes if isinstance(n, ast.AugAssign)}
        self.annotated = {
            n.target for n in nodes if isinstance(n, ast.AnnAssign) and n.value is None
        }
        self._walk(tree)
        for scope, names in self.declared.items():
            for name in names:  # `global x` means this scope does not bind x
                self.bindings[scope].pop(name, None)
        self.class_stores = self._class_stores()
        # Each class, then its base classes; and each class, then its subclasses.
        parents = {cls: self._bases(cls) for cls in self.class_chains}
        children = _subclasses(parents)
        self.ancestors = {cls: _closure(cls, parents) for cls in parents}
        self.descendants = {cls: _closure(cls, children) for cls in parents}
        self.assigned = self._assigned_to_self()
        # Classes whose instances may have attributes that this module does not show.
        self.extends_outside = {cls for cls in parents if self._has_outside_base(cls)}

    def _walk(self, tree: ast.Module) -> None:
        stack: list[tuple[ast.AST, Chain]] = [(stmt, (tree,)) for stmt in tree.body]
        # Bounded (rule 2): every node is pushed exactly once.
        for _ in range(sum(1 for _ in ast.walk(tree))):
            if not stack:
                break
            node, chain = stack.pop()
            stack.extend(self._visit(node, chain))

    def _visit(self, node: ast.AST, chain: Chain) -> list[tuple[ast.AST, Chain]]:
        match node:
            case ast.FunctionDef() | ast.AsyncFunctionDef() | ast.Lambda():
                children = self._function(node, chain)
            case ast.ClassDef():
                children = self._class(node, chain)
            case ast.ListComp() | ast.SetComp() | ast.DictComp() | ast.GeneratorExp():
                children = self._comprehension(node, chain)
            case ast.NamedExpr(target=ast.Name(id=name)):
                # `x := ...` binds x in the enclosing function, even in a comprehension.
                depth = next(i for i, s in enumerate(chain) if not isinstance(s, Comprehension))
                self._bind(chain[depth:], name, None)
                children = [(node.value, chain)]
            case _:
                self._record(node, chain)
                children = [(child, chain) for child in ast.iter_child_nodes(node)]
        return children

    def _bind(self, chain: Chain, name: str, target: Target) -> None:
        """`name` bound in the scope chain[0], as Python stores it: mangled
        (`__x` is `_C__x`) anywhere inside the body of a class `C`."""
        self.bindings[chain[0]][_mangled(name, _enclosing_class(chain))].append(target)

    def _class_stores(self) -> dict[ast.ClassDef, set[str]]:
        """The attributes that code anywhere in the module sets on a class from
        outside its body: `Class.x = v`, `Outer.Inner.x = v`, `cls.x = v` in a
        classmethod, `setattr(Class, "x", v)`. They are kept apart from what
        class bodies bind: such a store may run before or after any given line,
        so it hides no method that a base defines, and a name used in a class
        body never means it (the body runs before the class exists)."""
        stores = [
            (node.value, _mangled(node.attr, _enclosing_class(chain)), chain)
            for node, chain in self.attribute_stores
        ]
        found: defaultdict[ast.ClassDef, set[str]] = defaultdict(set)
        for obj, name, chain in [*stores, *self.writes]:
            for target in self._base_targets(obj, chain):
                cls = target.cls if isinstance(target, Instance) and target.is_class else target
                if isinstance(cls, ast.ClassDef):
                    found[cls].add(name)
        return dict(found)

    def _function(
        self, node: FunctionNode | ast.Lambda, chain: Chain
    ) -> list[tuple[ast.AST, Chain]]:
        if isinstance(node, FunctionNode):
            self._bind(chain, node.name, node)
            self.names[node] = _qualified_name(node.name, chain)
        for name, target in _parameters(node, chain[0]):
            self._bind((node, *chain), name, target)
        body = node.body if isinstance(node, FunctionNode) else [node.body]
        inner = (node, *chain)
        return [(n, chain) for n in _evaluated_outside(node)] + [(s, inner) for s in body]

    def _class(self, node: ast.ClassDef, chain: Chain) -> list[tuple[ast.AST, Chain]]:
        self._bind(chain, node.name, node)
        self.class_chains[node] = chain
        outside = [*node.decorator_list, *node.bases, *node.keywords]
        inner = (node, *chain)
        return [(n, chain) for n in outside] + [(s, inner) for s in node.body]

    def _comprehension(self, node: Comprehension, chain: Chain) -> list[tuple[ast.AST, Chain]]:
        inner = (node, *chain)
        # The first iterable is evaluated outside; everything else inside.
        children: list[tuple[ast.AST, Chain]] = [(node.generators[0].iter, chain)]
        for index, generator in enumerate(node.generators):
            parts = [generator.target, *generator.ifs] + ([generator.iter] if index else [])
            children += [(part, inner) for part in parts]
        elements = [node.key, node.value] if isinstance(node, ast.DictComp) else [node.elt]
        return children + [(element, inner) for element in elements]

    def _record(self, node: ast.AST, chain: Chain) -> None:
        if isinstance(node, ast.Global | ast.Nonlocal):
            owner = _enclosing_class(chain)
            self.declared[chain[0]].update(_mangled(name, owner) for name in node.names)
        elif isinstance(node, ast.Name) and not isinstance(node.ctx, ast.Load):
            self._bind(chain, node.id, None)
        elif isinstance(node, ast.Name | ast.Attribute):
            self._record_use(node, chain)
        elif node not in self.annotated:  # `self.__dict__["x"]: int` sets nothing
            self.writes += [(obj, name, chain) for obj, name in _attribute_writes(node)]
        for name in _bound_names(node):
            self._bind(chain, name, None)

    def _record_use(self, node: ast.Name | ast.Attribute, chain: Chain) -> None:
        """A name or attribute used in a function may be a call; and `obj.x = v`,
        in a function or not, may give a class an attribute (`Class.x = v`)."""
        if (
            isinstance(node, ast.Attribute)
            and isinstance(node.ctx, ast.Store)
            and node not in self.annotated  # `Class.x: int` sets nothing
        ):
            self.attribute_stores.append((node, chain))
        owner = next((s for s in chain if isinstance(s, FunctionNode)), None)
        if owner is not None:
            self.references += [Reference(owner, chain, node, a) for a in self._accesses(node)]

    def _accesses(self, node: ast.Name | ast.Attribute) -> list[Access]:
        """What this use of a name or attribute does to it."""
        if isinstance(node.ctx, ast.Del):
            return ["delete"]
        if not isinstance(node.ctx, ast.Store):
            return ["load"]
        if node in self.annotated:
            return []
        return ["load", "store"] if node in self.updated else ["store"]

    def _assigned_to_self(self) -> dict[ast.ClassDef, set[str]]:
        """The attributes that each class's methods set on `self`, with ANY_NAME
        for a class that sets some by names known only at run time."""
        assigned: dict[ast.ClassDef, set[str]] = {cls: set() for cls in self.class_chains}
        stores = [
            (
                reference.node.value,
                _mangled(reference.node.attr, _enclosing_class(reference.chain)),
                reference.chain,
            )
            for reference in self.references
            if reference.access == "store"
            and isinstance(reference.node, ast.Attribute)
            and isinstance(reference.node.value, ast.Name)  # not `super().x = v`, which fails
        ]
        for obj, name, chain in [*stores, *self.writes]:
            for cls in self._instance_of(obj, chain):
                # `self.__dict__ = values` can bring any attribute.
                assigned[cls].add(ANY_NAME if name == "__dict__" else name)
        return assigned

    def _instance_of(self, obj: ast.expr, chain: Chain) -> list[ast.ClassDef]:
        """The class whose method `obj` is the `self` of, if it is: `self`,
        `super()` (the method's own class), or `super(C, self)`."""
        target = obj
        if isinstance(obj, ast.Call) and isinstance(obj.func, ast.Name) and obj.func.id == "super":
            if not obj.args:
                owner_class = self._class_of(chain)
                return [] if owner_class is None else [owner_class]
            if len(obj.args) != SUPER_WITH_OBJECT_ARGS:
                return []  # super(C) is not bound to an object: it sets nothing
            target = obj.args[1]  # super(C, x) sets the attribute on x
        targets = self.resolve(target.id, chain) if isinstance(target, ast.Name) else []
        return [t.cls for t in targets if isinstance(t, Instance) and not t.is_class]

    def _has_outside_base(self, cls: ast.ClassDef) -> bool:
        """Whether `cls` has a metaclass, or a base other than `object` that is not
        written as a class of this module (`Base` or `Outer.Inner`): what its
        instances have is unknown here."""
        if any(keyword.arg == "metaclass" for keyword in cls.keywords):
            return True
        chain = self.class_chains[cls]
        for base in cls.bases:
            targets = self._base_targets(base, chain)
            if not targets and isinstance(base, ast.Name) and base.id == "object":
                continue  # the builtin `object`
            if not targets or not all(isinstance(t, ast.ClassDef) for t in targets):
                return True
        return False

    def resolve(self, name: str, chain: Chain) -> list[Target]:
        """What `name` means at a point inside `chain`, by Python's scoping rules.
        Inside a class `C`, Python reads `__x` as `_C__x` in every scope."""
        key = _mangled(name, _enclosing_class(chain))
        for depth, scope in enumerate(chain):
            if isinstance(scope, ast.ClassDef) and depth > 0:
                continue  # code inside a method cannot see its class's names
            bound = self.bindings.get(scope)  # get() adds nothing to a defaultdict
            targets = bound.get(key) if bound is not None else None
            if targets:
                return targets
        return []  # a builtin, or something defined elsewhere

    def lookup(self, cls: ast.ClassDef, attr: str, *, skip_self: bool = False) -> list[Target]:
        """`cls.attr`: the class body first, then its base classes, breadth-first."""
        classes = self.ancestors[cls]
        for current in classes[1:] if skip_self else classes:
            found = self.bindings[current].get(attr) if current in self.bindings else None
            if found:
                return found
        return []

    def _bases(self, cls: ast.ClassDef) -> list[ast.ClassDef]:
        chain = self.class_chains[cls]
        targets = [t for base in cls.bases for t in self._base_targets(base, chain)]
        return [t for t in targets if isinstance(t, ast.ClassDef)]

    def _base_targets(self, base: ast.expr, chain: Chain) -> list[Target]:
        """What a base class names: `Base`, or `Outer.Inner` through the class
        statements in `Outer`'s own body. [None] for what can't be followed,
        such as `module.Base`; [] when no class body binds the name."""
        attributes: list[str] = []
        node = base
        for _ in range(sum(1 for _ in ast.walk(base))):  # one step per `.attribute`
            if not isinstance(node, ast.Attribute):
                break
            attributes.append(_mangled(node.attr, _enclosing_class(chain)))
            node = node.value
        unknown: list[Target] = [None]
        targets = self.resolve(node.id, chain) if isinstance(node, ast.Name) else unknown
        for attribute in reversed(attributes):
            classes = [t for t in targets if isinstance(t, ast.ClassDef)]
            if not classes or len(classes) < len(targets):
                return unknown
            targets = [t for c in classes for t in self._member(c, attribute)]
        return targets

    def _member(self, cls: ast.ClassDef, attr: str) -> list[Target]:
        """What `cls.attr` may be where a class statement names it as a base:
        what the body of `cls` binds it to, and something unknown too if code
        anywhere sets `cls.attr = v` (it may run first), or if the body lacks it
        but `cls` may inherit it. [] if `cls.attr` would fail: only the other
        classes the name may mean count."""
        found = list(self.bindings[cls].get(attr, [])) if cls in self.bindings else []
        stored = not {attr, ANY_NAME}.isdisjoint(self.class_stores.get(cls, set()))
        inherits = bool(cls.keywords) or any(
            not (isinstance(base, ast.Name) and base.id == "object") for base in cls.bases
        )
        return [*found, None] if stored or (inherits and not found) else found

    def callees(self, reference: Reference) -> list[FunctionNode]:
        """The functions that this name or attribute may run."""
        node = reference.node
        if isinstance(node, ast.Name):
            targets = self.resolve(node.id, reference.chain)
        else:
            targets = self._attribute(node, reference)
        called = node in self.calls
        functions: list[FunctionNode] = []
        for target in targets:
            if isinstance(target, FunctionNode):
                functions.append(target)
            elif called and isinstance(target, ast.ClassDef):  # ClassName(...)
                functions += self._special([target], "__init__", "__new__")
            elif called and isinstance(target, Instance):  # self(...) or cls(...)
                names = ("__init__", "__new__") if target.is_class else ("__call__",)
                functions += self._special(self.descendants[target.cls], *names)
        return functions

    def _attribute(self, node: ast.Attribute, reference: Reference) -> list[Target]:
        attr = _mangled(node.attr, _enclosing_class(reference.chain))  # `self.__x`
        value = node.value
        if isinstance(value, ast.Call) and isinstance(value.func, ast.Name):
            owner_class = self._class_of(reference.chain)
            # super() can only read: assigning or deleting through it fails.
            if value.func.id != "super" or owner_class is None or reference.access != "load":
                return []
            return _run_by(self.lookup(owner_class, attr, skip_self=True), "load")
        if not isinstance(value, ast.Name):
            return []
        found: list[Target] = []
        for base in self.resolve(value.id, reference.chain):
            if isinstance(base, Instance):  # self.x or cls.x
                found += self._on_instance(base, attr, reference.access)
            elif isinstance(base, ast.ClassDef) and reference.access == "load":  # ClassName.x
                found += _run_by(self.lookup(base, attr), "load")
        return found

    def _on_instance(self, instance: Instance, attr: str, access: Access) -> list[Target]:
        """What `self.attr` or `cls.attr` may run, for `self` an instance of this
        class or of a subclass in the module (a subclass may override `attr`)."""
        classes = self.descendants[instance.cls]
        found = [t for cls in classes for t in _run_by(self.lookup(cls, attr), access)]
        if instance.is_class:  # `cls.x = v` and `del cls.x` change the class: no setter runs
            return found if access == "load" else []
        found += self._special(classes, _hook(access))
        if access == "load":  # a name that nothing defines falls through to __getattr__
            missing = [cls for cls in classes if not self._defines(cls, attr)]
            found += self._special(missing, "__getattr__")
        return found

    def _defines(self, cls: ast.ClassDef, attr: str) -> bool:
        """Whether instances of `cls` may have `attr`: `object` gives it, a class
        body binds it, a method sets it on `self`, or the class or a base may get
        attributes this module does not show (from a base or metaclass defined
        elsewhere, or set by names known only at run time)."""
        if attr in ALWAYS_THERE or self.lookup(cls, attr):
            return True
        names = {attr, ANY_NAME}
        return any(
            ancestor in self.extends_outside
            or not names.isdisjoint(self.assigned[ancestor])
            or not names.isdisjoint(self.class_stores.get(ancestor, set()))
            for ancestor in self.ancestors[cls]
        )

    def _special(self, classes: list[ast.ClassDef], *names: str) -> list[FunctionNode]:
        """The methods Python calls by itself, such as `__init__`, as each class finds them."""
        found = [t for cls in classes for name in names for t in self.lookup(cls, name)]
        return [f for f in found if isinstance(f, FunctionNode)]

    @staticmethod
    def _class_of(chain: Chain) -> ast.ClassDef | None:
        """The class whose method contains this point in the code (for `super()`)."""
        index = next((i for i, scope in enumerate(chain) if isinstance(scope, FunctionNode)), -1)
        parent = chain[index + 1] if 0 <= index < len(chain) - 1 else None
        return parent if isinstance(parent, ast.ClassDef) else None


def _call_graph(scopes: _Scopes) -> dict[FunctionNode, set[FunctionNode]]:
    graph: dict[FunctionNode, set[FunctionNode]] = {function: set() for function in scopes.names}
    for reference in scopes.references:
        graph[reference.owner].update(scopes.callees(reference))
    return graph


def _reachable(
    graph: dict[FunctionNode, set[FunctionNode]], start: FunctionNode
) -> set[FunctionNode]:
    """Every function that `start` can call, directly or through others."""
    seen: set[FunctionNode] = set()
    frontier = set(graph[start])
    # Bounded (rule 2): each round adds at least one new function or stops.
    for _ in range(len(graph) + 1):
        frontier -= seen
        if not frontier:
            break
        seen |= frontier
        frontier = set().union(*(graph[function] for function in frontier))
    return seen


def check_recursion(tree: ast.Module, path: str) -> Iterator[Violation]:
    scopes = _Scopes(tree)
    graph = _call_graph(scopes)
    for function, name in scopes.names.items():
        if function in _reachable(graph, function):
            yield Violation(path, function.lineno, "R1", f"`{name}` is recursive")


# ----------------------------------------------------------------- R2 ----


def _is_endless_iterator(call: ast.Call, imports: dict[str, str]) -> bool:
    name = _qualified(call.func, imports)
    if name == "itertools.repeat":
        has_times = any(keyword.arg == "times" for keyword in call.keywords)
        return len(call.args) < REPEAT_WITH_TIMES_ARGS and not has_times
    if name == "iter":
        return len(call.args) == ITER_WITH_SENTINEL_ARGS
    return name in ENDLESS_ITERATORS


def _grows_what_it_walks(loop: ast.For | ast.AsyncFor) -> bool:
    """`for x in items: items.append(x)` never ends."""
    if not isinstance(loop.iter, ast.Name | ast.Attribute):
        return False
    walked = ast.unparse(loop.iter)
    body = [node for statement in loop.body for node in ast.walk(statement)]
    appends = (
        n.func.value
        for n in body
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Attribute)
        and n.func.attr in GROWING_METHODS
    )
    extends = (  # items += [x]  (a list grows in place; a string or tuple would not)
        n.target
        for n in body
        if isinstance(n, ast.AugAssign) and isinstance(n.value, ast.List | ast.ListComp)
    )
    return any(ast.unparse(grown) == walked for grown in (*appends, *extends))


def check_loops(tree: ast.Module, path: str) -> Iterator[Violation]:
    imports = _import_names(tree)
    for node in ast.walk(tree):
        if isinstance(node, ast.While):
            yield Violation(
                path,
                node.lineno,
                "R2",
                "`while` loop has no fixed upper bound; use `for _ in range(MAX_...)`",
            )
        elif isinstance(node, ast.Call) and _is_endless_iterator(node, imports):
            yield Violation(path, node.lineno, "R2", "endless iterator has no upper bound")
        elif isinstance(node, ast.For | ast.AsyncFor) and _grows_what_it_walks(node):
            yield Violation(path, node.lineno, "R2", "`for` loop grows the list it walks over")


# --------------------------------------------------------- R4 R6 R7 R8 ----


def check_function_length(tree: ast.Module, path: str) -> Iterator[Violation]:
    for node in ast.walk(tree):
        if isinstance(node, FunctionNode):
            length = (node.end_lineno or node.lineno) - node.lineno + 1
            if length > MAX_FUNCTION_LINES:
                yield Violation(
                    path,
                    node.lineno,
                    "R4",
                    f"`{node.name}` is {length} lines long (max {MAX_FUNCTION_LINES})",
                )


def check_scope(tree: ast.Module, path: str) -> Iterator[Violation]:
    for node in ast.walk(tree):
        if isinstance(node, ast.Global | ast.Nonlocal):
            keyword = "global" if isinstance(node, ast.Global) else "nonlocal"
            yield Violation(path, node.lineno, "R6", f"`{keyword}` widens a variable's scope")


def check_swallowed_errors(tree: ast.Module, path: str) -> Iterator[Violation]:
    imports = _import_names(tree)
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and _qualified(node.func, imports) == "contextlib.suppress"
            and any(_qualified(arg, imports) in CATCH_ALL for arg in node.args)
        ):
            yield Violation(
                path, node.lineno, "R7", "suppressing every exception hides real errors"
            )


def check_dynamic_code(tree: ast.Module, path: str) -> Iterator[Violation]:
    imports = _import_names(tree)
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and _qualified(node.func, imports) in DYNAMIC_CODE:
            yield Violation(path, node.lineno, "R8", "dynamic code makes behavior unanalyzable")


# --------------------------------------------------------------- driver ----

CHECKS = (
    check_recursion,
    check_loops,
    check_function_length,
    check_scope,
    check_swallowed_errors,
    check_dynamic_code,
)


def _waived(violation: Violation, lines: Sequence[str]) -> bool:
    line = lines[violation.line - 1] if 0 < violation.line <= len(lines) else ""
    return any(match.group(1) == violation.rule for match in WAIVER.finditer(line))


def check_source(source: str, path: str = "<string>") -> list[Violation]:
    try:
        tree = ast.parse(source, filename=path)
    except SyntaxError as exc:
        return [Violation(path, exc.lineno or 0, "SYNTAX", exc.msg)]
    lines = LINE_BREAK.split(source)
    found = (v for check in CHECKS for v in check(tree, path))
    return sorted(v for v in found if not _waived(v, lines))


def check_file(path: Path) -> list[Violation]:
    """Read a file the way Python does (honouring a BOM or a coding line), then check it."""
    try:
        with tokenize.open(path) as stream:
            source = stream.read()
    except (SyntaxError, UnicodeDecodeError) as exc:
        return [Violation(str(path), 0, "SYNTAX", f"cannot decode the file: {exc}")]
    return check_source(source, str(path))


def python_files(paths: Iterable[Path]) -> list[Path]:
    files: set[Path] = set()
    for path in paths:
        if path.is_dir():
            files |= set(path.rglob("*.py"))
        elif path.suffix == ".py":
            files.add(path)
    return sorted(files)


def check_paths(paths: Iterable[Path]) -> list[Violation]:
    return [v for file in python_files(paths) for v in check_file(file)]


def main(argv: Sequence[str]) -> int:
    paths = [Path(arg) for arg in argv]
    missing = [str(p) for p in paths if not p.exists()]
    if not paths or missing:
        print(f"usage: check_power_of_ten.py PATH...  (not found: {missing})", file=sys.stderr)
        return 2
    violations = check_paths(paths)
    for violation in violations:
        print(violation)
    count = len(python_files(paths))
    print(f"Power of Ten: {len(violations)} violation(s) in {count} file(s)", file=sys.stderr)
    return 1 if violations else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
