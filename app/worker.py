"""The click worker: counts the clicks waiting in the Redis stream into
PostgreSQL, a batch at a time. Run it with `python -m app.worker`.

Each click is counted exactly once, without a distributed transaction. The
worker keeps a *checkpoint* in PostgreSQL: the ID of the last stream entry it
has counted. It moves the checkpoint in the same transaction that adds the
counts, so either both happen or neither does. After a crash it carries on
from where the database says, and no click is counted twice or lost. (Kafka
consumers that store their offsets together with their results do the same.)

Moving the checkpoint is a compare-and-set: "from where I started reading, to
the end of my batch". If another worker moved it first, the update changes no
row, and this worker throws its batch away. So a second worker can never
count a click twice; it only wastes work. One worker is plenty: a batch of a
thousand clicks takes one transaction.

The worker serves its metrics on port 8001 for Prometheus (app/metrics.py):
how many batches and clicks it counted, its errors, and the clicks waiting.
"""

import logging
import signal
import sys
import threading
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final
from wsgiref.simple_server import WSGIServer

import redis
from prometheus_client import CollectorRegistry, start_http_server
from sqlalchemy import bindparam, select, update
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from app import metrics
from app.clicks import STREAM
from app.config import settings
from app.db import SessionLocal
from app.heartbeat import HEARTBEAT
from app.models import ClickCheckpoint, Link
from app.redis_client import make_redis
from app.service import CODE_PATTERN

log = logging.getLogger(__name__)

BATCH_SIZE: Final = 1000  # stream entries per transaction, at most
WAIT_MS: Final = 1000  # how long one read waits for new clicks to arrive
SOCKET_MARGIN_SECONDS: Final = 1.0  # the client waits that much longer for Redis's answer
RETRY_SECONDS: Final = 1.0  # after an error, wait this long before trying again
# After a batch smaller than the batch size, wait this long before reading
# again: the clicks that arrive meanwhile make the next batch bigger. Without
# it, a read returns as soon as there is one click, and at 300 clicks a second
# the worker committed 260 transactions a second, one or two clicks each (the
# metrics showed it, LEARNING.md lesson 6). A full batch means a backlog: no
# waiting then. Kafka calls this wait "linger".
LINGER_SECONDS: Final = 0.2
START: Final = "0-0"  # comes before every stream ID: "nothing counted yet"
METRICS_PORT: Final = 8001  # where Prometheus reads the worker's metrics

# One statement, run once per link in the batch (an "executemany"). Clicks
# are added (clicks = clicks + n), never set: an UPDATE from the app, counting
# a click while Redis is down, can't be lost in between.
ADD_CLICKS: Final = (
    update(Link)
    .where(Link.code == bindparam("for_code"))
    .values(clicks=Link.clicks + bindparam("added"))
)


def parse_entries(reply: object) -> list[tuple[str, str | None]]:
    """(entry ID, code) pairs from an XREAD reply for our one stream.

    The reply looks like [[stream, [(id, {"code": code}), ...]]], or is empty
    when no click arrived. A reply of any other shape is an error, not "no
    clicks": a worker that quietly counted nothing, forever, would look
    perfectly healthy. (RESP3, a newer Redis protocol, shapes the reply
    differently; make_redis asks for RESP2.)
    """
    if not reply:
        return []  # [] or None: no new clicks
    match reply:
        case [[_, list() as entries]]:
            return [parse_entry(entry) for entry in entries]  # at most a batch
        case _:
            raise redis.ResponseError(f"unexpected reply to XREAD: {reply!r:.200}")


def parse_entry(entry: object) -> tuple[str, str | None]:
    """(entry ID, code) for one entry. Entries come from outside, so they are
    checked, not trusted: one without a valid code gets None, and is skipped."""
    match entry:
        case (str() as entry_id, {"code": str() as code}) if CODE_PATTERN.fullmatch(code):
            return entry_id, code
        case (str() as entry_id, _):
            return entry_id, None
        case _:
            raise redis.ResponseError(f"unexpected stream entry: {entry!r:.200}")


def following(entry_id: str) -> str:
    """The first possible stream ID after `entry_id`. A stream ID is a time in
    milliseconds and a sequence number: "1795000000000-4" gives "1795000000000-5"."""
    millis, _, sequence = entry_id.partition("-")
    return f"{millis}-{int(sequence) + 1}"


@dataclass(frozen=True)
class Batch:
    """Clicks read from the stream, not counted yet."""

    after: str  # the checkpoint they were read after
    last_id: str  # the ID of the last entry: the checkpoint once they are counted
    clicks: dict[str, int]  # how many clicks each code got
    size: int  # how many entries were read

    @property
    def skipped(self) -> int:
        """How many entries were not clicks on a valid code."""
        return self.size - sum(self.clicks.values())


