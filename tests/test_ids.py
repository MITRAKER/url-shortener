"""Snowflake IDs and the code cipher (app/ids.py). A fake clock stands in for
time, so the tests can cross seconds, and run out of IDs, without waiting."""

import itertools
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import pytest

from app import base62
from app.ids import (
    CODE_LENGTH,
    CODE_SPACE,
    EPOCH,
    EPOCH_SECONDS,
    ID_BITS,
    MAX_INSTANCE_ID,
    MAX_SECOND,
    MAX_SEQUENCE,
    MAX_WAITS,
    ClockError,
    CodeCipher,
    LinkIds,
    RetiredError,
    Snowflake,
    parse_id,
)
from tests.conftest import TEST_CODE_SECRET as SECRET
from tests.fakes import FakeClock

START_SECOND = 1_000  # seconds after 2026-01-01 when each test begins


def make_snowflake(
    instance_id: int = 5, wall: Callable[[], float] = lambda: EPOCH_SECONDS + START_SECOND + 0.25
) -> tuple[Snowflake, FakeClock]:
    clock = FakeClock()
    return Snowflake(instance_id, wall=wall, monotonic=clock, sleep=clock.sleep), clock


def test_every_id_fits_in_a_code() -> None:
    assert 2**ID_BITS <= CODE_SPACE


def test_an_id_is_second_instance_and_sequence_side_by_side() -> None:
    snowflake, _ = make_snowflake(instance_id=5)
    link_id = snowflake.next_id()
    second = START_SECOND + 1  # the first ID comes from the next second, see below
    assert link_id == (second << 16) | (5 << 10) | 0
    parts = parse_id(link_id)
    assert parts.created_at == EPOCH + timedelta(seconds=second)
    assert parts.instance_id == 5
    assert parts.sequence == 0


def test_the_first_id_waits_for_a_fresh_second() -> None:
    """A previous process with the same instance number may have made IDs in
    the current second, so the new one starts in the next second."""
    snowflake, clock = make_snowflake()
    before = clock.now
    assert parse_id(snowflake.next_id()).created_at == EPOCH + timedelta(seconds=START_SECOND + 1)
    assert 0.75 <= clock.now - before < 0.8  # slept the rest of the second, no more


def test_ids_in_one_second_count_up_and_a_new_second_starts_again() -> None:
    snowflake, clock = make_snowflake()
    first = [parse_id(snowflake.next_id()) for _ in range(3)]
    assert [p.sequence for p in first] == [0, 1, 2]
    clock.advance(1)
    assert parse_id(snowflake.next_id()).sequence == 0


def test_after_1024_ids_in_one_second_the_next_waits_for_the_next_second() -> None:
    snowflake, clock = make_snowflake()
    ids = [snowflake.next_id() for _ in range(MAX_SEQUENCE + 1)]
    assert {parse_id(i).created_at for i in ids} == {EPOCH + timedelta(seconds=START_SECOND + 1)}
    before = clock.now
    one_more = parse_id(snowflake.next_id())
    assert one_more.created_at == EPOCH + timedelta(seconds=START_SECOND + 2)
    assert one_more.sequence == 0
    assert 0 < clock.now - before <= 1.01


def test_ids_always_increase() -> None:
    snowflake, clock = make_snowflake()
    ids = []
    for step in range(3000):
        ids.append(snowflake.next_id())
        if step % 700 == 0:
            clock.advance(0.4)
    assert ids == sorted(ids)
    assert len(set(ids)) == len(ids)


def test_setting_the_wall_clock_back_changes_nothing() -> None:
    """Only the monotonic clock is read after startup."""
    wall = FakeClock(EPOCH_SECONDS + START_SECOND)
    snowflake, clock = make_snowflake(wall=wall)
    first = snowflake.next_id()
    wall.advance(-3600)  # NTP, or a person, sets the clock an hour back
    clock.advance(1)
    assert snowflake.next_id() > first


@pytest.mark.parametrize(
    "seconds_after_2026", [-1, MAX_SECOND + 1], ids=["before 2026", "after 2094"]
)
def test_a_clock_outside_the_id_range_stops_the_app(seconds_after_2026: int) -> None:
    with pytest.raises(ClockError, match="out of range"):
        make_snowflake(wall=lambda: EPOCH_SECONDS + seconds_after_2026)


def test_a_clock_that_stopped_is_reported() -> None:
    """Sleeping never moves this clock on, so the wait gives up after a few tries."""
    clock = FakeClock()
    naps: list[float] = []
    stuck = Snowflake(
        0, wall=lambda: EPOCH_SECONDS + START_SECOND, monotonic=clock, sleep=naps.append
    )
    with pytest.raises(ClockError, match="not moving forward"):
        stuck.next_id()
    assert len(naps) == MAX_WAITS


