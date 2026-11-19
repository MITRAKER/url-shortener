# url-shortener

[![CI](https://github.com/MITRAKER/url-shortener/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/MITRAKER/url-shortener/actions/workflows/ci.yml)
[![CodeQL](https://github.com/MITRAKER/url-shortener/actions/workflows/codeql.yml/badge.svg?branch=main)](https://github.com/MITRAKER/url-shortener/actions/workflows/codeql.yml)
[![Python 3.13](https://img.shields.io/badge/python-3.13-blue?logo=python&logoColor=white)](https://www.python.org/)
[![Ruff](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/ruff/main/assets/badge/v2.json)](https://github.com/astral-sh/ruff)
[![mypy: strict](https://img.shields.io/badge/mypy-strict-blue)](https://mypy-lang.org/)
[![Coverage gate: 95%](https://img.shields.io/badge/coverage%20gate-95%25-brightgreen)](pyproject.toml)
[![NASA/JPL Power of Ten](https://img.shields.io/badge/NASA%2FJPL-Power%20of%20Ten-0b3d91)](docs/CODING_STANDARD.md)
[![pre-commit](https://img.shields.io/badge/pre--commit-enabled-brightgreen?logo=pre-commit)](https://pre-commit.com/)
[![License: MIT](https://img.shields.io/github/license/MITRAKER/url-shortener)](LICENSE)

A URL shortener built step by step as a hands-on project in backend and
distributed-systems design: caching, load balancing, rate limiting, distributed
ID generation, async processing and load testing. It is held to professional
engineering standards, including NASA/JPL's "Power of Ten" rules for
safety-critical code.

> 📚 Every step is explained in [LEARNING.md](LEARNING.md): what was built, why,
> and the concepts behind it, with exercises.

## Roadmap

| Step | What | Status |
|------|------|--------|
| 1 | Core API (FastAPI + PostgreSQL), tests | ✅ |
| – | Quality foundation: CI/CD, strict typing, coverage, security scanning, NASA/JPL rules | ✅ |
| 2 | Redis cache + distributed rate limiting | ✅ |
| 3 | Multiple instances behind Nginx, distributed IDs | ⏳ |
| 4 | Async click analytics (Redis Streams + worker) | ⏳ |
| 5 | Metrics (Prometheus/Grafana), k6 load tests, benchmarks | ⏳ |

## Architecture

```
client ──▶ FastAPI app ──▶ Redis       rate limiting (token bucket) and link cache
                     └───▶ PostgreSQL  source of truth: links and click counts
```

## Run it

With Docker:

```bash
docker compose up --build
```

Without Docker (uses a local SQLite file, and no Redis unless you set `REDIS_URL`):

```bash
python -m venv .venv && source .venv/bin/activate
make install
uvicorn app.main:app --reload
```

Interactive API docs: http://localhost:8000/docs

## API

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/api/links` | Create a link. Body: `{"url": "...", "alias": "optional", "expires_in_days": 30}` |
| `GET`, `HEAD` | `/{code}` | Redirect (302) to the original URL. 404 if unknown, 410 if expired. `HEAD` shows where a link goes without counting a click |
| `GET` | `/api/links/{code}` | Link details and click count |
| `GET`, `HEAD` | `/health` | Health check |

Redirects carry an `X-Cache` header: `HIT` (served from Redis), `MISS` (from
PostgreSQL, now cached) or `BYPASS` (from PostgreSQL, not cached: Redis not
configured, down, or skipped by the circuit breaker, or the cache write did
not go through). Too many requests
from one client get `429 Too Many Requests` with a `Retry-After` header.

```bash
curl -X POST localhost:8000/api/links -H 'content-type: application/json' \
     -d '{"url": "https://github.com", "alias": "github"}'
curl -i localhost:8000/github
```

Limits: URLs up to 2048 characters, counted both as sent (in UTF-8 bytes) and
once normalized (an `é` in the path is stored as `%C3%A9`, 6 characters);
aliases 3 to 32 characters of `A-Z a-z 0-9 _ -`; request bodies up to 16 KB
(`413 Content Too Large` above that).

## Configuration

| Variable | Default | Meaning |
|----------|---------|---------|
| `DATABASE_URL` | `sqlite:///./dev.db` | SQLAlchemy database URL |
| `BASE_URL` | `http://localhost:8000` | Used to build `short_url` in responses |
| `CODE_LENGTH` | `7` | Length of random codes (3 to 32) |
| `REDIS_URL` | *(empty)* | e.g. `redis://localhost:6379/0`. Empty: no cache, no rate limiting. Checked at startup: a malformed URL stops the app |
| `CACHE_TTL_SECONDS` | `3600` | How long a link stays cached (±10% jitter) |
| `CACHE_NEGATIVE_TTL_SECONDS` | `60` | How long "this code does not exist" is cached |
| `RATE_LIMIT_CREATE` | `10/minute` | Link creations per client IP |
| `RATE_LIMIT_READ` | `300/minute` | Redirects and stats per client IP |

## Development

```bash
make install   # dependencies + git pre-commit hook
make check     # lint, format, types, Power of Ten, tests, dependency audit
make help      # all commands
```

Tests run on SQLite and an in-memory fake Redis by default. To use the real ones:
`TEST_DATABASE_URL=postgresql+psycopg://app:app@localhost:5432/shortener_test TEST_REDIS_URL=redis://localhost:6379/15 pytest`.
The tests empty that Redis database (number 15, not the app's 0) and drop and recreate
the app's tables in `TEST_DATABASE_URL`, so never point them at data you want to keep.

## Quality and CI/CD

| Check | Tool | Runs |
|-------|------|------|
| Lint (incl. security rules) and formatting | [Ruff](https://docs.astral.sh/ruff/) | pre-commit, CI |
| Strict static typing | [mypy](https://mypy-lang.org/) `--strict` | pre-commit, CI |
| NASA/JPL Power of Ten | [`scripts/check_power_of_ten.py`](scripts/check_power_of_ten.py) | pre-commit, CI |
| Tests on SQLite + fake Redis and PostgreSQL + Redis, 95% branch coverage gate | pytest, coverage.py | CI |
| Every dependency locked to an exact version and hash; base image pinned by digest | [pip-tools](https://pip-tools.readthedocs.io/), Docker | install, build |
| Known vulnerabilities in dependencies | pip-audit (daily), Dependabot | CI, GitHub |
| Semantic security analysis | CodeQL | CI (weekly too) |
| Workflow and Dockerfile linting | actionlint, hadolint | pre-commit, CI |
| The real Docker stack works end to end | `docker compose` + [`scripts/smoke_test.py`](scripts/smoke_test.py) | CI |
| Release: image on GHCR with SBOM and signed provenance, GitHub release | [`release.yml`](.github/workflows/release.yml) | on `vX.Y.Z` tags |

See [the coding standard](docs/CODING_STANDARD.md), [CONTRIBUTING](CONTRIBUTING.md)
and the [CHANGELOG](CHANGELOG.md).

## Project layout

```
app/        the service (FastAPI routes, business logic, database)
tests/      pytest suite
scripts/    Power of Ten checker, smoke test, release notes
docs/       coding standard
.github/    CI/CD workflows, Dependabot, issue and PR templates
```

## License

[MIT](LICENSE)
