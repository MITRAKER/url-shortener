# Learning notes

This file grows with every step of the project. Each lesson explains **what**
was built, **why** it was designed that way, the **key concepts**, and ends
with **exercises** so you can practise building it yourself.

- [Lesson 1: The core service](#lesson-1-the-core-service)
- [Lesson 2: Engineering like a professional team](#lesson-2-engineering-like-a-professional-team)
  (CI/CD, code quality, security scanning, NASA/JPL's Power of Ten)
- [Lesson 3: Caching and rate limiting](#lesson-3-caching-and-rate-limiting) (step 2)
- [Lesson 4: Many instances, one service](#lesson-4-many-instances-one-service) (step 3:
  load balancing, distributed IDs, migrations)
- [Lesson 5: Counting clicks without slowing redirects](#lesson-5-counting-clicks-without-slowing-redirects)
  (step 4: asynchronous processing, Redis Streams, exactly-once counting)
- [Lesson 6: Seeing what the system does](#lesson-6-seeing-what-the-system-does)
  (step 5: metrics, alerts, load testing, benchmarks)

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
| **Random + retry** (steps 1 and 2) | Random 7 chars, insert, retry on conflict | Unguessable; no coordination | Tiny chance of retry |

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

> Step 3 replaces random codes with IDs that can't collide in the first
> place, made by each server on its own, and scrambled so they stay
> unguessable. [Lesson 4](#part-2-unique-ids-without-asking-anyone) explains
> how, and why the database's `UNIQUE` constraint still stays.

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
   Compare the final count with the atomic version. Since step 4, a redirect
   appends its click to a Redis stream instead, and calls `record_click` only
   when Redis can't take the click: so stop Redis first
   (`docker compose stop redis`), or run the app without `REDIS_URL`. And use
   PostgreSQL, as Docker Compose does: on SQLite the race hardly ever shows
   (in our runs, SQLite lost none of 1,000 clicks; PostgreSQL lost most).
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

- **Lock files with hashes.** `requirements.in` lists the handful of packages
  the app imports. Those need about twenty more (Starlette, anyio, h11, ...).
  Pinning only the direct ones, as this repo first did, still lets pip pick the
  newest version of all the others at install time, so CI, the Docker build
  and your laptop could each get different code. `pip-compile` (from
  pip-tools) resolves the whole tree once and writes `requirements.txt`: every
  package at an exact version, plus the SHA-256 hash of every file pip may
  download for it. `pip install --require-hashes` refuses any file whose hash
  differs, so a package that was tampered with, or quietly re-uploaded, cannot
  get in. You edit the `.in` files and run `make lock`; Dependabot does the
  same for updates.
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
except framework hooks like FastAPI's `Depends`, standard-library factories
like `defaultdict(list)`, and injected dependencies that keep side effects
testable: a clock that a test can move by hand, or the operation given to
`RedisGateway.call` (both in lesson 3).

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
| `tests/test_cache.py`, `test_ratelimit.py`, `test_redis_client.py`, `test_breaker.py`, `test_config.py` | 92 new tests |
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
   `A-Z a-z 0-9 _ -`. `/bad.code` or a 10 KB string is rejected with 404
   before touching the cache or the database (only the rate limiter's script
   runs in Redis). A path with a `/` in it, like `/../../etc/passwd`, doesn't
   even match the `/{code}` route, so the router answers 404 before the rate
   limiter runs.
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
If the text before the `#` is a number, the URL even parses, as that port and
without the password, and the app would start without Redis. So the check
also refuses a URL whose `@` is not in the host part.
Now `_check_redis_url` catches redis-py's `ValueError` and raises its own,
with a fixed message, `from None`: unlike lesson 2's `from exc`, this
deliberately keeps the original error, which quotes the URL, out of the
traceback. The message names the setting and the problem, never the value or
any part of it.

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

---

## Lesson 4: Many instances, one service

Step 3 runs three copies of the app, called **instances**, behind an **Nginx**
load balancer. Two problems appear the moment there is more than one copy:
which instance answers each request (**load balancing**), and how instances
that never talk to each other avoid handing out the same short code
(**distributed IDs**). A third follows from the first: three instances can't
each create the tables when they start, so the schema now changes through
**migrations**.

### What we built

```
                            ┌──▶ app, instance 0 ──┐
client ──▶ Nginx :8000 ─────┼──▶ app, instance 1 ──┼──▶ Redis       cache and rate limits, shared
           round robin,     └──▶ app, instance 2 ──┴──▶ PostgreSQL  links, shared; instance numbers, leased
           retries, timeouts

POST /api/links, answered by instance 1:
  ID   = | seconds since 2026 | instance 1 | sequence |   made locally: nobody was asked
  code = Feistel(ID, CODE_SECRET) = "z4qLsrbd"           8 characters, unique, unguessable
  INSERT ...                                             the UNIQUE constraints still have the last word
```

| File | What's new |
|------|------------|
| `nginx/default.conf` | The load balancer: round robin, finding instances by DNS, timeouts, retries, keepalive, the client's address |
| `docker-compose.yml` | Nginx in front, `replicas: 3`, a one-shot `migrate` service, a fixed address for Nginx |
| `app/ids.py` | `Snowflake` makes IDs, `CodeCipher` turns an ID into a code and back |
| `app/instances.py` | Instance numbers leased from PostgreSQL, and a watchdog that notices a lost lease |
| `app/migrations/` | The schema as versioned migration scripts (Alembic) |
| `app/main.py` | The lease at startup, the proxy header middleware, the instance number in `/health` |
| `app/service.py` | `create_link` uses the IDs; the retry loop is now only a safety net |
| `app/config.py` | `CODE_SECRET` and `TRUSTED_PROXIES` |
| `scripts/check_instances.py` | For CI: does Nginx use every instance, and does losing one lose requests? |
| `tests/test_ids.py`, `test_instances.py`, `test_migrations.py`, `test_proxy.py` | 65 more tests than step 2 |

### Part 1: Load balancing

#### Why run more than one copy?

- **Capacity.** One Python process runs Python code on one CPU core at a time
  (the GIL). More processes use more cores, and more machines go further.
- **Availability.** With one copy, every crash, deploy or reboot is an outage.
  With three, one can die and the service stays up.
- **Deploys without downtime.** Replace the instances one at a time (a
  *rolling deploy*) and users never notice.

#### The rule that makes it work: stateless instances

The load balancer may send each request to any instance, so an instance must
keep nothing that a later request needs: no sessions in memory, no files on
its own disk. Everything shared lives in PostgreSQL and Redis.

Step 2's design pays off here. The rate limiter keeps its buckets in Redis,
so a client gets 10 link creations per minute in total, not 10 per instance.
Some state is per instance on purpose, and that's fine: the circuit breaker's
opinion of Redis, the connection pools.

#### How Nginx chooses an instance

| Algorithm | How | Good for | Watch out for |
|-----------|-----|----------|---------------|
| **Round robin** (ours) | 0, 1, 2, 0, 1, 2 ... | Identical instances, short similar requests | A slow instance gets as many requests as a fast one |
| Weighted round robin | Bigger machines get more turns | Mixed hardware | Weights to maintain |
| Least connections (`least_conn`) | The instance with the fewest requests in progress | Requests of very different lengths | Nginx must share the counts between its workers (our `zone`) |
| Power of two choices | Pick two at random, send to the less busy | Many load balancers sharing traffic | |
| Hashing (`hash $key consistent`) | The same client or key always goes to the same instance | Per-instance caches, "sticky" sessions | Uneven load. *Consistent* hashing moves only the leaving instance's keys |

Our instances are identical and stateless, and our requests are short, so
round robin it is. `/health` reports which instance answered, so you can
watch it: 30 requests in CI come back 10, 10 and 10.

Nginx works here at **layer 7** (HTTP): it reads each request, so it can retry
it, set headers and log the path. A **layer 4** balancer (a cloud network
load balancer, or Nginx's `stream` module) forwards TCP connections without
reading them: faster, but it can't retry a request it never looked at.

#### Finding the instances: DNS

Compose gives the three replicas one name, `app`, and Docker's built-in DNS
server (`127.0.0.11`) answers with all three addresses. A plain
`server app:8000;` is resolved once, when Nginx starts: restart an instance,
it gets a new address, and Nginx keeps knocking on the old one. So ours says:

```nginx
resolver 127.0.0.11 valid=10s;
upstream app_instances {
    zone app_instances 64k;              # the list lives in memory shared by all Nginx workers
    server app:8000 resolve max_fails=3 fail_timeout=10s;
}
```

`resolve` (in free Nginx since version 1.27.3) asks again every 10 seconds,
so instances that come and go are picked up. That's **service discovery**:
Kubernetes Services, Consul and cloud target groups solve the same problem.

#### When an instance fails

Free Nginx has **passive health checks**: it learns that an instance is down
from real requests.

- `proxy_connect_timeout 2s`: on the same network, a live instance accepts a
  connection in well under a millisecond. Two seconds means it's gone.
- `proxy_next_upstream error timeout`: when connecting fails or takes over
  2 s, or the instance closes the connection before it answers, try the next
  instance. `proxy_next_upstream_tries 2` and `proxy_next_upstream_timeout 10s`
  limit the retries: a retry is a loop, and every loop needs a bound
  (NASA/JPL rule 2). An instance that takes the request and then says nothing
  for 30 s (`proxy_read_timeout`) is past those 10 s, so that request is not
  retried: the client gets a `504`.
- `max_fails=3 fail_timeout=10s`: after 3 failures within 10 seconds, leave
  that instance out for 10 seconds, then give it another chance.

Which requests are retried? Nginx never retries a `POST` that already reached
an instance: creating a link twice is not safe (lesson 3: retry only what is
idempotent). A `POST` that could not even connect *is* retried: no instance
ever saw it.

**Active health checks** poll `/health` in the background instead (HAProxy,
Nginx Plus, Kubernetes readiness probes). Free Nginx doesn't have them, but
Docker's `HEALTHCHECK` and `depends_on: condition: service_healthy` keep Nginx
from starting before the instances are ready.

We measured it: while a client sent requests nonstop, we stopped one
instance, four times over. Of 1,741 requests, none failed. A few took 2.0
seconds: they went to the stopped instance, waited out the connect timeout,
and were retried on another. CI repeats a shorter version on every pull
request.

#### A race we found later: the list changes under a retry

CI's version found what our measurement missed. CI sends its requests
*after* `docker stop` has finished, so there is no moment when the stopping
instance still refuses connections at once: every request sent its way
waits out the 2-second connect timeout. If `resolve` asks DNS again during
that wait, the list of instances changes under the request. Nginx then
doesn't pick the retry from the new list: it answers 502, and its log says
`no live upstreams`, although two instances are up. We ran CI's sequence 8
times: 7 runs had one 502 each.

The fix is a second chance for what is safe to repeat:

```nginx
location / {
    proxy_pass http://app_instances;
    error_page 502 = @once_more;   # a 502 made by Nginx: no instance answered
}
location @once_more {
    if ($request_method !~ ^(GET|HEAD)$) {
        return 502;                # a POST is never asked twice
    }
    proxy_pass http://app_instances;   # a fresh try, with the new list
    proxy_next_upstream_tries 1;       # one try, not 2 more
}
```

The proxy settings moved up to the `server` block, so both locations share
them, all but one: the second chance makes a single try. So a `GET` or `HEAD`
reaches the instances at most 3 times, 2 tries and then 1. After the fix, CI's
sequence ran 11 times without a single 502. And to be sure the changing list
was the trigger, we ran the old configuration with DNS asked only every 5
minutes (`valid=300s`): 4 runs, no 502 either. Two lessons in one: the same
failure can be harmless or not depending on *when* requests arrive, so test
more than one timing; and a second chance is only for requests that are safe
to repeat. (Is a `GET` safe? In HTTP's terms, yes. Ours counts a click, the
one side effect we accept repeating. "No instance answered" doesn't mean no
instance received the request: one may count the click and die before it
answers. With 3 tries, a click can be counted up to 3 times.)

A later review found that our first version of `@once_more` had no
`proxy_next_upstream_tries` of its own. It inherited the `server` block's 2,
so a `GET` could reach the instances 4 times, not 3. A setting made once for
every location applies to every location, wanted or not. With the single try,
we replayed the race with stand-in instances, 10 runs each way: without the
second chance, 4 runs had a 502; with it, the race struck in 8 runs and every
request succeeded.

#### Keepalive, and a race at the edge of a timeout

A new TCP connection per request costs a round trip, so Nginx keeps
connections to the instances open and reuses them: `keepalive 16` (idle
connections kept per Nginx worker), with `proxy_http_version 1.1` and
`proxy_set_header Connection ""` (HTTP/1.0, or `Connection: close`, would end
each connection after one request).

Reuse has a classic race. The server closes an idle connection at the very
moment the proxy sends a new request on it, and the request fails with a 502.
The fix is an ordering of timeouts: **the server must wait longer than the
proxy.** Nginx closes idle connections to the app after 60 seconds
(`keepalive_timeout 60s`), uvicorn after 65 (`--timeout-keep-alive 65`; its
default is 5!). So Nginx always closes first. The same rule holds between a
cloud load balancer and Nginx.

#### Who is the client?

Behind Nginx, every request comes from Nginx's address, so all clients would
share one rate-limit bucket (lesson 3 warned about this). Nginx passes the
real address in the `X-Forwarded-For` header. But anyone can send that
header, so it may be believed only when it comes from our own proxy.

- **Nginx** *replaces* the header: `proxy_set_header X-Forwarded-For $remote_addr;`.
  The common `$proxy_add_x_forwarded_for` *appends* to whatever the client
  sent, which keeps a forgery at the front.
- **The app** runs uvicorn's `ProxyHeadersMiddleware` with
  `TRUSTED_PROXIES=10.89.0.10`. It reads the header only on requests from that
  address, and takes the **rightmost** address that isn't a trusted proxy: the
  left end of the header is whatever the client wrote. `TRUSTED_PROXIES="*"`
  would take the leftmost, so anyone could choose their own address and dodge
  their limit. The setting accepts only addresses and networks, so `*` (and
  any typo) stops the app at startup. So does `10.89.0.10/24`, an address
  with a prefix length (`docker network inspect` prints addresses that way):
  uvicorn would quietly ignore it, and every client would share Nginx's rate
  limit. A network is written `10.89.0.0/24`.
- **Compose** gives Nginx a fixed address, 10.89.0.10, and hands out addresses
  to other containers only from 10.89.0.128 up (`ip_range`), so no other
  container can ever get Nginx's.
- **The instances publish no ports**: the only way in is through Nginx. And
  `--no-proxy-headers` turns off uvicorn's own handling of the header, so
  exactly one place decides whom to believe.

`tests/test_proxy.py` checks all of it: one bucket per client behind Nginx,
the header ignored from anyone else, and a forged address in front ignored.

Nginx should pass on one more header: `Host`, the name and port the client
asked for. Left to itself, Nginx would send the name its configuration gives
the instances, `app_instances`. (Our redirects would survive that:
`proxy_redirect`, on by default, rewrites a `Location` that starts with
`http://app_instances/` to the address the client used. But the app would
have the wrong idea of its own address.) `proxy_set_header Host $http_host;` passes on
what the client sent, port included. The app uses it to build the few
redirects that point back at itself: `/docs/` sends you to `/docs`, on the
host and port you used. Our first version used the common `$host`, which
drops the port: `http://localhost:8000/docs/` sent you to
`http://localhost/docs`, on port 80, where the app isn't. The smoke test now
checks one such redirect.

### Part 2: Unique IDs without asking anyone

#### The problem with random codes

Until now, a new link got a random 7-character code; if the database already
had it, we tried another. That is correct with any number of instances (the
database's `UNIQUE` constraint decides), but look at the numbers.

There are 62^7 ≈ 3.5 trillion codes. With 10 million links stored, a new
code is taken with probability 0.0003%: rare. But the chance that *some* two
codes ever collide passes 50% after only about 2.2 million links. That's the
**birthday paradox**: `P ≈ 1 − e^(−k²/2N)` for `k` codes out of `N`. So
collisions are rare per request and certain over the life of the service,
each costs a failed `INSERT`, and they get more frequent as the table fills.

| Approach | Unique? | Coordination | Short? | Guessable? |
|----------|---------|--------------|--------|------------|
| Database sequence (`BIGSERIAL`) | Yes | Every insert asks the one database for a number | Yes | Yes: `/21`, `/22` ... |
| UUID v4 (122 random bits) | In practice | None | No: 22 base62 characters | No |
| Random code, retry on conflict (steps 1 and 2) | After a retry | The database settles conflicts | Yes | No |
| Ticket server, or blocks of IDs (Flickr) | Yes | Ask a central server, per ID or per block | Yes | Yes |
| **Snowflake ID + a keyed permutation** (ours) | By construction | Once, at startup, for an instance number | Yes: 8 characters | No |

#### Snowflake IDs

Twitter's idea (2010): put side by side three numbers that together can never
repeat.

```
| 31 bits: seconds since 2026-01-01 | 6 bits: instance number | 10 bits: sequence |
  only moves forward                  no two running instances  counts up within
                                      share one                 one second
```

- Same instance, same second: different sequence numbers. Different
  instances: different instance numbers. Different seconds: different times.
  So two IDs are never equal, and nobody had to be asked.
- 47 bits make 2^47 ≈ 1.4 × 10^14 IDs, which fit in 8 base62 characters
  (62^8 ≈ 2.2 × 10^14). That's why ours has 47 bits and Twitter's has 64 (41
  bits of milliseconds, 10 of machine, 12 of sequence): we traded millisecond
  timestamps for short links.
- The limits: 64 instances, 1024 IDs per second each (65,536 per second in
  total), until 19 January 2094. An instance that hands out 1024 IDs in one
  second makes the next request wait for the next second (under a second):
  back pressure instead of an error.
- IDs sort by creation time. New rows land at the right edge of the primary
  key's index, where the page is already in memory; random UUIDs (v4) write
  all over the index. UUID v7 (RFC 9562, 2024) standardises the same idea.
- `parse_id` takes an ID apart: when, which instance, which sequence. Handy
  when debugging.

#### Time is harder than it looks

- **The wall clock can go backwards.** NTP corrects drift, or someone sets the
  clock, and a second that runs twice could repeat IDs. So the wall clock is
  read once, at startup; from then on, time is that reading plus the
  **monotonic** clock (`time.monotonic()`), which never goes backwards.
- **Restarts.** A process that stops, and a new one with the same instance
  number in the same second, could repeat (second, sequence) pairs. So a new
  `Snowflake` counts the current second as used up, and its first ID waits for
  the next second: under a second, once per start.
- **A clock far off** (before 2026, after 2094) stops the app at startup with
  a `ClockError`. Failing loudly beats making IDs that sort wrong or overflow.
- **What's not covered:** a clock that is *behind* the previous run by more
  than a second when the instance restarts. Its IDs could repeat old ones. The
  `UNIQUE` constraints refuse them, the app retries with the next ID and logs a
  warning, and after 5 tries answers 503. Exercise 3 closes this gap.
- **Tests never sleep.** `Snowflake` takes its clocks and its `sleep` as
  parameters, so `tests/test_ids.py` moves a fake clock by hand, as lesson 3
  did for the circuit breaker.

#### Instance numbers: a lease, not a setting

Giving each instance its number in its configuration fails in practice. With
`replicas: 3`, Compose gives all three the very same environment. And sooner
or later, a copy-pasted configuration gives two machines the same number,
which silently makes duplicate IDs. So each instance **leases** a free number
from PostgreSQL when it starts:

```sql
SELECT pg_try_advisory_lock(1970433139, 0);   -- true: number 0 is ours
SELECT pg_try_advisory_lock(1970433139, 1);   -- false would mean "taken": try the next
```

An **advisory lock** is a lock on numbers we choose; PostgreSQL attaches no
meaning to them. The first number is a namespace, "urls" in ASCII
(0x75726C73, which is 1970433139), so our locks never clash with another
app's. The lock belongs to the database **session**, and PostgreSQL releases
it when the session ends: no clean-up job. A clean shutdown or a crash closes
the connection, and the number is free at once. A machine that loses power, or
a network that fails, closes nothing: PostgreSQL learns of it only through TCP
**keepalives**, probes it sends on a connection that has gone quiet. Its
defaults are the operating system's: on Linux, 2 hours of silence, then 9
probes 75 s apart, about 2 h 11 min in all, while the number stays taken. So
every session the app opens asks for a probe after 10 s of silence, then one
every 5 s, and for the session to end after 3 go unanswered (`app/db.py`):
about 25 s. That is the lease's real expiry. ZooKeeper's ephemeral nodes and
etcd's leases are the same idea, with the expiry in plain sight: a session
timeout, a TTL.

Two details from building it:

- SQLAlchemy opens a transaction for that `SELECT`, and left open, the session
  would sit "idle in transaction" for the instance's whole life. Databases are
  often set to end such sessions (`idle_in_transaction_session_timeout`), and
  the lease would end with it: we tried. So the app commits right away. The
  lock survives the commit because it belongs to the session, not the
  transaction.
- Advisory locks belong to one database. The first version of our tests
  looked at `pg_locks` without saying which database, and saw the locks of the
  Compose stack running next to the test database.

**Leases can be lost.** The flip side of "the lock ends with the session": if
PostgreSQL restarts, every session ends and every lock is released, while the
instances keep running. A new instance could then lease number 0 while the
old instance 0 is still using it. So each instance checks its session every 5
seconds (`watch` in `app/instances.py`). When the check fails, it retires its
`Snowflake` (no more IDs, so creating a link answers 503), and sends itself
`SIGTERM`, the signal `docker stop` sends. uvicorn finishes the requests in
progress and exits, and Docker starts it again (`restart: unless-stopped`)
with a fresh lease. The same check covers a network that fails between an
instance and PostgreSQL: the instance gives up within about 15 s (its next
check, plus 10 s of `tcp_user_timeout`), before the server's keepalives free
its number. We cut one off the network: it noticed after 14 s, and its number
was free after 23.

That's **crash-only design**: there is one well-tested way to start, and
recovering is just starting again. We tried it with `docker compose restart
db`: within 5 seconds every instance logged `lost instance number N`,
restarted, and leased numbers 0, 1 and 2 again.

For up to those 5 seconds, two instances could share a number; the `UNIQUE`
constraints catch any duplicate. Every lease system has such a window:
ZooKeeper sessions and etcd leases expire on a timer, and a process paused
for a while may not know its lease is gone. The general fix is a **fencing
token**: each lease carries a generation number that grows, every write
carries it, and storage refuses writes from an older generation (see Martin
Kleppmann's "How to do distributed locking").

#### Codes: scrambling the ID with a Feistel network

Writing the ID in base62 would give short, unique codes, but consecutive
links would get consecutive codes:

```
ID               base62     our code
1835008001024    wiZAz8k    mCZkN2ct
1835008001025    wiZAz8l    z4qLsrbd
1835008001026    wiZAz8m    4gEB6mN3
1835008001027    wiZAz8n    OiS0UIuG
```

Anyone could walk through everybody's links, an **enumeration attack**. We
need a function that scrambles numbers but never maps two of them to the same
code: a **permutation** of all the codes, chosen by a secret key. That's what
a block cipher is. AES works on 128-bit blocks, though, and our "block" is a
number below 62^8: we need **format-preserving encryption**. The classic way
to build one is a **Feistel network**:

```
split the number into halves L and R, each below 62^4
repeat 10 times:
    L, R = R, (L + F(round, R)) mod 62^4
```

`F` is a keyed hash (BLAKE2b, with a key made from `CODE_SECRET`). The trick:
`F` itself doesn't need to be reversible. Each round passes `R` along
unchanged, so it can be undone: `L = (new R − F(round, old R)) mod 62^4`. Undo
the rounds in reverse order and the ID comes back (`decode`). So the whole
thing is a permutation **whatever `F` is**: unique codes are guaranteed by the
structure, not by luck. DES (1977) and NIST's FF1 format-preserving encryption
are built this way; we use 10 rounds, as FF1 does. A code takes about 8
microseconds to make.

What follows from it:

- IDs use 2^47 of the 62^8 codes (64%). Every ID gets its own code; the other
  codes are never handed out, and asking for one is just a 404.
- Every instance needs the same `CODE_SECRET`, and it must never change: a new
  key would send new IDs to codes that old links already have (the `UNIQUE`
  constraint would refuse them, as clashes). `test_codes_do_not_change_between_versions`
  pins the algorithm for the same reason.
- It hides the *order*, not the *existence*: one code tells you nothing about
  the next. Guessing codes at random is still possible, and the rate limiter
  and the negative cache (lesson 3) deal with that.
- It is fine for unguessable links. It is not a substitute for real encryption
  of secrets.
- Aliases share the `code` column, so someone may choose, as an alias, the
  code a future ID will get. That insert then fails, and the retry takes the
  next ID (`test_a_code_that_clashes_with_an_alias_is_replaced`).

#### The database still has the last word

The `UNIQUE` constraints on `id` and `code` stay, and so does a short retry
loop (5 tries). With working leases and clocks they never fire. When they do,
something is wrong (a lost lease, a clock far behind), so every retry logs a
warning, and after 5 the app answers 503. **Defense in depth**: the design
makes duplicates impossible; the database makes them harmless.

### Part 3: Migrations

Until now, every instance ran `create_all()` when it started. With three
instances, three processes would race to create the same tables. Worse,
`create_all()` only creates what is missing; it never changes an existing
table, and step 3 *had* to change one: IDs no longer come from a database
sequence. So the schema now lives in versioned scripts, run by **Alembic**:

- `0001_baseline.py`: the table as v0.2.0 made it. On a database from v0.2.0
  it finds the table and does nothing (it *adopts* it); on an empty database it
  creates it. That's a **baseline** migration.
- `0002_ids_from_the_app.py`: removes the sequence that used to number links,
  because the app chooses every ID now. Its `downgrade` puts the sequence back,
  starting above the largest ID.

Alembic records the version in a table (`alembic_version`) and applies only
what is missing: `alembic upgrade head` (`make migrate`).

- **When they run.** A one-shot `migrate` service in Compose runs the
  migrations and exits; the instances wait for it
  (`depends_on: condition: service_completed_successfully`).
- **One at a time.** If two deploys start together, both run migrations.
  `env.py` takes an advisory lock first, so the second run waits for the
  first, then finds nothing left to do. It waits 2 seconds at most (the lock
  timeout), then fails instead of hanging; run it again once the first is done.
- **Timeouts.** A statement may run for 60 seconds, but waiting for a lock
  stays at 2: a schema change queued behind a long query blocks every query
  that arrives after it.
- **Expand, then contract.** During a rolling deploy, old and new instances
  run side by side, so every migration must work with both: add a column,
  deploy code that uses it, and remove the old one a release later.

Migration `0002` breaks that rule: it removes the sequence in the same
release that stops using it, instead of a release later. A v0.2.0 instance
that is still running afterwards can't create links: it leaves out the ID,
the database refuses the row, and the instance answers 503 (or 409, "Alias
is already taken", for an alias that is free). We accept that for this one
upgrade because it can't be rolling anyway: Nginx takes over port 8000 from
the single v0.2.0 instance, so that instance has to stop. Stop it before the
migrations run: `docker compose down` (which keeps the database's volume),
then `docker compose up --build`.

`tests/test_migrations.py` checks that the migrations build exactly what the
models describe (Alembic's autogenerate finds no difference), that each can be
undone, that a v0.2.0 database is adopted with its links, that the SQL can be
printed for review (`--sql`), and that a second run waits its turn. Each test
starts by dropping every table it finds, not only the ones this step knows,
so tables that a later step's tests left in the same database can't make it
fail.

### Testing it

- **Fake clocks.** `Snowflake` takes `wall`, `monotonic` and `sleep` as
  parameters, so the tests cross seconds and run out of IDs without waiting.
- **Threads.** 4 instances × 4 threads ask for 8,000 IDs at once: all
  different (and each instance runs out of a second's IDs along the way).
- **Forcing clashes.** `monkeypatch` makes `LinkIds.new` hand out IDs and
  codes that are already taken, to prove each retry path: an ID clash, a code
  that clashes with an alias, and giving up after exactly 5 tries.
- **PostgreSQL-only tests** (skipped on SQLite): instances get different
  numbers; a number is free again when its instance stops, or when the server
  ends its session (`pg_terminate_backend`, as its keepalives do once a machine
  is gone); every session asks the server for those keepalives; the lease
  leaves no transaction open; a second migration run waits its turn.
- **Pretending to be Nginx.** `TestClient(app, client=("10.89.0.10", 40000))`
  sends requests from Nginx's address.
- **The real stack, in CI.** `scripts/check_instances.py` checks that 30
  requests reach all 3 instances; then CI stops one with `docker stop`, and 30
  more must all succeed.

### Try it yourself

```bash
docker compose up --build
for i in $(seq 6); do curl -s localhost:8000/health; echo; done    # instances 0, 1, 2, 0, 1, 2
docker compose logs nginx | tail -6                                # upstream=...: the round robin
docker compose exec db psql -U app -d shortener -c \
  "SELECT objid AS instance, pid FROM pg_locks WHERE locktype = 'advisory' AND objsubid = 2"

curl -s -X POST localhost:8000/api/links -H 'content-type: application/json' -d '{"url":"https://example.com"}'
docker compose exec db psql -U app -d shortener -c "SELECT id, code FROM links"
docker compose exec --index 1 app python -c "from app.ids import parse_id; print(parse_id(<id>))"

# Lose an instance: no request fails, a few wait 2 s for the retry.
docker stop "$(docker compose ps --quiet app | head -n 1)"
python -m scripts.check_instances http://localhost:8000 2
docker compose up --detach                                         # three again

# Lose the database: every instance gives up its number and restarts.
docker compose restart db
docker compose logs app | grep "lost instance number"

# Migrations
docker compose run --rm migrate alembic current
docker compose run --rm migrate alembic upgrade head --sql | less
```

### Exercises

1. **Least connections.** Pause one instance (`docker pause` on its
   container: it accepts connections but never answers) and send requests
   with a 5-second client timeout. Compare round robin with `least_conn`, then
   lower `proxy_read_timeout`. What's the trade-off between a slow request and
   a dead instance?
2. **Faster failover.** Requests to a stopped instance wait for the 2-second
   connect timeout. Why doesn't a stopped container refuse the connection at
   once? Try `proxy_connect_timeout 200ms` and measure with
   `scripts/check_instances.py`. What's the risk of a timeout that's too
   short?
3. **Close the clock gap.** When an instance leases a number, have it record
   the last second it uses (a new table, with a migration), and refuse to
   start if the clock reads earlier than the previous holder's last second.
   Write the test first.
4. **Fencing tokens.** Give every lease a generation number that grows, store
   it with each link, and make the database refuse a write from a lease that
   was taken over. Where would the check live?
5. **Inspect a code.** Write `scripts/inspect_code.py`: given a code, print
   when and on which instance it was created. Who should be allowed to run
   it, and why?
6. **More instances.** `docker compose up --scale app=5`. What does `/health`
   show? What happens to a 65th instance?
7. **A real migration.** Add a `last_clicked_at` column, following
   CONTRIBUTING's "Database changes". Make sure old instances keep working
   while new ones roll out.

### Interview questions this lesson prepares you for

- "How would you scale this service to 10 times the traffic?"
- "Layer 4 or layer 7 load balancer: what's the difference?"
- "Round robin, least connections, consistent hashing: when would you use each?"
- "How does a load balancer know an instance is down? Active vs passive health checks."
- "What can go wrong with keep-alive connections between a proxy and a server?"
- "How do you get the client's real IP address behind a proxy, and how can it be spoofed?"
- "Design a unique ID generator for a distributed system. Snowflake or UUIDs?"
- "What happens to your ID generator when the clock goes backwards?"
- "How do you give each worker a unique number without configuring it by hand?"
- "What is a lease? What goes wrong when a process loses one without knowing? What's a fencing token?"
- "How do you make short codes that are unique but can't be guessed?"
- "Why are sequential IDs (or UUID v7) better primary keys than random UUIDs?"
- "How do you change a database schema without downtime?"

**Next:** step 4 takes click counting off the redirect path: each click
becomes an event on a Redis stream, and a separate worker writes the counts
to PostgreSQL in batches.

---

## Lesson 5: Counting clicks without slowing redirects

Until step 3, every redirect counted its click with an `UPDATE` in
PostgreSQL. Step 4 takes that write off the request path: a redirect only
appends a small **event** ("a click on `abc`") to a **Redis stream**, and a
separate **worker** process adds the events up and writes the totals to
PostgreSQL in batches. The hard part is not speed but correctness: every
click in the stream must be counted **exactly once**, even when the worker,
Redis or PostgreSQL fails halfway through.

### What we built

```
GET /abc1234 ─▶ Nginx ─▶ app ── XADD clicks:v1 * code abc1234 ──▶ Redis stream
                 ◀── 302 ──┘    (well under a millisecond)          │
                                                                    │ XREAD, up to 1000 at a time
                                                                    ▼
                                            click worker: abc1234 +3, xyz9876 +1, ...
                                                                    │
                                     one transaction per batch:     ▼
                                       UPDATE click_checkpoints SET last_id = <last entry read>
                                         WHERE last_id = <where this batch started>
                                       UPDATE links SET clicks = clicks + 3 WHERE code = 'abc1234'
                                       UPDATE links SET clicks = clicks + 1 WHERE code = 'xyz9876'
                                     COMMIT, then XTRIM: forget what is counted
```

| File | What's new |
|------|------------|
| `app/clicks.py` | `ClickStream.append()`: one `XADD`, unless a million clicks already wait (a small Lua script), through the Redis gateway |
| `app/service.py` | `count_click()`: the stream, or PostgreSQL straight away when Redis can't take the click |
| `app/worker.py` | The click worker: read a batch, add it up, count it with its checkpoint in one transaction; a watchdog that ends it when its loop is stuck |
| `app/heartbeat.py` | The worker's health check: was its last round recent? |
| `app/models.py`, migration `0003` | `click_checkpoints`: how far the worker got |
| `docker-compose.yml` | The `worker` service; Redis keeps the stream on disk and never evicts it |
| `scripts/check_clicks.py` | For CI: clicks wait while the worker is stopped, and survive a Redis restart |
| `tests/test_clicks.py`, `tests/test_worker.py` | 39 tests: the redirect path, batching, every failure in between, two workers at once |

### Part 1: Why take work off the request path?

#### What a click used to cost

In step 3, every redirect ran `UPDATE links SET clicks = clicks + 1` in a
transaction of its own. Two costs hide in that line:

- **A commit is a disk write.** PostgreSQL makes a commit durable by flushing
  its write-ahead log (WAL) before it answers. One click, one flush.
- **A popular link is a hot row.** An `UPDATE` locks the row until the
  transaction ends, so concurrent clicks on the same link wait in line. The
  more popular the link, the longer the line. Viral links are exactly the
  ones that get clicked at the same moment.

The fix follows a general rule: **on the request path, do only what the
response needs.** The browser needs the target URL, not an updated counter.
Anything else can happen a moment later, in the background. That is
**asynchronous processing**, and it's how most systems count views, likes and
clicks.

#### The price: eventual consistency

The count you read from `GET /api/links/{code}` may now lag a moment behind
the clicks. Normally the lag is a fraction of a second (the worker lets
clicks gather for 0.2 s, then counts them together: lesson 6 shows why);
while the worker is down, it grows until it comes back. Nothing is lost, but
nothing is instant either. The count is **eventually consistent**: stop
clicking, and it catches up to the truth.

For analytics, that's fine. For a bank balance it would not be: always ask
"who reads this, and what happens if they read it a second late?" before
moving a write off the request path.

#### Measured

Same machine (4 cores, everything in Docker, the benchmark client too), step 3
and step 4 back to back, rate limits raised for the test, every request a
redirect to the same link (a cache hit):

| | Step 3 | Step 4 |
|---|---|---|
| One client: median (p50) | 4.44 ms | 2.55 ms |
| One client: p99 | 7.58 ms | 4.04 ms |
| 16 clients: throughput | 583 redirects/s | 905 redirects/s |
| 16 clients: p50 / p99 | 22.8 / 87.1 ms | 14.2 / 55.4 ms |

All 10,200 clicks of the run were counted by the time the benchmark asked for
the count. On a cache hit, a redirect no longer touches PostgreSQL at all;
what's left is mostly Python handling HTTP on 4 shared cores.

### Part 2: A log of events

#### Queue or log?

Where should the clicks wait? Two families of tools:

- A **queue** (a Redis list with `LPUSH`/`BRPOP`, RabbitMQ, SQS): a message
  is handed to one consumer and removed. If the consumer crashes after taking
  it and before finishing, the message is gone, unless the queue has
  acknowledgements and redelivery.
- A **log** (Kafka, Redis Streams): entries are appended and *stay*, each
  with a position. Consumers don't remove what they read; each one remembers
  how far it got, called its **offset**. Read it again after a crash, replay
  history, let two different consumers read the same events.

We use a log, **Redis Streams**, because Redis is already there, and because
"remember how far you got" turns out to be exactly what exactly-once
counting needs (Part 3).

#### Redis Streams in four commands

```bash
XADD clicks:v1 * code abc1234        # append; "*" lets Redis choose the ID
#  → "1795042446927-0": milliseconds since 1970, then a sequence number
XREAD COUNT 1000 BLOCK 1000 STREAMS clicks:v1 1795042446927-0
#  → up to 1000 entries *after* that ID, waiting up to 1 s for one to arrive
XTRIM clicks:v1 MINID 1795042446927-1  # drop the entries before that ID
XLEN clicks:v1                         # how many are waiting
```

IDs only grow, so "everything after ID x" is a well-defined question, and an
ID is all a reader needs to remember. The worker asks for up to 1000 entries
and waits at most a second; it gets whatever has arrived. So batches adapt to
the load by themselves: one click a second makes batches of one, with no
waiting for a batch to fill; ten thousand a second make full batches. (After
a batch that wasn't full, the worker now pauses 0.2 s so that the next one
gathers more clicks; lesson 6 tells how the metrics showed it was needed.)

Redis also has **consumer groups** (`XREADGROUP`, `XACK`), which share a
stream among several workers and remember, in Redis, what each one has
acknowledged. We don't use them, and the reason is the heart of this lesson:
our progress must be remembered *in PostgreSQL*, next to the counts.

### Part 3: Exactly once, without a distributed transaction

#### Three delivery guarantees

A worker does two things for each batch: it **writes the counts**, and it
**records its progress**. They live in two different systems, Redis and
PostgreSQL, and a crash can strike between any two steps. The order decides
what a crash costs:

| Order | A crash in between... | Guarantee |
|-------|-----------------------|-----------|
| Record progress, then write | ...loses the batch | **at most once** |
| Write, then record progress | ...counts the batch again on restart | **at least once** |
| Both in one transaction | ...can't happen: both or neither | **exactly once** (in effect) |

Exactly-once *delivery* across a network is impossible in general (a lost
reply leaves the sender guessing). Exactly-once *effect* is not: deliver at
least once, and make the effect happen only once. Two ways to get there:
make the write **idempotent** (doing it twice equals doing it once), or
**store the progress with the result, in one transaction**. `clicks = clicks
+ 3` is not idempotent, so we take the second way.

#### The checkpoint

`click_checkpoints` has one row: the stream's name, and the ID of the last
entry already counted. The worker moves it in the same transaction that
adds the counts:

```python
moved = session.connection().execute(
    update(ClickCheckpoint)
    .where(ClickCheckpoint.stream == STREAM, ClickCheckpoint.last_id == batch.after)
    .values(last_id=batch.last_id)
)
if moved.rowcount != 1:
    session.rollback()  # someone else counted these: throw the batch away
    return False
session.connection().execute(ADD_CLICKS, [...])  # clicks = clicks + n, per link
session.commit()  # the counts and the checkpoint, together
```

Now walk through the crashes:

- **Before the commit** (the worker dies, PostgreSQL refuses): the transaction
  rolls back, the checkpoint hasn't moved, and the next round reads the same
  entries again. Nothing counted, nothing lost.
- **After the commit, before `XTRIM`**: the entries are still in the stream,
  but behind the checkpoint, so they are never read again. The next batch
  trims them along with its own.
- **During the commit, reply lost** (did it commit?): the worker doesn't need
  to know. It reads the checkpoint from the database, which knows.

Kafka consumers do the same when they store their offsets in the database
that receives their results; Kafka's own "exactly-once" transactions are this
idea, built into the broker.

#### Compare-and-set: a second worker can't double count

`WHERE last_id = :after` makes the update a **compare-and-set**: "move the
checkpoint from where I started to where I ended, *if it's still where I
started*." If a second worker counted the same entries first, the checkpoint
has moved, the update changes no row, and this worker rolls back. One worker
is plenty (it counted 100,000 clicks in 2.3 seconds, start-up included), but
two can't break anything: the second only wastes work.

This is **optimistic concurrency control**, the same idea as a version column
that an `UPDATE ... WHERE version = 7` checks, and a cousin of lesson 4's
fencing tokens. PostgreSQL makes it safe: the second worker's `UPDATE` waits
for the first one's row lock, then checks its `WHERE` again on the committed
row.

The `UPDATE`s on `links` always run in the same order, sorted by code. A
transaction that locks several rows in an arbitrary order can deadlock with
another one locking them in another order; one global order prevents that.
(Here the checkpoint's row lock, taken first, already makes workers take
turns, but the habit costs nothing.)

#### Adding up: the hot row disappears

A batch of 1000 clicks on one viral link becomes **one** `UPDATE ... + 1000`
and **one** commit, instead of 1000 of each. That's **write coalescing**:
batching turns a thousand row locks and WAL flushes into one. All the
`UPDATE`s of a batch go to PostgreSQL in one go (an *executemany*, which
psycopg 3 sends as a pipeline, without waiting for each answer).

#### Junk in the stream: poison messages

Anyone who can write to Redis can write to the stream. An entry that is not a
click on a valid code (a missing field, a code with spaces) is **skipped and
logged**, but the checkpoint still moves past it. The alternative, stopping
at it, would make it a **poison message**: the worker would read it, fail,
read it again, forever, and no click behind it would ever be counted.

The review found one we had missed: bytes that aren't UTF-8. Redis stores
bytes, and redis-py, asked for `str`, decoded every reply strictly, so such
an entry raised `UnicodeDecodeError` before our checks ever saw it. That is
not a Redis error, so the worker crashed, started again, read the same
entry, and crashed again: a poison message after all. Now `make_redis()`
decodes with `encoding_errors="replace"`: the bad bytes become U+FFFD (�),
and the entry fails validation like any other junk.

#### The stream holds only what is left to count

After each batch, `XTRIM ... MINID <the ID right after the last one counted>`
removes the counted entries. So the stream's length is exactly the
**backlog**, and memory stays bounded by how far behind the worker is, not by
how many clicks there ever were.

#### Fail loudly, not quietly

`parse_entries()` raises an error when Redis's reply has an unexpected shape,
instead of treating it as "no clicks". We found out why while building it:
redis-py speaks two protocols, and under the newer one, RESP3, `XREAD`
returns a dict instead of a list. A parser that silently returned "no clicks"
for that would make a worker that runs, passes its health check, and never
counts anything again. So `make_redis()` now asks for RESP2 by name, and an
unexpected shape shows up in the log every second. **A silent failure is the
worst kind**: it is found by a customer, weeks later.

### Part 4: When things fail

#### Redis down or full: fall back, don't drop

`count_click()` appends to the stream; if Redis can't take the click (not
configured, down, circuit breaker open, out of memory, or the stream full),
the click is counted the old way, with an `UPDATE`. The system degrades to
step 3's speed instead of losing clicks or failing redirects.

The stream must be a **bounded buffer**: a buffer without a bound turns a
slow consumer into an out-of-memory crash. When it fills up, something must
give, and the choices are the classic three:

- **Drop** the oldest (`XADD ... MAXLEN 1000000`): quiet data loss. No.
- **Block** the producer until there is room: a redirect waiting on the
  worker. No.
- **Push back**: refuse the write and let the producer take another path.
  That's our fallback, and it's called **backpressure**.

Where does the bound go? Our first version let Redis's memory budget (64 MB)
be the bound, and the review showed why that is the wrong place: the stream
shares those 64 MB with the cache and the rate limits. A long worker outage
filled Redis with waiting clicks, and a full Redis refuses every command that
needs more memory, not only new clicks. The rate limiter's script failed, so
the limits failed open (lesson 3), and every cache write failed too. Each
refusal also counted as a Redis failure, so the circuit breaker kept opening.
A stopped worker had switched off a security control.

So the stream has a bound of its own, `MAX_WAITING_CLICKS`: a million
clicks. The append is a small Lua script, atomic like the rate limiter's:

```lua
if redis.call('XLEN', KEYS[1]) >= tonumber(ARGV[2]) then
  return false  -- full: nil for the app, which counts the click itself
end
return redis.call('XADD', KEYS[1], '*', 'code', ARGV[1])
```

At the bound the script returns nil, and the click goes to PostgreSQL like
any other refusal. A full stream is an answer, not an error, so the circuit
breaker doesn't count it. Measured on Redis 7.4, a million waiting clicks
take about 21 MB (with 8-character codes; 43 MB if every click were on a
32-character alias), so the cache and the rate limits keep the rest. The
script costs about 10 to 20 microseconds more per click than a bare `XADD`.

#### The eviction policy that would have lost clicks

Step 2's Redis was a pure cache: `maxmemory-policy allkeys-lfu`, "when full,
evict the least frequently used keys, any of them". With the stream in the
same Redis, that policy could evict the stream itself, and every click
waiting in it would vanish without an error. Step 4 switches to
`volatile-lfu`: evict only keys that have an expiry time anyway (cache
entries, rate limit buckets). The stream never expires, so it is never
evicted, and its bound keeps it from crowding out the keys that can be.
Should Redis still reach its limit with nothing left to evict, it refuses
every write: the click's (the fallback above takes over), but also the rate
limiter's, so the limits fail open, and the cache's, so the cache stops
working until there is room again.

#### Redis restarts: persistence

A cache can start empty; a buffer of uncounted clicks can't. So Redis now
writes every change to an **append-only file** (AOF), on a Docker volume:

- `appendfsync everysec`: Redis hands each write to the operating system at
  once, and the OS flushes to disk every second. If the Redis *process*
  crashes, nothing is lost: the writes are already in the kernel. If the
  whole *machine* crashes, up to about a second of clicks is.
- `appendfsync always` flushes on every write: no loss, but far slower.
  `no` leaves flushing to the OS (often 30 seconds).

CI proves the restart part on every pull request: it stops the worker, clicks
5 times, restarts Redis, starts the worker, and waits for all 5 clicks to be
counted (`scripts/check_clicks.py`). It also checks that the 5 clicks were
*not* counted before the restart: had they gone straight to PostgreSQL
through the fallback, the test would prove nothing.

#### Ambiguous failures

A timeout on the append doesn't say whether the click landed. We count it in
PostgreSQL to be safe, so if it did land, the worker counts it a second time.
A connection can also break after Redis ran the `XADD`, before the reply
arrived. The client then retries once, as it does on any broken connection
([lesson 3](#retries-and-a-lesson-about-measuring)), and the click lands
twice. That's a choice, not an accident: when you can't know, decide which
way to be wrong. For click counts, a rare double count beats a lost click.
(The **two generals problem** says no protocol removes this doubt over an
unreliable network; idempotency keys only move it somewhere you can deal
with it.)

So "exactly once" is a promise about the worker: it counts each click in
the stream once, whatever fails. Around it, a click can still be counted
twice: after an ambiguous failure, or when Nginx retries a redirect on
another instance (lesson 4). And a crash of the whole machine can lose up to
about a second of clicks (see "Redis restarts" above).

#### The worker: crash-only, and alive is not the same as working

- **It crashes**: Docker starts it again (`restart: unless-stopped`), and it
  carries on from the checkpoint. It has no shutdown procedure that must run
  for the data to be right, which is lesson 4's **crash-only design** again.
- **It's stopped** (`docker stop` sends SIGTERM): it finishes the round it is
  in and exits. Uncounted clicks wait in the stream.
- **PostgreSQL or Redis is down**: it logs the error and tries again every
  second. Restarting it wouldn't help, so nothing should restart it.
- **Its loop is stuck**, by a bug or a wait without a limit: restarting it
  would help, but a stuck process never exits by itself.

Restarting is the cure for the last case only, so whatever restarts the
worker must ask "is it going round its loop?", not "is it making progress?".
The worker touches a **heartbeat** file on every round, failed or not;
`python -m app.heartbeat` checks that the last touch is less than 30 seconds
old. Every wait inside a round has a limit (2 seconds for Redis; for
PostgreSQL, 10 to get a connection and 5 per statement), with the one gap
that lesson 2 names: a PostgreSQL that is alive but stuck holds a query on an
open connection until it answers. So the heartbeat goes stale only when the
loop is stuck, in our code or in that one wait. (After a restart, a new
connection to a stuck PostgreSQL gives up after 5 seconds, so the worker's
rounds fail, and go on, until the database answers again.)

That check is the container's health check, but a failing health check
restarts nothing. Docker and Docker Compose only mark the container
**unhealthy**: `docker compose ps` shows it, and `docker compose up --wait`
and `depends_on: condition: service_healthy` wait for a healthy one. A
restart policy acts only when the process exits. (Restarting on a failed
check is what a Kubernetes **liveness probe** does.) So the worker brings
its own **watchdog**: a thread that reads the heartbeat every 5 seconds and,
once the health check would fail, ends the process with `os._exit(1)`. The
restart policy does the rest, and crash-only design makes that safe.
`sys.exit` would end only the watchdog's thread, and SIGTERM only asks the
loop to stop, which a stuck loop never notices. A new worker gets 30 seconds
for its first round: Docker restarts a container with its files, the old
heartbeat included.

**Progress** is a different signal: the backlog (`XLEN`) and the age of its
oldest entry (its ID is a timestamp). That's a metric to graph and alert on,
which is step 5.

The health check imports nothing but the standard library, on purpose: our
first version ran `python -m app.worker --alive`, which imports SQLAlchemy,
Pydantic and redis-py, and took 1.0 second of CPU every 10 seconds. The
heartbeat module takes 0.15 s.

### Going further

- **Partitions.** One stream and one worker counted 100,000 clicks in 2.3 s.
  Past that, split the stream (`clicks:v1:0` ... `clicks:v1:7`, by a hash of
  the code), with one checkpoint row and one worker per partition; the
  table's primary key (`stream`) already allows it. That is how Kafka
  scales: partitions, each read by one consumer of a group.
- **The outbox pattern**, the same problem the other way round: to publish an
  event *because* a database row changed, write the event into an `outbox`
  table in the same transaction, and let a relay copy it to the stream. Never
  "commit, then publish": a crash in between loses the event.
- **Kafka or Redis Streams?** Kafka for retention measured in days, replay by
  many teams, and volumes beyond one machine; Redis Streams when Redis is
  already there and the stream is a short buffer.

### Testing it

- **Failure injection.** To fail the transaction halfway, a test swaps the
  `UPDATE` for one on a table that doesn't exist: the checkpoint must not
  move, and the same clicks must be counted once when the database is "back".
  To crash after the commit, `xtrim` raises: the clicks stay in the stream but
  are never counted again.
- **A race, made deterministic.** Worker B reads a batch, worker A counts the
  same entries, then B tries to count its batch: the compare-and-set refuses.
  No threads, no timing, the same result every run.
- **A race, made likely.** Two threads drain 300 clicks with a batch size of
  7, so they collide again and again: every link must end up at exactly 100.
- **A bound, without a million clicks.** The tests lower `MAX_WAITING_CLICKS`
  to 1 or 2 with `monkeypatch`: at the bound, a click is counted in
  PostgreSQL at once, and the circuit breaker stays shut.
- **The checkpoint's birth.** Two workers starting together both find no
  checkpoint and both insert one. A SQLAlchemy `before_flush` event inserts
  the row from "another worker" just before ours: one row, no error.
- **Shapes and junk.** Replies of other shapes (RESP3's dict, a broken entry)
  raise; entries that aren't clicks, bytes that aren't UTF-8 among them, are
  skipped without blocking the clicks behind them.
- **A watchdog that can't end pytest.** The watchdog takes its clock and its
  exit function as arguments. The tests pass a fake clock, and an exit
  function that only notes the status: a heartbeat 30 seconds old ends the
  worker with status 1; rounds less than 30 seconds apart, and a new
  worker's first 30 seconds, don't.
- **The real stack, in CI.** The smoke test waits for its click to be counted;
  `scripts/check_clicks.py` checks clicks across a Redis restart.

### Try it yourself

```bash
docker compose up --build
code=$(curl -s -X POST localhost:8000/api/links -H 'content-type: application/json' \
       -d '{"url":"https://example.com"}' | python3 -c 'import json,sys; print(json.load(sys.stdin)["code"])')
curl -s -o /dev/null localhost:8000/$code
curl -s localhost:8000/api/links/$code                   # "clicks": 1
docker compose exec redis redis-cli XLEN clicks:v1        # 0: counted, and forgotten

# Stop the worker: the clicks wait in the stream.
docker compose stop worker
for i in 1 2 3; do curl -s -o /dev/null localhost:8000/$code; done
docker compose exec redis redis-cli XRANGE clicks:v1 - +  # three entries
curl -s localhost:8000/api/links/$code                   # still 1
docker compose restart redis                             # they survive this too
docker compose start worker
curl -s localhost:8000/api/links/$code                   # 4, a moment later

docker compose exec db psql -U app -d shortener -c "SELECT * FROM click_checkpoints"
docker compose exec redis redis-cli CONFIG GET maxmemory-policy   # volatile-lfu
```

### Exercises

1. **Watch the lag.** Write a script that prints, every second, how many
   clicks are waiting and how old the oldest one is (hint: its ID). Stop and
   start the worker while clicking. What would you alert on, and after how
   long?
2. **Batch size.** Measure the time to drain 100,000 clicks with batch sizes
   of 10, 100, 1,000 and 10,000. Explain the curve: what does each transaction
   cost, and what does each row cost?
3. **Back off.** During an outage the worker logs a failure every second:
   3,600 lines an hour. Add capped exponential backoff with jitter, and log
   only when the state changes ("database unreachable", "back"). Test it with
   a fake clock.
4. **Partitions.** Split the stream into 4 by a hash of the code, with a
   checkpoint row and a worker each. Counting would be correct with any split
   (sums don't care about order), so why split by code at all? (Think about
   two workers updating the same row.)
5. **Consumer groups.** Rewrite the worker with `XREADGROUP` and `XACK`.
   Where does a crash between `COMMIT` and `XACK` leave you, and what would
   you need to add to keep exactly-once?
6. **Unique visitors.** Count distinct visitors per link with HyperLogLog
   (`PFADD`, `PFCOUNT`: at most 12 KB per link, about 0.81% error). What
   identifies a visitor, and what does privacy law say about storing it?
7. **Durability, measured.** Under load, kill Redis with
   `docker compose kill -s SIGKILL redis`, with `appendfsync everysec` and
   then `always`. Count the clicks lost each time, and the throughput. Can you
   lose clicks without crashing the whole machine?

### Interview questions this lesson prepares you for

- "Design a view counter for a site with 100,000 views a second."
- "What's the difference between a message queue and a log? Kafka or RabbitMQ?"
- "At-most-once, at-least-once, exactly-once: how do you get exactly-once?"
- "Your consumer crashed after writing its results but before committing its offset. What happens?"
- "How do you stop two consumers from processing the same message twice?"
- "What is a hot row (or hot key), and how do you deal with one?"
- "What happens when the producer is faster than the consumer? What's backpressure?"
- "Redis persistence: RDB or AOF? What does `appendfsync everysec` risk?"
- "Which Redis eviction policy would you choose, and what can go wrong with `allkeys-lru`?"
- "How do you health-check a background worker? Liveness, readiness and progress."
- "What is eventual consistency? When is it not acceptable?"
- "What is the outbox pattern, and what problem does it solve?"

**Next:** step 5 measures it all: metrics (Prometheus and Grafana, including
the click backlog), a k6 load test, and benchmarks across every step.

---

## Lesson 6: Seeing what the system does

Steps 1 to 4 built a service that is fast and survives failures, but we could
only see it through tests and one-off benchmarks. Running for real, you need
to know at any moment: is it working, how fast, for how many people, and
what is about to break? And you need to know before your users tell you.

Step 5 makes the system **observable**: every app instance and the click
worker count what they do; **Prometheus** collects the numbers every 15
seconds and checks **alerts**; **Grafana** draws them; a **load test** (k6)
pushes realistic traffic through the whole stack; and the same load test
**benchmarks** every step of the project. The first thing the numbers showed
was a waste nobody had noticed: the click worker was committing a transaction
for every click or two (Part 5).

### What we built

```
                 every 15 s: GET /metrics
   Prometheus ──────────────────────────────▶ app × 3   (found by DNS, port 8000)
   │   │      ──────────────────────────────▶ click worker (port 8001)
   │   │
   │   └─ alerts.yml: recording rules and 10 alerts ─▶ localhost:9090/alerts
   │                                                    each links to docs/RUNBOOK.md
   ▼
   Grafana, localhost:3000: one dashboard, 23 panels, each a PromQL query

   k6: 200 redirects a second, Zipf-popular links ─▶ Nginx ─▶ app × 3
```

| File | What's new |
|------|------------|
| `app/metrics.py` | Every metric: requests (a middleware), cache, clicks, rate limits, Redis fallbacks, the circuit breaker; collectors for the connection pool and the click backlog |
| `app/main.py` | `GET /metrics`, put together on a thread of its own; the middleware; the instance's version and number; the click backlog, on every instance's page too |
| `app/worker.py` | The worker serves its own metrics on port 8001, and lingers 0.2 s after a batch that wasn't full |
| `monitoring/prometheus/` | `prometheus.yml` (what to collect), `alerts.yml` (recording rules, alerts), `alerts_test.yml` (12 tests for promtool) |
| `monitoring/grafana/` | The data source and the dashboard, as files: Grafana starts ready to use |
| `docs/RUNBOOK.md` | What to do when each alert fires |
| `loadtest/redirects.js` | The k6 load test; `make loadtest`, and a workflow to run it on GitHub |
| `nginx/default.conf` | `/metrics` is refused from outside |
| `scripts/check_metrics.py` | For CI: every target up, every rule working, every dashboard query valid |
| `docs/BENCHMARKS.md` | Every step, measured with the same load test |
| `tests/test_metrics.py`, `tests/test_monitoring.py` | 37 tests: each metric moves when it should; every query names metrics that exist |

### Part 1: Metrics

#### Three ways to see inside a running system

- **Logs**: a line per event, with all its detail. "This request failed, with
  this traceback." The answer to "what happened to *this* request?", but
  costly at volume and hard to add up.
- **Metrics**: numbers, added up as things happen. "18,234 redirects so far,
  3 failed." Cheap (a counter costs the same whether it counts ten or ten
  million), ideal for graphs and alerts, but with no detail about any single
  request.
- **Traces**: the path of one request through every service, with the time
  spent in each. Essential with dozens of services; with two, logs and
  metrics are enough.

Metrics answer "is something wrong, and since when?"; logs and traces answer
"why?". An incident needs both.

#### Pull, not push

Every 15 seconds, Prometheus **pulls** (it *scrapes*): it asks each target
for its `/metrics` page, which is plain text.

```
# HELP http_requests_total HTTP requests answered, by method, route and status code.
# TYPE http_requests_total counter
http_requests_total{method="GET",route="/{code}",status="302"} 18234.0
http_requests_total{method="GET",route="/{code}",status="404"} 951.0
```

Why pull rather than have each app push its numbers?

- **Silence is a signal.** A target that doesn't answer is recorded as
  `up == 0`. With push, silence is ambiguous: down, or nothing to say?
- **The app doesn't depend on its monitoring.** It doesn't need to know
  where Prometheus is, and a slow Prometheus can't slow it down.
- **Prometheus sets the pace**, so a misbehaving app can't flood it.

The price: Prometheus must find its targets (Part 2), and a job that lives
two seconds may end between two scrapes. Such jobs push to a *Pushgateway*.

#### Four kinds of metric

| Kind | What it is | Ours, for example |
|------|------------|-------------------|
| **Counter** | Only goes up (and back to 0 when the process restarts). You never read its value, only how fast it grows | `http_requests_total`, `cache_lookups_total` |
| **Gauge** | Goes up and down: a value right now | `http_requests_in_progress`, `click_backlog_entries` |
| **Histogram** | Counts observations into buckets: how many took up to 1 ms, up to 2.5 ms... up to 5 s, plus their count and sum | `http_request_duration_seconds` |
| **Summary** | Counts and sums observations too, and can work out quantiles (the p50, the p99) inside the process. The Python client keeps only the count and sum | None: quantiles worked out in each process can't be added up across instances, so we use histograms (below) |

`prometheus_client` adds a shorthand, **Info**: a constant (1) whose labels
carry facts, such as `app_info{version="0.5.0", instance_number="2"}`. On the
/metrics page it is a gauge (`# TYPE app_info gauge`).

Why count requests rather than compute "requests per second" in the app?
The app would have to choose a time window, and every graph would be stuck
with it. A counter lets Prometheus compute the rate over any window
afterwards, across all instances. And it survives a missed scrape: the next
one still has the total.

Why a histogram rather than a summary, with "the p99" computed in the app?
**Percentiles can't be averaged**: the p99 of three instances is not the
average of their three p99s. Bucket counts, though, can be added across
instances, and Prometheus estimates the percentile from the sum. The
estimate is only as precise as the buckets, so ours are dense where
redirects live (1 to 25 ms), and 0.1 s is a bucket boundary on purpose: it's
our latency objective (Part 3), so "how many took 100 ms or less" is an
exact count.

#### Labels, and how to break Prometheus

Every combination of label values is a separate **time series**, held in
memory by the app and by Prometheus. `route="/{code}"` is one series.
`route="/abc1234"` would be one per link, and a scanner requesting `/aaaa`,
`/aaab`, ... would create new series without end, until Prometheus runs out
of memory. That is a **cardinality explosion**, the most common way to break
a Prometheus server, and anyone on the internet could trigger ours.

So every label value comes from a short, fixed list:

- **route**: the route's *template*. FastAPI's router leaves the route it
  matched in the request's `scope` (the dictionary handed down through every
  middleware), and the middleware reads `scope["route"].path` once the app
  has answered. It does that for our routes only: FastAPI's own pages
  (`/docs`, `/docs/oauth2-redirect`, `/redoc`, `/openapi.json`) are labelled
  with their paths, a fixed list the middleware is given at startup. A
  request that matched no route is `unmatched`: a 404 for `/no/such/page`,
  the router's redirect from `/health/` to `/health`, or a request refused
  before it reaches the router, such as a 413 for a body over 16 KB.
- **method**: one of the seven known methods, or `other`. Anyone can send a
  method called `FOOBAR`.
- **status**: in practice, a dozen codes.

Link codes, user IDs, URLs and IP addresses are never labels; they belong in
logs. One more detail: a labelled counter only appears once a label value is
used. We create the known ones at start-up, at 0, so that a query for
`cache_lookups_total{result="bypass"}` finds 0, not nothing.

#### What to measure: RED and USE

Two checklists cover most of it:

- For anything that serves requests, the **RED** method: **R**ate, **E**rrors,
  **D**uration. It describes what users experience.
- For every resource, the **USE** method: **U**tilization (how busy),
  **S**aturation (how much work waits), **E**rrors. It explains *why*.

Google's "four golden signals" (latency, traffic, errors, saturation) are the
same idea. Ours:

| Question | Metric |
|----------|--------|
| How many requests, of which kind? | `http_requests_total{method, route, status}` |
| How many fail? | the same, with `status=~"5.."` |
| How long do they take? | `http_request_duration_seconds` |
| Is an instance saturated? | `http_requests_in_progress`, `process_cpu_seconds_total` |
| Is the connection pool saturated? | `db_pool_connections_in_use` / `db_pool_connections_limit` |
| Is the cache working? | `cache_lookups_total{result}` |
| Do clicks keep up? | `click_backlog_entries`, `click_backlog_oldest_age_seconds` |
| Is Redis failing? | `redis_fallbacks_total`, `circuit_breaker_open` |

#### How the numbers are counted

- **A middleware** wraps every request: it starts a timer, notes the status
  code when the answer starts, and in a `finally` (so that a crash still
  counts, as a 500) adds 1 to the counter and records the duration.
- **Counters in place**, where the event happens: a cache lookup, a 429, a
  Redis call that fell back, the circuit breaker opening.
- **Collectors**, for numbers cheaper to measure when asked than to keep up
  to date. When Prometheus scrapes, one asks SQLAlchemy's pool how many
  connections are lent out; another asks Redis, in one round trip, how many
  clicks wait in the stream (`XLEN`), the oldest one (`XRANGE ... COUNT 1`),
  and the time (`TIME`). A stream ID starts with its arrival time in
  milliseconds, so the oldest click's age is Redis's clock minus its ID. Both
  come from Redis's clock: a difference between two machines' clocks can't
  skew it.
- **A gauge misses what happens between scrapes.** A circuit breaker that
  opens for 5 seconds and closes again between two scrapes never shows as
  open. So the breaker also counts how many times it opened: a counter can't
  miss it.
- **A gauge worked out when asked** (`set_function`). The breaker's gauge
  reads the breaker's clock at each scrape: 1 while calls skip Redis. Set
  only when the state changed, it would still say "open" on an instance
  without requests long after Redis came back, because only a call closes
  the breaker.

The worker answers no HTTP requests, so it has a registry of its own, served
on port 8001 by `prometheus_client`'s small built-in server, on a thread:
batches, clicks counted, errors, time per batch, the backlog, and its
process's CPU and memory. Every app instance serves the backlog too, from
the same collector: the worker's page goes when the worker stops, and that
is exactly when the clicks pile up (Part 3 tells how we found out).

`/metrics` is for Prometheus only: it tells whoever reads it how much traffic
you have and what fails. Nginx answers 404 to it from outside; Prometheus asks
each instance directly, on Docker's internal network. Taking the path makes
`metrics` a reserved code, which breaks a link that already had that alias:
it stops redirecting (the CHANGELOG says so, under 0.5.0). And the page is put
together on a thread of its own (`anyio.to_thread.run_sync`, with a
`CapacityLimiter(1)` of its own). Not on the event loop: measuring the
backlog asks Redis, and a slow Redis would hold up every request meanwhile.
Not under the requests' limit of 40 threads either: an instance whose
threads are all busy must still answer. The moment you need the numbers most
is when an instance is overloaded.

### Part 2: Prometheus and PromQL

#### Finding the targets

Prometheus asks every instance directly, not through Nginx: through Nginx,
each scrape would reach a different instance, and every graph would show a
random third of the truth. To find them, it uses lesson 4's trick: Docker's
DNS server answers the name `app` with the address of every instance, and
`dns_sd_configs` asks it every 15 seconds. Start a fourth instance and
Prometheus finds it. (On Kubernetes, `kubernetes_sd_configs` asks the API
server instead.)

Prometheus adds two labels to every series it collects, `job` (`app`,
`click-worker`) and `instance` (address and port), and records a few series
of its own for every target, such as `up` (1 if the scrape worked) and
`scrape_duration_seconds`.

#### PromQL in five queries

```promql
# Requests per second, over the last 5 minutes, all instances together
sum(rate(http_requests_total{job="app"}[5m]))

# The same, by route
sum by (route) (rate(http_requests_total{job="app"}[5m]))

# The share of redirects that failed
  sum(rate(http_requests_total{route="/{code}", status=~"5.."}[5m]))
/ sum(rate(http_requests_total{route="/{code}"}[5m]))

# The 99th percentile of redirect latency, across all instances
histogram_quantile(0.99,
  sum by (le) (rate(http_request_duration_seconds_bucket{route="/{code}"}[5m])))

# Requests per second by instance *number*, not by IP address
sum by (instance_number) (
  rate(http_requests_total{job="app"}[5m])
  * on (instance) group_left (instance_number) app_info)
```

- `rate(x[5m])` is how fast a counter grew, per second, over 5 minutes. It
  understands restarts: a counter that drops to 0 started again, and the
  count carries on from there.
- `sum by (label)` adds series up, keeping only that label.
- **`rate` first, then `sum`.** Summing raw counters first would turn one
  instance's restart into a huge drop in the total.
- `histogram_quantile` works on the rates of the buckets; the `le` label
  ("less than or equal") is each bucket's upper bound.
- The last query is a **join**. `app_info` has the value 1 and carries the
  instance number as a label; multiplying by it copies that label onto the
  request rates. IP addresses change at every restart, while the instance
  numbers are the ones in the logs and in `/health`.

#### Recording rules

Some queries run all the time: the error ratios over 5 minutes, 30 minutes,
1 hour and 6 hours, and the share of fast redirects. **Recording rules** make
Prometheus compute them every 15 seconds and store the result as a new
series, such as `slo:redirect_errors:ratio_rate5m`. The alerts and the
dashboard read those: computed once, and defined once. The name follows
Prometheus's convention, `level:metric:operations`.

### Part 3: Alerts that mean something

#### SLIs, SLOs and the error budget

- An **SLI** (service level *indicator*) measures what users experience.
  Ours: the share of redirects answered without a server error, and the
  share answered within 100 ms.
- An **SLO** (*objective*) is a target for an SLI over a period. Ours, over
  30 days: 99.9% of redirects without a server error, 99% within 100 ms.
- An **SLA** (*agreement*) is a contract that costs money when broken. It is
  looser than the SLO, so that you hear about trouble before it's expensive.

Why not 100%? Nothing is 100% reliable (not the network, not the user's
phone), and a 100% target would forbid every change. The 0.1% left over is
the **error budget**: 0.001 × 30 days × 24 hours × 60 minutes = about **43
minutes** of total failure a month. While budget is left, ship features;
once it's spent, work on reliability. An argument between "move fast" and
"don't break things" becomes a number both sides can read.

#### Alert on symptoms, not causes

An alert that **pages** wakes someone up, so it should mean that users are
hurt *now*: "redirects are failing", not "CPU at 90%". CPU at 90% while every
request is fast is fine; a full disk on a spare machine may hurt no one.
Causes belong on the dashboard, or in a **ticket** for working hours. And
every alert must call for an action: if the right answer to a page is "wait
and see", it shouldn't be a page. People learn to ignore alerts that cry
wolf; that's **alert fatigue**, and it is how real alerts get missed.

| Severity | Our alerts |
|----------|------------|
| **page** | Redirects failing fast (`RedirectErrorBudgetBurningFast`); no instance answering (`AppDown`) |
| **ticket** | Errors burning the budget slowly; slow redirects; an instance missing; the worker down or behind; clicks bypassing the stream; the Redis circuit open; a saturated connection pool |

#### Burn rates: how fast is the budget going?

"Alert when more than 0.1% of requests fail" is a poor alert. A five-minute
blip at 0.2% fires it, though it costs a sliver of the month's budget. A
better question is: **how fast are we spending the budget?**

The **burn rate** is the error rate divided by the budgeted rate (0.1%). At
a burn rate of 1, the budget lasts exactly 30 days. At **14.4**, an hour
spends 2% of it (14.4 × 1 hour ÷ 720 hours), and it's all gone in about two
days. For a 99.9% objective, Google's SRE workbook (the chapter "Alerting on
SLOs") recommends three rules:

| Severity | Burn rate | Long window | Short window | Budget spent before it fires | Ours |
|----------|-----------|-------------|--------------|------------------------------|------|
| page | 14.4 | 1 hour | 5 minutes | 2% | `RedirectErrorBudgetBurningFast`, a page |
| page | 6 | 6 hours | 30 minutes | 5% | `RedirectErrorBudgetBurningSlowly`, a **ticket** |
| ticket | 1 | 3 days | 6 hours | 10% | None |

We make the second rule a ticket on purpose. At 6 times the budgeted rate,
0.6% of redirects fail, and the month's budget would last five days: time
enough to fix it in working hours, without waking anyone. The price: from
Friday evening to Monday morning, that pace spends about half the month's
budget. We also leave out the third rule, so a steady burn below 6 times the
budgeted rate raises no alert at all: at 3 times, the budget is gone in ten
days.

```yaml
- alert: RedirectErrorBudgetBurningFast
  expr: |
    slo:redirect_errors:ratio_rate1h > (14.4 * 0.001)
    and
    slo:redirect_errors:ratio_rate5m > (14.4 * 0.001)
  for: 2m
  labels:
    severity: page
```

Why two windows? The long one says "this is serious, not a blip". The short
one says "and it's still going on": without it, the alert would keep firing
for up to an hour after the fix, while the hourly average comes down.
`for: 2m` makes the condition hold for 2 minutes (8 checks) before the alert
fires, which filters out a single odd scrape.

#### When nobody is left to count the errors

If every app instance is down, Nginx answers every redirect with a 502, but
no instance is left to count anything. `http_requests_total` gets no new
data, the ratios have nothing to divide, and the burn-rate alerts stay
quiet. The metric is *missing*, not bad. So `AppDown` counts instances
instead:

```yaml
expr: (count(up{job="app"} == 1) or vector(0)) == 0
```

`count()` of nothing is not 0, it is *no result*, and "no result == 0" is
no result again: without `or vector(0)`, this alert could never fire. An
alert on a metric that disappears exactly when things go wrong is the most
common silent failure in monitoring. (Better still would be to measure where
the user stands: scrape Nginx, which sees every 502, or probe the service
from outside, with Prometheus's blackbox exporter.)

We fell into this trap ourselves. At first only the worker measured the
backlog, so a stopped worker took the backlog off the dashboard, and
`ClicksWaitingTooLong` (`max(click_backlog_oldest_age_seconds) > 60`) had
nothing left to compare: it could not fire while the worker was down, the
very moment clicks pile up. Our promtool test fed it a backlog that kept
growing on the worker's own page, so it passed. The review found it. Now
every app instance measures the backlog too, and a test stops the worker:
both `ClickWorkerDown` and `ClicksWaitingTooLong` fire.

#### Runbooks

Every alert links to its section of `docs/RUNBOOK.md`: what it means, what
users feel, and the first things to check, with the commands. At 3 a.m., the
person paged shouldn't have to work out what
`slo:redirect_errors:ratio_rate1h` means. A test checks that every alert has
its section, and links to it.

#### Testing alerts

An alert you have never seen fire is a guess. Prometheus's own tool,
**promtool**, runs the rules against made-up history:

```yaml
- name: no instance at all pages someone
  interval: 1m
  input_series:
    - series: 'up{job="click-worker", instance="worker:8001"}'
      values: "1x5"          # the worker answers; no app instance exists at all
  alert_rule_test:
    - eval_time: 2m
      alertname: AppDown
      exp_alerts:
        - exp_labels:
            severity: page
```

Twelve such tests check that each alert fires when it should, and that the
burn-rate alerts stay quiet within budget. `make monitoring-check` runs them
in Docker, and CI on every pull request. One more test reads every query in
`alerts.yml` and in the dashboard, and checks each metric name against the
metrics the code defines. To Prometheus, a query on a metric that doesn't
exist is not an error: it finds nothing. Rename a metric and forget an
alert, and the alert can never fire, without a word.

### Part 4: Load testing

#### Open and closed models

There are two ways to generate load:

- **Closed**: N virtual users, each sending a request, waiting for the
  answer, then sending the next. When the server slows down, the users wait,
  so fewer requests are sent: the load drops exactly when the server
  struggles.
- **Open**: requests arrive at a fixed rate, whatever happened to the ones
  before. Real users behave like this: a thousand people don't wait for each
  other's pages to load.

The closed model's flaw has a name, **coordinated omission** (Gil Tene).
Say the server freezes for one second. A closed client with one user records
one slow request, then carries on. An open client at 100 requests a second
records 100 slow requests, which waited from 0 to 1 second each: that is
what real users lived through. A closed test can report a p99 that is wrong
by orders of magnitude.

k6's `constant-arrival-rate` executor is an open model. It starts RATE
requests every second and keeps a pool of virtual users only as workers. If
all of them are busy waiting for answers, it can't start a request on time,
and counts it in `dropped_iterations`: the server didn't keep up. Our
threshold allows none.

#### Traffic that looks real

- **Popularity follows Zipf's law**: the k-th most popular link gets clicks
  in proportion to 1/k. With 100 links, the most popular gets 19% of the
  clicks, and the top ten 56%. Clicking a single link would measure only the
  cache; clicking all links equally would hide the hot ones.
- **5% of requests ask for codes that don't exist** (typos, scanners), which
  exercises the 404 path and the negative cache.
- **Rate limits are raised** (`loadtest/compose.yml`): every k6 request
  comes from one IP address, which a real limit would rightly stop.
- k6 runs in Docker and goes through Nginx, so the numbers include it.

#### Thresholds: a load test that passes or fails

```js
thresholds: {
  http_req_failed: ["rate<0.001"],                   // 99.9% succeed
  "http_req_duration{kind:redirect}": ["p(99)<100"], // 99% within 100 ms
  checks: ["rate>0.999"],
  dropped_iterations: ["count==0"],                  // the server kept up
},
```

These are the SLOs again, seen from outside. CI runs a 10-second load test on
every pull request, to prove that the script and the stack work together;
longer ones run when asked: `make loadtest RATE=500 DURATION=2m`, or from
GitHub's Actions tab (Load test, Run workflow).

#### Little's law

**L = λ × W**: the average number of requests inside a system equals their
arrival rate times the average time each one spends there. At 300 requests a
second and 3 ms each, about one request is in flight at any moment. If the
time grows to 1 second, 300 are, and each holds a thread, and maybe a
database connection. Each instance runs requests on 40 threads, so past
about 40 requests in progress, new ones queue for a thread. That's why the
dashboard shows requests in progress next to latency: when W grows, L grows,
until it hits a limit and everything queues. The law also tells k6 how many
virtual users it needs: the rate times the slowest answer you expect.

### Part 5: What the numbers showed

#### The worker committed for every click or two

The first run of the load test, with the dashboard open: 300 redirects a
second, and PostgreSQL committing **805 transactions a second**. Redirects
were served from Redis, and clicks were supposed to be counted in batches,
so where did they all come from? The worker's own metrics answered: **259
batches a second**, about one click each, and the worker using 55% of a CPU.

Lesson 5 said batches adapt to the load by themselves, and they do, but not
the way we hoped. `XREAD ... BLOCK` returns as soon as there is anything to
read. At 300 clicks a second, a click arrives every 3.3 ms; a round of the
worker (read, count, commit, trim) took about 4 ms, so each read found one or
two new clicks, and each became a transaction. Batches only grew towards
1000 when the worker fell behind.

The fix is to wait a little before reading again, when the last batch wasn't
full: a **linger**. Kafka's producers call it `linger.ms`; TCP's Nagle
algorithm does the same with small packets. 0.2 seconds at 300 clicks a
second gathers about 60 clicks per batch. A full batch means a backlog, and
then the worker doesn't wait: a backlog still drains at full speed.

| At 300 redirects a second | No linger | Linger 0.2 s |
|---------------------------|-----------|--------------|
| Batches per second | 259 | 4.8 |
| PostgreSQL commits per second | 805 | 28 |
| Worker CPU | 55% of a core | 2.2% |
| Redirect p99, seen by k6 | 9.1 ms | 4.6 ms |

The price is that a click is counted up to 0.2 seconds later; for a click
counter, no one can tell. Redirects got faster too, because the worker no
longer competed with the app instances for the same four cores.

And why 805 commits for 259 batches, three per batch where we expected one?
Logging every statement for one click (`ALTER SYSTEM SET log_statement =
'all'`) showed it. Before it lends a connection, SQLAlchemy checks that the
connection still works (`pool_pre_ping=True`), and for psycopg it sends an
empty statement in autocommit mode, which PostgreSQL counts as a transaction
of its own. The worker borrows a connection twice per batch, once to read
the checkpoint and once to count, so each batch was two checks and one real
commit. The checks are cheap (no disk write), but each is a round trip, and
the linger removed most of them along with the batches.

The lesson is bigger than the fix: a number we had no plan to look at
contradicted what we believed about our own design. Observability is not for
confirming what you know; it is for showing you what you didn't.

### Part 6: Every step, measured

With a load test that any step can run, we could finally answer what each
lesson had only argued: what did each step buy, and what did it cost? We
ran the same test against all five steps, each from its own branch, on the
same machine, twice. [docs/BENCHMARKS.md](docs/BENCHMARKS.md) has the method
and every number; this part is what to take from them.

#### A fair comparison

A benchmark compares, so everything but the thing compared must stay the
same:

- **The same test, the same machine, a fresh start.** Each step ran from its
  own branch, with an empty database, after the same warm-up.
- **The load generator on a core of its own.** k6 needs CPU too. Sharing
  the cores of the system it measures, it would slow it down, and more so at
  higher rates. The system under test got cores 0 to 2, k6 core 3.
- **Several rates, not one.** A single number hides where things break.
  Running 100, 300, 600 and 900 requests a second shows the *knee*: the rate
  where latency stops being flat and starts to climb, because requests
  queue.
- **Twice.** With a single run, you can't tell a difference from noise.
- **Resources, not only latency.** Latency tells you *that* a step is slow;
  the CPU of each container tells you *where* the time goes.

And measure the measurement. Our first CPU figures were samples from
`docker stats`, each over about a second, and they were too high. Every 10
seconds, Docker runs each app container's health check, which starts a
Python interpreter to call `/health`: about a quarter of a second of CPU. A
sample that catches one shows a quarter of a core more than the requests
used. So we read each container's CPU counter instead (the total CPU time it
has used, kept by its cgroup), before and after each run: an average over 30
seconds, which no single moment can skew.

#### What each step bought

| Step | Answered a second, 900 asked | Median redirect, 600 asked |
|------|-----------------------------:|---------------------------:|
| 1: one instance | 254 to 260 | 2.2 to 2.3 s |
| 2: a Redis cache | 152 to 244 | 2.3 to 2.5 s |
| 3: three instances behind Nginx | 516 to 536 | 0.5 to 0.7 s |
| 4: clicks counted by a worker | 879 to 880 | 3.6 to 3.9 ms |
| 5: metrics, and the worker's linger | 876 to 881 | 3.0 to 3.1 ms |

The ranges are the two runs. Each line has a lesson:

- **A process is a ceiling.** One instance answered about 250 redirects a
  second, however many arrived. Past that, they queued, and the median went
  from 5 ms to seconds. Python runs Python code on one core at a time (the
  global interpreter lock), and a redirect cost close to 5 ms of CPU.
- **Speeding up what isn't the bottleneck changes nothing.** The cache
  halved PostgreSQL's CPU per redirect, and the ceiling didn't move: the
  limit was the app's process, and asking Redis costs Python about as much
  as asking PostgreSQL. Find where the time goes before you optimize. The
  cache isn't wasted, though. On a real system, the database is on another
  machine, shared, with more data than fits in its memory, and it is often
  the first thing to fall over; here, it sat on the same machine with every
  link in memory. And step 4 builds on the cache: with the link from Redis
  and the click into a stream, a redirect never touches PostgreSQL.
- **Scaling out works until something shared saturates.** Three instances
  doubled the throughput (not tripled: they share three cores with
  PostgreSQL, Redis and Nginx). Then the hot rows took over. Every redirect
  still ran `UPDATE links SET clicks = clicks + 1`, and only one transaction
  at a time can update a row. With PostgreSQL logging every lock wait over
  10 ms, a 30-second run logged 10 waits at 300 requests a second, and
  29,037 at 600. A lock is a part of the work that runs one at a time, and
  no number of instances makes that part faster (Amdahl's law).
- **Take work off the request path.** Step 4 replaced the `UPDATE` with an
  append to a Redis stream: no lock, no transaction, and the app's CPU per
  redirect fell from about 3.5 ms to a little over 2 ms. At 900 requests a
  second, it answered 880 with a median of 22 ms, where step 3 answered
  about 520 with a median of 1.5 s.
- **Observability is cheap, and it paid for itself.** Metrics cost 0.1 to
  0.2 ms of CPU per redirect, and Prometheus and Grafana together about 3% of
  a core. The linger they led to (Part 5) saved more: the worker went from
  25-51% of a core to 4%, PostgreSQL from 11-23% to 3-4%, and step 5's p99 at
  600 requests a second was 30 to 42 ms, against 51 to 140 ms for step 4.

At 900 requests a second, steps 4 and 5 fall behind. They still answer
about 880 a second, but k6 can't start 61 to 145 requests on time, and the
p99 rises to 237-321 ms: both miss their thresholds (Part 4). Step 5 keeps
up at 300 a second, and at 600 in one run of two (the other left 2
requests unsent): at 600 it is near its limit on three cores, and 900 is
past it. At 900, the cores are 76 to 83% busy, and the limit now is plain CPU: the
app needs about 2 ms of it per redirect. More instances wouldn't help on
this machine; more cores, or less work per request, would.

#### Noise

At 100 requests a second, the two runs agreed on every median within 0.3 ms,
but not on the p99s: step 1's was 13 ms in one run and 43 ms in the other,
step 3's 74 ms and 11 ms. Over 30 seconds at 100 a second, the p99 is decided
by the 30 slowest requests, and a single hiccup (a slow disk flush, a busy
neighbour on the same physical machine) moves it. Overloaded, even the
throughput varies: step 2 answered 244 a second at 900 asked in one run, and
152 in the other. Two rules follow. Compare p99s only over long runs or many
runs. And don't believe a difference you haven't seen twice.

### Going further

- **Alertmanager** receives the alerts, groups them, silences them during
  maintenance, and sends pages to PagerDuty or Opsgenie and tickets to a
  chat channel. It can also *inhibit*: while `AppDown` fires, the burn-rate
  alerts add nothing. We stop at Prometheus's alerts page.
- **Tracing** with OpenTelemetry follows one redirect through Nginx, the app,
  Redis and PostgreSQL. *Exemplars* link a histogram bucket to traces of
  requests that landed in it: "show me one of those slow ones".
- **Logs**, structured as JSON, collected in one place (Loki,
  Elasticsearch), with a request ID shared with the traces.
- **Long-term storage**: our Prometheus keeps 7 days (at most 1 GB). Thanos or
  Mimir keep months, for many Prometheus servers at once.
- **Native histograms** (Prometheus 3) choose their own exponential buckets,
  at a much finer resolution, for about the cost of a few classic buckets.

### Testing it

- **Each metric moves when it should** (`tests/test_metrics.py`): a 404 on a
  short code is counted under `/{code}`, the API docs under their paths, an
  unknown path and a redirect that drops a trailing slash under `unmatched`,
  a crash as a 500, a strange method as `other`; a cache hit, a click through
  the stream or the database, a 429, three Redis errors and then a circuit
  open, which reads closed again once its cool-down is over, without a call;
  the pool collector, and the backlog with a click added 5 seconds in the
  past (a stream ID chosen by the test), whose collector is registered
  without a call to Redis. An instance's own page shows the backlog, and
  comes quickly without it when Redis refuses or never answers; a scrape
  held up on purpose holds up no request, and takes none of their threads.
- **The worker's metrics**: batches, clicks counted, entries skipped (once,
  even when a failed batch is read again), errors by system, and its own
  `/metrics` page, served on a free port. A fake stop event records every
  wait, to check the linger happens after a small batch only.
- **Every query names real metrics** (`tests/test_monitoring.py`): a small
  PromQL reader takes each query of the dashboard and the alerts apart, and
  every name must be one the app, the worker or a recording rule defines.
  Every alert has a runbook section, a link to it, and a test that fires it.
- **The alerts behave** (promtool, `alerts_test.yml`), including a worker
  stopped while clicks pile up: both click alerts fire.
- **The real stack, in CI**: `scripts/check_metrics.py` waits until
  Prometheus sees 3 app instances, the worker and itself, every rule works,
  every dashboard query runs, and Grafana serves the dashboard; `/metrics` is
  refused through Nginx. Then k6 runs for 10 seconds.
- **The tests were tested**: we renamed a metric and changed a threshold on
  purpose, and checked that the tests failed.

### Try it yourself

```bash
make loadtest                         # starts the stack (limits raised), then 200 redirects/s for a minute
# Grafana, no login needed to look: http://localhost:3000
# Prometheus's targets and alerts:  http://localhost:9090/targets, http://localhost:9090/alerts
make loadtest RATE=600 DURATION=2m    # where does it break? which panel shows it first?

# One instance's own page (from inside: Nginx refuses it)
docker compose exec app python -c "import urllib.request as u; print(u.urlopen('http://localhost:8000/metrics').read().decode())" | grep '^http_requests_total'

# Ask Prometheus directly
curl -s localhost:9090/api/v1/query --data-urlencode 'query=sum by (route) (rate(http_requests_total[1m]))'

# Make two alerts fire: stop the worker while clicks keep coming
docker compose stop worker
docker compose -f docker-compose.yml -f loadtest/compose.yml run --rm --no-deps -e RATE=20 -e DURATION=8m k6
#   ClickWorkerDown fires after 2 minutes; ClicksWaitingTooLong after about 6
#   (the oldest click must have waited over a minute, for 5 minutes in a row).
#   "Clicks waiting" keeps climbing meanwhile: the app instances measure it.
docker compose start worker           # the backlog is counted within seconds
docker compose up --detach --wait     # back to the normal rate limits
```

### Exercises

1. **A latency budget.** Write burn-rate alerts for the latency objective
   (99% of redirects within 100 ms). What's the budget? Which windows? Test
   them with promtool.
2. **Count the 502s.** When every instance is down, the error-rate SLI sees
   nothing. Export Nginx's status codes (the nginx-prometheus-exporter, or a
   log-based exporter) and compute the SLI from Nginx instead. Which one is
   closer to what users see, and why?
3. **Alertmanager.** Add it to the stack: pages to one receiver, tickets to
   another, `AppDown` inhibiting the burn-rate alerts. Test the routing with
   `amtool`.
4. **Cardinality.** Add a `code` label to `clicks_recorded_total`, run the
   load test with `LINKS=10000`, and watch `prometheus_tsdb_head_series` and
   the size of `/metrics`. Then remove it.
5. **Coordinated omission, measured.** Run the load test with k6's
   `constant-vus` executor (a closed model) while you `docker compose pause
   redis` for 2 seconds. Compare its p99 with the open model's.
6. **Tune the linger.** Measure batches, commits and the worker's CPU with a
   linger of 0, 0.05, 0.2 and 1 second. Where is the knee? What happens to
   the oldest click's age?
7. **Find the limit.** Raise RATE until a threshold fails. Which resource ran
   out first (the USE panels)? Add instances: does it scale linearly? Why
   not on four cores?
8. **Trace a click.** Add OpenTelemetry to the app and the worker, send
   traces to Jaeger, and follow one click from the redirect to the worker's
   commit.
9. **Benchmark one change.** Pick one idea to make a redirect cheaper (an
   `async` redirect with `redis.asyncio` instead of a thread per request, two
   uvicorn processes per container, one middleware fewer) and measure it the
   way `docs/BENCHMARKS.md` does: before and after, twice each, with the CPU
   per redirect. Was it worth the code?

### Interview questions this lesson prepares you for

- "How would you monitor a web service? What would you alert on, and what would you only graph?"
- "Logs, metrics and traces: what is each good for?"
- "Pull or push for metrics? What are the trade-offs?"
- "How do you get the p99 latency across ten instances? Why can't you average percentiles?"
- "What is cardinality, and how can it take down a monitoring system?"
- "What are SLIs, SLOs and SLAs? How do you choose an SLO? What is an error budget for?"
- "Explain burn-rate alerting. Why two windows?"
- "Your service was down for an hour and no alert fired. How could that happen?"
- "How do you avoid alert fatigue?"
- "What is coordinated omission? Open or closed load model?"
- "State Little's law, and use it to size a thread pool or a connection pool."
- "The RED and USE methods: what are they, and when do you use each?"
- "How would you load test a service before launch? What traffic would you generate?"
- "Your service tops out at 250 requests a second. How do you find the bottleneck?"
- "A change made your benchmark 10% faster. How do you know it's real?"

### Looking back

Six lessons, one small service, and most of what a real system needs: an
API with sound status codes and validation (lesson 1), the tooling and
discipline of a professional team (lesson 2), a cache that survives its own
failures and rate limits that hold across instances (lesson 3), load
balancing, unique IDs without coordination and safe migrations (lesson 4),
work taken off the request path, with each click in the stream counted
exactly once (lesson 5), and now the means to see all of it, and to prove
what each step was worth.

The habits matter more than the tools: measure before you believe, decide
how you want to fail before you do, test the failure paths as carefully as
the happy one, and write down why. Those carry over to any system you build.
