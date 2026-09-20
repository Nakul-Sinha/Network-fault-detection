"""Collector contract and shared result types.

A collector is a small, restartable unit that turns one layer of the network
into features. The contract is deliberately narrow so that the scheduler can
treat every collector identically and so that a collector crash degrades one
layer rather than the agent (PRD N8).
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field

from ..config import NetPulseConfig
from ..core.ratelimit import ProbeBudget
from ..features.schema import FEATURE_BY_NAME, Layer, clip
from ..logging_setup import get_logger
from ..store.models import ProbeRecord

log = get_logger(__name__)


@dataclass(slots=True)
class Capability:
    """Whether this collector can run here, and why not when it cannot."""

    supported: bool
    reason: str = ""
    needs_privilege: bool = False

    def as_dict(self) -> dict[str, object]:
        return {
            "supported": self.supported,
            "reason": self.reason,
            "needs_privilege": self.needs_privilege,
        }


@dataclass(slots=True)
class CollectorResult:
    """Output of one collection round."""

    values: dict[str, float] = field(default_factory=dict)
    probes: list[ProbeRecord] = field(default_factory=list)
    ok: bool = True
    error: str = ""
    skipped_reason: str = ""

    def set(self, name: str, value: float | None) -> None:
        """Record a feature, clamped to its declared physical range."""
        if value is None:
            return
        try:
            numeric = float(value)
        except (TypeError, ValueError):
            return
        if numeric != numeric or numeric in (float("inf"), float("-inf")):
            return
        if name not in FEATURE_BY_NAME:
            raise KeyError(f"{name!r} is not part of the feature schema")
        self.values[name] = clip(name, numeric)

    def probe(
        self,
        collector: str,
        target: str,
        ok: bool,
        duration_ms: float | None = None,
        detail: str = "",
    ) -> None:
        self.probes.append(
            ProbeRecord(
                ts=time.time(),
                collector=collector,
                target=target,
                ok=ok,
                duration_ms=duration_ms,
                detail=detail,
            )
        )


class Collector(ABC):
    """Base class for every layer collector.

    Subclasses implement :meth:`_collect`. The public :meth:`collect` wraps it
    so an unexpected exception becomes a failed result with an error string,
    never a propagating crash.
    """

    #: Stable identifier, also the probe-ledger collector name.
    name: str = "collector"
    #: Layer this collector feeds. Used for capability reporting and RCA.
    layer: Layer = "os"
    #: Whether the collector puts traffic on the wire. Active collectors are
    #: silenced by the pause switch and metered by the probe budget.
    active: bool = False

    def __init__(self, config: NetPulseConfig, budget: ProbeBudget | None = None) -> None:
        self.config = config
        self.budget = budget
        self._capability: Capability | None = None
        self.consecutive_failures = 0
        self.last_run: float | None = None
        self.last_error: str = ""

    # ------------------------------------------------------------- contract

    @abstractmethod
    def interval_s(self) -> float:
        """Nominal seconds between runs, before jitter and battery backoff."""

    @abstractmethod
    def enabled(self) -> bool:
        """Whether the operator has this layer switched on."""

    @abstractmethod
    def _collect(self) -> CollectorResult:
        """Do the work. May raise; the wrapper contains it."""

    def detect_capability(self) -> Capability:
        """Probe the platform once and cache the answer."""
        return Capability(True)

    # ---------------------------------------------------------------- public

    @property
    def capability(self) -> Capability:
        if self._capability is None:
            try:
                self._capability = self.detect_capability()
            except Exception as exc:
                self._capability = Capability(False, f"capability check failed: {exc}")
        return self._capability

    def refresh_capability(self) -> Capability:
        """Re-run detection, for example after a VPN or adapter change."""
        self._capability = None
        return self.capability

    def collect(self) -> CollectorResult:
        self.last_run = time.time()
        if not self.enabled():
            return CollectorResult(ok=True, skipped_reason="disabled by configuration")
        capability = self.capability
        if not capability.supported:
            return CollectorResult(ok=True, skipped_reason=capability.reason or "unsupported")
        if self.active and self.budget is not None and self.budget.paused:
            return CollectorResult(ok=True, skipped_reason="probing paused")
        try:
            result = self._collect()
        except Exception as exc:
            self.consecutive_failures += 1
            self.last_error = str(exc)
            log.warning(
                "collector %s failed (%d consecutive): %s",
                self.name,
                self.consecutive_failures,
                exc,
            )
            return CollectorResult(ok=False, error=str(exc))
        if result.ok:
            self.consecutive_failures = 0
            self.last_error = ""
        else:
            self.consecutive_failures += 1
            self.last_error = result.error
        return result

    def status(self) -> dict[str, object]:
        return {
            "name": self.name,
            "layer": self.layer,
            "active_probe": self.active,
            "enabled": self.enabled(),
            "interval_s": self.interval_s(),
            "capability": self.capability.as_dict(),
            "last_run": self.last_run,
            "consecutive_failures": self.consecutive_failures,
            "last_error": self.last_error,
        }


def percentile(values: list[float], fraction: float) -> float:
    """Nearest-rank percentile over a small sample, without pulling in numpy."""
    if not values:
        raise ValueError("percentile of an empty sample")
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    index = max(0, min(len(ordered) - 1, round(fraction * (len(ordered) - 1))))
    return ordered[int(index)]


def mean_absolute_deviation(values: list[float]) -> float:
    if len(values) < 2:
        return 0.0
    average = sum(values) / len(values)
    return sum(abs(value - average) for value in values) / len(values)
