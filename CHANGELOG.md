# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Fixed

- The check of `REDIS_URL` at startup refused some valid URLs and passed some
  broken ones. It now refuses a URL that redis-py would read wrongly: an
  unencoded `#`, `/` or `?` in the password can make it use the wrong
  password, host or port, so the check refuses any `#`, an `@` in the path
  (except in a unix socket path without user info), and a query argument
  without `=`. It accepts an `@` in a query value (`?password=p@ss`) and in a
  socket path. It also refuses a query argument that a connection does not
  take or whose value it refuses (`?dbb=1`, a TLS option on `redis://`),
  which used to fail every request that touched Redis, and one that would
  override what the app sets itself, such as `?socket_timeout=30` or
  `?retry_on_timeout=true`: redis-py lets the URL win, which would undo the
  limits that keep a slow Redis from slowing down the app.
- The click worker's watchdog could end a worker that was making progress.
  The worker touched its heartbeat once per round, but a round runs one
  `UPDATE` per link in a batch of up to 1,000, so a database that answers
  each one within its 5 s limit could still keep a round past 30 seconds.
  The next worker then read the same batch and was ended again. Now the
  `UPDATE`s go in chunks of 3, all in the batch's one transaction, and the
  worker touches the heartbeat after each step of a round: reading the
  checkpoint, reading the stream, moving the checkpoint, and each chunk.
- The recursion check (R1) wrote `Class.x = v` into the class's own names.
  So a store on a subclass, even under an `if` that never runs, hid the
  method it inherits, and the recursion through it went unreported; and a
  name in a class body could mean a store that runs only later. Such stores
  are now kept apart, and they count on nested classes, through `cls.x = v`
  and through `setattr(Class, "x", v)` too. A private plain name inside a
  class is now looked up mangled (`__walk` is `_Walker__walk`), as Python
  does. A base `Outer.Inner` is unknown when code may set `Outer.Inner` or
  when `Outer` may inherit it. And `update(*a, *b)` and
  `setdefault(key, **options)` no longer count as calls that must fail.
- Documentation that said less than the whole truth. The tests drop every
  table in `TEST_DATABASE_URL`, not only the app's, so give them a database
  of their own (README). Nginx doesn't count a try whose kept-alive
  connection breaks, so a redirect can reach the instances up to once per
  instance in each of its two passes, not only 3 times (lesson 4,
  `nginx/default.conf`). Without `proxy_set_header Host`, the app's own
  redirects would break (lesson 4). A 413 for a chunked body keeps its
  route's label (lesson 6, `app/metrics.py`), and the dashboard's clicks
  panel names every reason a click goes straight to PostgreSQL.

## [0.5.0] - 2026-11-19

### Added

- Metrics for Prometheus. Each app instance serves `GET /metrics`: requests
  by method, route and status code, their duration (a histogram with a
  bucket at the 100 ms objective), requests in progress, cache lookups by
  result, clicks by path (the stream or the database), requests refused with
  429, Redis calls that fell back, the circuit breaker's state and openings,
  the database connection pool, the clicks waiting in the stream with the age
  of the oldest (the worker serves them too, but they must still show while
  it is down), and the app's version and instance number. Label values come
  from fixed lists only. The page is put together on a thread of its own: a
  slow Redis can't hold up requests, and busy request threads can't hold up
  the page. Nginx refuses `/metrics` from outside.
- The click worker serves its own metrics on port 8001: batches, clicks
  counted, entries skipped, errors by system, time per batch, and the clicks
  waiting in the stream with the age of the oldest.
- Prometheus (the `prometheus` service, port 9090 on 127.0.0.1). It finds
  the app instances through Docker's DNS, keeps 7 days of data (1 GB at
  most), and checks recording rules and 10 alerts: burn-rate alerts over two
  windows for the redirect error objective (99.9% over 30 days), slow
  redirects (objective: 99% within 100 ms), an instance missing or all of
  them down, the click worker down or behind, clicks bypassing the stream,
  the Redis circuit open, and a saturated connection pool. Each alert links
  to its section of the new runbook (`docs/RUNBOOK.md`) and has a promtool
  test (`make monitoring-check`, and CI).
- Grafana (the `grafana` service, port 3000 on 127.0.0.1), with the
  Prometheus data source and a dashboard of 23 panels, provisioned from
  files. Anyone may look without logging in.
