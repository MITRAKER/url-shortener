# Learning notes

This file grows with every step of the project. Each lesson explains **what**
was built, **why** it was designed that way, the **key concepts**, and ends
with **exercises** so you can practise building it yourself.

- [Lesson 1: The core service](#lesson-1-the-core-service)
- [Lesson 2: Engineering like a professional team](#lesson-2-engineering-like-a-professional-team)
  (CI/CD, code quality, security scanning, NASA/JPL's Power of Ten)
- [Lesson 3: Caching and rate limiting](#lesson-3-caching-and-rate-limiting) (step 2)

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

- **Lock files with hashes.** `requirements.in` lists the 7 packages the app
  imports. Those need about 18 more (Starlette, anyio, h11, ...). Pinning only
  the direct ones, as this repo first did, still lets pip pick the newest version of the
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
except framework hooks like FastAPI's `Depends` and injected dependencies that
keep side effects testable: a clock that a test can move by hand, or the
operation given to `RedisGateway.call` (both in lesson 3).

**Rule 10: all warnings on from day one, zero warnings, daily analysis.** This
is the whole lesson in one rule: Ruff with a broad rule set, `mypy --strict`,
warnings as test failures, CodeQL, and a CI run every day. "Zero" matters:
with 200 old warnings nobody notices the 201st.

### Our own static analyzer

Ruff and mypy can't check rules 1, 2, 4, 6 and 8 the way JPL meant them (nor
the `suppress` trap of rule 7), so we wrote
[`scripts/check_power_of_ten.py`](scripts/check_power_of_ten.py), about 650
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
bodies and for code that sets it on `self` (`self.x = v`,
`setattr(self, "x", v)`, `self.__dict__["x"] = v`). When a class gets
attributes from code it cannot read (a base class from another module, such
as `dict` or `Exception`, a metaclass, or `setattr(self, key, v)` with a name
known only at run time), it assumes `x` may be there and says nothing.

It is still an approximation, like every static analyzer for a dynamic
language: it only knows the classes of one module, and it cannot see calls
through other objects (`node.walk()`, `self.__class__(...)`), across modules,
through `getattr`, `setattr` or metaclasses, through descriptors other than
decorated methods, or through the special methods that operators and
built-ins run (`len(self)`, `self == other`, `self[k]`, `with self`,
`str(self)`). It takes `super().m` to mean `m` in the bases of the class that
calls it, though with multiple inheritance Python picks the next class in the
instance's method resolution order (MRO). It can also be wrong the other way:
`self` and `cls` stand for every subclass in the module, so a factory such as
`Base.make()`, which runs `cls(...)`, is reported when a subclass's `__init__`
calls it, though `cls` is `Base` there. Code like that needs a waiver,
`# p10-waiver: R1 <reason>` (see below).

Knowing what your tool can't see is part of using it well. The first version
was simpler, and an independent review found seven kinds of recursion it
missed; a later review found six more, through properties, special methods
and subclasses. A third review found the opposite mistake: false alarms on
classes whose attributes come from outside the module. Each one is now a test
case.

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

**Next:** Lesson 3 adds a Redis cache and rate limiting, held to these standards.

---

## Lesson 3: Caching and rate limiting

Step 2 puts **Redis**, an in-memory data store, in front of PostgreSQL. It
does two jobs: it remembers where short codes point (a **cache**), and it
counts how fast each client is sending requests (a **rate limiter**).

### What we built

The path of a redirect now looks like this:

```
GET /abc1234
  │
  ├─ 1. rate limiter: one atomic script in Redis ───── bucket empty? → 429 Too Many Requests
  ├─ 2. does "abc1234" even look like a code? ──────── no → 404, touching nothing
  ├─ 3. Redis: GET link:v1:abc1234
  │       ├─ found ─────────────────────────────────── redirect, X-Cache: HIT
  │       ├─ not found → PostgreSQL SELECT → save in Redis → redirect, X-Cache: MISS
  │       └─ Redis down → PostgreSQL SELECT ──────────── redirect, X-Cache: BYPASS
  └─ 4. count the click (still a PostgreSQL write; step 4 moves it off this path)
```

| File | What's new |
|------|------------|
| `app/redis_client.py` | `RedisGateway`: the one door to Redis, with strict timeouts and a circuit breaker |
| `app/breaker.py` | The circuit breaker: after 3 Redis failures in a row, skip Redis for 5 seconds |
| `app/cache.py` | `LinkCache`: get, set, and "remember that this code does not exist" |
| `app/ratelimit.py` | The token bucket (a Lua script) and the `RateLimit` FastAPI dependency |
| `app/service.py` | `resolve()` (cache first, then database), write-through on create |
| `app/main.py` | Rate limits on the routes, the `X-Cache` header, the code-shape check |
| `app/config.py` | `REDIS_URL`, checked when the app starts |
| `docker-compose.yml` | A `redis` container with a 64 MB memory budget |
| `tests/test_cache.py`, `test_ratelimit.py`, `test_redis_client.py`, `test_breaker.py`, `test_config.py` | 89 new tests |
| `tests/fakes.py` | `SilentServer`: a fake Redis that accepts connections and never answers |

### Part 1: Caching

#### Why cache at all?

Two reasons: speed, and protecting the database.

On the development machine, both on localhost, with 100,000 links:

| Lookup | Median | 99th percentile |
|--------|--------|-----------------|
| PostgreSQL `SELECT` by indexed code (through SQLAlchemy) | 269 µs | 798 µs |
| Redis `GET` | 61 µs | 119 µs |

About 4 times faster in the median and almost 7 times faster at the 99th
percentile ("p99": 99% of requests are faster than this). Those *tail
latencies* are what users notice. The bigger win shows under load: the
database is the one component that's hard to scale, and every request the
cache answers is a request the database never sees.

URL shorteners are a perfect case: they are **read-heavy** (a link is created
once and clicked many times), and popularity is **skewed** (a few viral links
get most of the clicks), so a small cache answers most requests.

#### Caching patterns

| Pattern | Reads | Writes | Used here |
|---------|-------|--------|-----------|
| **Cache-aside** (lazy loading) | App checks the cache; on a miss it reads the DB and fills the cache | App writes the DB only | ✅ for redirects |
| **Read-through** | Like cache-aside, but the cache library loads from the DB itself | – | |
| **Write-through** | – | App writes DB *and* cache together | ✅ on create |
| **Write-behind** | – | App writes the cache; the DB is updated later, in the background | (step 4, for click counts) |

Why write-through on create? A new short link is usually shared and clicked
right away, so we cache it immediately instead of making the first visitor
pay for a miss.

#### TTL and jitter

Every cache entry gets a **TTL** (time to live): Redis deletes it after about
an hour. That bounds memory and bounds how long a stale entry can live.

But if 10,000 links are cached in the same second (say, after a deploy),
they would all expire in the same second, and 10,000 requests would hit the
database at once. That is a **cache avalanche**. The fix is **jitter**: each
entry gets a random TTL between 54 and 66 minutes (`ttl_with_jitter`), so
expiries spread out.

#### Negative caching

What about codes that don't exist? Without care, every request for
`/doesnotexist` misses the cache *and* queries the database. An attacker can
send millions of made-up codes, and the cache doesn't help at all. That is
**cache penetration**. Two defenses:

1. **Validate first.** A code must look like a code: 3 to 32 characters of
   `A-Z a-z 0-9 _ -`. `/../../etc/passwd` or a 10 KB string is rejected with
   404 before touching the cache or the database (only the rate limiter's
   script runs in Redis).
2. **Cache the absence.** If the database says "not found", we store an empty
   string under that key for 60 seconds. Repeated lookups now hit Redis.

(A third defense, for huge systems, is a **Bloom filter**: a compact
structure that can say "definitely not present" without storing every code.)

#### Cache stampede

When one *very* popular entry expires, many requests miss at the same moment
and all query the database for the same row. That's a **cache stampede** (or
"thundering herd"). For us it's cheap, one indexed lookup per request, so we
accept it. Big systems use a **lock** (only the first request refills the
entry; the others wait or get the old value) or **early refresh** (refill
shortly *before* expiry). The exercises include building the lock.

> Penetration, stampede and avalanche are a classic interview trio. You now
> know all three and a defense for each.

#### Invalidation, and a race we closed

> "There are only two hard things in Computer Science: cache invalidation and
> naming things." (Phil Karlton)

A cache is a *copy*. When the original changes, the copy is wrong until it is
updated or deleted. `test_cache_hit_does_not_read_the_database` shows this on
purpose: it changes a link's target directly in the database, and the
redirect still goes to the old target, because the cache answered. So every
code path that changes a link must also update or delete its cache entry. Our
links never change after creation, which keeps this easy. (Lesson 1's
"delete endpoint" exercise would need to delete the cache entry too.)

One subtle **race condition** remained, between "not found" and "create":

```
time │ request A: GET /promo                  request B: POST alias=promo
  1  │ cache miss
  2  │ database: not found
  3  │                                        database: insert promo ✓
  4  │                                        cache: set promo → URL (write-through)
  5  │ cache: set promo → "not found"   ✗ overwrites B's entry!
```

For 60 seconds, `/promo` would answer 404 although it exists. The fix is one
flag: A writes "not found" with `SET ... NX` ("only if the key doesn't exist").
At step 5 the key exists, so nothing happens. If the steps happen in the other
order, B's write simply replaces A's. Both orders are now correct, and
`test_not_found_never_overwrites_a_real_entry` checks it.

Both orders rely on B's write reaching Redis, though. If it fails (a Redis
error or timeout, or the circuit is open), A's "not found" entry wins, and
`/promo` answers 404 with `X-Cache: HIT` for up to 60 seconds
(`CACHE_NEGATIVE_TTL_SECONDS`), although B got `201 Created`. We accept that
window, and keep it short, rather than make creating a link depend on Redis.
`test_failed_write_through_leaves_the_not_found_entry` shows it.

#### What we deliberately don't cache

Click counts change on every redirect, so the stats endpoint always reads the
database. The cache holds only what a redirect needs: the target URL and the
expiry time. (We store the expiry time and check it on every hit, so an
expired link answers 410 even when it comes from the cache.)

#### Key design and eviction

Keys look like `link:v1:abc1234`. The prefix keeps cache keys apart from
rate-limit keys, and the `v1` lets a future version change the stored format
by switching to `v2`, without reading old entries it can't understand.

Redis gets a fixed memory budget in `docker-compose.yml` (64 MB, NASA/JPL rule
3). When it's full, Redis must **evict** something. We chose `allkeys-lfu`:
evict the **least frequently used** key. The more common LRU (least
*recently* used) has a weakness here: a crawler visiting 100,000 old links
once each would push the viral links out. LFU remembers that the viral links
are used constantly.

#### When Redis is down: graceful degradation

The cache is an optimization, so its failure must not become our failure. If
Redis is down, a redirect still works: it reads the database and answers with
`X-Cache: BYPASS`, and the rate limiter lets the request through. This is
called **failing open**, or **graceful degradation**.

Every Redis call goes through one door, `RedisGateway.call` in
`app/redis_client.py`. It runs the operation; if Redis raises an error, it
logs a warning and returns a fallback value instead ("unavailable" for the
cache, "no decision" for the rate limiter). One door means one place decides
what a Redis failure costs, instead of a `try/except` in every caller. Errors
that are *not* Redis errors still raise: a bug in our own code must never be
mistaken for "Redis is down".

Failing open only works if failure is **fast**, and two kinds of "down"
behave very differently:

| Redis is... | A call gets | After |
|-------------|-------------|-------|
| **stopped** (nothing listening) | "connection refused" | about 0 ms |
| **hung** (frozen, overloaded, network cut) | silence | the full timeout |

The hung Redis is the dangerous one. Every call waits for its timeout, and a
redirect makes up to three Redis calls: the rate limiter, the cache read and
the cache write. An independent review measured the first version of this
step against a hung Redis: **1.5 seconds per redirect**, and 120 concurrent
redirects took 6.2 s instead of 0.8 s. Three defenses fixed it:

1. **Every wait has a bound** (NASA/JPL rule 2): connecting and each command
   give up after 0.25 s.
2. **Don't wait twice in one request.** If the cache read failed, `resolve()`
   doesn't write the answer back to Redis: that would only wait for another
   timeout.
3. **A circuit breaker** (`app/breaker.py`), named after the one in a fuse
   box. After 3 failures in a row it **opens**: for the next 5 seconds, every
   call skips Redis at once instead of waiting. Then it lets **one** trial
   call through (**half-open**). If the trial works, the breaker **closes**
   and traffic flows again; if not, it stays open for another 5 seconds.

```
 closed ──(3 failures in a row)──► open ──(5 s later)──► half-open
   ▲                                 ▲                        │
   │                                 └─────(trial fails)──────┤
   └───────────────────────(trial works)──────────────────────┘
```

Now a hung Redis costs three timeouts in total, then nothing, plus one
0.25 s trial every 5 seconds to notice when Redis is back.
`test_survives_a_hung_redis_quickly` proves it with a fake Redis that
accepts connections and never answers: once the circuit is open, five
redirects take well under a second and don't touch Redis at all. The breaker
is shared by every request in the process and protected by a lock, because
requests run on many threads at once; `test_only_one_of_many_concurrent_callers_gets_the_trial`
lets 50 threads race for the trial call, and exactly one wins.

#### Retries, and a lesson about measuring

The first version of this step said: "redis-py's default settings retry 10
times with growing pauses: 3.4 seconds per call." The review checked that
against the library's source, and it was wrong for our code. That default
belongs to the `Redis(host=..., port=...)` constructor (we measured 4.4 s
with Redis stopped). `make_redis` uses `Redis.from_url`, which **doesn't
retry at all** by default, so our "fix" had *added* a retry. And the test
meant to catch a slow outage couldn't, because it only tried a stopped
Redis, where every attempt fails instantly. Two habits to take away: measure
the code you actually run, and check that your test fails without your fix.

Should a failed call be retried at all? It depends on the error:

- A **connection error** usually means the command never reached Redis (for
  example, Redis restarted and dropped the connection). One retry on a fresh
  connection is cheap and often succeeds.
- A **timeout** means "no answer yet". The command may well have run; only
  the reply is missing. Retrying runs it **again**. For a `GET` that's
  harmless. For the rate limiter's script it is not: the review showed a slow
  reply making one request pay **two** tokens.

An operation that has the same effect however many times it runs is
**idempotent**: `GET`, or `SET key value`. "Take a token" is not. So
`make_redis` retries once, at once, and only on a `ConnectionError`; a
timeout is never retried (`test_only_a_broken_connection_is_retried`).
Strictly, even a connection error can hide a command that ran: Redis may run
the script and then lose the connection before the reply arrives. That
window is tiny and costs one token, so we accept it. Knowing exactly which
risk you accepted, and why, is the difference between a decision and an
accident.

#### A typo should stop the app, not every request

`REDIS_URL=localhost:6379` (no `redis://`) used to start without complaint and
report itself healthy, then answer every request that touched Redis with a
500, because the URL was first parsed on the first request. Now `Settings`
checks it at startup (`app/config.py`): building a connection pool parses the
URL without connecting, and a malformed URL stops the app with a clear error.
The rule: check configuration when the process starts (**fail fast**), so a
bad deploy never looks healthy.

One more detail: that error ends up in the logs, and a URL can hold a password
(`redis://:password@host`). By default, Pydantic's error message repeats the
value it rejected, so `Settings` sets `hide_input_in_errors=True`. A review
showed that this was not enough, because redis-py's own message can quote part
of the URL. In `redis://:S3cret#Pass@redis:6379/0`, the `#` in the password is
not percent-encoded, so the parser stops there and takes `S3cret` for the
port. The error read "Port could not be cast to integer value as 'S3cret'".
A `/` or a `?` does the same, and generated passwords often contain a `/`.
Now `_check_redis_url` catches redis-py's `ValueError` and raises its own,
with a fixed message: it names the setting and the problem, never the value
or any part of it.

#### Making the cache visible

The `X-Cache` header is borrowed from CDNs: `HIT` (answered from Redis),
`MISS` (read from the database and now cached) or `BYPASS` (read from the
database and not cached: Redis is not configured, down, or the circuit is
open, or the cache write did not go through). It costs nothing, and
`curl -i` tells you at once what happened.

A header like this is only useful if it tells the truth. The first version
sent `MISS` even when the cache write had failed, so the next request missed
again. Now the cache's writes report whether Redis stored the value, and
`resolve()` sends `MISS` only when it did. If the write failed, or
`SET ... NX` found a link created a moment ago, the header says `BYPASS`.

### Part 2: Rate limiting

#### Why

Without limits, one client can create millions of links (spam, phishing) or
flood redirects until the service is slow for everybody. A **rate limiter**
gives each client a budget: here 10 link creations and 300 reads per minute
per IP address. Over the budget, the answer is `429 Too Many Requests`.

#### The algorithms

| Algorithm | How it works | Weakness |
|-----------|--------------|----------|
| **Fixed window** | Count requests per clock minute; reset at :00 | 10 requests at 0:59 and 10 more at 1:00: 20 in two seconds |
| **Sliding log** | Store each request's timestamp; count those in the last 60 s | Exact, but stores one entry per request |
| **Sliding window counter** | Weighted mix of the current and previous window's counts | An approximation, but cheap |
| **Token bucket** (ours) | A bucket holds up to N tokens; each request takes one; tokens refill at a steady rate | Needs a little arithmetic |
| **Leaky bucket** | Requests queue and leave at a fixed rate | Smooth output, but adds waiting |

The **token bucket** allows short bursts (a full bucket of 10) but enforces
the average (one new token every 6 seconds for 10/minute). It needs just two
numbers per client: the tokens left, and when we last looked.

```
capacity 10, refill 1 token / 6 s

tokens  10 ██████████  a new client starts full
         7 ███████     after a burst of 3 requests
         8 ████████    6 seconds later: one token earned
         0             after 8 more requests → the next one gets 429, Retry-After: 6
```

#### Why the bucket lives in Redis

In step 3 we will run three copies of the app behind a load balancer. If each
copy counted in its own memory, a client would get three buckets, and three
times the limit. The bucket must live in one shared place: Redis.

#### Atomicity: why a Lua script

The obvious Python version has a race:

```python
tokens = redis.get(key)  # two app instances both read 1 token...
if tokens >= 1:
    redis.set(key, tokens - 1)  # ...both allow the request, both write 0
```

Redis runs a **Lua script** as one indivisible step: no other command can run
in the middle. So `RATE_LIMIT_LUA` reads the bucket, refills it, takes a token
and writes it back, atomically. `test_exactly_limit_requests_win_under_concurrency`
proves it: 50 threads race for 10 tokens, and exactly 10 win.

Details worth noticing in the script:

- **Whose clock?** Refilling needs "how much time passed?". App servers'
  clocks can differ by a few milliseconds or more; there is one Redis. So the
  script asks Redis for the time (`redis.call('TIME')`), and all instances
  agree.
- **Lua numbers come back as integers.** Redis truncates numbers returned
  from a script, so the script rounds the token count itself with `math.floor`.
- **Idle buckets are deleted.** `EXPIRE` removes a client's bucket once it
  would be full again anyway, so memory doesn't grow with every IP that ever
  visited.
- `register_script` sends the script's SHA-1 hash (`EVALSHA`) instead of the
  whole script on every call; Redis caches scripts by hash.

#### Speaking HTTP properly

A blocked request gets:

```
HTTP/1.1 429 Too Many Requests
Retry-After: 6
X-RateLimit-Limit: 10
X-RateLimit-Remaining: 0
```

`Retry-After` tells a well-behaved client exactly how long to wait, so it
doesn't keep hammering.

#### Who is "a client"?

We use the client's IP address. That's simple but imperfect: everyone behind
one office or mobile-carrier network (NAT) shares an IP. And in step 3,
behind the Nginx load balancer, every request will come from *Nginx's* IP,
so all users would share one bucket. We'll fix that by reading the
`X-Forwarded-For` header, but only when it was set by our own proxy, because
anyone can send a fake `X-Forwarded-For`.

#### Fail open or fail closed?

If Redis is down, the limiter **fails open**: requests are allowed, with a
warning in the log. For a URL shortener, availability matters more than strict
limits for a few minutes. A login endpoint protecting against password
guessing might choose to **fail closed** (refuse requests) instead. Neither is
always right; it's a decision you make on purpose, per endpoint.

Failing open has a price: while Redis is unavailable there are no limits at
all. That's why the gateway and the circuit breaker log a warning, and why
step 5 turns those warnings into a metric with an alert.

### Testing it

- **Two Redis setups.** Locally, tests use **fakeredis**, an in-memory Redis
  written in Python, including Lua scripting (through the `lupa` package): no
  server needed. CI runs the same tests a second time against a real Redis and
  PostgreSQL, because a fake is only as good as its imitation.
- **Clean state.** Every test gets an empty Redis (`flushdb`) and an empty
  database, so tests can't affect each other.
- **Dependency overrides.** `app.dependency_overrides[get_redis] = ...` swaps
  the real Redis for the test one, the same mechanism FastAPI recommends for
  any external service.
- **Testing failure on purpose.** `client_with_redis_down` points at a port
  where nothing listens, and `client_with_redis_hung` at `SilentServer`
  (`tests/fakes.py`): a socket that accepts connections and never answers.
  The operating system completes each connection, and nobody ever reads from
  it, which is exactly what a frozen Redis looks like from outside.
  `connections_made()` counts how often the app tried. Both clients are built
  with the production settings, to prove the app degrades gracefully *and*
  fast.
- **A clock you control.** The circuit breaker takes its clock as a
  parameter, so `tests/test_breaker.py` moves time forward by hand instead of
  sleeping for 5 seconds.
- **Configuration.** `tests/test_config.py` checks that a malformed
  `REDIS_URL` is refused when the settings are read, before any request.

### Try it yourself

```bash
docker compose up --build
curl -s -X POST localhost:8000/api/links -H 'content-type: application/json' -d '{"url":"https://example.com"}'
curl -si localhost:8000/<code> | grep -i x-cache           # HIT (cached on create)
docker compose exec redis redis-cli --scan                   # see the keys
docker compose exec redis redis-cli ttl link:v1:<code>       # about 3600, with jitter
for i in $(seq 12); do curl -s -o /dev/null -w "%{http_code} " -X POST localhost:8000/api/links \
  -H 'content-type: application/json' -d '{"url":"https://example.com"}'; done   # ... 201 201 429 429
docker compose stop redis && curl -si localhost:8000/<code> | grep -i x-cache   # BYPASS, still works
docker compose start redis

# A hung Redis: "pause" freezes the container, but connections still open.
docker compose pause redis
for i in 1 2 3 4 5; do curl -s -o /dev/null -w "%{time_total}s " localhost:8000/<code>; done
# the first requests wait for timeouts, then the circuit opens and the rest are fast
docker compose logs app | grep -i circuit     # "Redis failed 3 times in a row, circuit open"
docker compose unpause redis
```

### Exercises

1. **Stampede lock.** In `resolve()`, on a miss, take a short lock with
   `SET lock:<code> 1 NX EX 2`. Only the winner queries the database; the
   others wait briefly (a *bounded* number of times!) and re-check the cache.
   How would you test it?
2. **Sliding window.** Implement a sliding-window-counter limiter as a second
   Lua script and compare its behaviour at the minute boundary with the token
   bucket.
3. **Rate-limit headers everywhere.** Send `X-RateLimit-Remaining` on
   *successful* responses too, so clients can slow down before hitting 429.
   (Hint: FastAPI dependencies can declare a `response: Response` parameter.)
4. **Which errors should trip the breaker?** Today every Redis error counts as
   a failure, even `WRONGTYPE` (Redis answered, but the key holds the wrong
   kind of value). Redis is clearly up in that case. Change `RedisGateway.call`
   so only `ConnectionError` and `TimeoutError` count, write the test first,
   and think about who could exploit the current behaviour.
5. **Measure it.** Use `curl -w "%{time_total}\n"` to compare `X-Cache: HIT`
   and `MISS` response times on your machine.

### Interview questions this lesson prepares you for

- "Where would you add a cache to this system, and what would you cache?"
- "Cache-aside, write-through, write-behind: what's the difference?"
- "Explain cache penetration, cache stampede and cache avalanche, and how to prevent each."
- "How do you keep a cache consistent with the database?"
- "LRU or LFU?"
- "Design a rate limiter. Which algorithm, and where does its state live when
  you have many servers?"
- "What happens to your system when the cache goes down? And when it hangs?"
- "What is a circuit breaker? What are its three states?"
- "Which requests are safe to retry? What does idempotent mean?"

**Next:** step 3 runs three instances of the app behind an Nginx load
balancer, and replaces random codes with distributed, collision-free IDs.
