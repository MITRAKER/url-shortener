# Coding standard

This project follows NASA/JPL's **"Power of Ten" rules for safety-critical
code**, adapted from C to Python, on top of the usual Python conventions.
Every rule is enforced by a tool where a tool can check it, and by code
review where it can't. CI runs the linters, the type checker, the Power of Ten
checker, the tests and the dependency audit on every pull request and once a
day; CodeQL runs on every pull request and once a week.

## The Power of Ten, adapted to Python

The rules come from Gerard J. Holzmann, *"The Power of Ten: Rules for
Developing Safety-Critical Code"*, IEEE Computer, June 2006, written at NASA's
Jet Propulsion Laboratory. They aim for code simple enough that tools can
prove things about it. Some rules are about C itself (pointers, the
preprocessor, `malloc`), so we keep their purpose and translate the mechanics.

| # | Original rule (C) | Our Python version | Enforced by |
|---|-------------------|--------------------|-------------|
| 1 | Only simple control flow: no `goto`, `setjmp`/`longjmp` or recursion. | No recursion, direct or indirect. Low cyclomatic complexity (at most 8), few branches and returns per function. | `check_power_of_ten.py` (R1), Ruff `C901`, `PLR0911`, `PLR0912` |
| 2 | Every loop has a fixed upper bound that a tool can prove. | No `while` loops, no endless iterators, and no `for` loop that grows the list it walks over; loops are `for` over a finite range or collection, e.g. `for _ in range(MAX_ATTEMPTS)`. Every network call and every wait has a timeout: connecting, waiting for a pooled connection, each SQL statement, each lock wait, and a dead network. One known gap: `statement_timeout` and `lock_timeout` need a server that answers, keepalives and `tcp_user_timeout` only detect a dead peer or network, and libpq has no client-side read timeout, so a database server that is alive but stuck (a paused container, a hung pooler or proxy) is not bounded by the app. | `check_power_of_ten.py` (R2); timeouts by review and `tests/test_db.py` |
| 3 | No dynamic memory allocation after initialization. | Python allocates all the time, so we keep the goal, *bounded resources*: fixed-size connection pools, a maximum size for every input (request bodies up to 16 KB, refused before they are read; URLs up to 2048 characters as stored; aliases up to 32), bounded caches. | `app/limits.py`, Pydantic limits, tests; review |
| 4 | No function longer than one printed page (about 60 lines). | At most 60 lines, 30 statements, 6 arguments per function. | `check_power_of_ten.py` (R4), Ruff `PLR0913`, `PLR0915` |
| 5 | Assertion density averages at least two per function; assertions have no side effects, and a failed one triggers an explicit recovery action. | `assert` is removed by `python -O`, so production code checks preconditions with explicit `if ...: raise ValueError(...)`, and validates all outside input with Pydantic at the API boundary. Tests use `assert` freely. | Ruff `S101` (no `assert` in app code); review |
| 6 | Declare data at the smallest possible scope. | No `global` or `nonlocal`. Module-level objects are only constants and singletons created once at startup (settings, engine). Prefer immutable data: tuples, `frozenset`, frozen dataclasses. | `check_power_of_ten.py` (R6), Ruff `PLW0603`, `B006`, `RUF012` |
| 7 | Check every return value; validate parameters in every function. | Errors are exceptions in Python, so: never swallow them (no bare or blind `except`, no `except: pass`, no `contextlib.suppress(Exception)`), and `mypy --strict` forces every possible `None` to be handled before use. | Ruff `E722`, `BLE001`, `S110`, `check_power_of_ten.py` (R7), mypy strict |
| 8 | Limit the preprocessor to includes and simple macros. | No metaprogramming that hides behavior from tools: no `eval`, `exec`, `compile`, `__import__`, `importlib.import_module`. | `check_power_of_ten.py` (R8), Ruff `S102`, `S307` |
| 9 | At most one level of pointer dereferencing; no function pointers. | Python has references rather than pointers. We avoid mutable state shared through several references (no mutable default arguments, frozen dataclasses) and long attribute chains. Passing functions is limited to framework hooks (FastAPI's `Depends`), standard-library factories such as `defaultdict(list)`, and injected dependencies, such as clocks and the operation given to `RedisGateway.call`, which keep side effects testable. | Ruff `B006`, `B008`; review |
| 10 | All warnings on, at the most pedantic level, from day one; zero warnings; daily static analysis. | Ruff with a broad rule set, `mypy --strict`, warnings turned into test failures, CodeQL, and a daily CI run. Zero warnings is the only accepted state. | CI (daily schedule), CodeQL (weekly), pre-commit |

### Waivers

Sometimes breaking a rule is the right call. A waiver must name the rule and
say why, on the offending line, so a reviewer can judge it:

```python
while True:  # p10-waiver: R2 server loop, runs until the process stops
```

A `# noqa: CODE` for Ruff or a `# type: ignore[code]` for mypy needs a reason
in a comment too. Today there are four, all for Ruff:

| Where | Rule | Why |
|-------|------|-----|
| `app/cache.py` | `S311` (non-cryptographic random) | The cache TTL jitter doesn't need to be unpredictable, only spread out |
| `tests/conftest.py`, 3 imports | `E402` (import not at the top) | The app reads its settings on import, so the test settings must be set first |

## Other rules

- **Formatting:** whatever `ruff format` produces (PEP 8 style, 100 columns).
  Nobody argues about formatting in review.
- **Types:** every function has type hints; `mypy --strict` passes with zero errors.
- **Errors:** raise specific exception types named `...Error`; chain them with
  `raise ... from exc`, unless the original error could carry a secret into the
  logs: then `raise ... from None`, as in `_check_redis_url`; log, don't print.
- **Security:** Ruff's `S` (Bandit) rules; no secrets in code; every
  dependency locked with hashes (edit `requirements*.in`, then `make lock`) and
  audited daily with `pip-audit`; the base image pinned by digest; CodeQL on
  every pull request.
- **Tests:** every feature and every bug fix comes with a test. Branch coverage
  must stay at or above 95%. Tests must not print warnings.
- **Time:** only timezone-aware datetimes, in UTC (Ruff `DTZ`).
- **Commits:** [Conventional Commits](https://www.conventionalcommits.org/),
  e.g. `feat: add rate limiting`, `fix: handle expired links`.
