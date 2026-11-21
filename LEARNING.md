# Learning notes

This file grows with every step of the project. Each lesson explains **what**
was built, **why** it was designed that way, the **key concepts**, and ends
with **exercises** so you can practise building it yourself.

- [Lesson 1: The core service](#lesson-1-the-core-service)
- [Lesson 2: Engineering like a professional team](#lesson-2-engineering-like-a-professional-team)
  (CI/CD, code quality, security scanning, NASA/JPL's Power of Ten)

---

## Lesson 1: The core service

### What we built

A small web service with three jobs:

1. **Shorten**: `POST /api/links` takes a long URL and returns a short code.
2. **Redirect**: `GET /{code}` sends the browser to the long URL.
3. **Report**: `GET /api/links/{code}` shows details and the click count.

Request flow:

```
client ──POST /api/links──▶ FastAPI ──validate (Pydantic)──▶ service.create_link ──INSERT──▶ PostgreSQL
client ──GET /abc1234────▶ FastAPI ──▶ service.get_link ──SELECT by code (index)──▶ PostgreSQL
                                    ◀── 302 Location: https://long-url ──
```

### Project layout and why it is split this way

| File | Responsibility |
|------|----------------|
| `app/config.py` | Settings from environment variables |
| `app/db.py` | Database engine, connection pool, one session per request |
| `app/models.py` | The `links` table |
| `app/schemas.py` | What requests/responses look like, plus validation |
| `app/service.py` | The business logic (no HTTP here) |
| `app/main.py` | HTTP routes only: translate requests to service calls and errors to status codes |
| `app/base62.py` | Turning numbers into short strings |

Keeping HTTP (`main.py`) separate from logic (`service.py`) is called
**layering**. It means you can test the logic without a web server, and
later swap in a cache or a queue without touching the routes.

### Key concepts

#### 1. Base62 and how short a code can be

A short code needs characters that are safe in a URL: `0-9`, `a-z`, `A-Z`.
That is 62 symbols, so a 7-character code has `62^7 ≈ 3.5 trillion`
possibilities. Base62 is just like converting a number to base 10 or base 2,
but with 62 digits (see `base62.encode`: repeated `divmod` by 62).

> Why not Base64? Base64 adds `+` and `/`, which have special meaning in URLs.

#### 2. Generating unique codes: insert and retry

There are three classic strategies:

| Strategy | How | Pros | Cons |
|----------|-----|------|------|
| Hash the URL (MD5 → first 7 chars) | Deterministic | Same URL → same code | Collisions still possible; can't have two codes for one URL |
| Counter (auto-increment ID → base62) | `id=125 → "21"` | Never collides | Codes are guessable (`/21`, `/22` …); needs one central counter |
| **Random + retry** (what we use) | Random 7 chars, insert, retry on conflict | Unguessable; no coordination | Tiny chance of retry |

The important detail is **how** we check for collisions. A naive version is:

```python
if not exists(code):  # request A and B both see "free"...
    insert(code)  # ...and both insert → duplicate!
```

This is a **race condition** (check-then-act). Instead we rely on the
database's `UNIQUE` constraint: just try to insert, and if the database says
`IntegrityError`, pick another code. The database is the single source of
truth, so this stays correct even with many servers (which matters in step 3).

How often will we retry? With `n` links stored, the chance a random code is
taken is `n / 62^7`. Even with 10 million links that is `0.0003%`.

#### 3. Atomic counters (no lost updates)

Counting clicks the naive way:

```python
link.clicks = link.clicks + 1  # read 5 ... another request also reads 5
session.commit()  # both write 6 → one click lost
```

We instead send `UPDATE links SET clicks = clicks + 1 WHERE code = ...`. The
database does the read and write as one atomic step, so no click is lost.

(In step 4 we will move click counting off the redirect path entirely, because
writing to the database on every redirect is slow at scale.)

#### 4. 301 vs 302 redirects

- `301 Moved Permanently`: browsers **cache** it. Later clicks never reach
  our server, which is fast but means we can't count them or change the target.
- `302 Found`: temporary, browsers ask us every time.

We use 302 because analytics and expiry need every click. Real services
(e.g. Bitly) make the same trade-off.

#### 5. Validation at the edge

`LinkCreate` uses Pydantic's `HttpUrl`, so `"not a url"` or `ftp://...` are
rejected with `422` before our code runs. Aliases must match
`^[A-Za-z0-9_-]{3,32}$`, and words like `api` or `health` are reserved so a user
can't hijack our own routes.

#### 6. Choosing HTTP status codes

| Situation | Code | Why |
|-----------|------|-----|
| Link created | `201 Created` | A new resource exists |
| Alias taken | `409 Conflict` | Request is valid but clashes with current state |
| Bad input | `422 Unprocessable Entity` | FastAPI's default for validation errors |
| Unknown code | `404 Not Found` | |
| Expired link | `410 Gone` | It existed, and is intentionally gone |

#### 7. Database index

`code` has a `UNIQUE` index. Without it, finding a code means scanning every
row (O(n)). A B-tree index makes it O(log n): about 25 steps for 30 million
rows. Redirects are the hottest path, so this index matters most.

#### 8. Connection pooling

Opening a Postgres connection costs a TCP handshake plus authentication
(several milliseconds). `db.py` keeps a pool of 10 open connections that
requests borrow and return. `pool_pre_ping` checks a connection is still alive
before using it.

#### 9. Configuration from the environment

`config.py` reads `DATABASE_URL` etc. from environment variables (the
"12-factor app" rule). The same Docker image runs locally, in CI and in
production; only the environment changes.

#### 10. Testing strategy

- Unit tests for pure logic (`test_base62.py`).
- API tests with FastAPI's `TestClient`: a real HTTP request through the full
  stack, without starting a server.
- The collision test uses `monkeypatch` to **force** a collision, so we can
  prove the retry loop works instead of hoping.
- CI runs the suite twice: fast SQLite, then real PostgreSQL (as a GitHub
  Actions "service container"), because databases differ in small ways
  (SQLite, for example, drops timezone info; see `_as_utc` in `service.py`).

### Try it yourself

```bash
docker compose up --build
curl -X POST localhost:8000/api/links -H 'content-type: application/json' -d '{"url":"https://example.com"}'
curl -i localhost:8000/<code-from-above>
```

Open http://localhost:8000/docs for an interactive UI that FastAPI generates
from the code.

### Exercises

1. **Delete endpoint.** Add `DELETE /api/links/{code}` returning `204`. Write
   a test that the redirect returns `404` afterwards.
2. **Same URL, same code?** Change `create_link` so shortening a URL that
   already exists (without an alias) returns the existing code. What index
   would you need to keep that fast?
3. **Race condition lab.** Temporarily rewrite `record_click` the naive way
   (`link.clicks += 1`), then fire 200 concurrent requests
   (`seq 200 | xargs -P 50 -I{} curl -s -o /dev/null localhost:8000/<code>`).
   Compare the final count with the atomic version.
4. **Math check.** How many characters would you need to store 1 trillion
   links with less than a 1% chance that any single new code collides?

### Interview questions this step prepares you for

- "Design a URL shortener." (Start with exactly this API and data model.)
- "How do you generate unique short IDs?" (Hash vs counter vs random, and the
  check-then-act race.)
- "301 or 302?"
- "Why does an index make lookups fast?"

**Next:** Lesson 2 puts professional quality gates around this code before
we add more features.

---

## Lesson 2: Engineering like a professional team

### What we built

A **quality foundation**: checks that run automatically at three moments,
so mistakes are caught by machines instead of by users.

```
 you type `git commit`          you open a pull request           you push a tag v1.2.3
          │                                │                                │
          ▼                                ▼                                ▼
  pre-commit hooks (seconds)      CI on GitHub (minutes)            CD: release workflow
  - Ruff lint + format            - the same hooks, all files        - full CI again
  - mypy --strict                 - tests on SQLite + PostgreSQL     - Docker image → ghcr.io
  - Power of Ten checker          - coverage gate (95%)                + SBOM + signed provenance
  - actionlint, hadolint          - pip-audit (vulnerabilities)      - GitHub release with notes
  - whitespace, YAML, secrets     - Docker build + smoke test          from CHANGELOG.md
                                  - CodeQL security analysis
```

The idea is called **shifting left**: the earlier a bug is caught, the cheaper
it is. A typo caught by a pre-commit hook costs a second; the same typo found
in production costs an incident.

New files, and why each exists:

| File | Purpose |
|------|---------|
| `pyproject.toml` | Project metadata and the configuration of every tool, in one place |
| `.pre-commit-config.yaml` | The hooks that run on every commit |
| `.github/workflows/ci.yml` | Continuous integration: every PR, every push to `main`, daily |
| `.github/workflows/codeql.yml` | Security analysis |
| `.github/workflows/release.yml` | Continuous delivery on version tags |
| `.github/dependabot.yml` | Automatic dependency update PRs |
| `scripts/check_power_of_ten.py` | Our own static analyzer for the NASA/JPL rules |
| `scripts/smoke_test.py` | End-to-end test of the real Docker containers |
| `scripts/release_notes.py` | Pulls a version's notes out of the changelog |
| `docs/CODING_STANDARD.md` | The rules we follow, and which tool enforces each |
| `CONTRIBUTING.md`, `SECURITY.md`, `CODE_OF_CONDUCT.md` | How to contribute, report vulnerabilities, behave |
| `.github/ISSUE_TEMPLATE/`, `pull_request_template.md`, `CODEOWNERS` | Structured issues and PRs, automatic reviewers |
| `CHANGELOG.md` | Human-readable history of every release |
| `Makefile`, `.editorconfig` | Short commands; consistent editor settings |

### The tools, one by one

#### Ruff: linter and formatter

A **linter** reads code without running it and flags likely bugs and bad
patterns. A **formatter** rewrites code into one consistent style, so nobody
argues about spaces in code review. Ruff does both. It replaces older tools
such as flake8, isort and black, re-implements most of Bandit's security
checks, and is written in Rust, so it checks the whole project in milliseconds.

We switched on a broad set of rule families (listed with comments in
`pyproject.toml`). Things they made us fix in the step 1 code:

- `B904`: inside `except`, write `raise HTTPException(...) from exc`, so the
  original error stays attached to the new one for debugging.
- `N818`: exception names end in `Error` (`AliasTakenError`).
- `B008`: FastAPI's `session = Depends(get_session)` default argument became
  the modern `SessionDep = Annotated[Session, Depends(get_session)]`.
- `UP017`: use `datetime.UTC`, the modern spelling.
- `S` rules (from Bandit) look for security problems: `eval`, hard-coded
  passwords, `subprocess` with `shell=True`, insecure randomness, and more.

#### mypy --strict: static type checking

Type hints (`def get_link(...) -> Link | None`) are checked before the code
ever runs. `--strict` is the most demanding mode: every function must be typed,
and nothing may silently become `Any`.

The most valuable thing it catches is the forgotten `None`. Our old step 1
code did this:

```python
return to_out(service.get_link(session, link.code))
#             ^ get_link returns Link | None, but to_out needs a Link
```

mypy rejects it: *Argument 1 to "to_out" has incompatible type "Link | None"*.
That is exactly NASA/JPL rule 7 ("check every return value"), enforced by a
machine.

#### pytest, coverage and "warnings are errors"

- **Coverage** measures which code the tests executed. We use **branch
  coverage**: for `if x:` it checks that the tests took *both* the true and the
  false path, not just that the line ran. CI fails below 95%. We're at about 99%.
- 100% coverage does not mean bug-free: it shows what was *run*, not what was
  *checked*. It is a floor, not a goal.
- `filterwarnings = ["error"]` turns every warning during the tests into a
  failure. It caught a real one right away: Starlette's test client warned
  that using `httpx` is deprecated, so we moved to `httpx2`.

#### pre-commit: the same checks, before every commit

`pre-commit install` registers a git hook. From then on, `git commit` first
runs the hooks on the files you changed, and refuses the commit if one fails
(many, like the formatter, fix the problem for you; just `git add` again).
CI runs `pre-commit run --all-files` with the very same tool versions (they
are locked in `requirements-dev.txt`), so **your laptop and CI run the same
checks**. "The same" takes care: actionlint also checks the shell scripts
inside workflows, but only if it can find a program called `shellcheck`.
GitHub's runners have one; a laptop usually doesn't, and actionlint then
skips that check without a word. So `shellcheck-py` is pinned too.

Extra hooks: `actionlint` checks the GitHub workflow files, `hadolint` checks
the Dockerfile, `detect-private-key` stops you committing a secret key.

Every hook in `.pre-commit-config.yaml` is a `repo: local` hook with
`language: system`: pre-commit runs its command in your environment, where
the tools from `requirements-dev.txt` are installed. That includes the file
hygiene hooks such as `trailing-whitespace` and `check-yaml`, which come from
the `pre-commit-hooks` package. The usual setup points pre-commit at the
hooks' GitHub repository instead, by a tag such as `rev: v6.0.0`. pre-commit
then clones that repository and installs it with pip, without hashes, taking
whatever versions of its dependencies are newest at that moment. That code
would escape our lock files, and a tag can be moved, as the next section
shows. With local hooks pre-commit downloads nothing, so CI has nothing to
cache for it.

#### Supply-chain security: pip-audit, Dependabot, pinned actions

Most code we run, we didn't write: it comes from dependencies. A **supply-chain
attack** hides malicious code in one of them.

- **Lock files with hashes.** `requirements.in` lists the 6 packages the app
  imports. Those need about 18 more (Starlette, anyio, h11, ...). Pinning only
  the 6, as this repo first did, still lets pip pick the newest version of the
  other 18 at install time, so CI, the Docker build and your laptop could each
  get different code. `pip-compile` (from pip-tools) resolves the whole tree
  once and writes `requirements.txt`: every package at an exact version, plus
  the SHA-256 hash of every file pip may download for it. `pip install
  --require-hashes` refuses any file whose hash differs, so a package that was
  tampered with, or quietly re-uploaded, cannot get in. You edit the `.in`
  files and run `make lock`; Dependabot does the same for updates.
- **The base image pinned by digest:** `FROM python:3.13-slim@sha256:e48f…`.
  `3.13-slim` is a moving name: Docker rebuilds it for every Python patch and
  every Debian security fix. The digest is the hash of one exact image, so the
  same Dockerfile always starts from the same bytes. Dependabot proposes the
  new digest when the tag moves.
- **pip-audit** checks those versions against a database of known
  vulnerabilities (CVEs). It runs on every PR *and daily*, because new
  vulnerabilities are published about code that didn't change.
- **Dependabot** opens PRs to update dependencies; CI then tells us whether the
  update is safe to merge.
- **Actions pinned to commit SHAs**: `actions/checkout@3d3c42e…  # v7.0.1`
  instead of `@v7`. A tag can be moved to point to different code; a commit SHA
  cannot. In March 2025 attackers did exactly that to the popular
  `tj-actions/changed-files` action: they moved its tags to a malicious commit,
  and repositories that referenced it by tag ran the attacker's code, which
  printed their secrets into the build logs. Dependabot updates the SHAs for us.
- **Least privilege**: `permissions: contents: read` means a compromised step
  can't push code; only the release job may write, and only what it needs.

#### CodeQL: following data through the program

CodeQL turns the code into a database and asks questions like "can text from
an HTTP request reach a database query, a shell command or a file path without
being checked?" (called **taint tracking**). Linters look at one line at a
time; CodeQL follows data across functions and files. Ours also scans the
workflows themselves for mistakes such as untrusted input inside a `run:`
script. Findings appear in the repository's **Security** tab.

#### The Docker smoke test

Unit tests check the code. The smoke test checks the thing we actually ship:
CI builds the image, starts the real stack with
`docker compose up --wait` (which waits for every container's health check),
and `scripts/smoke_test.py` creates a link, follows the redirect and checks the
click count over real HTTP. A broken Dockerfile, a missing dependency in
`requirements.txt` or a container that can't reach the database all pass the
unit tests, and all fail here.

The Dockerfile itself got hardened: the app runs as a **non-root user**, has a
`HEALTHCHECK`, and the code is owned by root, so even a compromised app can't
modify it.

Health checks must test what the next service really needs. The database's
check is `pg_isready -h 127.0.0.1`, over TCP, the way the app connects. On its
very first start, the Postgres image runs a temporary server that listens only
on a Unix socket while it creates the database, then restarts. A socket check
(`pg_isready` without `-h`) can call that temporary server healthy, Compose
starts the app, and the app's first connection is refused. Rare, but exactly
the kind of failure that makes CI fail one run in fifty.

#### CD: versions, changelog, releases

- **Semantic Versioning**, `MAJOR.MINOR.PATCH`: bump PATCH for bug fixes,
  MINOR for new features that don't break anyone, MAJOR for breaking changes.
  Before 1.0.0, anything may change.
- **Keep a Changelog** (`CHANGELOG.md`): an *Unreleased* section collects
  changes, grouped as Added / Changed / Fixed / Security; a release renames
  it to the version number.
- **Conventional Commits**: `feat: ...`, `fix: ...`, `docs: ...`. Readable
  history, and tools can generate changelogs and version bumps from it.
- **The release workflow**: pushing tag `v0.2.0` runs the full CI again, checks
  that the tag equals `app.__version__` (a test checks the changelog has notes
  for that version), pushes the Docker image to `ghcr.io`, and creates the
  GitHub release. The image comes with an **SBOM** (a list of every package
  inside it) and a **signed provenance attestation**: anyone can verify that
  this exact image was built by this workflow from this commit.

### NASA/JPL's "Power of Ten"

In 2006, Gerard Holzmann of NASA's Jet Propulsion Laboratory published ten
rules for safety-critical code (*IEEE Computer*, "The Power of Ten: Rules for
Developing Safety-Critical Code"). JPL's coding standard for C builds on them,
and it was used for the flight software of the Curiosity Mars rover. The goal
is not style: it's code **so simple that tools can prove things about it**, for
example that a loop ends or that the stack can't overflow. When your software
is on Mars, hundreds of millions of kilometres away, nobody can walk over and
restart it.

The rules were written for C. Here is what each one means and how we adapted
it to Python. The full table, including which tool enforces each rule, is in
[docs/CODING_STANDARD.md](docs/CODING_STANDARD.md).

**Rule 1: simple control flow, no recursion.** Recursion makes stack depth
depend on the input, so you can't bound memory use. (Python stops at about
1000 nested calls with `RecursionError`.) Any recursion can be rewritten as a
loop with an explicit, bounded work list. We also cap cyclomatic complexity
(the number of independent paths through a function) at 8.

**Rule 2: every loop has a fixed upper bound.** A tool must be able to see that
the loop ends. We ban `while` loops entirely; loops are `for` over a finite
range. You already met one in lesson 1:

```python
for _ in range(MAX_ATTEMPTS):  # at most 5 tries, provably
    if _insert(session, link):
        return link
raise CodeSpaceExhaustedError
```

`base62.encode` used `while num:`. It did end, but proving it takes math a
checker can't do. The new version states the bound in the code:

```python
MAX_VALUE = 2**64 - 1  # we only encode 64-bit numbers...
MAX_LENGTH = 11  # ...and 62**11 > 2**64, so 11 digits always suffice

if not 0 <= num <= MAX_VALUE:
    raise ValueError(...)
for _ in range(MAX_LENGTH):
    num, remainder = divmod(num, BASE)
    digits.append(ALPHABET[remainder])
    if num == 0:
        break
```

The distributed-systems version of this rule: **every wait has a timeout**.
A network call without a timeout is an unbounded loop in disguise. Talking to
Postgres involves several different waits, and each needs its own limit
(see `app/db.py`). One of them still has none:

| Wait | Limit | What happens without it |
|------|-------|-------------------------|
| Opening a connection | `connect_timeout` = 5 s | A server that doesn't answer blocks the request forever |
| Getting a connection from our pool | `pool_timeout` = 10 s | Under load, requests queue without end |
| Running one SQL statement | `statement_timeout` = 5 s (Postgres cancels it) | One slow query holds a connection forever |
| Waiting for a row lock | `lock_timeout` = 2 s | A transaction that holds a lock freezes everyone behind it |
| A dead network | TCP keepalives and `tcp_user_timeout` | The server vanished, but we keep waiting for its answer |
| A server that is alive but stuck (a paused container, a hung pooler or proxy) | **None** in the app | The request's thread and its pooled connection wait until the server answers |

Each limit covers only its own case. `statement_timeout` and `lock_timeout`
are enforced by Postgres itself, so they need a server that answers.
Keepalives and `tcp_user_timeout` only notice a dead peer or network: they
fire when the other machine stops acknowledging our packets, and the kernel of
a machine whose Postgres is frozen still acknowledges them. (A paused VM is
different: its own kernel stops too, nothing acknowledges our packets, and
`tcp_user_timeout` ends the wait as it does for a dead network, unless a port
forwarder on the host keeps answering for it.) libpq, the C library under
psycopg, has no client-side read timeout. So nothing in the app
bounds a query sent on an open connection to a server that is alive but stuck.
(Opening a new connection to it still fails after 5 s: `connect_timeout`
covers the whole handshake.)

Our first version only had the first two limits. An independent review caught
the gap: once a connection was open, nothing stopped a query from waiting
forever for its answer. The limits we added next cover a slow query, a lock
and a dead network. A later review froze the database with `docker pause`, and
a lookup waited exactly as long as the pause (the last row). That is a lesson
in itself: "we have timeouts" is not the same as "every wait has a timeout",
and only a test of each case shows which waits are covered. Every CI job also
has `timeout-minutes`.

**Rule 3: no dynamic memory allocation after start-up.** In C, `malloc` can
fail or fragment memory at the worst moment. Python allocates constantly, so we
keep the *purpose*: **resources are bounded**. The connection pool has a fixed
size, and every input has a maximum: URLs up to 2048 characters (new, with
tests), aliases up to 32. Without a limit, one request with a 50 MB "URL" would
be stored, and later cached, forever.

The limit must hold for what we store, not only for what we were sent.
Pydantic normalizes a URL, which percent-encodes non-ASCII characters in the
path: `é` (2 bytes in UTF-8) is stored as `%C3%A9` (6 characters). Its
`max_length` counts the bytes of the input as typed, so on its own it accepted
a 2048-byte URL made mostly of emoji, then stored it, and sent it back in
`Location`, as 6104 characters. `app/schemas.py` keeps it as a cheap first
check, then measures the normalized URL.

There is a catch: Pydantic checks the URL only *after* FastAPI has read and
parsed the whole request body. A 200 MB request would fill the server's memory
before any check runs. So the outermost input gets a limit too:
`app/limits.py` refuses any body over 16 KB with `413 Content Too Large`,
before reading it. Check sizes at the door, not after the guest is inside.

**Rule 4: functions fit on one page (60 lines).** Short functions can be
understood, tested and reviewed. Our checker enforces 60 lines; Ruff also
limits statements (30), branches (8), returns (4) and arguments (6).

**Rule 5: assertions, on average at least two per function.** Holzmann asks
for an *assertion density* of at least two per function on average, with no
side effects, and a recovery action when one fails. Assertions check that what
"can't happen" really doesn't. Python's `assert` disappears when Python runs
with `-O`, so in app code we write real checks instead:
`if not 0 <= num <= MAX_VALUE: raise ValueError(...)`. All input from outside
is validated by Pydantic before our code sees it. (Ruff rule `S101` forbids
`assert` outside tests.)

**Rule 6: smallest possible scope.** No `global` or `nonlocal`: a variable
that anything can change from anywhere is impossible to reason about. Python
has no block scope, so we keep functions small instead (rule 4) and prefer
immutable data: `frozenset` for `RESERVED`, frozen dataclasses later.

**Rule 7: check return values; validate parameters.** In Python, errors are
exceptions, so the rule becomes: never swallow an exception (no bare `except:`,
no `except Exception: pass`), and let `mypy --strict` force every possible
`None` to be handled, as shown above. Watch out for one trap: for
`try: ... except Exception: pass`, Ruff suggests `contextlib.suppress(Exception)`,
which swallows exactly the same errors but passes every Ruff rule. Our checker
reports it (R7).

**Rule 8: limit the preprocessor.** C macros can make code mean something
different from what it looks like. Python's equivalent is code that writes or
loads code at run time: `eval`, `exec`, `compile`, `__import__`,
`importlib.import_module`. Banned, because no tool can analyze code that
doesn't exist yet.

**Rule 9: restrict pointers.** Python has references instead of pointers. The
danger is the same: two parts of the program changing one object through
different references. Hence no mutable default arguments (`def f(x=[])` is a
classic Python bug), immutable data where possible, and no function pointers
except framework hooks like FastAPI's `Depends`.

**Rule 10: all warnings on from day one, zero warnings, daily analysis.** This
is the whole lesson in one rule: Ruff with a broad rule set, `mypy --strict`,
warnings as test failures, CodeQL, and a CI run every day. "Zero" matters:
with 200 old warnings nobody notices the 201st.

### Our own static analyzer

Ruff and mypy can't check rules 1, 2, 4, 6 and 8 the way JPL meant them (nor
the `suppress` trap of rule 7), so we wrote
[`scripts/check_power_of_ten.py`](scripts/check_power_of_ten.py), about 900
lines. It uses Python's `ast` module, which turns source code into a tree:

```python
>>> import ast
>>> print(ast.dump(ast.parse("while True: pass")))
Module(body=[While(test=Constant(value=True), body=[Pass()])])
```

Each rule is a function that walks the tree looking for a node type: `While`
for rule 2, `Global` and `Nonlocal` for rule 6, calls to `eval` for rule 8.
Imports are resolved first, so `import itertools as it; it.count()` is caught
as surely as `itertools.count()`.

Rule 1 is the interesting one. The checker builds a **call graph** (which
function may call which) and reports every function that can reach itself in
that graph. To know what a name like `walk` refers to, it applies Python's own
scoping rule, **LEGB**: Local, then Enclosing functions, then Global (the
module), then Builtins. Class bodies are skipped, which is why a method `sleep`
that calls `sleep(1)` calls `time.sleep`, not itself. It also follows
`self.m()` (including an override of `m` in a subclass), `ClassName.m()`,
`super().m()`, `ClassName(...)` and `cls(...)` (which run `__init__` and
`__new__`), and functions passed as values, as in `map(walk, nodes)`. The
checker follows its own rules, so even that graph search is a bounded `for`
loop instead of the usual recursive depth-first search.

Python also calls methods for you, and those calls can recurse just as well.
Reading `self.x` runs a property's getter, `__getattribute__`, and
`__getattr__` when nothing defines `x`. `self.x = v` runs the property's
setter and `__setattr__`, `del self.x` its deleter and `__delattr__`, and
`self(...)` runs `__call__`. So a setter that does `self.x = v` calls itself,
though no line says `x(...)`. The checker follows these calls on `self`.
"Nothing defines `x`" is a judgment: the checker looks for `x` in the class
bodies, for `Class.x = v` anywhere in the module, and for code that sets it on
`self` (`self.x = v`, `setattr(self, "x", v)`, `self.__dict__["x"] = v`,
`vars(self).update(x=v)`). It compares names the way Python stores them:
inside class `C`, `self.__x` is really `self._C__x` ("name mangling"), while
the string in `setattr(self, "__x", v)` stays as written. When a class gets
attributes from code it cannot read, it assumes `x` may be there and says
nothing. That covers a metaclass, a base class it cannot trace to a `class`
statement in the module (one from another module, such as `dict` or
`Exception`, but also an alias such as `Shape = Base`: only `Base`, and
`Outer.Inner` with `Inner` written in `Outer`'s own body, are traced), and
names known only at run time (`setattr(self, key, v)`,
`self.__dict__.update(values)`, or a whole new `self.__dict__`).

It is still an approximation, like every static analyzer for a dynamic
language: it only knows the classes of one module, and it cannot see calls
through other objects (`node.walk()`, `self.__class__(...)`), across modules,
through `getattr`, `setattr` or metaclasses, through descriptors other than
decorated methods, or through the special methods that operators and
built-ins run (`len(self)`, `self == other`, `self[k]`, `with self`,
`str(self)`). Nor does it follow Python's fallback to `__getattr__` when a
property's getter raises AttributeError. It takes `super().m` to mean `m` in
the bases of the class that calls it, though with multiple inheritance Python
picks the next class in the instance's method resolution order (MRO). It can
also be wrong the other way: `self` and `cls` stand for every subclass in the
module, so a factory such as `Base.make()`, which runs `cls(...)`, is reported
when a subclass's `__init__` calls it, though `cls` is `Base` there. Code like
that needs a waiver, `# p10-waiver: R1 <reason>` (see below).

Knowing what your tool can't see is part of using it well. The first version
was simpler, and an independent review found seven kinds of recursion it
missed; a later review found six more, through properties, special methods
and subclasses. A third review found the opposite mistake: false alarms on
classes whose attributes come from outside the module. A fourth and a fifth
found gaps in both directions in how it decides what a class defines, such as
taking `vars(self).update(x=v)` to set any name at all, or missing that
Python stores `self.__x` as `self._C__x`. Each one is now a test case.

Rules sometimes need breaking. A waiver must name the rule and give a reason,
right on the line, so a reviewer can judge it:

```python
while True:  # p10-waiver: R2 server loop, runs until the process stops
```

The checker has its own tests (`tests/test_power_of_ten.py`), including one
that runs it over this whole repository, so a rule violation also fails `pytest`.

### Try it yourself

```bash
make install    # once: tools + git hook
make check      # what CI runs, except Docker and the PostgreSQL tests
```

Then break a rule on purpose: add `while True: pass` to `app/service.py` and
try to commit. The hook stops you with
``app/service.py:NN: R2 `while` loop has no fixed upper bound; use `for _ in range(MAX_...)` ``. Open the
**Actions** tab on GitHub to watch the CI jobs of a pull request.

### Exercises

1. **A new rule.** Unbounded caches break rule 3: add a check that reports
   `@functools.cache` and `@lru_cache(maxsize=None)`. Write the tests first.
2. **Watch CI fail.** On a branch, add an unused import and push. Read the CI
   log, fix it with `make format`, push again.
3. **Protect `main`.** In the repository settings, require pull requests and
   the CI checks to pass before merging (see CONTRIBUTING.md). Then try to
   push directly to `main`.
4. **Cut a release** on a fork: bump the patch number of `__version__` in
   `app/__init__.py` (for example `0.1.0` becomes `0.1.1`), move the changelog
   section, tag `v` plus that version, push the tag, and verify the published
   image with
   `gh attestation verify oci://ghcr.io/<you>/url-shortener:<version> --repo <you>/url-shortener`.
5. **Coverage is not correctness.** Write a test that reaches 100% coverage of
   `base62.encode` but would still pass if the function were wrong. What makes
   a test actually *check* something?

### Interview questions this lesson prepares you for

- "What's the difference between CI and CD?"
- "Linting, type checking and testing: what does each catch that the others don't?"
- "Line coverage versus branch coverage? Is 100% coverage enough?"
- "What is a supply-chain attack, and how do you defend against one?"
- "Why pin GitHub Actions to a commit SHA rather than a tag?"
- "Why would safety-critical code ban recursion and unbounded loops?"
- "What does a MAJOR version bump mean in SemVer?"
