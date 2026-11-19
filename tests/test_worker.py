"""The click worker (app/worker.py): it counts the clicks in the Redis stream
into PostgreSQL, each one exactly once, whatever fails in between."""

import signal
import threading
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
from app.db import Base, SessionLocal, engine
from app.heartbeat import ALIVE_SECONDS, HEARTBEAT, is_alive
from app.models import ClickCheckpoint, Link
from app.worker import (
    START,
    Batch,
    ClickWorker,
    following,
    main,
    parse_entries,
    run,
    stop_on_signals,
)

FIRST, SECOND, THIRD = "first", "second", "third"


@pytest.fixture(autouse=True)
def links() -> None:
    """An empty database with three links, none clicked yet."""
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    with SessionLocal() as session:
        for number, code in enumerate([FIRST, SECOND, THIRD], start=1):
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
    assert not second.count(batch)  # the checkpoint moved: compare-and-set fails
    assert clicks()[FIRST] == 2
    assert "another worker counted the clicks" in caplog.text


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

    def process_batch() -> int:
        outcome = outcomes.pop(0)
        if not outcomes:
            stop.set()  # as SIGTERM would
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(click_worker, "process_batch", process_batch)
    monkeypatch.setattr(worker_module, "RETRY_SECONDS", 0.0)
    run(click_worker, stop, tmp_path / "alive")
    assert outcomes == []
    assert caplog.text.count("counting clicks failed") == 2
    assert is_alive(tmp_path / "alive")


def test_any_other_error_is_a_bug_and_ends_the_worker(
    click_worker: ClickWorker, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def bug() -> int:
        raise ValueError("a bug")

    monkeypatch.setattr(click_worker, "process_batch", bug)
    with pytest.raises(ValueError, match="a bug"):
        run(click_worker, threading.Event(), tmp_path / "alive")


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


def test_the_worker_takes_no_arguments(caplog: pytest.LogCaptureFixture) -> None:
    assert main(["--alive"]) == 2
    assert "takes no arguments" in caplog.text


def test_without_redis_there_is_nothing_to_count(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(settings, "redis_url", "")
    assert main([]) == 2
    assert "REDIS_URL is not set" in caplog.text


def test_main_runs_the_worker(monkeypatch: pytest.MonkeyPatch) -> None:
    started: list[tuple[ClickWorker, Path]] = []

    def fake_run(worker: ClickWorker, _stop: threading.Event, heartbeat: Path) -> None:
        started.append((worker, heartbeat))

    monkeypatch.setattr(settings, "redis_url", "redis://127.0.0.1:1/0")  # never connected
    monkeypatch.setattr(worker_module, "stop_on_signals", lambda _stop: None)
    monkeypatch.setattr(worker_module, "run", fake_run)
    assert main([]) == 0
    [(worker, heartbeat)] = started
    assert isinstance(worker, ClickWorker)
    assert heartbeat == HEARTBEAT
