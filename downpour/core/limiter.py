"""Token-bucket speed limiting shared across threads."""

from __future__ import annotations

import threading
import time


class RateLimiter:
    """Caps throughput at ``rate`` bytes per second; 0 means unlimited.

    Callers take bytes first and pay afterwards: the bucket may go into debt,
    and the debt is what each caller sleeps off. That keeps the long-run rate
    exact without ever holding the lock while sleeping.
    """

    BURST_SECONDS = 0.5

    def __init__(self, rate: int = 0):
        self._lock = threading.Lock()
        self._rate = max(0, int(rate))
        self._tokens = 0.0
        self._last = time.monotonic()

    @property
    def rate(self) -> int:
        return self._rate

    def set_rate(self, rate: int) -> None:
        with self._lock:
            self._rate = max(0, int(rate))
            self._tokens = 0.0
            self._last = time.monotonic()

    def chunk_hint(self, default: int) -> int:
        """A read size that keeps throttled transfers smooth."""
        rate = self._rate
        if rate <= 0:
            return default
        return max(4096, min(default, rate // 8))

    def reserve(self, nbytes: int) -> float:
        """Take ``nbytes``; return how long the caller must wait to pay for them."""
        with self._lock:
            rate = self._rate
            if rate <= 0:
                return 0.0
            now = time.monotonic()
            self._tokens = min(self._tokens + (now - self._last) * rate, rate * self.BURST_SECONDS)
            self._last = now
            self._tokens -= nbytes
            if self._tokens >= 0:
                return 0.0
            return -self._tokens / rate

    def throttle(self, nbytes: int, stop: threading.Event) -> None:
        delay = self.reserve(nbytes)
        if delay > 0:
            stop.wait(delay)