def test_a_retired_snowflake_makes_no_more_ids() -> None:
    """Retired when the instance number was lost: it may be someone else's now."""
    snowflake, _ = make_snowflake()
    snowflake.next_id()
    snowflake.retire()
    with pytest.raises(RetiredError, match="instance number 5"):
        snowflake.next_id()


@pytest.mark.parametrize("instance_id", [-1, MAX_INSTANCE_ID + 1])
def test_instance_id_must_fit_in_6_bits(instance_id: int) -> None:
    with pytest.raises(ValueError, match="instance_id"):
        make_snowflake(instance_id=instance_id)


def test_parse_id_rejects_numbers_that_are_not_ids() -> None:
    for number in (-1, 2**ID_BITS):
        with pytest.raises(ValueError, match="47-bit"):
            parse_id(number)


def test_no_two_ids_are_equal_across_instances_and_threads() -> None:
    """4 instances, each with 4 threads asking at once: 8000 IDs, all different.
    Each instance needs 2000 IDs, so each also runs out of a second's worth."""
    snowflakes = [make_snowflake(instance_id=n)[0] for n in (0, 1, 2, MAX_INSTANCE_ID)]
    with ThreadPoolExecutor(max_workers=16) as pool:
        batches = pool.map(
            lambda s: [s.next_id() for _ in range(500)], [s for s in snowflakes for _ in range(4)]
        )
        ids = [i for batch in batches for i in batch]
    assert len(set(ids)) == len(ids) == 8000


# --- CodeCipher -----------------------------------------------------------

EDGE_NUMBERS = [0, 1, 61, 62, 2**ID_BITS - 1, CODE_SPACE - 1]


def test_codes_are_8_base62_characters() -> None:
    cipher = CodeCipher(SECRET)
    for number in [*EDGE_NUMBERS, *range(1000, 2000)]:
        code = cipher.encode(number)
        assert len(code) == CODE_LENGTH
        assert set(code) <= set(base62.ALPHABET)


def test_decode_undoes_encode() -> None:
    cipher = CodeCipher(SECRET)
    for number in [*EDGE_NUMBERS, *range(5000)]:
        assert cipher.decode(cipher.encode(number)) == number


def test_no_two_numbers_get_the_same_code() -> None:
    cipher = CodeCipher(SECRET)
    codes = {cipher.encode(n) for n in range(20_000)}
    assert len(codes) == 20_000


def test_codes_of_neighbouring_ids_look_unrelated() -> None:
    """Consecutive IDs must not give guessable codes. For random codes, two
    neighbours share their first character 1 time in 62."""
    cipher = CodeCipher(SECRET)
    codes = [cipher.encode(n) for n in range(10_000, 16_200)]
    same_start = sum(a[0] == b[0] for a, b in itertools.pairwise(codes))
    assert same_start < 3 * len(codes) / 62  # about 100 expected
    assert codes != sorted(codes)


def test_without_the_key_the_codes_are_different() -> None:
    mine, theirs = CodeCipher(SECRET), CodeCipher(SECRET + "!")
    same = sum(mine.encode(n) == theirs.encode(n) for n in range(1000))
    assert same == 0


def test_codes_do_not_change_between_versions() -> None:
    """Pins the algorithm: changing it would make clashes with existing codes possible."""
    assert CodeCipher(SECRET).encode(123_456_789) == "ARbDm9D1"


def test_a_short_secret_is_refused() -> None:
    with pytest.raises(ValueError, match="at least 16"):
        CodeCipher("too-short")


@pytest.mark.parametrize("number", [-1, CODE_SPACE])
def test_numbers_outside_the_code_space_are_refused(number: int) -> None:
    with pytest.raises(ValueError, match="62\\*\\*8"):
        CodeCipher(SECRET).encode(number)


@pytest.mark.parametrize("code", ["abc", "abcdefghi", "abc!efgh"])
def test_malformed_codes_are_refused(code: str) -> None:
    with pytest.raises(ValueError, match=r"characters|not a base62"):
        CodeCipher(SECRET).decode(code)


def test_link_ids_hand_out_an_id_with_its_code() -> None:
    snowflake, _ = make_snowflake()
    cipher = CodeCipher(SECRET)
    link_id, code = LinkIds(snowflake, cipher).new()
    assert cipher.decode(code) == link_id
    assert parse_id(link_id).instance_id == snowflake.instance_id
