"""L1: online statistical baselines with hour-of-day seasonality.

This is the layer that answers "is this unusual *for this network, at this
time of day*". Home and small-office networks have strong diurnal structure
(the evening congestion every ISP customer knows), so a single global mean
would flag every weeknight as an anomaly. Donut (R9) makes the same argument
for seasonal KPI baselines; this is the small, dependency-free version of
that idea, sized for a laptop.

Three things are tracked per feature:

* a global EWMA mean and variance, which adapts quickly after a change
* 24 hour-of-day buckets, used once a bucket has seen enough observations
* a two-sided CUSUM, which catches a sustained small shift that a z-score
  would dismiss as noise

Everything is serialisable, so baselines survive a restart and the agent does
not re-enter its warm-up window every time the user reboots.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from typing import Any

from ..features.pipeline import FeatureFrame
from ..features.schema import FEATURE_BY_NAME, MODELLED_FEATURES, Layer, layer_of

HOURS = 24
#: Observations a seasonal bucket needs before it is trusted over the global
#: baseline. At a 15 second cadence this is roughly ten minutes inside the
#: hour, which one evening of running satisfies.
BUCKET_WARM = 12
#: Upper bound on the EWMA learning rate. Without a cap, a long quiet period
#: followed by a step change would let one sample rewrite the baseline.
MAX_ALPHA = 0.05
MIN_ALPHA = 0.002
CUSUM_K = 0.5
CUSUM_LIMIT = 6.0
#: Z below this is treated as ordinary noise and contributes nothing. Real
#: host metrics (CPU above all) are noisy enough that an undeadbanded z-score
#: flags a quiet network several times an hour.
DEADBAND_Z = 1.5
#: Smoothing applied to each feature's contribution. Degradation that matters
#: persists across samples; sampling noise does not. Roughly a four sample
#: memory.
PERSISTENCE_BETA = 0.3


@dataclass(slots=True)
class OnlineMoments:
    """EWMA mean and variance with a bounded learning rate."""

    count: int = 0
    mean: float = 0.0
    var: float = 0.0

    def update(self, value: float) -> None:
        self.count += 1
        alpha = max(MIN_ALPHA, min(MAX_ALPHA, 1.0 / self.count))
        if self.count == 1:
            self.mean = value
            self.var = 0.0
            return
        previous = self.mean
        self.mean += alpha * (value - previous)
        self.var = (1.0 - alpha) * (self.var + alpha * (value - previous) ** 2)

    @property
    def std(self) -> float:
        return math.sqrt(max(0.0, self.var))

    def to_dict(self) -> dict[str, float]:
        return {"count": self.count, "mean": self.mean, "var": self.var}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> OnlineMoments:
        return cls(
            count=int(data.get("count", 0)),
            mean=float(data.get("mean", 0.0)),
            var=float(data.get("var", 0.0)),
        )


@dataclass(slots=True)
class FeatureBaseline:
    """Global plus seasonal baseline and CUSUM state for one feature."""

    name: str
    higher_is_worse: bool
    global_moments: OnlineMoments = field(default_factory=OnlineMoments)
    hourly: list[OnlineMoments] = field(
        default_factory=lambda: [OnlineMoments() for _ in range(HOURS)]
    )
    cusum_high: float = 0.0
    cusum_low: float = 0.0
    smoothed: float = 0.0

    def reference(self, hour: int) -> OnlineMoments:
        bucket = self.hourly[hour % HOURS]
        if bucket.count >= BUCKET_WARM:
            return bucket
        return self.global_moments

    def spread(self, hour: int) -> float:
        """Standard deviation with a floor proportional to the mean.

        A feature that has been perfectly flat (a LAN RTT pinned at 1 ms) has
        zero variance, and without a floor any wobble would read as an
        enormous anomaly.
        """
        reference = self.reference(hour)
        return max(reference.std, abs(reference.mean) * 0.05, 1e-6)

    def score(self, value: float, hour: int) -> float:
        """Signed z-score, oriented so positive always means worse."""
        reference = self.reference(hour)
        if reference.count < 4:
            return 0.0
        z = (value - reference.mean) / self.spread(hour)
        return z if self.higher_is_worse else -z

    def contribution(self, value: float, hour: int) -> float:
        """Deadbanded anomaly contribution for one observation."""
        return max(0.0, self.score(value, hour) - DEADBAND_Z)

    def update(self, value: float, hour: int) -> float:
        """Fold in an observation and return its smoothed contribution.

        The returned value is smoothed rather than instantaneous so that a
        single noisy sample cannot move the health score. Learning happens
        after scoring, so a frame is always judged against the past.
        """
        oriented = self.score(value, hour)
        instant = max(0.0, oriented - DEADBAND_Z)
        self.smoothed += PERSISTENCE_BETA * (instant - self.smoothed)
        self._update_cusum(oriented)
        self.global_moments.update(value)
        self.hourly[hour % HOURS].update(value)
        return self.smoothed

    def _update_cusum(self, oriented_z: float) -> None:
        self.cusum_high = max(0.0, min(CUSUM_LIMIT, self.cusum_high + oriented_z - CUSUM_K))
        self.cusum_low = max(0.0, min(CUSUM_LIMIT, self.cusum_low - oriented_z - CUSUM_K))

    @property
    def shift_detected(self) -> bool:
        """True when a sustained shift in the bad direction has accumulated."""
        return self.cusum_high >= CUSUM_LIMIT * 0.75

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "higher_is_worse": self.higher_is_worse,
            "global": self.global_moments.to_dict(),
            "hourly": [bucket.to_dict() for bucket in self.hourly],
            "cusum_high": self.cusum_high,
            "cusum_low": self.cusum_low,
            "smoothed": self.smoothed,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> FeatureBaseline:
        hourly = [OnlineMoments.from_dict(item) for item in data.get("hourly", [])]
        while len(hourly) < HOURS:
            hourly.append(OnlineMoments())
        return cls(
            name=str(data["name"]),
            higher_is_worse=bool(data.get("higher_is_worse", True)),
            global_moments=OnlineMoments.from_dict(data.get("global", {})),
            hourly=hourly[:HOURS],
            cusum_high=float(data.get("cusum_high", 0.0)),
            cusum_low=float(data.get("cusum_low", 0.0)),
            smoothed=float(data.get("smoothed", 0.0)),
        )


@dataclass(slots=True)
class L1Result:
    anomaly: float
    per_feature: dict[str, float]
    per_layer: dict[str, float]
    shifted: list[str]
    warm: bool


class L1Baselines:
    """The full L1 stage: one baseline per modelled feature."""

    def __init__(self, warmup_samples: int = 120) -> None:
        self.warmup_samples = warmup_samples
        self.samples_seen = 0
        self.created_at = time.time()
        self.baselines: dict[str, FeatureBaseline] = {
            name: FeatureBaseline(name, FEATURE_BY_NAME[name].higher_is_worse)
            for name in MODELLED_FEATURES
        }

    @property
    def warm(self) -> bool:
        return self.samples_seen >= self.warmup_samples

    def observe(self, frame: FeatureFrame) -> L1Result:
        """Score a frame against the baselines, then fold it in."""
        self.samples_seen += 1
        per_feature: dict[str, float] = {}
        shifted: list[str] = []

        for name, baseline in self.baselines.items():
            value = frame.values.get(name)
            if value is None:
                continue
            contribution = baseline.update(value, frame.hour)
            if contribution > 0.0:
                per_feature[name] = contribution
            if baseline.shift_detected:
                shifted.append(name)

        per_layer = aggregate_by_layer(per_feature)
        return L1Result(
            anomaly=combine(per_feature.values()),
            per_feature=per_feature,
            per_layer=per_layer,
            shifted=shifted,
            warm=self.warm,
        )

    def score_only(self, frame: FeatureFrame) -> dict[str, float]:
        """Score without learning, used when the user pauses learning."""
        scores: dict[str, float] = {}
        for name, baseline in self.baselines.items():
            value = frame.values.get(name)
            if value is None:
                continue
            contribution = baseline.contribution(value, frame.hour)
            if contribution > 0.0:
                scores[name] = contribution
        return scores

    def reset_feature(self, name: str) -> None:
        """Drop one feature's baseline, used by the drift monitor."""
        spec = FEATURE_BY_NAME.get(name)
        if spec is None:
            return
        self.baselines[name] = FeatureBaseline(name, spec.higher_is_worse)

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": 1,
            "samples_seen": self.samples_seen,
            "created_at": self.created_at,
            "warmup_samples": self.warmup_samples,
            "baselines": {name: b.to_dict() for name, b in self.baselines.items()},
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> L1Baselines:
        instance = cls(warmup_samples=int(data.get("warmup_samples", 120)))
        instance.samples_seen = int(data.get("samples_seen", 0))
        instance.created_at = float(data.get("created_at", time.time()))
        for name, payload in data.get("baselines", {}).items():
            if name in instance.baselines:
                instance.baselines[name] = FeatureBaseline.from_dict(payload)
        return instance


def combine(scores: list[float] | Any) -> float:
    """Combine per-feature anomaly scores into one number in [0, 1].

    The worst feature dominates, with a smaller contribution from breadth, so
    one badly broken layer and five mildly odd ones do not score the same.
    """
    values = sorted((value for value in scores if value > 0.01), reverse=True)
    if not values:
        return 0.0
    top = values[0]
    breadth = sum(values[1:4]) / 3.0 if len(values) > 1 else 0.0
    raw = top + 0.35 * breadth
    # Contributions are already deadbanded, so a sustained 3 above the
    # deadband is a firmly abnormal network. Map that onto the top of [0, 1].
    return float(min(1.0, raw / 3.0))


def aggregate_by_layer(per_feature: dict[str, float]) -> dict[str, float]:
    grouped: dict[Layer, list[float]] = {}
    for name, score in per_feature.items():
        grouped.setdefault(layer_of(name), []).append(score)
    return {layer: combine(scores) for layer, scores in grouped.items()}
