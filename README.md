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
ID generation, async processing, monitoring and load testing. It is held to professional
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
| 3 | Multiple instances behind Nginx, distributed IDs | ✅ |
| 4 | Async click analytics (Redis Streams + worker) | ✅ |
| 5 | Metrics (Prometheus/Grafana), alerts, k6 load tests, benchmarks | ✅ |

## Architecture

```
                              ┌──▶ app instance 0 ──┐
client ──▶ Nginx (port 8000) ─┼──▶ app instance 1 ──┼──▶ Redis       rate limiting (token bucket), link cache,
           load balancer      └──▶ app instance 2 ──┤                and clicks waiting to be counted (a stream)
                                                    └──▶ PostgreSQL  links, click counts, instance number leases
                                                              ▲
                              click worker ──reads the stream─┘ adds the clicks up, one transaction per batch

Prometheus (port 9090) ── every 15 s ──▶ /metrics of each app instance and of the click worker
     ▲                                    and checks the alerts (each one with a runbook entry)
Grafana (port 3000)                       draws them: one dashboard
```

The instances share nothing but Redis and PostgreSQL, so any of them can
answer any request. Each one makes its own link IDs (Snowflake IDs: time,
instance number and a counter), unique without asking anyone first; a keyed
permutation turns each ID into an 8-character code that can't be guessed.

A redirect doesn't write to PostgreSQL: it appends the click to a Redis
stream, and the click worker counts the clicks in batches, each click in the
stream exactly once. Click counts can lag a moment behind; measured back to
back, redirects went from 583 to 905 per second (16 clients, one popular
link). Around the stream, a click can still be counted more than once: when
Nginx retries a redirect (lesson 4 in LEARNING.md gives the bound), or twice
when the click reached Redis but the reply didn't. And a crash of the whole
machine can lose up to about a second of clicks (LEARNING.md, lesson 5).

## Run it

With Docker (Nginx, three app instances, the click worker, PostgreSQL, Redis,
Prometheus and Grafana; the database migrations run first, once):

```bash
docker compose up --build
for i in 1 2 3 4 5 6; do curl -s localhost:8000/health; echo; done   # watch Nginx take turns
make loadtest            # 200 redirects a second for a minute, with k6: watch the dashboard
```

| What | Where |
|------|-------|
| The service | <http://localhost:8000>, API docs at <http://localhost:8000/docs> |
| The dashboard (Grafana) | <http://localhost:3000>: anyone may look; to edit, log in as `admin`, password `admin` (or `GRAFANA_PASSWORD`) |
| Prometheus: targets, alerts, queries | <http://localhost:9090> |

Prometheus and Grafana listen on this machine only (127.0.0.1).

Without Docker (uses a local SQLite file, and no Redis unless you set `REDIS_URL`):

```bash
python -m venv .venv && source .venv/bin/activate
make install
make migrate                 # create the tables
uvicorn app.main:app --reload
make worker                  # only with REDIS_URL set: counts the clicks waiting in Redis
```

Without Redis, each click is counted in the database straight away.

Interactive API docs: http://localhost:8000/docs

