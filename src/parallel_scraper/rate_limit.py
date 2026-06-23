"""Thread-safe token bucket rate limiter."""
from __future__ import annotations

import threading
import time


class TokenBucket:
    """Leaky-bucket rate limiter.

    Tokens refill at `rate` per second up to `capacity`. Threads call
    `acquire(n)` and block (with optional timeout) until n tokens are
    available, then atomically deduct them.
    """

    def __init__(self, rate: float, capacity: int) -> None:
        if rate <= 0:
            raise ValueError("rate must be > 0")
        if capacity < 1:
            raise ValueError("capacity must be >= 1")
        self._rate = float(rate)
        self._capacity = float(capacity)
        self._tokens = float(capacity)
        self._last_refill = time.monotonic()
        self._cv = threading.Condition()

    def _refill_locked(self) -> None:
        now = time.monotonic()
        delta = now - self._last_refill
        if delta > 0:
            self._tokens = min(self._capacity, self._tokens + delta * self._rate)
            self._last_refill = now

    def acquire(self, n: int = 1, timeout: float | None = None) -> bool:
        """Block until n tokens available, then deduct. Returns True on success,
        False if timeout elapsed."""
        if n <= 0:
            return True
        deadline = None if timeout is None else time.monotonic() + timeout
        with self._cv:
            while True:
                self._refill_locked()
                if self._tokens >= n:
                    self._tokens -= n
                    return True
                deficit = n - self._tokens
                wait_s = deficit / self._rate
                if deadline is not None:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        return False
                    wait_s = min(wait_s, remaining)
                self._cv.wait(timeout=wait_s)

    def try_acquire(self, n: int = 1) -> bool:
        with self._cv:
            self._refill_locked()
            if self._tokens >= n:
                self._tokens -= n
                return True
            return False

    def set_rate(self, rate: float, capacity: float | None = None) -> None:
        """Adjust the refill rate (and optionally capacity) at runtime.

        Used by the key manager to scale discovery throughput to the number
        of currently-active API keys. Wakes any waiters so a rate increase
        takes effect immediately.
        """
        rate = max(0.1, float(rate))
        with self._cv:
            self._refill_locked()  # settle tokens at the old rate first
            self._rate = rate
            if capacity is not None:
                self._capacity = max(1.0, float(capacity))
                self._tokens = min(self._tokens, self._capacity)
            self._cv.notify_all()

    @property
    def rate(self) -> float:
        return self._rate
