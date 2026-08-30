"""Small cross-cutting runtime primitives used by the submission service.

These implementations deliberately keep the demo self-contained. Production
deployments should move rate-limit, cache, idempotency, and breaker state into
shared infrastructure (for example Redis and the API gateway) so it survives
process restarts and is consistent across replicas.
"""

from __future__ import annotations

import threading
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from typing import Callable, Generic, TypeVar

T = TypeVar("T")


@dataclass(frozen=True)
class RateLimitDecision:
    allowed: bool
    retry_after_seconds: int = 0


class SlidingWindowRateLimiter:
    """Thread-safe, bounded-process sliding-window limiter."""

    def __init__(self) -> None:
        self._hits: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def check(
        self, key: str, *, limit: int, window_seconds: int
    ) -> RateLimitDecision:
        now = time.monotonic()
        cutoff = now - window_seconds
        with self._lock:
            hits = self._hits[key]
            while hits and hits[0] <= cutoff:
                hits.popleft()
            if len(hits) >= limit:
                retry_after = max(1, int(window_seconds - (now - hits[0])) + 1)
                return RateLimitDecision(False, retry_after)
            hits.append(now)
            return RateLimitDecision(True)


class TTLCache(Generic[T]):
    """Minimal lock-protected TTL cache for frequently-read immutable data."""

    def __init__(self, ttl_seconds: float) -> None:
        self.ttl_seconds = ttl_seconds
        self._value: T | None = None
        self._expires_at = 0.0
        self._lock = threading.Lock()

    def get_or_load(self, loader: Callable[[], T]) -> T:
        now = time.monotonic()
        with self._lock:
            if self._value is not None and now < self._expires_at:
                return self._value
            self._value = loader()
            self._expires_at = now + self.ttl_seconds
            return self._value

    def clear(self) -> None:
        with self._lock:
            self._value = None
            self._expires_at = 0.0


class CircuitOpen(RuntimeError):
    """Raised when a dependency circuit is open and calls must fail fast."""


class CircuitBreaker:
    """A compact closed/open/half-open circuit breaker.

    A single successful half-open probe closes the circuit. The implementation
    is sufficient for one demo process; distributed deployments need shared or
    service-mesh state and per-dependency/per-tenant isolation.
    """

    def __init__(self, *, failure_threshold: int = 3, reset_seconds: float = 30):
        self.failure_threshold = failure_threshold
        self.reset_seconds = reset_seconds
        self._failures = 0
        self._opened_at: float | None = None
        self._probe_in_flight = False
        self._lock = threading.Lock()

    @property
    def state(self) -> str:
        with self._lock:
            if self._opened_at is None:
                return "closed"
            if time.monotonic() - self._opened_at >= self.reset_seconds:
                return "half_open"
            return "open"

    def call(self, operation: Callable[[], T]) -> T:
        with self._lock:
            if self._opened_at is not None:
                elapsed = time.monotonic() - self._opened_at
                if elapsed < self.reset_seconds:
                    raise CircuitOpen("dependency circuit is open")
                if self._probe_in_flight:
                    raise CircuitOpen("dependency recovery probe is in flight")
                self._probe_in_flight = True

        try:
            result = operation()
        except Exception:
            with self._lock:
                self._probe_in_flight = False
                self._failures += 1
                if self._failures >= self.failure_threshold:
                    self._opened_at = time.monotonic()
            raise

        with self._lock:
            self._failures = 0
            self._opened_at = None
            self._probe_in_flight = False
        return result


class IdempotencyStore(Generic[T]):
    """Process-local idempotency registry with TTL and conflict detection."""

    def __init__(self, ttl_seconds: float = 3600) -> None:
        self.ttl_seconds = ttl_seconds
        self._items: dict[str, tuple[float, str, T]] = {}
        self._lock = threading.Lock()

    def get(self, key: str, fingerprint: str) -> T | None:
        now = time.monotonic()
        with self._lock:
            item = self._items.get(key)
            if item is None:
                return None
            expires_at, existing_fingerprint, value = item
            if now >= expires_at:
                del self._items[key]
                return None
            if existing_fingerprint != fingerprint:
                raise ValueError("idempotency key was reused with a different request")
            return value

    def put(self, key: str, fingerprint: str, value: T) -> None:
        with self._lock:
            self._items[key] = (
                time.monotonic() + self.ttl_seconds,
                fingerprint,
                value,
            )
