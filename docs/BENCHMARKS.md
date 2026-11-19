# Benchmarks

Every step of the [roadmap](../README.md#roadmap), measured with the same load
test, on the same machine, one after the other. What did each step buy, and
what did it cost?

| Step | What changed on the redirect path |
|------|-----------------------------------|
| 1 | One app instance. Each redirect reads the link from PostgreSQL and counts the click with an `UPDATE`, in its own transaction |
| 2 | The link comes from Redis (a cache), not PostgreSQL. The click is still an `UPDATE` |
| 3 | Three instances behind Nginx. Still one `UPDATE` per click |
| 4 | The click goes to a Redis stream; a worker counts the clicks in batches. No PostgreSQL on a cache hit |
| 5 | Metrics on every request; Prometheus and Grafana running; the worker waits 0.2 s between batches that aren't full |

## How we measured

- **The load**: [`loadtest/redirects.js`](../loadtest/redirects.js), the
  load test of step 5, run against each step: 100 links created first, then
  redirects at a fixed rate (an *open* model: requests keep arriving however
  slowly the answers come), popular links clicked more (Zipf's law: the most
  popular of the 100 gets 19% of the clicks), and 5% of requests for codes
  that don't exist.
- **The rates**: a 15-second warm-up at 100 requests a second, then 30
  seconds each at 100, 300, 600 and 900.
- **Each step from its own branch**: its `docker-compose.yml` and its image,
  started fresh, with an empty database. Rate limits raised (k6 sends
  everything from one address).
- **The machine**: 4 cores, everything in Docker. The system under test
  shares cores 0 to 2 (app instances, Nginx, PostgreSQL, Redis, the worker,
  and in step 5 Prometheus and Grafana); k6 has core 3 to itself, so that the
  load generator doesn't steal CPU from what it measures.
- **Measured by k6, from outside**: through Nginx from step 3 on (steps 1 and
  2 have none, so k6 calls the app directly). Latency is for redirects only.
  "Answered/s" is the requests answered per second, "not sent" the requests
  k6 couldn't start on time because all its virtual users (up to 900) were
  still waiting for answers: the server had fallen behind.

To run it yourself, see [Reproducing](#reproducing) below.

## Results

Two complete runs, one after the other. Where they differ, a cell shows
both, as *run 1 / run 2*. Latency is for redirects, seen by k6.

**100 requests a second asked**

| Step | Answered/s | p50 | p90 | p99 | Not sent |
|------|-----------:|----:|----:|----:|---------:|
| 1 | 98 | 5.2 ms / 5.3 ms | 6.3 ms / 6.4 ms | 13 ms / 43 ms | 0 / 3 |
| 2 | 98 | 5.2 ms / 5.3 ms | 6.3 ms / 6.6 ms | 9.0 ms / 9.3 ms | 0 |
| 3 | 98 | 6.0 ms / 6.3 ms | 7.3 ms / 7.6 ms | 74 ms / 11 ms | 12 / 0 |
| 4 | 98 | 3.6 ms / 3.7 ms | 4.4 ms / 4.6 ms | 6.6 ms / 6.9 ms | 0 |
| 5 | 98 | 3.5 ms / 3.7 ms | 4.3 ms / 4.7 ms | 6.7 ms / 6.8 ms | 0 |

**300 requests a second asked**

| Step | Answered/s | p50 | p90 | p99 | Not sent |
|------|-----------:|----:|----:|----:|---------:|
| 1 | 260 / 249 | 928 ms / 1.1 s | 1.2 s | 1.3 s | 828 / 1,141 |
| 2 | 257 / 236 | 1.1 s / 1.2 s | 1.2 s / 1.3 s | 1.2 s / 1.7 s | 928 / 1,557 |
| 3 | 291 / 295 | 4.9 ms / 5.0 ms | 12 ms / 6.8 ms | 681 ms / 24 ms | 109 / 0 |
| 4 | 295 / 294 | 2.9 ms | 3.8 ms | 9.6 ms / 7.7 ms | 0 |
| 5 | 295 / 294 | 2.9 ms / 3.1 ms | 3.8 ms / 3.9 ms | 7.8 ms / 7.1 ms | 0 |

**600 requests a second asked**

| Step | Answered/s | p50 | p90 | p99 | Not sent |
|------|-----------:|----:|----:|----:|---------:|
| 1 | 265 / 257 | 2.2 s / 2.3 s | 2.3 s / 2.5 s | 4.9 s / 4.8 s | 9,398 / 9,681 |
| 2 | 251 / 209 | 2.3 s / 2.5 s | 2.4 s / 3.8 s | 2.5 s / 4.8 s | 9,871 / 10,761 |
| 3 | 560 / 532 | 477 ms / 671 ms | 882 ms / 1.3 s | 2.1 s / 2.5 s | 584 / 1,145 |
| 4 | 589 / 588 | 3.6 ms / 3.9 ms | 11 ms | 140 ms / 51 ms | 35 / 10 |
| 5 | 588 / 589 | 3.0 ms / 3.1 ms | 5.7 ms / 5.8 ms | 42 ms / 30 ms | 2 / 0 |

**900 requests a second asked**

| Step | Answered/s | p50 | p90 | p99 | Not sent |
|------|-----------:|----:|----:|----:|---------:|
| 1 | 260 / 254 | 3.4 s / 3.5 s | 3.6 s / 3.7 s | 10.3 s / 10.6 s | 18,341 / 18,519 |
| 2 | 244 / 152 | 3.6 s / 4.8 s | 3.8 s / 8.5 s | 5.3 s / 9.2 s | 18,841 / 21,052 |
| 3 | 536 / 516 | 1.4 s / 1.5 s | 2.1 s / 2.3 s | 3.2 s / 3.5 s | 9,964 / 10,243 |
| 4 | 880 / 879 | 22 ms | 108 ms / 90 ms | 256 ms / 237 ms | 101 / 89 |
| 5 | 881 / 876 | 24 ms / 15 ms | 96 ms / 87 ms | 248 ms / 321 ms | 61 / 145 |

**CPU**, in % of one core (three cores: 300%), averaged over each run of
run 2 from the containers' own counters. The app column adds up all its
instances.

| Step | Rate | app | nginx | db | redis | worker | prometheus | grafana | Total |
|------|-----:|-----:|-----:|-----:|-----:|-----:|-----:|-----:|------:|
| 1 | 100 | 34 |  | 10 |  |  |  |  | 44 |
| 1 | 300 | 120 |  | 57 |  |  |  |  | 177 |
| 1 | 600 | 118 |  | 52 |  |  |  |  | 170 |
| 1 | 900 | 118 |  | 51 |  |  |  |  | 168 |
| 2 | 100 | 38 |  | 8 | 4 |  |  |  | 49 |
| 2 | 300 | 113 |  | 28 | 5 |  |  |  | 146 |
| 2 | 600 | 105 |  | 36 | 4 |  |  |  | 146 |
| 2 | 900 | 76 |  | 21 | 4 |  |  |  | 100 |
| 3 | 100 | 49 | 3 | 7 | 4 |  |  |  | 64 |
| 3 | 300 | 105 | 7 | 16 | 7 |  |  |  | 135 |
| 3 | 600 | 186 | 8 | 50 | 6 |  |  |  | 251 |
| 3 | 900 | 191 | 8 | 49 | 6 |  |  |  | 254 |
| 4 | 100 | 36 | 3 | 11 | 6 | 25 |  |  | 81 |
| 4 | 300 | 75 | 6 | 23 | 10 | 51 |  |  | 165 |
| 4 | 600 | 131 | 10 | 22 | 14 | 42 |  |  | 219 |
| 4 | 900 | 173 | 13 | 17 | 15 | 32 |  |  | 249 |
| 5 | 100 | 37 | 3 | 3 | 5 | 4 | 2 | 2 | 55 |
| 5 | 300 | 78 | 7 | 3 | 9 | 4 | 2 | 1 | 104 |
| 5 | 600 | 138 | 11 | 4 | 14 | 4 | 1 | 1 | 173 |
| 5 | 900 | 190 | 13 | 4 | 14 | 4 | 1 | 2 | 229 |

## What the numbers say

### One process is a ceiling (steps 1 and 2)

With one instance, about 250 redirects a second get answered, however many
arrive. Past that, requests queue: at 300 a second the median redirect
waited over a second, and k6 couldn't even start about a thousand of them on
time. The app's single process was the limit. It used about 1.2 cores
(Python runs Python code on one core at a time, because of its global
interpreter lock; the rest is C code, such as the database driver, running
outside it), close to 5 ms of CPU per redirect.

### The cache (step 2) halves PostgreSQL's work, not the app's

At 300 requests a second, PostgreSQL's CPU fell from 57% of a core to 28%:
lookups now come from Redis, and only the click's `UPDATE` still goes to the
database. But the app's CPU per redirect didn't change (from Python, asking
Redis costs about as much as asking PostgreSQL), so the ceiling didn't move
either. A cache helps what it shields; it can't help what is actually full.
Find where the time goes before you optimize.

Overloaded, step 2 did worse in run 2 (152 answered a second at 900 asked,
against 244), with its process using only 76% of a core: something other
than the CPU held it back. An overloaded system isn't just slower; it is
less predictable.

### Three instances (step 3): twice the throughput, until the hot rows

Three processes answered 516 to 560 redirects a second, about twice step 2
(not three times: they share three cores with PostgreSQL, Redis and Nginx).
Below that they kept up easily. Above it, PostgreSQL's row locks became the
problem: every redirect still ran `UPDATE links SET clicks = clicks + 1` in
its own transaction, and a popular link's row can only be updated by one
transaction at a time. We asked PostgreSQL to log every lock wait over 10 ms
(`log_lock_waits = on`, `deadlock_timeout = '10ms'`) during a 30-second run:
at 300 requests a second it logged 10 waits; at 600, 29,037. That is
lesson 5's hot row, measured.

### Clicks off the request path (step 4): the hot rows are gone

Same three instances, same Nginx. On the redirect path only the click
changed: an `XADD` to a Redis stream instead of an `UPDATE`. At 900 requests
a second asked, step 4 answered 880 with a median of 22 ms, where step 3
managed about 520 with a median of 1.5 s. The app's CPU per redirect fell
from about 3.5 ms to 2 ms, and PostgreSQL only sees the click worker's
batches.

### Observability (step 5): almost free, and it paid for itself

Metrics on every request cost the app 3 to 10% more CPU than step 4 (0.1 to
0.2 ms per redirect); Prometheus and Grafana together used about 3% of a
core. The fix the metrics led to, the worker's linger
([lesson 6](../LEARNING.md#part-5-what-the-numbers-showed)), took the worker
from 25-51% of a core to 4%, and PostgreSQL from 11-23% to 3-4%. That CPU
went back to the requests: at 600 a second, step 5's p99 was 30 to 42 ms,
against 51 to 140 ms for step 4.

### Where the limit is now

At 900 a second, steps 4 and 5 just keep up: k6 couldn't start 61 to 145 of
27,000 requests on time, and the median rose to 15-24 ms, with the three
cores 76 to 83% busy. The next limit is plain CPU: the app needs about 2 ms
of it per redirect, plus what Nginx and Redis use. More instances wouldn't
help on this machine; more cores, or less work per request, would.

### The p99 of a short run is noisy

Both runs agree on the medians within half a millisecond at 100 a second,
but not always on the p99: step 1's was 13 ms in one run and 43 ms in the
other, step 3's 74 ms and 11 ms. Over 30 seconds at 100 a second, the p99
rests on the 30 slowest requests, and a single hiccup (a slow disk flush, a
busy neighbour on the same physical machine) moves it. Compare p99s over
long runs, or many runs, never a single short one.

## Caveats

- **One machine, shared.** The system under test and its database share three
  cores, as on a small server. Differences of 10% or so between two runs are
  noise; the differences that matter here are ten times larger.
- **Short runs.** 30 seconds per rate shows the steady state, not what takes
  longer to appear: cache entries expiring, memory growth, PostgreSQL's
  autovacuum.
- **CPU** figures come from the containers' own CPU counters (cgroups),
  read before and after each run of run 2. They include Docker's health
  checks: a Python health check costs about a quarter of a second of CPU,
  every 10 seconds, in each app container.
- **Not a production setup.** One machine means no network between the
  services, and the numbers would differ on real servers. What carries over
  is the shape: where each step's limit is, and why.

## Reproducing

The current step: `make loadtest RATE=300 DURATION=30s`.

An older step: start its stack from its branch, with the rate limits raised
by step 5's `loadtest/compose.yml` (it only sets two variables of the `app`
service, so it fits every step), then run step 5's load test against it.
From a checkout of step 5, for step 3:

```bash
git worktree add ../step3 step-3-load-balancing
docker compose -f ../step3/docker-compose.yml -f loadtest/compose.yml up --build --detach --wait
docker run --rm --network step3_default -v "$PWD/loadtest:/scripts:ro" grafana/k6:2.4.0 \
    run -e BASE_URL=http://nginx:8080 -e RATE=300 -e DURATION=30s /scripts/redirects.js
docker compose -f ../step3/docker-compose.yml down --volumes
```

Steps 1 and 2 have no Nginx: use `BASE_URL=http://app:8000`. Step 1's app has
no health check, so give it a few seconds after `up` before starting k6. To
keep k6 off the cores of the system under test, as we did, add
`--cpuset-cpus 3` to `docker run`, and `cpuset: "0-2"` to each service with an
override file.
