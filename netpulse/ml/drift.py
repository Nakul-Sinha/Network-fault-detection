"""Concept drift monitoring without labels.

Research.md 5.5 makes drift a first-class concern for this product rather
than an afterthought: home and small-office networks genuinely change. A new
router, a new ISP, a move to a different flat, a firmware update that halves
the RTT. A model that treats every such change as a fault produces a week of
false alarms and then an uninstall.

The monitor follows LEAF (R8) and the label-free drift work (R15): watch the
*distribution* of each feature rather than any individual value, and when it
has genuinely moved, adapt deliberately instead of drifting silently.

Population Stability Index is the measure, chosen because it is cheap, needs
no labels, and has conventional interpretation thresholds that a reviewer
can argue with: below 0.1 no meaningful change, 0.1 to 0.25 moderate, above
0.25 significant.

The adaptation policy is asymmetric, which matters. L1 seasonal baselines and
L2 autoencoders are reset for the drifted feature, because they describe
"normal for this network" and that is what changed. The L3 predictive head is
never touched here: it encodes what degradation looks like in general, and a
network changing its baseline latency is not a reason to forget that.
"""

from __future__ import annotations

import math
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any

from ..features.pipeline import FeatureFrame
from ..features.schema import MODELLED_FEATURES
from ..logging_setup import get_logger
from ..store.models import DriftEvent

log = get_logger(__name__)

#: Observations held in each half of the comparison.
REFERENCE_SIZE = 480  # two hours at a 15 second cadence
RECENT_SIZE = 240  # one hour
BINS = 10
#: Minimum observations in both halves before PSI means anything.
MIN_SAMPLES = 120
#: Smallest gap between two drift decisions for the same feature.
COOLDOWN_S = 1800.0


def population_stability_index(
    reference: list[float], recent: list[float], bins: int = BINS
) -> float:
    """PSI between two samples, using quantile bins from the reference.

    Quantile bins rather than equal-width ones, because network latency
    distributions are heavily skewed and equal-width bins would put almost
    every observation in the first bucket.
    """
    if len(reference) < 2 or len(recent) < 2:
        return 0.0
    ordered = sorted(reference)
    edges: list[float] = []
    for index in range(1, bins):
        position = index * (len(ordered) - 1) / bins
        low = math.floor(position)
        high = min(low + 1, len(ordered) - 1)
        fraction = position - low
        edges.append(ordered[low] * (1 - fraction) + ordered[high] * fraction)
    edges = sorted(set(edges))
    if not edges:
        return 0.0

    def histogram(values: list[float]) -> list[float]:
        counts = [0] * (len(edges) + 1)
        for value in values:
            index = 0
            while index < len(edges) and value > edges[index]:
                index += 1
            counts[index] += 1
        total = float(len(values))
        # A small floor keeps an empty bucket from producing an infinite term.
        return [max(count / total, 1e-4) for count in counts]

    reference_bins = histogram(reference)
    recent_bins = histogram(recent)
    return float(
        sum(
            (actual - expected) * math.log(actual / expected)
            for expected, actual in zip(reference_bins, recent_bins, strict=True)
        )
    )


@dataclass
class FeatureDrift:
    """Reference and recent buffers for one feature."""

    name: str
    reference: deque[float] = field(default_factory=lambda: deque(maxlen=REFERENCE_SIZE))
    recent: deque[float] = field(default_factory=lambda: deque(maxlen=RECENT_SIZE))
    last_event_ts: float = 0.0
    last_psi: float = 0.0

    def push(self, value: float) -> None:
        # The recent buffer feeds the reference as it ages out, so the
        # reference is always "how this network behaved before the last hour".
        if len(self.recent) == self.recent.maxlen:
            self.reference.append(self.recent[0])
        self.recent.append(value)

    def ready(self) -> bool:
        return len(self.reference) >= MIN_SAMPLES and len(self.recent) >= MIN_SAMPLES

    def psi(self) -> float:
        if not self.ready():
            return 0.0
        self.last_psi = population_stability_index(list(self.reference), list(self.recent))
        return self.last_psi

    def accept(self, now: float) -> None:
        """Treat the recent window as the new normal."""
        self.reference.clear()
        self.reference.extend(self.recent)
        self.last_event_ts = now


@dataclass(slots=True)
class DriftDecision:
    feature: str
    psi: float
    action: str
    severity: str

    def to_event(self, ts: float) -> DriftEvent:
        return DriftEvent(ts=ts, feature=self.feature, psi=self.psi, action=self.action)


class DriftMonitor:
    """Watches every modelled feature and decides when to relearn."""

    def __init__(self, threshold: float = 0.25, check_every: int = 60) -> None:
        self.threshold = threshold
        self.check_every = max(1, check_every)
        self.features: dict[str, FeatureDrift] = {
            name: FeatureDrift(name) for name in MODELLED_FEATURES
        }
        self.samples_seen = 0

    def observe(self, frame: FeatureFrame, *, now: float | None = None) -> list[DriftDecision]:
        """Fold a frame in and, periodically, report features that have moved."""
        now = time.time() if now is None else now
        self.samples_seen += 1
        for name, drift in self.features.items():
            value = frame.values.get(name)
            if value is not None:
                drift.push(value)

        if self.samples_seen % self.check_every != 0:
            return []

        decisions: list[DriftDecision] = []
        for name, drift in self.features.items():
            if not drift.ready() or now - drift.last_event_ts < COOLDOWN_S:
                continue
            psi = drift.psi()
            if psi < self.threshold:
                continue
            severity = "significant" if psi >= self.threshold * 2 else "moderate"
            decisions.append(
                DriftDecision(
                    feature=name,
                    psi=psi,
                    action="relearn_baseline",
                    severity=severity,
                )
            )
            drift.accept(now)
            log.info("drift detected on %s (PSI %.3f), relearning its baseline", name, psi)
        return decisions

    def report(self) -> list[dict[str, Any]]:
        return [
            {
                "feature": name,
                "psi": round(drift.last_psi, 4),
                "reference_n": len(drift.reference),
                "recent_n": len(drift.recent),
                "ready": drift.ready(),
            }
            for name, drift in sorted(self.features.items())
            if drift.last_psi > 0.0 or drift.ready()
        ]
