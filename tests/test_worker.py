"""The click worker (app/worker.py): it counts the clicks in the Redis stream
into PostgreSQL, each one exactly once, whatever fails in between."""

import os
import signal
import threading
import time
import urllib.request
from collections.abc import Callable, Iterator, Mapping, Sequence
from pathlib import Path

import pytest
import redis
from sqlalchemy import event, insert, select, text
from sqlalchemy.exc import OperationalError, SQLAlchemyError
from sqlalchemy.orm import sessionmaker

from app import heartbeat as heartbeat_module
from app import worker as worker_module
from app.clicks import STREAM
from app.config import settings
from app.db import STATEMENT_TIMEOUT_MS, Base, SessionLocal, engine
from app.heartbeat import ALIVE_SECONDS, HEARTBEAT, is_alive
from app.metrics import WORKER_REGISTRY
from app.models import ClickCheckpoint, Link
from app.worker import (
    CHUNK_SIZE,
    LINGER_SECONDS,
    START,
    Batch,
    ClickWorker,
    Watchdog,
    following,
    main,
    parse_entries,
    run,
    serve_metrics,
    stop_on_signals,
)
from tests.fakes import FakeClock

FIRST, SECOND, THIRD = "first", "second", "third"
needs_postgres = pytest.mark.skipif(
    engine.dialect.name != "postgresql", reason="needs PostgreSQL (set TEST_DATABASE_URL)"
)


@pytest.fixture(autouse=True)
def links() -> None:
    """An empty database with three links, none clicked yet."""
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    with SessionLocal() as session:
        for number, code in enumerate([FIRST, SECOND, THIRD], start=1):
            session.add(Link(id=number, code=code, target_url="https://example.com/"))
        session.commit()


def more_links(*codes: str) -> None:
    """Links beside the three that every test has, none clicked yet."""
    with SessionLocal() as session:
        for number, code in enumerate(codes, start=4):
            session.add(Link(id=number, code=code, target_url="https://example.com/"))
        session.commit()


def click(client: redis.Redis, *codes: str) -> str:
    """Append clicks to the stream, as redirects do. Returns the last entry's ID."""
    ids = [str(client.xadd(STREAM, {"code": code})) for code in codes]
    return ids[-1]


def bytes_client(client: redis.Redis) -> redis.Redis:
    """Another client of the same Redis, one that sends and returns bytes as
    they are: what a program other than ours might use."""
    pool = client.connection_pool
    options = pool.connection_kwargs | {"decode_responses": False}
    return redis.Redis(connection_pool=redis.ConnectionPool(pool.connection_class, **options))


def clicks() -> dict[str, int]:
    with SessionLocal() as session:
        return dict(session.execute(select(Link.code, Link.clicks)).all())


def checkpoint() -> str | None:
    with SessionLocal() as session:
        return session.scalar(select(ClickCheckpoint.last_id))


def metric(name: str, /, **labels: str) -> float:
    """One of the worker's metrics (app/metrics.py). They add up over the
    whole test run, so tests compare a value before and after."""
    return WORKER_REGISTRY.get_sample_value(name, labels) or 0.0


# ------------------------------------------------------------- counting ----


def test_an_empty_stream_leaves_nothing_to_do(click_worker: ClickWorker) -> None:
    assert click_worker.process_batch() == 0
    assert checkpoint() == START  # made on first use
    assert clicks() == {FIRST: 0, SECOND: 0, THIRD: 0}


def test_clicks_are_added_up_per_link(redis_client: redis.Redis, click_worker: ClickWorker) -> None:
    """A popular link's clicks become one UPDATE per batch, not one per click."""
    last = click(redis_client, FIRST, SECOND, FIRST, FIRST, SECOND)
    assert click_worker.read() == Batch(START, last, {FIRST: 3, SECOND: 2}, 5)


def test_counting_moves_the_checkpoint_and_empties_the_stream(
    redis_client: redis.Redis, click_worker: ClickWorker
) -> None:
    last = click(redis_client, FIRST, SECOND, FIRST)
    assert click_worker.process_batch() == 3
    assert clicks() == {FIRST: 2, SECOND: 1, THIRD: 0}
    assert checkpoint() == last
    assert redis_client.xlen(STREAM) == 0  # the stream holds only clicks still to count
    assert click_worker.process_batch() == 0


