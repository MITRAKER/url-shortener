# Learning notes

This file grows with every step of the project. Each lesson explains **what**
was built, **why** it was designed that way, the **key concepts**, and ends
with **exercises** so you can practise building it yourself.

- [Lesson 1: The core service](#lesson-1-the-core-service)

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
if not exists(code):   # request A and B both see "free"...
    insert(code)       # ...and both insert → duplicate!
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
link.clicks = link.clicks + 1   # read 5 ... another request also reads 5
session.commit()                # both write 6 → one click lost
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

**Next:** Lesson 2 adds a Redis cache in front of the database and rate
limiting that works across servers.
