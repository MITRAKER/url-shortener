# Runbook

What to do when an alert fires. Every alert in
[`monitoring/prometheus/alerts.yml`](../monitoring/prometheus/alerts.yml)
links to its section here.

- **page**: users are hurt now. Act at once, day or night.
- **ticket**: something is wrong, but users are fine so far, or only a few
  are hurt. Look at it during working hours.

Where to look, on the machine running the stack:

| What | Where |
|------|-------|
| The dashboard | <http://localhost:3000> |
| Alerts, targets, queries | <http://localhost:9090/alerts>, <http://localhost:9090/targets> |
| Containers and their health | `docker compose ps` |
| Logs | `docker compose logs --tail 100 <service>` (app, worker, nginx, db, redis) |

First question, always: **what changed?** A deployment, a configuration
change, a traffic spike. Most incidents start with a change.

## Redirects

### RedirectErrorBudgetBurningFast

**Severity: page.** Over the last hour, and still in the last five minutes,
more than 1.44% of redirects failed with a server error (5xx). At that pace,
the month's error budget (0.1% of redirects) is gone in about two days.

1. Dashboard, "Responses per second, by status": which 5xx? A 500 is a bug
   or an unexpected error in the app; a 503 means the app refused on purpose
   (it could not allocate a code, for example).
2. `docker compose logs --tail 200 app`: the errors and their tracebacks.
3. If it began with a deployment, roll it back first and investigate after.
4. Is PostgreSQL down or slow? A redirect needs it on every cache miss.
   `docker compose ps db`, and "Database connections in use".

### RedirectErrorBudgetBurningSlowly

**Severity: ticket.** Over the last six hours, more than 0.6% of redirects
failed. Not an emergency, but the budget will be spent within the month at
this pace. Same checks as above, with more time: look for a pattern (one
instance, one time of day, one kind of link).

### RedirectsSlow

**Severity: ticket.** Fewer than 99% of redirects took 100 ms or less over
the last 10 minutes (measured inside the app, so Nginx and the network add
to it).

1. "Cache hit ratio": a drop means more redirects go to PostgreSQL. Did Redis
   restart and lose the cache? It refills by itself as links are clicked.
2. "Requests in progress" above 40 on an instance: requests are queueing for
   a thread. "CPU, by process" near 100% of a core: the instance is saturated.
   More instances (`deploy.replicas` in `docker-compose.yml`) spread the load.
3. "Database connections in use" at the limit: requests wait for a connection
   (see [DatabasePoolSaturated](#databasepoolsaturated)).

## Instances

### AppDown

**Severity: page.** Prometheus reaches no app instance at all: every
redirect fails at Nginx with a 502. The app can't count its own errors when
it is down, which is why this alert counts instances instead.

1. `docker compose ps app`: are the containers running, restarting, unhealthy?
2. `docker compose logs --tail 100 app`: the last words before they stopped.
   An instance that can't reach PostgreSQL at startup exits, and Docker
   starts it again: check the database first.
3. `docker compose up -d app` starts them again if they were stopped.

### AppInstancesMissing

**Severity: ticket.** Fewer than 3 instances have answered Prometheus for 5
minutes. The others carry the load, so users are fine as long as they keep up.

1. `docker compose ps app`, and the logs of the missing one.
2. An instance that lost its instance number (PostgreSQL restarted, say)
   stops on purpose and is started again (`app/instances.py`); it should be
   back within a minute. One that keeps restarting needs its logs read.

## Clicks

### ClickWorkerDown

**Severity: ticket.** Prometheus can't reach the click worker. Clicks keep
arriving in the Redis stream and wait there, safely; their counts lag until
the worker is back. Nothing is lost while Redis has memory to spare.

1. `docker compose ps worker`, then `docker compose logs --tail 100 worker`.
2. `docker compose up -d worker`. It carries on from its checkpoint in
   PostgreSQL, and counts the backlog within seconds.

### ClicksWaitingTooLong

**Severity: ticket.** The oldest click in the stream has waited over a minute
for 5 minutes: the worker is running but not counting.

1. The worker's logs: "counting clicks failed" means PostgreSQL or Redis
   keeps failing; the worker retries every second, by design.
2. "Click worker: batches and errors": errors by system tell which one.
3. Batches of 1,000 back to back mean a backlog it is still working through;
   the age should then be falling.

### ClicksBypassingTheStream

**Severity: ticket.** Redis is not taking clicks, so redirects count them in
PostgreSQL directly: slower redirects, and more load on the database. Nothing
is lost.

1. Is Redis up? `docker compose ps redis`.
2. Is it full? `docker compose exec redis redis-cli info memory`: when
   `used_memory` reaches `maxmemory` with nothing left to evict, Redis refuses
   new clicks. A long worker outage fills it with waiting clicks: bring the
   worker back (see [ClickWorkerDown](#clickworkerdown)).

## Dependencies

### RedisCircuitOpen

**Severity: ticket.** An instance stopped calling Redis after repeated
failures (`app/breaker.py`). Redirects still work, from PostgreSQL, without
the cache, rate limits or the click stream. The breaker tries again every 5
seconds and closes by itself once Redis answers.

1. `docker compose ps redis`, `docker compose logs --tail 50 redis`.
2. Slow rather than down? Every Redis call has 250 ms to answer
   (`app/redis_client.py`). `redis-cli --latency` shows how long it takes.

### DatabasePoolSaturated

**Severity: ticket.** An instance had over 80% of its 30 database connections
lent out for 5 minutes. Once all are taken, requests wait up to 10 seconds
for one, then fail.

1. Are queries slow? Look for long-running ones:
   `docker compose exec db psql -U app -d shortener -c "SELECT pid, now() - query_start AS running, left(query, 60) FROM pg_stat_activity WHERE state = 'active' ORDER BY running DESC"`.
2. Is the cache working? A low hit ratio sends more redirects to PostgreSQL.
3. Has traffic grown? More instances means more connections in total: check
   that PostgreSQL's `max_connections` (100 by default) still covers them all.
