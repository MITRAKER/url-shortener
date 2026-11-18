"""Instance numbers: every running instance leases its own (app/instances.py).

The leases are PostgreSQL advisory locks, so most of these tests need a real
PostgreSQL server: they run in CI's PostgreSQL job (and locally when
TEST_DATABASE_URL is set)."""

import asyncio
import time
from collections.abc import Iterator
from contextlib import ExitStack

import pytest
from sqlalchemy import Engine, create_engine, text

from app import instances
from app.db import engine, make_engine
from app.ids import MAX_INSTANCE_ID
from app.instances import (
    LOCK_NAMESPACE,
    Lease,
    NoFreeInstanceIdError,
    first_free,
    lease_instance_id,
    watch,
)

needs_postgres = pytest.mark.skipif(
    engine.dialect.name != "postgresql", reason="needs PostgreSQL (set TEST_DATABASE_URL)"
)
# Which session holds instance number :number, and what it is doing. A lock
# taken with two numbers shows up in pg_locks as classid, objid and objsubid 2.
# Advisory locks belong to one database: other databases on the same server
# (another deployment's, say) have numbers of their own.
HOLDER = text(
    "SELECT a.pid, a.state FROM pg_locks AS l JOIN pg_stat_activity AS a ON a.pid = l.pid"
    " WHERE l.locktype = 'advisory' AND l.granted AND l.objsubid = 2"
    " AND l.classid::bigint = :namespace AND l.objid::bigint = :number"
    " AND l.database = (SELECT oid FROM pg_database WHERE datname = current_database())"
)
RELEASE_CHECKS = 50  # a session ends a moment after its client hangs up:
RELEASE_CHECK_EVERY_SECONDS = 0.1  # wait for it, up to 5 s


def test_first_free_takes_the_lowest_number_it_can_lock() -> None:
    tried: list[int] = []

    def try_lock(number: int) -> bool:
        tried.append(number)
        return number >= 3  # 0, 1 and 2 are taken

    assert first_free(try_lock) == 3
    assert tried == [0, 1, 2, 3]


def test_first_free_gives_up_after_trying_all_64_numbers() -> None:
    tried: list[int] = []

    def try_lock(number: int) -> bool:
        tried.append(number)
        return False

    with pytest.raises(NoFreeInstanceIdError, match="all 64"):
        first_free(try_lock)
    assert tried == list(range(MAX_INSTANCE_ID + 1))


def test_with_sqlite_there_is_only_instance_0() -> None:
    """A SQLite database is a file on one machine, so it has one instance."""
    sqlite = create_engine("sqlite://")
    with lease_instance_id(sqlite) as first, lease_instance_id(sqlite) as second:
        assert first.number == second.number == 0
        assert first.is_held()  # no session, so nothing to lose
    sqlite.dispose()


def test_the_watchdog_reports_a_lost_lease_once(monkeypatch: pytest.MonkeyPatch) -> None:
    """It keeps checking while the lease is held, and stops once it is lost."""
    answers = iter([True, True, False])  # asking a fourth time would fail the test
    lease = Lease(None)
    monkeypatch.setattr(lease, "is_held", lambda: next(answers))
    monkeypatch.setattr(instances, "CHECK_EVERY_SECONDS", 0)
    lost: list[int] = []
    asyncio.run(watch(lease, lambda: lost.append(lease.number)))
    assert lost == [0]


@pytest.fixture
def pg_engine() -> Iterator[Engine]:
    """A fresh engine built exactly like the app's."""
    fresh = make_engine(engine.url.render_as_string(hide_password=False))
    yield fresh
    fresh.dispose()


def holder(pg_engine: Engine, number: int) -> tuple[int, str] | None:
    """(process ID, state) of the session holding `number`, or None."""
    with pg_engine.connect() as connection:
        row = connection.execute(HOLDER, {"namespace": LOCK_NAMESPACE, "number": number}).first()
    return None if row is None else (row.pid, row.state)


def wait_until_free(pg_engine: Engine, number: int) -> None:
    for _ in range(RELEASE_CHECKS):
        if holder(pg_engine, number) is None:
            return
        time.sleep(RELEASE_CHECK_EVERY_SECONDS)
    pytest.fail(f"instance number {number} was never released")


@needs_postgres
def test_instances_running_at_once_get_different_numbers(pg_engine: Engine) -> None:
    with ExitStack() as stack:
        numbers = [stack.enter_context(lease_instance_id(pg_engine)).number for _ in range(3)]
        assert len(set(numbers)) == 3
        assert all(holder(pg_engine, number) is not None for number in numbers)


@needs_postgres
def test_a_number_is_free_again_once_its_instance_stops(pg_engine: Engine) -> None:
    with lease_instance_id(pg_engine) as lease:
        assert holder(pg_engine, lease.number) is not None
        assert lease.is_held()
    wait_until_free(pg_engine, lease.number)


@needs_postgres
def test_the_lease_leaves_no_transaction_open(pg_engine: Engine) -> None:
    """Databases often end sessions left "idle in transaction" for long
    (idle_in_transaction_session_timeout), and the lease would end with it."""
    with lease_instance_id(pg_engine) as lease:
        session = holder(pg_engine, lease.number)
        assert session is not None
        assert session[1] == "idle"
        assert lease.is_held()  # checking the lease leaves none open either
        assert holder(pg_engine, lease.number) == session


@needs_postgres
def test_the_number_of_an_instance_that_died_comes_back(pg_engine: Engine) -> None:
    """No clean shutdown this time: the instance's database session is killed,
    as when its machine dies. The session's locks go with it."""
    with lease_instance_id(pg_engine) as lease:
        session = holder(pg_engine, lease.number)
        assert session is not None
        with pg_engine.connect() as connection:
            connection.execute(text("SELECT pg_terminate_backend(:pid)"), {"pid": session[0]})
        wait_until_free(pg_engine, lease.number)
        with lease_instance_id(pg_engine) as successor:
            assert holder(pg_engine, successor.number) is not None
        # The instance that died finds out at its next check.
        assert not lease.is_held()
