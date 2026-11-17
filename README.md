# url-shortener

A URL shortener built step by step as a hands-on project in backend and
distributed-systems design: caching, load balancing, rate limiting, distributed
ID generation, async processing and load testing.

> 📚 Every step is explained in [LEARNING.md](LEARNING.md): what was built, why,
> and the concepts behind it, with exercises.

## Roadmap

| Step | What | Status |
|------|------|--------|
| 1 | Core API (FastAPI + PostgreSQL), tests, CI | ✅ |
| 2 | Redis read-through cache + distributed rate limiting | ⏳ |
| 3 | Multiple instances behind Nginx, distributed IDs | ⏳ |
| 4 | Async click analytics (Redis Streams + worker) | ⏳ |
| 5 | Metrics (Prometheus/Grafana), k6 load tests, benchmarks | ⏳ |

## Run it

With Docker:

```bash
docker compose up --build
```

Without Docker (uses a local SQLite file):

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
uvicorn app.main:app --reload
```

Interactive API docs: http://localhost:8000/docs

## API

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/api/links` | Create a link. Body: `{"url": "...", "alias": "optional", "expires_in_days": 30}` |
| `GET` | `/{code}` | Redirect (302) to the original URL. 404 if unknown, 410 if expired |
| `GET` | `/api/links/{code}` | Link details and click count |
| `GET` | `/health` | Health check |

```bash
curl -X POST localhost:8000/api/links -H 'content-type: application/json' \
     -d '{"url": "https://github.com", "alias": "gh"}'
curl -i localhost:8000/gh
```

## Tests

```bash
pytest                       # SQLite
TEST_DATABASE_URL=postgresql+psycopg://app:app@localhost:5432/shortener_test pytest   # Postgres
```

CI runs both on every pull request.
