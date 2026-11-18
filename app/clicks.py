"""Click events: a redirect only appends one to a Redis stream.

Until step 4, every redirect counted its click with an UPDATE in PostgreSQL:
a transaction, a flush to disk, and a row lock that every click on the same
popular link had to wait for. Now a redirect appends an entry to a Redis
stream (XADD, a fraction of a millisecond), and the click worker
(app/worker.py) adds the clicks up and writes them to PostgreSQL in batches.

When Redis can't take a click (not configured, down, or out of memory), the
click is counted the old way, straight in PostgreSQL: slower, never lost.
"""

from typing import Final

from app.redis_client import RedisGateway

# Every click goes to this one stream. "v1" names the format of its entries:
# a new format would get a stream of its own, as the cache's keys do.
STREAM: Final = "clicks:v1"


class ClickStream:
    def __init__(self, gateway: RedisGateway) -> None:
        self._gateway = gateway

    def append(self, code: str) -> bool:
        """Add a click on `code` to the stream. False if Redis couldn't take it."""
        return self._gateway.call(
            "recording a click", lambda r: bool(r.xadd(STREAM, {"code": code})), fallback=False
        )
