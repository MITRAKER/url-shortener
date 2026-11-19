"""A circuit breaker: stop calling something that keeps failing, try again later.

It works like the breaker in a fuse box. When a dependency fails several times
in a row, the breaker "opens": calls skip the dependency at once, instead of
each one waiting for a timeout. After a cool-down, one trial call goes through.
If it works, the breaker closes again; if not, it stays open for another
cool-down.

    closed --(N failures in a row)--> open --(cool-down)--> half-open
      ^                                                         |
      +--------------------(the trial call works)---------------+
"""

import logging
import threading
import time
from collections.abc import Callable

from app import metrics

log = logging.getLogger(__name__)


class CircuitBreaker:
    def __init__(
        self,
        name: str,
        failure_threshold: int,
        reset_after: float,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if failure_threshold < 1:
            raise ValueError("failure_threshold must be at least 1")
        if reset_after <= 0:
            raise ValueError("reset_after must be positive")
        self.name = name
        self.failure_threshold = failure_threshold
        self.reset_after = reset_after
        self._clock = clock
        # Requests run on many threads at once, so every change is made under a lock.
        self._lock = threading.Lock()
        self._failures = 0  # failures in a row, capped at failure_threshold
        self._blocked_until = 0.0  # while open, calls are skipped until this time
        metrics.CIRCUIT_OPEN.labels(name).set(0)
        metrics.CIRCUIT_OPENED.labels(name)  # 0 until it first opens

    @property
    def is_open(self) -> bool:
        with self._lock:
            return self._failures >= self.failure_threshold

    def allow(self) -> bool:
        """May the caller use the dependency right now?"""
        with self._lock:
            now = self._clock()
            if now < self._blocked_until:
                return False  # open: fail fast, don't wait for a timeout
            if self._failures >= self.failure_threshold:
                # Half-open: this caller makes the trial call. Everyone else
                # keeps skipping until it reports back or the cool-down ends.
                self._blocked_until = now + self.reset_after
            return True

    def record_success(self) -> None:
        with self._lock:
            was_open = self._failures >= self.failure_threshold
            self._failures = 0
            self._blocked_until = 0.0
        if was_open:
            metrics.CIRCUIT_OPEN.labels(self.name).set(0)
            log.warning("%s is answering again, circuit closed", self.name)

    def record_failure(self) -> None:
        with self._lock:
            just_opened = self._failures == self.failure_threshold - 1
            self._failures = min(self._failures + 1, self.failure_threshold)
            if self._failures >= self.failure_threshold:
                self._blocked_until = self._clock() + self.reset_after
        if just_opened:
            metrics.CIRCUIT_OPEN.labels(self.name).set(1)
            metrics.CIRCUIT_OPENED.labels(self.name).inc()
            log.warning(
                "%s failed %d times in a row, circuit open: skipping it for %.0f s",
                self.name,
                self.failure_threshold,
                self.reset_after,
            )
