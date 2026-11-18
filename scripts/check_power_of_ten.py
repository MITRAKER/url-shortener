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
`self.m` and `cls.m`, `ClassName.m`, `super().m`, and `ClassName(...)`, which
runs `__init__`. It cannot follow calls through other objects (`obj.m()`),
through other modules, or through `getattr`.
"""

import ast
import re
import sys
import tokenize
from collections import defaultdict
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path

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


@dataclass(frozen=True)
class Instance:
    """`self` or `cls` inside a method: `self.name` is looked up on this class."""

    cls: ast.ClassDef


# What a name can be bound to. None means something we cannot follow, such as
# a parameter, an assignment or an import.
Target = FunctionNode | ast.ClassDef | Instance | None


@dataclass(frozen=True)
class Reference:
    """A name or attribute read inside a function: a possible call."""

    owner: FunctionNode  # the function whose execution reads it
    chain: Chain
    node: ast.Name | ast.Attribute


def _qualified_name(name: str, chain: Chain) -> str:
    outer = [s.name for s in reversed(chain) if isinstance(s, FunctionNode | ast.ClassDef)]
    return ".".join([*outer, name])


def _is_staticmethod(node: FunctionNode) -> bool:
    return any(
        (isinstance(d, ast.Name) and d.id == "staticmethod")
        or (isinstance(d, ast.Attribute) and d.attr == "staticmethod")
        for d in node.decorator_list
    )


def _parameters(node: FunctionNode | ast.Lambda, owner: ast.AST) -> Iterator[tuple[str, Target]]:
    """Parameter names, with `self`/`cls` of a method bound to its class."""
    args = node.args
    positional = [*args.posonlyargs, *args.args]
    method = (
        isinstance(node, FunctionNode)
        and isinstance(owner, ast.ClassDef)
        and not _is_staticmethod(node)
    )
    if positional and method and isinstance(owner, ast.ClassDef):
        yield positional[0].arg, Instance(owner)
        positional = positional[1:]
    others = [*positional, *args.kwonlyargs, args.vararg, args.kwarg]
    yield from ((arg.arg, None) for arg in others if arg is not None)


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


class _Scopes:
    """One pass over a module that records which names each scope binds, and
    which names and attributes each function reads."""

    def __init__(self, tree: ast.Module) -> None:
        self.bindings: defaultdict[ast.AST, defaultdict[str, list[Target]]] = defaultdict(
            lambda: defaultdict(list)
        )
        self.declared: defaultdict[ast.AST, set[str]] = defaultdict(set)  # global/nonlocal
        self.class_chains: dict[ast.ClassDef, Chain] = {}
        self.names: dict[FunctionNode, str] = {}  # every function, by qualified name
        self.references: list[Reference] = []
        self.calls = {n.func for n in ast.walk(tree) if isinstance(n, ast.Call)}
        self._walk(tree)
        for scope, names in self.declared.items():
            for name in names:  # `global x` means this scope does not bind x
                self.bindings[scope].pop(name, None)

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
                scope = next(s for s in chain if not isinstance(s, Comprehension))
                self._bind(scope, name, None)
                children = [(node.value, chain)]
            case _:
                self._record(node, chain)
                children = [(child, chain) for child in ast.iter_child_nodes(node)]
        return children

    def _bind(self, scope: ast.AST, name: str, target: Target) -> None:
        self.bindings[scope][name].append(target)

    def _function(
        self, node: FunctionNode | ast.Lambda, chain: Chain
    ) -> list[tuple[ast.AST, Chain]]:
        if isinstance(node, FunctionNode):
            self._bind(chain[0], node.name, node)
            self.names[node] = _qualified_name(node.name, chain)
        for name, target in _parameters(node, chain[0]):
            self._bind(node, name, target)
        body = node.body if isinstance(node, FunctionNode) else [node.body]
        inner = (node, *chain)
        return [(n, chain) for n in _evaluated_outside(node)] + [(s, inner) for s in body]

    def _class(self, node: ast.ClassDef, chain: Chain) -> list[tuple[ast.AST, Chain]]:
        self._bind(chain[0], node.name, node)
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
            self.declared[chain[0]].update(node.names)
        elif isinstance(node, ast.Name) and not isinstance(node.ctx, ast.Load):
            self._bind(chain[0], node.id, None)
        elif isinstance(node, ast.Name | ast.Attribute) and isinstance(node.ctx, ast.Load):
            owner = next((s for s in chain if isinstance(s, FunctionNode)), None)
            if owner is not None:
                self.references.append(Reference(owner, chain, node))
        for name in _bound_names(node):
            self._bind(chain[0], name, None)

    def resolve(self, name: str, chain: Chain) -> list[Target]:
        """What `name` means at a point inside `chain`, by Python's scoping rules."""
        for depth, scope in enumerate(chain):
            if isinstance(scope, ast.ClassDef) and depth > 0:
                continue  # code inside a method cannot see its class's names
            targets = self.bindings[scope].get(name) if scope in self.bindings else None
            if targets:
                return targets
        return []  # a builtin, or something defined elsewhere

    def lookup(self, cls: ast.ClassDef, attr: str, *, skip_self: bool = False) -> list[Target]:
        """`cls.attr`: the class body first, then its base classes, breadth-first."""
        queue = self._bases(cls) if skip_self else [cls]
        seen = set(queue)
        for _ in range(len(self.class_chains)):  # each class is visited at most once
            if not queue:
                break
            current = queue.pop(0)
            found = self.bindings[current].get(attr) if current in self.bindings else None
            if found:
                return found
            fresh = [base for base in self._bases(current) if base not in seen]
            seen.update(fresh)
            queue += fresh
        return []

    def _bases(self, cls: ast.ClassDef) -> list[ast.ClassDef]:
        chain = self.class_chains[cls]
        names = [base.id for base in cls.bases if isinstance(base, ast.Name)]
        targets = [t for name in names for t in self.resolve(name, chain)]
        return [t for t in targets if isinstance(t, ast.ClassDef)]

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
            elif isinstance(target, ast.ClassDef) and called:
                found = self.lookup(target, "__init__") + self.lookup(target, "__new__")
                functions += [f for f in found if isinstance(f, FunctionNode)]
        return functions

    def _attribute(self, node: ast.Attribute, reference: Reference) -> list[Target]:
        value = node.value
        if isinstance(value, ast.Call) and isinstance(value.func, ast.Name):
            if value.func.id != "super":
                return []
            owner_class = self._class_of(reference)
            return (
                [] if owner_class is None else self.lookup(owner_class, node.attr, skip_self=True)
            )
        if not isinstance(value, ast.Name):
            return []
        found: list[Target] = []
        for base in self.resolve(value.id, reference.chain):
            if isinstance(base, ast.ClassDef):
                found += self.lookup(base, node.attr)
            elif isinstance(base, Instance):
                found += self.lookup(base.cls, node.attr)
        return found

    @staticmethod
    def _class_of(reference: Reference) -> ast.ClassDef | None:
        """The class whose method contains this reference (for `super()`)."""
        chain = reference.chain
        index = chain.index(reference.owner)
        parent = chain[index + 1] if index + 1 < len(chain) else None
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
