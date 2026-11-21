"""Distributed IDs: unique without asking anyone, unguessable without the key.

Until step 2, each new code was random, and the database rejected the rare
duplicate (insert and retry). Step 3 makes codes that cannot collide in the
first place, even with many app instances, and without them talking to each
other or to the database first. Two parts:

1. `Snowflake` makes 47-bit IDs (the idea comes from Twitter's Snowflake).
   An ID is three numbers side by side:

       | 31 bits: seconds since 2026 | 6 bits: instance | 10 bits: sequence |

   No two running instances share an instance number (app/instances.py
   leases them), and one instance never repeats a (second, sequence) pair.
   So no two IDs are ever equal.

2. `CodeCipher` scrambles an ID into an 8-character code with a secret key.
   Unscrambled, codes would count up, and anyone could guess the links other
   people just made. Scrambling is a permutation: every ID gets its own code,
   so codes are exactly as unique as the IDs.
"""

import hashlib
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Final

from app import base62

# --- The ID layout -------------------------------------------------------
EPOCH: Final = datetime(2026, 1, 1, tzinfo=UTC)  # second 0 of our clock
EPOCH_SECONDS: Final = EPOCH.timestamp()
TIME_BITS: Final = 31  # 2**31 seconds = 68 years: IDs run out in 2094
INSTANCE_BITS: Final = 6  # up to 64 instances
SEQUENCE_BITS: Final = 10  # up to 1024 IDs per second per instance
ID_BITS: Final = TIME_BITS + INSTANCE_BITS + SEQUENCE_BITS  # 47
MAX_INSTANCE_ID: Final = 2**INSTANCE_BITS - 1  # 63
MAX_SEQUENCE: Final = 2**SEQUENCE_BITS - 1  # 1023
MAX_SECOND: Final = 2**TIME_BITS - 1
# Waiting for the next second takes one sleep; allow a few (NASA/JPL rule 2).
MAX_WAITS: Final = 3
# Wake up just after the second changes, not just before it.
SLEEP_MARGIN_SECONDS: Final = 0.001

