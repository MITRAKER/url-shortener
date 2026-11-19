"""Click events: a redirect only appends one to a Redis stream.

Until step 4, every redirect counted its click with an UPDATE in PostgreSQL:
a transaction, a flush to disk, and a row lock that every click on the same
popular link had to wait for. Now a redirect appends an entry to a Redis
stream (XADD, a fraction of a millisecond), and the click worker
(app/worker.py) adds the clicks up and writes them to PostgreSQL in batches.

When Redis can't take a click (not configured, down, out of memory, or the
stream is full), the click is counted the old way, straight in PostgreSQL:
slower, never lost.
"""

from typing import Final

import redis

from app.redis_client import RedisGateway

# Every click goes to this one stream. "v1" names the format of its entries:
# a new format would get a stream of its own, as the cache's keys do.
STREAM: Final = "clicks:v1"
# The most clicks that may wait in the stream (NASA/JPL rule 3). They share
# Redis's 64 MB with the cache and the rate limits (docker-compose.yml).
# Without a limit, a long worker outage would fill Redis, and a full Redis
# refuses every write: the rate limits would fail open and the cache would
# stop working. A million waiting clicks take about 21 MB (measured on
# Redis 7.4 with 8-character codes; 43 MB if every click were on a
# 32-character alias), which leaves the rest room.
MAX_WAITING_CLICKS: Final = 1_000_000

# Runs inside Redis, atomically: no other command can run between the XLEN
# and the XADD, so the stream never grows past the limit.
APPEND_LUA: Final = """
if redis.call('XLEN', KEYS[1]) >= tonumber(ARGV[2]) then
  return false  -- full: nil for the app, which counts the click itself
end
return redis.call('XADD', KEYS[1], '*', 'code', ARGV[1])
"""


def _append(client: redis.Redis, code: str) -> bool:
    """Run APPEND_LUA. False if the stream is full."""
    entry_id = client.register_script(APPEND_LUA)(keys=[STREAM], args=[code, MAX_WAITING_CLICKS])
    return entry_id is not None


class ClickStream:
    def __init__(self, gateway: RedisGateway) -> None:
        self._gateway = gateway

    def append(self, code: str) -> bool:
        """Add a click on `code` to the stream. False if Redis couldn't take
        it, or if MAX_WAITING_CLICKS clicks already wait. A full stream is an
        answer, not a failure: the circuit breaker doesn't count it."""
        return self._gateway.call("recording a click", lambda r: _append(r, code), fallback=False)