- A k6 load test (`loadtest/redirects.js`, `make loadtest`, and a Load test
  workflow on GitHub, run by hand): requests at a fixed rate (an open
  model), links clicked by Zipf's law, 5% unknown codes, and the objectives
  as thresholds. CI runs it for 10 seconds.
- CI checks that Prometheus collects every instance and the worker, that
  every rule works and every dashboard query runs, and that Grafana serves
  the dashboard (`scripts/check_metrics.py`). Tests check that every query of
  the alerts and the dashboard names a metric that exists, and that every
  alert has a runbook section and a test.
- Benchmarks of every step with the same load test (`docs/BENCHMARKS.md`):
  on the same three cores, step 1 answers about 250 redirects a second.
  Step 5 keeps up at 300 a second, and at 600 in one run of two; at 900 it
  answers about 880 a second but falls behind, with a p99 of 248-321 ms.
- Lesson 6 in LEARNING.md: metrics, PromQL, SLOs and burn-rate alerts, load
  testing, what the numbers showed, and what each step bought.

### Changed

- `metrics` is now a reserved code, like `api` and `health`: a new link
  can't have it as its alias (409). This breaks a link that already has that
  alias: it no longer redirects, because an instance answers `/metrics` with
  Prometheus's page, and Nginx answers it with 404. The link itself is kept;
  `GET /api/links/metrics` tells whether you have one.
- After a batch that wasn't full, the click worker waits 0.2 s before reading
  again, so that clicks gather into bigger batches. At 300 clicks a second,
  it went from 259 transactions a second to 4.8, and from 55% of a CPU to
  2.2%. A click is counted up to 0.2 s later than before.

## [0.4.0] - 2026-11-18

### Added

- Clicks are counted off the redirect path. A redirect appends the click to
  a Redis stream (`clicks:v1`) instead of updating PostgreSQL, and a new
  click worker (`python -m app.worker`, the `worker` service in Docker
  Compose, `make worker`) adds the clicks up per link and writes them in
  batches of up to 1000.
- The worker counts each click in the stream exactly once. It stores how
  far it got (a checkpoint, in the new `click_checkpoints` table, migration
  `0003`) in the same transaction as the counts, and moves it with a
  compare-and-set, so a crash of the worker at any moment, or a second
  worker, can't count a click twice or lose one. Counted clicks are then
  removed from the stream. Around the stream, a click can still be counted
  more than once: when Nginx retries a redirect (lesson 4 in LEARNING.md
  gives the bound), or twice when the click reached Redis but the reply
  didn't (a timeout, or a broken connection, which the client retries
  once). A crash of the whole machine can lose up to about a second of
  clicks.
- When Redis can't take a click (not configured, down, or full), it is
  counted in PostgreSQL at once, as before. So is a click that finds a
  million clicks already waiting: the stream stops there (about 21 MB), so
  that a long worker outage can't fill Redis, whose cache and rate limits
  share its 64 MB.
- A health check for the worker: it touches a heartbeat file on every round,
  and `python -m app.heartbeat` checks that the last one is recent. Docker
  only marks a container that fails it as unhealthy, so a watchdog thread
  ends the worker when its loop is stuck (no heartbeat for 30 seconds), and
  Docker starts it again.
- CI checks that clicks wait while the worker is stopped, and survive a
  Redis restart (`scripts/check_clicks.py`). The smoke test waits for the
  worker to count its click.
- Lesson 5 in LEARNING.md: asynchronous processing, streams, and exactly-once
  counting.

### Changed

- Redis keeps the click stream on disk (append-only file, written every
  second, on a `redisdata` volume), and when memory is full evicts only keys
  that expire anyway (`volatile-lfu` instead of `allkeys-lfu`), never the
  stream.
- Click counts in `GET /api/links/{code}` can lag a moment behind, while the
  worker catches up.
- The app asks redis-py for the RESP2 protocol by name, so a new default in
  a later redis-py can't change the shape of the replies it reads.
- The app's Redis clients decode bytes that aren't UTF-8 as U+FFFD instead
  of raising an error, so such bytes no longer stop the worker: an entry
  whose code isn't valid UTF-8 fails validation and is skipped as junk,
  like any other entry that isn't a click on a valid code.

## [0.3.0] - 2026-11-18

### Added