# --- The code format -----------------------------------------------------
CODE_LENGTH: Final = 8
HALF_SPACE: Final[int] = base62.BASE ** (CODE_LENGTH // 2)  # 62**4: one half of a code
CODE_SPACE: Final[int] = HALF_SPACE**2  # 62**8, about 2.2e14: every possible code
ROUNDS: Final = 10  # as in FF1, NIST's format-preserving encryption
MIN_SECRET_LENGTH: Final = 16
# 2**47 IDs fit in 62**8 codes, so every ID gets a code of its own
# (tests/test_ids.py checks this).


class ClockError(Exception):
    """The clock is outside what the ID layout can represent, or stopped."""


class RetiredError(Exception):
    """This Snowflake was retired: its instance number may be someone else's now."""


class Snowflake:
    """Makes IDs that are unique across all instances.

    Thread-safe: requests run on many threads, so state changes under a lock.
    """

    def __init__(
        self,
        instance_id: int,
        wall: Callable[[], float] = time.time,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if not 0 <= instance_id <= MAX_INSTANCE_ID:
            raise ValueError(f"instance_id must be 0 to {MAX_INSTANCE_ID}, got {instance_id}")
        self.instance_id = instance_id
        self._monotonic = monotonic
        self._sleep = sleep
        # The wall clock is read only once, here. After that, time moves with
        # the monotonic clock, which never goes backwards, even when NTP or a
        # person sets the wall clock back. A second that ran twice could
        # repeat IDs.
        self._offset = wall() - EPOCH_SECONDS - monotonic()
        self._lock = threading.Lock()
        # Start as if this second were used up, so the first ID comes from the
        # next one: an earlier process with our instance number may have made
        # IDs in this very second, just before it stopped.
        self._second = self._current_second()
        self._sequence = MAX_SEQUENCE
        self._retired = False

    def _seconds(self) -> float:
        return self._offset + self._monotonic()

    def _current_second(self) -> int:
        seconds = self._seconds()
        if not 0 <= seconds <= MAX_SECOND:
            raise ClockError(f"the clock reads {seconds:.0f} s after 2026-01-01, out of range")
        return int(seconds)

    def retire(self) -> None:
        """Make no more IDs, for good: the instance number was lost (app/instances.py)."""
        with self._lock:
            self._retired = True

    def next_id(self) -> int:
        with self._lock:
            if self._retired:
                raise RetiredError(f"instance number {self.instance_id} was given up")
            second = self._current_second()
            if second > self._second:
                self._second, self._sequence = second, 0
            elif self._sequence < MAX_SEQUENCE:
                self._sequence += 1
            else:
                # 1024 IDs in one second: wait for the next second (under 1 s).
                self._second, self._sequence = self._wait_for_next_second(), 0
            return (
                (self._second << (INSTANCE_BITS + SEQUENCE_BITS))
                | (self.instance_id << SEQUENCE_BITS)
                | self._sequence
            )

    def _wait_for_next_second(self) -> int:
        for _ in range(MAX_WAITS):  # bounded (NASA/JPL rule 2)
            self._sleep(max(0.0, self._second + 1 - self._seconds()) + SLEEP_MARGIN_SECONDS)
            second = self._current_second()
            if second > self._second:
                return second
        raise ClockError("the clock is not moving forward")


@dataclass(frozen=True)
class IdParts:
    """What an ID is made of. Handy for debugging: when, and on which instance?"""

    created_at: datetime
    instance_id: int
    sequence: int


def parse_id(link_id: int) -> IdParts:
    if not 0 <= link_id < 2**ID_BITS:
        raise ValueError(f"not a {ID_BITS}-bit ID: {link_id}")
    second = link_id >> (INSTANCE_BITS + SEQUENCE_BITS)
    instance_id = (link_id >> SEQUENCE_BITS) & MAX_INSTANCE_ID
    sequence = link_id & MAX_SEQUENCE
    return IdParts(EPOCH + timedelta(seconds=second), instance_id, sequence)


class CodeCipher:
    """Turns a number into an 8-character code and back, with a secret key.

    It is a Feistel network, the structure inside the DES cipher and inside
    NIST's format-preserving encryption (FF1). Split the number into two
    halves. In each round, add a keyed hash of one half to the other, then
    swap them. Every round can be undone, so the whole thing can be too: it
    is a permutation, and no two numbers ever get the same code.
    """

    def __init__(self, secret: str) -> None:
        if len(secret) < MIN_SECRET_LENGTH:
            raise ValueError(f"the secret must be at least {MIN_SECRET_LENGTH} characters")
        # A secret of any length becomes a 32-byte key.
        self._key = hashlib.sha256(secret.encode()).digest()

    def _round(self, round_number: int, half: int) -> int:
        """A keyed hash of one half, as a number below HALF_SPACE."""
        data = bytes([round_number]) + half.to_bytes(4, "big")
        digest = hashlib.blake2b(data, digest_size=8, key=self._key).digest()
        return int.from_bytes(digest, "big") % HALF_SPACE

    def encode(self, number: int) -> str:
        if not 0 <= number < CODE_SPACE:
            raise ValueError(f"number must be 0 to 62**8 - 1, got {number}")
        left, right = divmod(number, HALF_SPACE)
        for round_number in range(ROUNDS):
            left, right = right, (left + self._round(round_number, right)) % HALF_SPACE
        return base62.encode(left * HALF_SPACE + right).rjust(CODE_LENGTH, base62.ALPHABET[0])

    def decode(self, code: str) -> int:
        """The number behind a code: `encode`, run backwards."""
        if len(code) != CODE_LENGTH:
            raise ValueError(f"a code has {CODE_LENGTH} characters, got {len(code)}")
        left, right = divmod(base62.decode(code), HALF_SPACE)
        for round_number in reversed(range(ROUNDS)):
            left, right = (right - self._round(round_number, left)) % HALF_SPACE, left
        return left * HALF_SPACE + right


@dataclass(frozen=True)
class LinkIds:
    """Hands out what a new link needs: a unique ID, and its code."""

    snowflake: Snowflake
    cipher: CodeCipher

    def new(self) -> tuple[int, str]:
        link_id = self.snowflake.next_id()
        return link_id, self.cipher.encode(link_id)
