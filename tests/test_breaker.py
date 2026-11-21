import logging
from concurrent.futures import ThreadPoolExecutor

import pytest

from app.breaker import CircuitBreaker
from tests.fakes import FakeClock

THRESHOLD = 3
RESET_AFTER = 5.0


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def breaker(clock: FakeClock) -> CircuitBreaker:
    return CircuitBreaker("Redis", THRESHOLD, RESET_AFTER, clock=clock)


def trip(breaker: CircuitBreaker) -> None:
    for _ in range(THRESHOLD):
        breaker.record_failure()


def test_starts_closed(breaker: CircuitBreaker) -> None:
    assert not breaker.is_open
    assert breaker.allow()


def test_opens_after_threshold_failures_in_a_row(breaker: CircuitBreaker) -> None:
    for _ in range(THRESHOLD - 1):
        breaker.record_failure()
    assert breaker.allow()  # one failure short: still closed
    breaker.record_failure()
    assert breaker.is_open
    assert not breaker.allow()


def test_a_success_starts_the_count_again(breaker: CircuitBreaker) -> None:
    for _ in range(THRESHOLD - 1):
        breaker.record_failure()
    breaker.record_success()
    for _ in range(THRESHOLD - 1):
        breaker.record_failure()
    assert breaker.allow()  # never THRESHOLD failures in a row


def test_open_circuit_lets_one_trial_call_through_after_the_cool_down(
    breaker: CircuitBreaker, clock: FakeClock
) -> None:
    trip(breaker)
    clock.advance(RESET_AFTER - 0.1)
    assert not breaker.allow()
    clock.advance(0.1)
    assert breaker.allow()  # half-open: this caller makes the trial call
    assert not breaker.allow()  # everyone else keeps skipping meanwhile


def test_successful_trial_closes_the_circuit(breaker: CircuitBreaker, clock: FakeClock) -> None:
    trip(breaker)
    clock.advance(RESET_AFTER)
    assert breaker.allow()
    breaker.record_success()
    assert not breaker.is_open
    assert all(breaker.allow() for _ in range(10))


def test_failed_trial_waits_a_full_cool_down_again(
    breaker: CircuitBreaker, clock: FakeClock
) -> None:
    trip(breaker)
    clock.advance(RESET_AFTER)
    assert breaker.allow()
    breaker.record_failure()  # the trial failed
    clock.advance(RESET_AFTER - 0.1)
    assert not breaker.allow()
    clock.advance(0.1)
    assert breaker.allow()


def test_trial_that_never_reports_back_does_not_block_forever(
    breaker: CircuitBreaker, clock: FakeClock
) -> None:
    """E.g. the trial request crashed before it could record anything."""
    trip(breaker)
    clock.advance(RESET_AFTER)
    assert breaker.allow()
    clock.advance(RESET_AFTER)
    assert breaker.allow()  # the next caller gets the trial instead


def test_logs_once_when_it_opens_and_once_when_it_closes(
    breaker: CircuitBreaker, clock: FakeClock, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.WARNING, logger="app.breaker"):
        for _ in range(10):  # more failures than the threshold
            breaker.record_failure()
        clock.advance(RESET_AFTER)
        breaker.allow()
        breaker.record_success()
        breaker.record_success()
    assert [r.getMessage() for r in caplog.records] == [
        "Redis failed 3 times in a row, circuit open: skipping it for 5 s",
        "Redis is answering again, circuit closed",
    ]


def test_many_failures_at_once_open_the_circuit_exactly_once(
    breaker: CircuitBreaker, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.WARNING, logger="app.breaker"), ThreadPoolExecutor(50) as pool:
        list(pool.map(lambda _: breaker.record_failure(), range(200)))
    assert breaker.is_open
    assert len(caplog.records) == 1


def test_only_one_of_many_concurrent_callers_gets_the_trial(
    breaker: CircuitBreaker, clock: FakeClock
) -> None:
    """Without the lock, several threads could all see "cool-down over" and
    all hit a Redis that may still be hanging."""
    trip(breaker)
    clock.advance(RESET_AFTER)
    with ThreadPoolExecutor(50) as pool:
        allowed = list(pool.map(lambda _: breaker.allow(), range(200)))
    assert sum(allowed) == 1


@pytest.mark.parametrize(("threshold", "reset_after"), [(0, 5.0), (3, 0.0), (3, -1.0)])
def test_invalid_settings_are_rejected(threshold: int, reset_after: float) -> None:
    with pytest.raises(ValueError, match="must be"):
        CircuitBreaker("Redis", threshold, reset_after)
