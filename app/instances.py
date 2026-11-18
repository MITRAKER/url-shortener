"""Each running instance leases its own number, 0 to 63, for its IDs (app/ids.py).

Giving every instance a number in its configuration works until two of them
get the same one by mistake. (And Docker Compose gives all replicas of a
service the very same configuration.) Instead, an instance asks PostgreSQL
for a free number when it starts, and keeps it until it stops.

The lease is a PostgreSQL *advisory lock*: a lock on a number we choose. It
means nothing to PostgreSQL itself; the app decides what it protects. It
belongs to one database session, so when an instance dies, its connection
closes and its number is free again: no clean-up job, no expiry to tune.
(ZooKeeper's ephemeral nodes and etcd's leases are the same idea.)

The flip side: the lease ends with the session, even when the instance lives
on. If PostgreSQL restarts, every lock is gone, and a new instance may take a
number that a running one still uses. So each instance checks its session
every few seconds, and once it is gone, stops making IDs and shuts down, to be
started again with a fresh lease (see `watch`).

Advisory locks belong to one database, so instance numbers do too: two
deployments with databases of their own on one server never get in each
other's way. The lease holds one connection of the pool for as long as the
instance runs.
"""

import asyncio
import logging
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Final

from sqlalchemy import Connection, Engine, text
from sqlalchemy.exc import DBAPIError

from app.ids import MAX_INSTANCE_ID

log = logging.getLogger(__name__)

# pg_try_advisory_lock(namespace, number). The namespace keeps our locks apart
# from those of other apps sharing the database: it is "urls" in ASCII.
LOCK_NAMESPACE: Final = 0x75726C73
TRY_LOCK: Final = text("SELECT pg_try_advisory_lock(:namespace, :number)")
STILL_THERE: Final = text("SELECT 1")
# How often a running instance checks that it still holds its number. It is
# also the longest time two instances could share a number after a restart
# of PostgreSQL. (The database's UNIQUE constraints still refuse duplicates.)
CHECK_EVERY_SECONDS = 5.0


class NoFreeInstanceIdError(Exception):
    """All instance numbers are taken."""


def first_free(try_lock: Callable[[int], bool]) -> int:
    """The first number, from 0 to 63, that `try_lock` manages to lock."""
    for number in range(MAX_INSTANCE_ID + 1):  # bounded: 64 tries at most
        if try_lock(number):
            return number
    raise NoFreeInstanceIdError(f"all {MAX_INSTANCE_ID + 1} instance numbers are in use")


class Lease:
    """An instance number, held by one database session for as long as it lives.

    Without a session (SQLite) there is nothing to lose: the lease is number 0.
    """

    def __init__(self, connection: Connection | None) -> None:
        self.number = 0
        self._connection = connection
        # A session serves one thread at a time, and the checks run in a
        # worker thread: one may still be running when the lease ends.
        self._lock = threading.Lock()

    def try_lock(self, number: int) -> bool:
        """Take `number`, unless another session holds it."""
        if self._connection is None:
            return True
        with self._lock:
            taken = self._connection.scalar(
                TRY_LOCK, {"namespace": LOCK_NAMESPACE, "number": number}
            )
            # End the transaction SQLAlchemy opened for the query. Databases
            # often end sessions left "idle in transaction" for long (the
            # idle_in_transaction_session_timeout setting), and that would
            # end the lease too. The lock belongs to the session, so it stays.
            self._connection.commit()
        return bool(taken)

    def is_held(self) -> bool:
        """False once the session has ended (PostgreSQL restarted, say): our
        number is free again, and another instance may take it."""
        if self._connection is None:
            return True
        with self._lock:
            try:
                self._connection.execute(STILL_THERE)
                self._connection.rollback()  # no transaction left open
            except DBAPIError:
                return False
        return True

    def end(self) -> None:
        """Close the session for real instead of returning its connection to
        the pool: ending the session is what releases the lock."""
        if self._connection is None:
            return
        with self._lock:
            self._connection.invalidate()
            self._connection.close()


@contextmanager
def lease_instance_id(engine: Engine) -> Iterator[Lease]:
    """Hold an instance number for as long as the `with` block runs."""
    # SQLite is a file on one machine, so there is only ever one instance.
    postgres = engine.dialect.name == "postgresql"
    lease = Lease(engine.connect() if postgres else None)
    try:
        lease.number = first_free(lease.try_lock)
        log.info("this instance has number %d", lease.number)
        yield lease
    finally:
        lease.end()


async def watch(lease: Lease, on_lost: Callable[[], None]) -> None:
    """Check every few seconds that `lease` is still held. Once it is not,
    call `on_lost` and stop. Runs until then, or until it is cancelled."""
    # The check talks to the database, so it runs in a worker thread: the
    # event loop goes on serving requests meanwhile.
    while await asyncio.to_thread(lease.is_held):  # p10-waiver: R2 runs while the instance runs
        await asyncio.sleep(CHECK_EVERY_SECONDS)
    log.critical("lost instance number %d: its database session ended", lease.number)
    on_lost()