def test_a_batch_holds_at_most_batch_size_entries(redis_client: redis.Redis) -> None:
    worker = ClickWorker(redis_client, SessionLocal, wait_ms=None, batch_size=2)
    click(redis_client, FIRST, FIRST, FIRST, FIRST, FIRST)
    assert [worker.process_batch() for _ in range(4)] == [2, 2, 1, 0]
    assert clicks()[FIRST] == 5


def test_the_updates_go_in_chunks_with_a_step_after_each(
    redis_client: redis.Redis, click_worker: ClickWorker
) -> None:
    """A batch runs one UPDATE per link, up to a thousand, and a slow database
    may take up to 5 s for each. So they go in chunks, still sorted by code,
    and the worker reports a step after each chunk: run() touches the
    heartbeat then, so a long batch doesn't look like a stuck loop."""
    # On a database that answers, a chunk ends within half of ALIVE_SECONDS.
    assert 0 < CHUNK_SIZE * STATEMENT_TIMEOUT_MS / 1000 <= ALIVE_SECONDS / 2
    codes = [f"link{number}" for number in range(2 * CHUNK_SIZE + 1)]  # 3 chunks
    more_links(*codes)
    click(redis_client, *reversed(codes))
    chunks: list[list[str]] = []  # the codes of each chunk sent to the database
    steps: list[int] = []  # how many chunks had been sent at each step

    def record(
        _conn: object,
        statement: object,
        multiparams: Sequence[Mapping[str, object]],
        params: Mapping[str, object],
        _options: object,
    ) -> None:
        if statement is worker_module.ADD_CLICKS:
            chunks.append([str(row["for_code"]) for row in multiparams or [params]])

    event.listen(engine, "before_execute", record)
    try:
        assert click_worker.process_batch(lambda: steps.append(len(chunks))) == len(codes)
    finally:
        event.remove(engine, "before_execute", record)
    assert chunks == [sorted(codes)[n : n + CHUNK_SIZE] for n in range(0, len(codes), CHUNK_SIZE)]
    # The checkpoint read, the stream read, the checkpoint moved, each chunk.
    assert steps == [0, 0, 0, 1, 2, 3]
    assert all(clicks()[code] == 1 for code in codes)


def test_entries_that_are_not_clicks_are_skipped(
    redis_client: redis.Redis, click_worker: ClickWorker, caplog: pytest.LogCaptureFixture
) -> None:
    """Anyone who can write to Redis can write to the stream. An entry without
    a valid code is skipped, but doesn't block the clicks behind it."""
    redis_client.xadd(STREAM, {"code": "no spaces allowed"})
    redis_client.xadd(STREAM, {"something": "else"})
    redis_client.xadd(STREAM, {"code": "nosuchlink"})  # a valid code, but no such link
    last = click(redis_client, FIRST)
    assert click_worker.process_batch() == 4
    assert clicks() == {FIRST: 1, SECOND: 0, THIRD: 0}
    assert checkpoint() == last
    assert "skipped 2 stream entries" in caplog.text


def test_the_worker_counts_what_it_did(
    redis_client: redis.Redis, click_worker: ClickWorker
) -> None:
    def counts() -> list[float]:
        return [
            metric("click_worker_batches_total", result="counted"),
            metric("click_worker_clicks_counted_total"),
            metric("click_worker_entries_skipped_total"),
            metric("click_worker_batch_duration_seconds_count"),  # batches timed
        ]

    before = counts()
    click(redis_client, FIRST, SECOND, FIRST)
    redis_client.xadd(STREAM, {"code": "not valid!"})
    assert click_worker.process_batch() == 4
    assert counts() == [before[0] + 1, before[1] + 3, before[2] + 1, before[3] + 1]
    assert click_worker.process_batch() == 0  # nothing to read: no batch
    assert counts() == [before[0] + 1, before[1] + 3, before[2] + 1, before[3] + 1]


def test_a_batch_of_nothing_but_junk_still_moves_the_checkpoint(
    redis_client: redis.Redis, click_worker: ClickWorker
) -> None:
    """Otherwise the worker would read the same junk again, forever (a
    "poison message"), and no click behind it would ever be counted."""
    last = str(redis_client.xadd(STREAM, {"something": "else"}))
    assert click_worker.process_batch() == 1
    assert checkpoint() == last
    assert redis_client.xlen(STREAM) == 0


