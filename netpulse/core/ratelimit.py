"""Token-bucket probe budgets.

PRD N5 requires probe rates to be enforced in code, not merely implied by the
configured cadence. Every collector that puts a packet on the wire asks a
bucket for permission first; a denied request is recorded so the UI can show
"the budget stopped this probe" rather than silently skipping it.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field


@dataclass
class TokenBucket:
    """Classic token bucket with a wall-clock refill.

    ``capacity`` doubles as the burst allowance, so a collector that has been
    idle can fire its whole per-period allocation at once and then wait.
    """

    capacity: float
    refill_per_second: float
    tokens: float = field(default=0.0)
    granted: int = 0
    denied: int = 0
    _last: float = field(default_factory=time.monotonic)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def __post_init__(self) -> None:
        if self.capacity <= 0:
            raise ValueError("capacity must be positive")
        if self.refill_per_second <= 0:
            raise ValueError("refill_per_second must be positive")
        if not self.tokens:
            self.tokens = self.capacity

    @classmethod
    def per_minute(cls, count: int) -> TokenBucket:
        return cls(capacity=float(count), refill_per_second=count / 60.0)

    @classmethod
    def per_hour(cls, count: int) -> TokenBucket:
        return cls(capacity=float(count), refill_per_second=count / 3600.0)

    def _refill(self, now: float) -> None:
        elapsed = max(0.0, now - self._last)
        self._last = now
        self.tokens = min(self.capacity, self.tokens + elapsed * self.refill_per_second)

    def try_acquire(self, amount: float = 1.0, *, now: float | None = None) -> bool:
        """Take ``amount`` tokens if available. Never blocks."""
        with self._lock:
            self._refill(time.monotonic() if now is None else now)
            if self.tokens >= amount:
                self.tokens -= amount
                self.granted += 1
                return True
            self.denied += 1
            return False

    def available(self, *, now: float | None = None) -> float:
        with self._lock:
            self._refill(time.monotonic() if now is None else now)
            return self.tokens

    def stats(self) -> dict[str, float]:
        return {
            "capacity": self.capacity,
            "refill_per_second": round(self.refill_per_second, 4),
            "tokens": round(self.available(), 2),
            "granted": self.granted,
            "denied": self.denied,
        }


class ProbeBudget:
    """The set of buckets the agent enforces, plus a global pause switch.

    ``pause()`` is F9: it makes every acquisition fail, so a paused agent
    provably emits nothing, and the probe counter in the UI stops moving.
    """

    def __init__(
        self,
        *,
        https_per_minute: int,
        dns_per_minute: int,
        icmp_per_minute: int,
        traceroute_per_hour: int,
    ) -> None:
        self.buckets: dict[str, TokenBucket] = {
            "https": TokenBucket.per_minute(https_per_minute),
            "dns": TokenBucket.per_minute(dns_per_minute),
            "icmp": TokenBucket.per_minute(icmp_per_minute),
            "traceroute": TokenBucket.per_hour(traceroute_per_hour),
        }
        self._paused = False
        self._lock = threading.Lock()

    def acquire(self, kind: str, amount: float = 1.0) -> bool:
        if self.paused:
            return False
        bucket = self.buckets.get(kind)
        if bucket is None:
            return True
        return bucket.try_acquire(amount)

    def pause(self) -> None:
        with self._lock:
            self._paused = True

    def resume(self) -> None:
        with self._lock:
            self._paused = False

    @property
    def paused(self) -> bool:
        with self._lock:
            return self._paused

    def stats(self) -> dict[str, object]:
        return {
            "paused": self.paused,
            "buckets": {name: bucket.stats() for name, bucket in self.buckets.items()},
            "total_granted": sum(b.granted for b in self.buckets.values()),
            "total_denied": sum(b.denied for b in self.buckets.values()),
        }
