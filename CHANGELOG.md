# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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
  `404` before touching the cache or the database (only the rate limiter's
  script runs in Redis).
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

[Unreleased]: https://github.com/MITRAKER/url-shortener/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/MITRAKER/url-shortener/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/MITRAKER/url-shortener/releases/tag/v0.1.0