- Three app instances behind an Nginx load balancer (round robin) in Docker
  Compose. Nginx finds the instances through Docker's DNS and looks again
  every 10 seconds. When an instance can't be reached, Nginx tries another
  (2 tries at most), and leaves it out for 10 seconds after 3 failures. A
  `GET` or `HEAD` that ends in a 502 made by Nginx itself (no instance
  answered: one closed the connection mid-answer, or the list of instances
  changed while it waited) is tried once more, so it usually reaches the
  instances at most 3 times. Nginx doesn't count a try whose kept-alive
  connection breaks, so the hard limit is each instance once in each of
  the two passes: 6 times with 3 instances. A click is counted once more
  for each instance that dies mid-answer. A request whose last try times
  out gets a 504 and no second chance.
  Nginx keeps connections to the instances open, and refuses request bodies
  over 16 KB.
- Snowflake IDs, made by each instance without asking anyone: 31 bits of
  seconds since 2026, a 6-bit instance number and a 10-bit sequence (up to
  1024 IDs per second per instance, 64 instances, until 2094). IDs sort by
  creation time.
- Instance numbers are leased from PostgreSQL with advisory locks when an
  instance starts. Every 5 seconds the instance checks that it still holds
  its number; if not (say, PostgreSQL restarted), it stops making IDs and
  shuts down, and Docker starts it again with a new lease. A number is free
  again as soon as its instance stops or crashes, or about 25 seconds after
  its machine or network vanishes: the app asks PostgreSQL to probe its
  sessions with TCP keepalives.
- Codes are IDs scrambled by a keyed Feistel network (`CODE_SECRET`): unique
  without retries, and impossible to guess from each other without the key.
- `TRUSTED_PROXIES`: the app takes the client's address from
  `X-Forwarded-For` only on requests from these proxies, so behind Nginx each
  client gets its own rate limit, and nobody can choose their own address.
- Database migrations with Alembic (`make migrate`; a one-shot `migrate`
  service runs them in Docker Compose before the instances start). A database
  created by v0.2.0 is adopted as it is. Only one migration run at a time.
- `/health` reports which instance answered.
- CI checks that Nginx spreads requests over all three instances, and that
  stopping one loses no requests.
- Lesson 4 in LEARNING.md: load balancing and distributed IDs.

### Changed

