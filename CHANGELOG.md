# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.4.0] - 2026-11-18

### Added

- Clicks are counted off the redirect path. A redirect appends the click to
  a Redis stream (`clicks:v1`) instead of updating PostgreSQL, and a new
  click worker (`python -m app.worker`, the `worker` service in Docker
  Compose, `make worker`) adds the clicks up per link and writes them in
  batches of up to 1000.
- Each click is counted exactly once. The worker stores how far it got (a
  checkpoint, in the new `click_checkpoints` table, migration `0003`) in the
  same transaction as the counts, and moves it with a compare-and-set, so a
  crash at any moment, or a second worker, can't count a click twice or
  lose one. Counted clicks are then removed from the stream.
- When Redis can't take a click (not configured, down, or full), it is
  counted in PostgreSQL at once, as before.
- A health check for the worker: it touches a heartbeat file on every round,
  and `python -m app.heartbeat` checks that the last one is recent.
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
  of raising an error, so a stream entry holding such bytes is skipped as
  junk instead of stopping the worker.

## [0.3.0] - 2026-11-18

### Added

- Three app instances behind an Nginx load balancer (round robin) in Docker
  Compose. Nginx finds the instances through Docker's DNS and looks again
  every 10 seconds. When an instance can't be reached, Nginx tries another
  (2 tries at most), and leaves it out for 10 seconds after 3 failures. A
  `GET` or `HEAD` that finds no instance, because the list of instances
  changed while it waited, is asked once more. Nginx keeps connections to
  the instances open, and refuses request bodies over 16 KB.
- Snowflake IDs, made by each instance without asking anyone: 31 bits of
  seconds since 2026, a 6-bit instance number and a 10-bit sequence (up to
  1024 IDs per second per instance, 64 instances, until 2094). IDs sort by
  creation time.
- Instance numbers are leased from PostgreSQL with advisory locks when an
  instance starts. Every 5 seconds the instance checks that it still holds
  its number; if not (say, PostgreSQL restarted), it stops making IDs and
  shuts down, and Docker starts it again with a new lease.
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
- Link IDs come from the app, not from a database sequence.
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
  of failing every request. The error never repeats the value, which may
  hold a password.
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
  call graph that follows Python's scoping rules.
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
  `404` before touching Redis or the database.
- URLs longer than 2048 characters are rejected with `422`.
- Every wait on the database now has a timeout: connecting (5 s), waiting
  for a pooled connection (10 s), each SQL statement (5 s), each lock wait
  (2 s), and a dead network (TCP keepalives and `tcp_user_timeout`).
- `base62.encode` accepts 0 to 2^64 - 1 and uses a loop with a fixed bound;
  `base62.decode` rejects empty, too long or invalid input with `ValueError`.
- The Docker image runs as a non-root user and has a health check.
- Dependencies are installed from lock files with hashes (`requirements.txt`,
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

[Unreleased]: https://github.com/MITRAKER/url-shortener/compare/v0.4.0...HEAD
[0.4.0]: https://github.com/MITRAKER/url-shortener/compare/v0.3.0...v0.4.0
[0.3.0]: https://github.com/MITRAKER/url-shortener/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/MITRAKER/url-shortener/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/MITRAKER/url-shortener/releases/tag/v0.1.0