## API

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/api/links` | Create a link. Body: `{"url": "...", "alias": "optional", "expires_in_days": 30}` |
| `GET`, `HEAD` | `/{code}` | Redirect (302) to the original URL. 404 if unknown, 410 if expired. `HEAD` shows where a link goes without counting a click |
| `GET` | `/api/links/{code}` | Link details and click count (counted by the click worker, so it can lag a moment behind) |
| `GET`, `HEAD` | `/health` | Health check, and which instance answered: `{"status": "ok", "instance": 0}` |

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

## Monitoring

![The traffic panels of the Grafana dashboard, during a load test of 300 redirects a second](docs/images/dashboard.png)

- **Metrics.** Each app instance serves `/metrics` to Prometheus, and the click
  worker serves its own on port 8001: requests by route and status, latency
  histograms, cache hits, clicks, rate limits, Redis fallbacks, the circuit
  breaker, the database connection pool and the clicks waiting to be counted.
  Nginx refuses `/metrics` from outside.
- **Alerts.** Two objectives for redirects, over 30 days: 99.9% without a
  server error, and 99% within 100 ms. Burn-rate alerts page someone when
  errors would spend the month's error budget fast, and open a ticket when
  they spend it slowly; others watch the instances, the click worker, Redis
  and the connection pool. Each alert has its entry in the
  [runbook](docs/RUNBOOK.md), and a test.
- **Load test.** [`loadtest/redirects.js`](loadtest/redirects.js), with
  [k6](https://k6.io/): requests arrive at a fixed rate whatever the answers'
  speed (an *open* model, as with real users), popular links are clicked more
  (Zipf's law), and the objectives are its pass/fail thresholds.
  `make loadtest RATE=500 DURATION=2m`, or on GitHub: Actions, Load test, Run
  workflow.
- **Benchmarks.** Every step of the roadmap, measured with the same load test,
  on the same three cores. Step 1 answers about 250 redirects a second. Step 5
  keeps up at 300 a second, and at 600 in one run of two (the other left 2 of
  18,000 requests unsent). At 900 it still answers about 880 a second, but
  falls behind: its p99 rises to 248-321 ms, past the 100 ms objective. What
  each step bought, and why: [docs/BENCHMARKS.md](docs/BENCHMARKS.md).

## Configuration

| Variable | Default | Meaning |
|----------|---------|---------|
| `DATABASE_URL` | `sqlite:///./dev.db` | SQLAlchemy database URL |
| `BASE_URL` | `http://localhost:8000` | Used to build `short_url` in responses |
| `CODE_SECRET` | *(a public default)* | Key that scrambles IDs into codes. At least 16 characters, the same on every instance. Set your own in production: the app logs a warning while the default is in use |
| `TRUSTED_PROXIES` | *(empty)* | IP addresses or networks of your proxies, comma-separated, e.g. `10.89.0.10` or `10.89.0.0/24`. Only requests from these may name the client's address in `X-Forwarded-For`. Empty: trust nobody. Checked at startup: anything else, such as `*` or `10.89.0.10/24`, stops the app |
| `REDIS_URL` | *(empty)* | e.g. `redis://localhost:6379/0`. Empty: no cache, no rate limiting, and clicks counted in the database at once. The click worker needs it. Checked at startup: a malformed URL stops the app |
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
The tests empty that Redis database (number 15, not the app's 0), and they drop every
table in `TEST_DATABASE_URL`, not only the app's, before creating the app's again. Give
them a database of their own, and never point them at data you want to keep.
Some tests (instance number leases, the migration lock) need PostgreSQL and are
skipped on SQLite.

The database schema changes only through migrations in `app/migrations/`
([Alembic](https://alembic.sqlalchemy.org/)). After changing `app/models.py`:

```bash
alembic revision --autogenerate -m "add a column for ..."   # then read and fix the new file
make migrate
```

A test fails if the models and the migrations ever disagree.

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
| Nginx spreads requests over all instances; stopping one loses no requests | [`scripts/check_instances.py`](scripts/check_instances.py) | CI |
| Clicks wait while the worker is stopped, and survive a Redis restart | [`scripts/check_clicks.py`](scripts/check_clicks.py) | CI |
| Prometheus's configuration is valid; every alert fires when it should (unit tests) | promtool: `make monitoring-check` | CI |
| Prometheus collects every instance and the worker, every rule works, every dashboard query runs | [`scripts/check_metrics.py`](scripts/check_metrics.py) | CI |
| Load test, with the objectives as thresholds | [k6](https://k6.io/): [`loadtest/redirects.js`](loadtest/redirects.js) | CI (10 s), [on demand](.github/workflows/loadtest.yml) |
| Release: image on GHCR with SBOM and signed provenance, GitHub release | [`release.yml`](.github/workflows/release.yml) | on `vX.Y.Z` tags |

See [the coding standard](docs/CODING_STANDARD.md), [CONTRIBUTING](CONTRIBUTING.md)
and the [CHANGELOG](CHANGELOG.md).

## Project layout

```
app/        the service (FastAPI routes, business logic, database, IDs) and the click worker
app/migrations/  database migrations (Alembic)
nginx/      the load balancer's configuration
monitoring/ Prometheus (what to collect, alerts and their tests) and Grafana (data source, dashboard)
loadtest/   the k6 load test
tests/      pytest suite
scripts/    Power of Ten checker, smoke test, instance, click and metrics checks, release notes
docs/       coding standard, runbook, benchmarks
.github/    CI/CD workflows, Dependabot, issue and PR templates
```

## License

[MIT](LICENSE)