def test_an_entry_that_is_not_utf8_is_skipped_too(
    redis_client: redis.Redis, click_worker: ClickWorker, caplog: pytest.LogCaptureFixture
) -> None:
    """Redis stores bytes, so the stream can hold bytes that aren't UTF-8.
    Decoded strictly, such an entry crashed the worker at every start, and no
    click behind it was ever counted. make_redis decodes it to U+FFFD, and it
    fails validation like any other junk."""
    click(redis_client, FIRST)
    with bytes_client(redis_client) as raw:
        raw.xadd(STREAM, {"code": b"\xff\xfe"})
    last = click(redis_client, SECOND)
    assert click_worker.process_batch() == 3
    assert clicks() == {FIRST: 1, SECOND: 1, THIRD: 0}
    assert checkpoint() == last
    assert "skipped 1 stream entries" in caplog.text


# ------------------------------------------------ exactly once, anyway ----


def test_a_failed_transaction_counts_nothing_and_loses_nothing(
    redis_client: redis.Redis, click_worker: ClickWorker, monkeypatch: pytest.MonkeyPatch
) -> None:
    click(redis_client, FIRST, FIRST)
    # The checkpoint moves, then adding the clicks fails: the transaction
    # is rolled back, so the checkpoint moves back too.
    broken = text("UPDATE no_such_table SET clicks = :added WHERE code = :for_code")
    monkeypatch.setattr(worker_module, "ADD_CLICKS", broken)
    with pytest.raises(SQLAlchemyError):
        click_worker.process_batch()
    assert checkpoint() == START
    assert clicks()[FIRST] == 0
    assert redis_client.xlen(STREAM) == 2
    monkeypatch.undo()  # the database is back: the same clicks, counted once
    assert click_worker.process_batch() == 2
    assert clicks()[FIRST] == 2