- Generated codes are 8 characters instead of 7.
- The app no longer creates its tables at startup: run the migrations first.
- Link IDs come from the app, not from a database sequence. The migration
  that drops the sequence (0002) stops v0.2.0 instances from creating
  links, so stop every v0.2.0 instance before the migrations run
  (`docker compose down`, which keeps the database's volume).
- In Docker Compose, the app instances can only be reached through Nginx, and
  start again whenever they stop by themselves (`restart: unless-stopped`).
- The app, not uvicorn, decides whom to trust with proxy headers
  (`TRUSTED_PROXIES`). uvicorn keeps idle connections open for 65 seconds,
  longer than Nginx does, so Nginx never reuses one the app is closing.

### Removed

- The `CODE_LENGTH` setting, and random codes with their insert-and-retry
  loop. (Retries remain as a safety net, should the database ever refuse an
  ID.)

## [0.2.0] - 2026-11-18

### Added

- Redis cache in front of PostgreSQL for redirects (cache-aside reads,
  write-through on create, negative caching of unknown codes, TTL with
  jitter). Responses carry an `X-Cache: HIT | MISS | BYPASS` header.
- Distributed rate limiting with a token bucket in Redis (an atomic Lua
  script): 10 link creations and 300 reads per minute per client IP by
  default, answered with `429 Too Many Requests` and `Retry-After`.
- Redis is optional (`REDIS_URL`). When it is down or hangs, the app keeps
  working without cache or limits instead of failing (fail open): each Redis
  call waits at most 0.25 s, a timeout is never retried, and after 3 failures
  in a row a circuit breaker skips Redis for 5 seconds.
- `REDIS_URL` is checked at startup, so a malformed URL stops the app instead
  of failing every request. The error never shows the value or any part of
  it, since it may hold a password.
- Redis in Docker Compose (64 MB, LFU eviction) and in CI.
- Lesson 3 in LEARNING.md: caching, rate limiting and graceful degradation.
- CI pipeline on every pull request: Ruff lint and format, strict mypy, the
  NASA/JPL Power of Ten checker, tests on SQLite and PostgreSQL with a 95%
  coverage gate, a dependency vulnerability audit, and a Docker build with an
  end-to-end smoke test. Runs daily as well.
- CodeQL security analysis of the Python code and the GitHub workflows.
- Release workflow: pushing a `vX.Y.Z` tag publishes a Docker image to the
  GitHub Container Registry, with an SBOM and a signed build provenance
  attestation, and creates a GitHub release from this changelog.
- `scripts/check_power_of_ten.py`: a static checker for the NASA/JPL rules that
  Ruff and mypy do not cover (recursion, unbounded loops, function length,
  widened scope, swallowed errors, dynamic code). Recursion is found with a
  call graph that follows Python's scoping rules, overrides in subclasses,
  and some of the methods Python runs for you: property getters, setters and
  deleters, `__getattribute__`, `__getattr__`, `__setattr__`, `__delattr__`,
  `__call__`, and `__init__` and `__new__` through `cls(...)`. The script's
  docstring lists what it cannot see.
- `HEAD` requests on short links and `/health`. A `HEAD` shows where a link
  goes without counting a click.
- Request bodies over 16 KB are refused with `413 Content Too Large` before
  they are read.
- Pre-commit hooks, Dependabot, a Makefile, and contributor documentation:
  CONTRIBUTING, SECURITY, CODE_OF_CONDUCT, issue and pull request templates,
  CODEOWNERS and the coding standard in `docs/CODING_STANDARD.md`.
- Lesson 2 in LEARNING.md: professional engineering practices.

### Changed

- Redirects reject impossible codes (not 3 to 32 of `A-Z a-z 0-9 _ -`) with
  `404` before touching the cache or the database (only the rate limiter's
  script runs in Redis).
- URLs longer than 2048 characters are rejected with `422`. The length is
  checked on the input as sent, in UTF-8 bytes, and on the normalized URL that
  is stored and redirected to, where an `é` in the path takes 6 characters
  (`%C3%A9`).
- Waits on the database now have timeouts: connecting (5 s), waiting for a
  pooled connection (10 s), each SQL statement (5 s), each lock wait (2 s),
  and a dead network (TCP keepalives and `tcp_user_timeout`). They do not
  cover a server that is alive but stuck (a paused container, a hung pooler
  or proxy): the statement and lock limits need a server that answers, the
  keepalives only notice a dead peer or network, and libpq has no client-side
  read timeout, so a query on an open connection waits until the server
  answers.
- `base62.encode` accepts 0 to 2^64 - 1 and uses a loop with a fixed bound;
  `base62.decode` rejects empty, too long or invalid input with `ValueError`.
- The Docker image runs as a non-root user and has a health check.
- Dependencies, including the tools that the pre-commit hooks run, are
  installed from lock files with hashes (`requirements.txt`,
  `requirements-dev.txt`, generated from `requirements*.in` with `make lock`),
  and the Docker base image is pinned by digest.
- `make check` also runs the dependency audit, and actionlint checks workflow
  shell scripts with a pinned shellcheck, locally as in CI.
- Exceptions renamed to `AliasTakenError` and `CodeSpaceExhaustedError`.

### Fixed

- The database health check in Docker Compose and CI probes over TCP, so the
  app no longer starts while Postgres is still initialising; the app container
  restarts if it fails at startup.
- The large-file check now looks at every file in CI, not only newly staged ones.

## [0.1.0] - 2026-11-17

### Added

- Core API: create short links (random 7-character base62 codes or custom
  aliases), redirect with `302`, optional expiry (`410 Gone`), atomic click
  counting, and a stats endpoint.
- Docker Compose setup with PostgreSQL.
- GitHub Actions CI running the tests on SQLite and PostgreSQL.
- Lesson 1 in LEARNING.md.

[Unreleased]: https://github.com/MITRAKER/url-shortener/compare/v0.5.0...HEAD
[0.5.0]: https://github.com/MITRAKER/url-shortener/compare/v0.4.0...v0.5.0
[0.4.0]: https://github.com/MITRAKER/url-shortener/compare/v0.3.0...v0.4.0
[0.3.0]: https://github.com/MITRAKER/url-shortener/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/MITRAKER/url-shortener/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/MITRAKER/url-shortener/releases/tag/v0.1.0