class ClickWorker:
    """Reads clicks from the stream and counts them, one batch at a time."""

    def __init__(
        self,
        client: redis.Redis,
        sessions: sessionmaker[Session],
        wait_ms: int | None = WAIT_MS,
        batch_size: int = BATCH_SIZE,
    ) -> None:
        self._redis = client
        self._sessions = sessions
        self._wait_ms = wait_ms  # None: don't wait for new clicks (tests)
        self.batch_size = batch_size

    def checkpoint(self) -> str:
        """The ID of the last entry already counted. Made on first use."""
        with self._sessions() as session:
            done = session.scalar(
                select(ClickCheckpoint.last_id).where(ClickCheckpoint.stream == STREAM)
            )
            if done is not None:
                return done
            session.add(ClickCheckpoint(stream=STREAM, last_id=START))
            try:
                session.commit()
            except IntegrityError:  # another worker made it a moment ago
                session.rollback()
        return START

    def read(self) -> Batch | None:
        """The next entries after the checkpoint, up to the batch size, or
        None if no click arrived within `wait_ms`."""
        after = self.checkpoint()
        reply = self._redis.xread({STREAM: after}, count=self.batch_size, block=self._wait_ms)
        entries = parse_entries(reply)
        if not entries:
            return None
        clicks = Counter(code for _, code in entries if code is not None)
        return Batch(after, entries[-1][0], dict(clicks), len(entries))

    def count(self, batch: Batch) -> bool:
        """Add the batch's clicks and move the checkpoint past it, in one
        transaction. False if another worker counted these clicks first."""
        with self._sessions() as session:
            moved = session.connection().execute(
                update(ClickCheckpoint)
                .where(ClickCheckpoint.stream == STREAM, ClickCheckpoint.last_id == batch.after)
                .values(last_id=batch.last_id)
            )
            if moved.rowcount != 1:
                session.rollback()
                metrics.BATCHES.labels("already_counted").inc()
                log.warning("another worker counted the clicks after %s first", batch.after)
                return False
            if batch.clicks:
                # Rows always locked in the same order (sorted by code): the
                # habit that keeps transactions locking several rows from
                # deadlocking each other. (Here the checkpoint's row lock,
                # taken first, already makes two workers take turns.)
                session.connection().execute(
                    ADD_CLICKS,
                    [{"for_code": code, "added": n} for code, n in sorted(batch.clicks.items())],
                )
            session.commit()
        # Counted only now that the checkpoint is past these entries, junk
        # included: a batch that failed is read again, and counted again.
        metrics.BATCHES.labels("counted").inc()
        metrics.CLICKS_COUNTED.inc(sum(batch.clicks.values()))
        if batch.skipped:
            metrics.ENTRIES_SKIPPED.inc(batch.skipped)
            log.warning(
                "skipped %d stream entries that are not clicks on a valid code", batch.skipped
            )
        self._forget(batch)
        return True

    def _forget(self, batch: Batch) -> None:
        """Remove the counted entries from the stream, so that it holds only
        clicks still to count. If this fails, the next batch removes them."""
        try:
            self._redis.xtrim(STREAM, minid=following(batch.last_id), approximate=False)
        except redis.RedisError as exc:
            log.warning("could not remove counted clicks from the stream: %s", exc)

    def process_batch(self) -> int:
        """Read and count one batch. Returns how many entries it counted."""
        batch = self.read()
        if batch is None:
            return 0
        with metrics.BATCH_DURATION.time():
            counted = self.count(batch)
        return batch.size if counted else 0


def run(worker: ClickWorker, stop: threading.Event, heartbeat: Path) -> None:
    """Count batches until `stop` is set, touching `heartbeat` on every round
    (the health check reads it, see app/heartbeat.py). Errors from Redis or
    PostgreSQL are logged, then retried. Any other error is a bug: it ends
    the process, and Docker starts a new one."""
    while not stop.is_set():  # p10-waiver: R2 runs until the process is told to stop
        heartbeat.touch()
        try:
            counted = worker.process_batch()
        except (redis.RedisError, SQLAlchemyError) as exc:
            # Nothing is lost: whatever wasn't counted is still in the stream.
            system = "redis" if isinstance(exc, redis.RedisError) else "database"
            metrics.WORKER_ERRORS.labels(system).inc()
            log.warning("counting clicks failed, trying again in %.0f s: %s", RETRY_SECONDS, exc)
            stop.wait(RETRY_SECONDS)
            continue
        if counted < worker.batch_size:
            stop.wait(LINGER_SECONDS)  # no backlog: let the next clicks gather


def serve_metrics(client: redis.Redis, port: int) -> WSGIServer:
    """Serve the worker's metrics, and the clicks waiting in the stream, on
    `port`, from a thread of their own, until the server is shut down."""
    registry = CollectorRegistry()
    registry.register(metrics.WORKER_REGISTRY)  # a registry is a collector too
    registry.register(metrics.BacklogCollector(client, STREAM))
    server, _ = start_http_server(port, registry=registry)
    return server


def stop_on_signals(stop: threading.Event) -> None:
    """SIGTERM (from `docker stop`) and Ctrl+C end the loop after this batch."""
    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, lambda _signum, _frame: stop.set())


def main(argv: Sequence[str]) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    if argv:
        log.error("python -m app.worker takes no arguments: settings come from the environment")
        return 2
    if not settings.redis_url:
        log.error("REDIS_URL is not set, so there is no stream of clicks to count")
        return 2
    # Each read asks Redis to wait up to WAIT_MS for clicks: wait a bit longer.
    client = make_redis(settings.redis_url, WAIT_MS / 1000 + SOCKET_MARGIN_SECONDS)
    stop = threading.Event()
    stop_on_signals(stop)
    server = serve_metrics(client, METRICS_PORT)
    log.info("counting clicks from the Redis stream %s", STREAM)
    try:
        run(ClickWorker(client, SessionLocal), stop, HEARTBEAT)
    finally:
        server.shutdown()
        server.server_close()
    log.info("stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