def test_a_batch_read_again_skips_its_junk_only_once(
    redis_client: redis.Redis,
    click_worker: ClickWorker,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Entries are skipped for good when the checkpoint moves past them, in
    the commit. A batch that failed is read again, junk and all."""
    redis_client.xadd(STREAM, {"code": "not valid!"})
    click(redis_client, FIRST)
    before = metric("click_worker_entries_skipped_total")
    broken = text("UPDATE no_such_table SET clicks = :added WHERE code = :for_code")
    monkeypatch.setattr(worker_module, "ADD_CLICKS", broken)
    with pytest.raises(SQLAlchemyError):
        click_worker.process_batch()
    assert metric("click_worker_entries_skipped_total") == before  # nothing skipped yet
    monkeypatch.undo()
    assert click_worker.process_batch() == 2
    assert metric("click_worker_entries_skipped_total") == before + 1
    assert caplog.text.count("not clicks on a valid code") == 1


# The steps of a batch of three links, with a chunk per link: the checkpoint
# read, the stream read, the checkpoint moved, then the three chunks.
@pytest.mark.parametrize(
    "last_step",
    [3, 4, 5, 6],
    ids=["checkpoint moved", "first chunk", "second chunk", "last chunk"],
)
def test_the_chunks_of_a_batch_are_one_transaction(
    redis_client: redis.Redis,
    click_worker: ClickWorker,
    monkeypatch: pytest.MonkeyPatch,
    last_step: int,
) -> None:
    """A worker that dies after any step of count() has counted nothing:
    the checkpoint and all the chunks are committed together, or not at all.
    (A commit slipped in just before a step fails the case that dies after
    that step; one just after it fails the next case.)"""
    monkeypatch.setattr(worker_module, "CHUNK_SIZE", 1)  # a chunk per link
    click(redis_client, FIRST, SECOND, THIRD)
    steps: list[None] = []

    def die_after_the_last_step() -> None:
        steps.append(None)
        if len(steps) == last_step:
            raise RuntimeError("the worker died")

    with pytest.raises(RuntimeError, match="died"):
        click_worker.process_batch(die_after_the_last_step)
    assert checkpoint() == START
    assert clicks() == {FIRST: 0, SECOND: 0, THIRD: 0}
    assert click_worker.process_batch() == 3  # a new worker: the same clicks, once
    assert clicks() == {FIRST: 1, SECOND: 1, THIRD: 1}


def test_a_crash_after_the_commit_counts_nothing_twice(
    redis_client: redis.Redis,
    click_worker: ClickWorker,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Counted, but not yet removed from the stream (Redis failed, or the
    worker died, just after the commit). The checkpoint says they're done."""

    def down(*_args: object, **_kwargs: object) -> None:
        raise redis.ConnectionError("Redis went away")

    click(redis_client, FIRST, FIRST)
    monkeypatch.setattr(redis_client, "xtrim", down)
    assert click_worker.process_batch() == 2
    assert "could not remove counted clicks" in caplog.text
    assert redis_client.xlen(STREAM) == 2  # still there...
    assert click_worker.process_batch() == 0  # ...but behind the checkpoint
    assert clicks()[FIRST] == 2
    monkeypatch.undo()
    click(redis_client, SECOND)
    assert click_worker.process_batch() == 1
    assert redis_client.xlen(STREAM) == 0  # the next batch removed them too
    assert clicks() == {FIRST: 2, SECOND: 1, THIRD: 0}


def test_a_batch_another_worker_counted_first_is_thrown_away(
    redis_client: redis.Redis, caplog: pytest.LogCaptureFixture
) -> None:
    first, second = (ClickWorker(redis_client, SessionLocal, wait_ms=None) for _ in range(2))
    click(redis_client, FIRST, FIRST)
    batch = second.read()
    assert batch is not None
    assert first.process_batch() == 2  # the first worker is faster
    lost = metric("click_worker_batches_total", result="already_counted")
    assert not second.count(batch)  # the checkpoint moved: compare-and-set fails
    assert clicks()[FIRST] == 2
    assert "another worker counted the clicks" in caplog.text
    assert metric("click_worker_batches_total", result="already_counted") == lost + 1


def test_two_workers_at_once_count_every_click_exactly_once(redis_client: redis.Redis) -> None:
    last = click(redis_client, *[FIRST, SECOND, THIRD] * 100)
    workers = [ClickWorker(redis_client, SessionLocal, wait_ms=None, batch_size=7) for _ in "ab"]

    def drain(worker: ClickWorker) -> None:
        for _ in range(1000):  # far more rounds than 300 clicks need
            if worker.checkpoint() == last:
                return
            worker.process_batch()

    threads = [threading.Thread(target=drain, args=[worker]) for worker in workers]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    assert clicks() == {FIRST: 100, SECOND: 100, THIRD: 100}
    assert checkpoint() == last


def test_workers_starting_together_make_one_checkpoint(redis_client: redis.Redis) -> None:
    """Both find no checkpoint, both try to make it: one INSERT loses."""
    sessions = sessionmaker(bind=engine)

    def other_worker_is_faster(*_args: object) -> None:
        with engine.begin() as connection:
            connection.execute(insert(ClickCheckpoint).values(stream=STREAM, last_id=START))

    event.listen(sessions, "before_flush", other_worker_is_faster, once=True)
    assert ClickWorker(redis_client, sessions).checkpoint() == START
    with SessionLocal() as session:
        assert len(session.scalars(select(ClickCheckpoint)).all()) == 1


# -------------------------------------------------- the stream's replies ----


def test_an_empty_reply_means_no_clicks() -> None:
    assert parse_entries([]) == []
    assert parse_entries(None) == []  # what Redis sends when a blocking read times out


def test_entries_are_checked_not_trusted() -> None:
    reply = [[STREAM, [("1-0", {"code": "abc"}), ("1-1", {"code": "a b"}), ("1-2", {"x": "abc"})]]]
    assert parse_entries(reply) == [("1-0", "abc"), ("1-1", None), ("1-2", None)]


@pytest.mark.parametrize(
    "reply",
    [
        {STREAM: [[("1-0", {"code": "abc"})]]},  # RESP3's shape
        [[STREAM, "not a list"]],
        [[STREAM, [("1-0",)]]],  # an entry without fields
        [[STREAM, [(1, {"code": "abc"})]]],  # an ID that isn't a string
    ],
)
def test_a_reply_of_another_shape_is_an_error_not_silence(reply: object) -> None:
    with pytest.raises(redis.ResponseError, match="unexpected"):
        parse_entries(reply)


def test_following_is_the_next_possible_stream_id() -> None:
    assert following("1795000000000-4") == "1795000000000-5"
    assert following(START) == "0-1"


# ----------------------------------------------------------- the process ----


def test_run_carries_on_after_errors_until_stopped(
    click_worker: ClickWorker,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    stop = threading.Event()
    outcomes: list[int | Exception] = [
        redis.ConnectionError("Redis is down"),
        OperationalError("SELECT 1", {}, Exception("PostgreSQL is down")),
        3,
    ]

    def process_batch(_on_step: Callable[[], object]) -> int:
        outcome = outcomes.pop(0)
        if not outcomes:
            stop.set()  # as SIGTERM would
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(click_worker, "process_batch", process_batch)
    monkeypatch.setattr(worker_module, "RETRY_SECONDS", 0.0)
    before = [metric("click_worker_errors_total", system=s) for s in ("redis", "database")]
    run(click_worker, stop, tmp_path / "alive")
    assert outcomes == []
    assert caplog.text.count("counting clicks failed") == 2
    assert is_alive(tmp_path / "alive")
    after = [metric("click_worker_errors_total", system=s) for s in ("redis", "database")]
    assert after == [before[0] + 1, before[1] + 1]


class RecordingStop(threading.Event):
    """A stop event that notes how long the worker waits, without waiting."""

    def __init__(self) -> None:
        super().__init__()
        self.waits: list[float | None] = []

    def wait(self, timeout: float | None = None) -> bool:
        self.waits.append(timeout)
        return self.is_set()


def test_the_worker_lingers_after_a_batch_that_was_not_full(
    click_worker: ClickWorker, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A full batch means more clicks are waiting: read again at once.
    Otherwise wait a moment, so that the next batch gathers more clicks."""
    stop = RecordingStop()
    results = [click_worker.batch_size, click_worker.batch_size, 3]

    def process_batch(_on_step: Callable[[], object]) -> int:
        result = results.pop(0)
        if not results:
            stop.set()
        return result

    monkeypatch.setattr(click_worker, "process_batch", process_batch)
    run(click_worker, stop, tmp_path / "alive")
    assert stop.waits == [LINGER_SECONDS]  # after the batch of 3 only


def test_any_other_error_is_a_bug_and_ends_the_worker(
    click_worker: ClickWorker, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def bug(_on_step: Callable[[], object]) -> int:
        raise ValueError("a bug")

    monkeypatch.setattr(click_worker, "process_batch", bug)
    with pytest.raises(ValueError, match="a bug"):
        run(click_worker, threading.Event(), tmp_path / "alive")


def test_run_touches_the_heartbeat_after_each_step_too(
    click_worker: ClickWorker, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """On a slow database, one round can take longer than ALIVE_SECONDS. So
    run() hands the worker a way to touch the heartbeat after each step of
    the round, not only as the round begins."""
    heartbeat = tmp_path / "alive"
    stop = threading.Event()
    alive_before_the_step: list[bool] = []

    def process_batch(on_step: Callable[[], object]) -> int:
        beat(heartbeat, 0.0)  # as if the round had begun long ago
        alive_before_the_step.append(is_alive(heartbeat))
        on_step()
        stop.set()
        return 1

    monkeypatch.setattr(click_worker, "process_batch", process_batch)
    run(click_worker, stop, heartbeat)
    assert alive_before_the_step == [False]
    assert is_alive(heartbeat)


def test_a_stopped_worker_does_no_more_rounds(click_worker: ClickWorker, tmp_path: Path) -> None:
    stop = threading.Event()
    stop.set()
    run(click_worker, stop, tmp_path / "alive")
    assert not (tmp_path / "alive").exists()


def test_alive_means_a_recent_heartbeat(tmp_path: Path) -> None:
    heartbeat = tmp_path / "alive"
    assert not is_alive(heartbeat)  # never started
    heartbeat.touch()
    beat = heartbeat.stat().st_mtime
    assert is_alive(heartbeat, now=lambda: beat + 1)
    assert not is_alive(heartbeat, now=lambda: beat + ALIVE_SECONDS + 1)  # stuck


def test_sigterm_and_ctrl_c_stop_the_worker() -> None:
    saved = {signum: signal.getsignal(signum) for signum in (signal.SIGTERM, signal.SIGINT)}
    try:
        for signum in saved:
            stop = threading.Event()
            stop_on_signals(stop)
            signal.raise_signal(signum)
            assert stop.is_set()
    finally:
        for signum, handler in saved.items():
            signal.signal(signum, handler)


def test_the_health_check_reads_the_heartbeat(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """What `python -m app.heartbeat`, the container's health check, answers."""
    monkeypatch.setattr(heartbeat_module, "HEARTBEAT", tmp_path / "alive")
    assert heartbeat_module.main() == 1
    (tmp_path / "alive").touch()
    assert heartbeat_module.main() == 0


def beat(heartbeat: Path, at: float) -> None:
    """A round of the loop that began at `at`, a time on a FakeClock."""
    heartbeat.touch()
    os.utime(heartbeat, (at, at))


def never_exit(status: int) -> None:
    raise AssertionError(f"the watchdog ended a worker that isn't stuck (status {status})")


class FakeTimeStop(threading.Event):
    """A stop event whose waits move a FakeClock instead of waiting. After
    `limit` waits it is set, so a watchdog that never acts fails its test
    instead of hanging it."""

    def __init__(self, clock: FakeClock, limit: int) -> None:
        super().__init__()
        self._clock = clock
        self._waits_left = limit

    def wait(self, timeout: float | None = None) -> bool:
        self._clock.advance(timeout or 0.0)
        self._waits_left -= 1
        if self._waits_left <= 0:
            self.set()
        return self.is_set()


def test_a_stuck_loop_ends_the_worker_so_that_docker_starts_it_again(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    heartbeat = tmp_path / "alive"
    clock = FakeClock()
    stop = FakeTimeStop(clock, limit=100)
    exits: list[tuple[int, float]] = []

    def exit_process(status: int) -> None:
        exits.append((status, clock()))
        stop.set()  # os._exit never returns: the loop must end here too

    watchdog = Watchdog(heartbeat, now=clock, exit_process=exit_process)
    clock.advance(600)  # ten minutes of rounds later,
    last_round = clock()
    beat(heartbeat, last_round)  # a round begins that never ends
    watchdog.watch(stop)
    [(status, when)] = exits
    assert status == 1
    # Not while the round could still be a slow one, and at the next look after.
    waited = when - last_round
    assert ALIVE_SECONDS <= waited <= ALIVE_SECONDS + worker_module.WATCHDOG_SECONDS
    assert "the loop is stuck" in caplog.text


def test_a_worker_that_goes_round_its_loop_is_not_stuck(tmp_path: Path) -> None:
    """While PostgreSQL or Redis is down, every round fails, waits and tries
    again, and touches the heartbeat as it begins
    (test_run_carries_on_after_errors_until_stopped). Restarting the worker
    wouldn't help, so the watchdog leaves it alone, however long that lasts."""
    heartbeat = tmp_path / "alive"
    clock = FakeClock()
    watchdog = Watchdog(heartbeat, now=clock, exit_process=never_exit)
    for _ in range(200):  # 200 rounds, each just short of the limit
        beat(heartbeat, clock())
        clock.advance(ALIVE_SECONDS - 1)
        assert not watchdog.is_stuck()


@pytest.fixture
def slow_updates() -> Iterator[None]:
    """Every UPDATE on links takes 0.2 s (a trigger sleeps): a database that
    is slow, but answers. PostgreSQL only. (`links`, autouse, runs first.)"""
    with engine.begin() as connection:
        connection.execute(
            text(
                "CREATE OR REPLACE FUNCTION slow_update() RETURNS trigger LANGUAGE plpgsql"
                " AS $$ BEGIN PERFORM pg_sleep(0.2); RETURN NEW; END $$"
            )
        )
        connection.execute(
            text(
                "CREATE TRIGGER slow BEFORE UPDATE ON links"
                " FOR EACH ROW EXECUTE FUNCTION slow_update()"
            )
        )
    yield
    with engine.begin() as connection:
        connection.execute(text("DROP FUNCTION slow_update() CASCADE"))  # and its trigger


@needs_postgres
@pytest.mark.usefixtures("slow_updates")
def test_a_slow_database_that_answers_is_not_a_stuck_loop(
    redis_client: redis.Redis, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Every UPDATE takes 0.2 s, far within its limit, and the batch takes
    longer than ALIVE_SECONDS in all. The worker is getting on, a chunk at a
    time, so the watchdog must leave it alone. Shrunk to run in about two
    seconds: ALIVE_SECONDS is 1 s, and a chunk is one UPDATE."""
    for module in (heartbeat_module, worker_module):
        monkeypatch.setattr(module, "ALIVE_SECONDS", 1.0)
    monkeypatch.setattr(worker_module, "WATCHDOG_SECONDS", 0.05)
    monkeypatch.setattr(worker_module, "CHUNK_SIZE", 1)
    codes = [f"slow{number}" for number in range(8)]  # 8 UPDATEs: 1.6 s
    more_links(*codes)
    click(redis_client, *codes)
    worker = ClickWorker(redis_client, SessionLocal, wait_ms=None)
    stop = threading.Event()
    count_one_batch = worker.process_batch

    def count_one_batch_then_stop(on_step: Callable[[], object]) -> int:
        try:
            return count_one_batch(on_step)
        finally:
            stop.set()  # run() and the watchdog end here

    monkeypatch.setattr(worker, "process_batch", count_one_batch_then_stop)
    exits: list[int] = []
    watchdog = Watchdog(tmp_path / "alive", exit_process=exits.append)
    thread = threading.Thread(target=watchdog.watch, args=(stop,), daemon=True)
    thread.start()
    started = time.monotonic()
    run(worker, stop, tmp_path / "alive")
    took = time.monotonic() - started
    thread.join(timeout=5)
    assert took > 1.0  # longer than ALIVE_SECONDS: one heartbeat a round would be late
    assert exits == []  # but the watchdog never took the loop for stuck
    assert all(clicks()[code] == 1 for code in codes)


def test_a_new_worker_has_time_for_its_first_heartbeat(tmp_path: Path) -> None:
    """Docker starts the same container again, with the files it had: the
    heartbeat may be missing, or left over from the worker that got stuck."""
    heartbeat = tmp_path / "alive"
    clock = FakeClock()
    watchdog = Watchdog(heartbeat, now=clock, exit_process=never_exit)
    assert not watchdog.is_stuck()  # no heartbeat yet
    beat(heartbeat, clock() - 600)  # the one the stuck worker left, 10 minutes ago
    clock.advance(ALIVE_SECONDS - 1)
    assert not watchdog.is_stuck()  # still within its first round
    clock.advance(1)
    assert watchdog.is_stuck()  # no round in all that time: stuck as well


def test_the_watchdog_runs_beside_the_loop_until_the_worker_stops(tmp_path: Path) -> None:
    stop = threading.Event()
    thread = worker_module.start_watchdog(tmp_path / "alive", stop)
    assert thread.is_alive()
    assert thread.daemon  # it never keeps the process alive
    stop.set()  # SIGTERM, say
    thread.join(timeout=5)
    assert not thread.is_alive()


def test_the_worker_takes_no_arguments(caplog: pytest.LogCaptureFixture) -> None:
    assert main(["--alive"]) == 2
    assert "takes no arguments" in caplog.text


def test_without_redis_there_is_nothing_to_count(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(settings, "redis_url", "")
    assert main([]) == 2
    assert "REDIS_URL is not set" in caplog.text


def test_the_worker_serves_its_metrics(redis_client: redis.Redis) -> None:
    click(redis_client, FIRST, SECOND)
    server = serve_metrics(redis_client, port=0)  # 0: any free port
    try:
        url = f"http://127.0.0.1:{server.server_port}/metrics"
        with urllib.request.urlopen(url, timeout=5) as response:
            page = response.read().decode()
    finally:
        server.shutdown()
        server.server_close()
    assert "click_worker_batches_total" in page
    assert "click_backlog_entries 2.0" in page
    assert "process_cpu_seconds_total" in page
    # The HTTP metrics of the app instances have no place here.
    assert "http_requests_total" not in page


def test_main_runs_the_worker(monkeypatch: pytest.MonkeyPatch) -> None:
    started: list[tuple[ClickWorker, Path]] = []
    watched: list[Path] = []

    def fake_run(worker: ClickWorker, _stop: threading.Event, heartbeat: Path) -> None:
        started.append((worker, heartbeat))

    def fake_start_watchdog(heartbeat: Path, _stop: threading.Event) -> None:
        watched.append(heartbeat)

    monkeypatch.setattr(settings, "redis_url", "redis://127.0.0.1:1/0")  # never connected
    monkeypatch.setattr(worker_module, "start_watchdog", fake_start_watchdog)
    monkeypatch.setattr(worker_module, "stop_on_signals", lambda _stop: None)
    monkeypatch.setattr(worker_module, "run", fake_run)
    monkeypatch.setattr(worker_module, "METRICS_PORT", 0)
    assert main([]) == 0
    [(worker, heartbeat)] = started
    assert isinstance(worker, ClickWorker)
    assert heartbeat == HEARTBEAT
    assert watched == [HEARTBEAT]
