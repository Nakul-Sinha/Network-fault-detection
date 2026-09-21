"""Turn a stream of sparse samples into the frames the model ladder consumes.

Collectors run on different cadences, so a raw sample is always partial: the
system collector fires every 15 seconds while traceroute fires every 10
minutes. The pipeline is what makes those comparable:

1. carry a value forward until it goes stale, so a 10 minute traceroute does
   not leave a hole in every intermediate frame
2. keep rolling windows at 1, 5 and 15 minutes, which is where the lookahead
   signal lives (Research.md 5.3: precursors show up as rising variance and
   slope before the mean moves)
3. compute cross-layer ratios, the features that separate "everything is
   slow" from "only the remote leg is slow"
4. report coverage, so downstream scoring can say "I am judging on half the
   picture" instead of quietly pretending otherwise
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from datetime import datetime

from ..store.models import Sample
from .schema import (
    FEATURE_BY_NAME,
    LAYER_FEATURES,
    MODELLED_FEATURES,
    Layer,
)

#: Windows the pipeline keeps, in seconds. 15 minutes is the longest forecast
#: horizon, so nothing older than that earns its memory.
WINDOWS: tuple[int, ...] = (60, 300, 900)
MAX_WINDOW_S = max(WINDOWS)

#: How long a value stays usable after the collector that produced it last
#: ran. Anything older is dropped rather than carried forward, because a stale
#: RSSI is worse than an absent one.
STALENESS_S: dict[str, float] = {
    "path": 2400.0,  # traceroute cadence is 10 minutes, allow four periods
    "context": 1200.0,
    "remote_https": 420.0,
    "dns": 240.0,
    "wifi": 120.0,
    "gateway": 120.0,
    "os": 120.0,
    "vpn": 120.0,
}
DEFAULT_STALENESS_S = 180.0

#: Aggregates computed per feature per window.
AGGREGATES = ("mean", "std", "slope", "z")


@dataclass(slots=True)
class FeatureFrame:
    """One model-ready view of the network at a point in time."""

    ts: float
    values: dict[str, float] = field(default_factory=dict)
    aggregates: dict[str, float] = field(default_factory=dict)
    derived: dict[str, float] = field(default_factory=dict)
    fresh: set[str] = field(default_factory=set)
    coverage: float = 0.0
    layer_coverage: dict[str, float] = field(default_factory=dict)
    hour: int = 0
    weekday: int = 0
    samples_seen: int = 0

    def get(self, name: str, default: float = 0.0) -> float:
        if name in self.values:
            return self.values[name]
        if name in self.derived:
            return self.derived[name]
        return self.aggregates.get(name, default)

    def vector(self, names: tuple[str, ...]) -> list[float]:
        return [self.get(name) for name in names]

    def has_layer(self, layer: Layer) -> bool:
        return self.layer_coverage.get(layer, 0.0) > 0.0

    def to_dict(self) -> dict[str, object]:
        return {
            "ts": self.ts,
            "coverage": round(self.coverage, 3),
            "layer_coverage": {k: round(v, 3) for k, v in self.layer_coverage.items()},
            "values": {k: round(v, 4) for k, v in sorted(self.values.items())},
            "derived": {k: round(v, 4) for k, v in sorted(self.derived.items())},
            "fresh": sorted(self.fresh),
            "samples_seen": self.samples_seen,
        }


class RollingWindow:
    """Bounded time window with the statistics L1 and L3 need.

    Timestamps and values live in parallel lists with a head offset, so a
    prune is a pointer move rather than a copy, and the buffer is compacted
    only occasionally.

    :meth:`summarise` computes every statistic for every window in a single
    reverse pass using running sums. That structure is the whole reason this
    class exists. The pipeline summarises roughly 25 features on every frame,
    and both obvious implementations are far too expensive: rescanning the
    buffer once per window per statistic costs milliseconds, and pushing the
    work into numpy is worse still, because a 60 element array spends all its
    time in per-call overhead rather than arithmetic.
    """

    __slots__ = ("_head", "_ts", "_values", "max_age_s")

    #: Compact the backing lists once this many dead entries accumulate.
    COMPACT_AFTER = 64

    def __init__(self, max_age_s: float = MAX_WINDOW_S) -> None:
        self.max_age_s = max_age_s
        self._ts: list[float] = []
        self._values: list[float] = []
        self._head = 0

    def push(self, ts: float, value: float) -> None:
        self._ts.append(ts)
        self._values.append(value)
        self.prune(ts)

    def prune(self, now: float) -> None:
        cutoff = now - self.max_age_s
        ts = self._ts
        head = self._head
        size = len(ts)
        while head < size and ts[head] < cutoff:
            head += 1
        self._head = head
        if head >= self.COMPACT_AFTER:
            del self._ts[:head]
            del self._values[:head]
            self._head = 0

    def window(self, now: float, seconds: float) -> list[float]:
        cutoff = now - seconds
        ts = self._ts
        return [self._values[i] for i in range(self._head, len(ts)) if ts[i] >= cutoff]

    def summarise(self, name: str, now: float, current: float, into: dict[str, float]) -> None:
        """Compute every window statistic for this feature in one reverse pass."""
        ts = self._ts
        values = self._values
        head = self._head
        index = len(ts) - 1
        if index < head:
            return

        reference = ts[index]
        count = 0
        sum_v = sum_vv = 0.0
        sum_t = sum_tt = sum_tv = 0.0

        for seconds in WINDOWS:  # ascending, so the scan never rewinds
            cutoff = now - seconds
            while index >= head and ts[index] >= cutoff:
                minutes = (ts[index] - reference) / 60.0
                value = values[index]
                count += 1
                sum_v += value
                sum_vv += value * value
                sum_t += minutes
                sum_tt += minutes * minutes
                sum_tv += minutes * value
                index -= 1
            if count == 0:
                continue

            average = sum_v / count
            if count > 1:
                variance = (sum_vv - count * average * average) / (count - 1)
                spread = math.sqrt(variance) if variance > 0.0 else 0.0
            else:
                spread = 0.0

            label = f"{name}_{seconds // 60}m"
            into[f"{label}_mean"] = average
            into[f"{label}_std"] = spread
            if seconds < 300:
                continue

            slope = 0.0
            if count >= 3:
                t_mean = sum_t / count
                denominator = sum_tt - count * t_mean * t_mean
                if denominator > 1e-9:
                    slope = (sum_tv - count * t_mean * average) / denominator
            into[f"{label}_slope"] = slope

            if count >= 4:
                # A spread floor keeps a perfectly flat feature from turning a
                # rounding wobble into an enormous z-score.
                floor = max(1e-6, abs(average) * 0.02)
                into[f"{label}_z"] = (current - average) / max(spread, floor)
            else:
                into[f"{label}_z"] = 0.0

    def last(self) -> float | None:
        return self._values[-1] if len(self._values) > self._head else None

    def clear(self) -> None:
        self._ts.clear()
        self._values.clear()
        self._head = 0

    def __len__(self) -> int:
        return len(self._ts) - self._head


def mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def stddev(values: list[float]) -> float:
    """Sample standard deviation, used by tests and the drift monitor."""
    if len(values) < 2:
        return 0.0
    average = mean(values)
    variance = sum((value - average) ** 2 for value in values) / (len(values) - 1)
    return math.sqrt(max(0.0, variance))


def slope_per_minute(points: list[tuple[float, float]]) -> float:
    """Least-squares slope in units per minute.

    A rising slope with a still-normal mean is the early-degradation signal
    from Research.md 5.3, so this is a first-class feature rather than a
    smoothing detail.
    """
    if len(points) < 3:
        return 0.0
    t0 = points[0][0]
    xs = [(ts - t0) / 60.0 for ts, _ in points]
    ys = [value for _, value in points]
    x_mean = mean(xs)
    y_mean = mean(ys)
    denominator = sum((x - x_mean) ** 2 for x in xs)
    if denominator <= 1e-9:
        return 0.0
    numerator = sum((x - x_mean) * (y - y_mean) for x, y in zip(xs, ys, strict=True))
    return numerator / denominator


def robust_z(value: float, values: list[float]) -> float:
    """Z-score against the window, using a spread floor.

    A perfectly flat window would otherwise divide by zero and turn a one
    millisecond wobble into an infinite anomaly.
    """
    if len(values) < 4:
        return 0.0
    centre = mean(values)
    spread = stddev(values)
    floor = max(1e-6, abs(centre) * 0.02)
    return (value - centre) / max(spread, floor)


class FeaturePipeline:
    """Stateful assembler: sparse samples in, dense frames out."""

    def __init__(self, *, staleness: dict[str, float] | None = None) -> None:
        self._windows: dict[str, RollingWindow] = {
            name: RollingWindow() for name in MODELLED_FEATURES
        }
        self._last_value: dict[str, tuple[float, float]] = {}
        self._staleness = dict(STALENESS_S)
        if staleness:
            self._staleness.update(staleness)
        self.samples_seen = 0

    # ------------------------------------------------------------ ingestion

    def push(self, sample: Sample) -> FeatureFrame:
        """Fold one sample into the state and return the resulting frame."""
        now = sample.ts or time.time()
        self.samples_seen += 1
        fresh = set(sample.values)

        for name, value in sample.values.items():
            self._last_value[name] = (now, value)
            window = self._windows.get(name)
            if window is not None:
                window.push(now, value)

        values = self._carry_forward(now)
        frame = FeatureFrame(
            ts=now,
            values=values,
            fresh=fresh,
            samples_seen=self.samples_seen,
        )
        frame.aggregates = self._aggregates(now, values)
        frame.derived = derive_cross_layer(values)
        frame.coverage, frame.layer_coverage = self._coverage(values)
        local = datetime.fromtimestamp(now)
        frame.hour = local.hour
        frame.weekday = local.weekday()
        frame.values.setdefault("hour_of_day", float(frame.hour))
        frame.values.setdefault("day_of_week", float(frame.weekday))
        return frame

    def _carry_forward(self, now: float) -> dict[str, float]:
        values: dict[str, float] = {}
        for name, (ts, value) in list(self._last_value.items()):
            spec = FEATURE_BY_NAME.get(name)
            limit = self._staleness.get(spec.layer if spec else "", DEFAULT_STALENESS_S)
            if now - ts > limit:
                del self._last_value[name]
                continue
            values[name] = value
        return values

    def _aggregates(self, now: float, values: dict[str, float]) -> dict[str, float]:
        aggregates: dict[str, float] = {}
        for name, window in self._windows.items():
            if name not in values or len(window) == 0:
                continue
            window.prune(now)
            window.summarise(name, now, values[name], aggregates)
        return aggregates

    def _coverage(self, values: dict[str, float]) -> tuple[float, dict[str, float]]:
        per_layer: dict[str, float] = {}
        for layer, names in LAYER_FEATURES.items():
            modelled = [name for name in names if name in MODELLED_FEATURES]
            if not modelled:
                continue
            present = sum(1 for name in modelled if name in values)
            per_layer[layer] = present / len(modelled)
        overall = (
            sum(1 for name in MODELLED_FEATURES if name in values) / len(MODELLED_FEATURES)
            if MODELLED_FEATURES
            else 0.0
        )
        return overall, per_layer

    # --------------------------------------------------------------- access

    def window_values(self, name: str, seconds: float, now: float | None = None) -> list[float]:
        window = self._windows.get(name)
        if window is None:
            return []
        return window.window(time.time() if now is None else now, seconds)

    def warm(self, minimum_samples: int) -> bool:
        return self.samples_seen >= minimum_samples

    def reset(self) -> None:
        for window in self._windows.values():
            window.clear()
        self._last_value.clear()
        self.samples_seen = 0


# ------------------------------------------------------------ cross-layer


def derive_cross_layer(values: dict[str, float]) -> dict[str, float]:
    """Ratios and differences that isolate which leg of the path is slow.

    These are the features that make attribution possible at all. A rising
    ``https_ttfb`` on its own means nothing; a rising ``https_ttfb`` with a
    flat ``gw_rtt`` means the trouble is past the router, which is precisely
    the distinction PRD 8.2 asks for.
    """
    derived: dict[str, float] = {}

    gateway = values.get("gw_rtt_ms")
    path = values.get("path_rtt_ms")
    ttfb = values.get("https_ttfb_ms")
    dns_p50 = values.get("dns_p50_ms")
    tcp = values.get("https_tcp_ms")
    tls = values.get("https_tls_ms")

    if ttfb is not None and gateway is not None:
        derived["x_remote_over_gateway"] = ttfb / max(gateway, 1.0)
    if path is not None and gateway is not None:
        # How much latency the ISP leg adds over the LAN leg.
        derived["x_path_minus_gateway_ms"] = max(0.0, path - gateway)
    if ttfb is not None and path is not None:
        derived["x_remote_minus_path_ms"] = max(0.0, ttfb - path)
    if dns_p50 is not None and ttfb is not None:
        derived["x_dns_share"] = dns_p50 / max(dns_p50 + ttfb, 1.0)
    if tcp is not None and tls is not None and tcp + tls > 0:
        derived["x_tls_share"] = tls / (tcp + tls)

    rssi = values.get("wifi_rssi_dbm")
    retries = values.get("wifi_tx_retry_rate")
    if rssi is not None:
        # 0 at -40 dBm or better, 1 at -90 dBm or worse.
        derived["x_wifi_weakness"] = min(1.0, max(0.0, (-rssi - 40.0) / 50.0))
        if retries is not None:
            derived["x_wifi_pressure"] = derived["x_wifi_weakness"] * 0.5 + retries * 0.5
    link = values.get("wifi_link_mbps")
    if link is not None and link > 0:
        derived["x_link_headroom"] = min(1.0, link / 300.0)

    loss = values.get("gw_loss_rate")
    dns_fail = values.get("dns_fail_rate")
    https_fail = values.get("https_fail_rate")
    failures = [value for value in (loss, dns_fail, https_fail) if value is not None]
    if failures:
        derived["x_failure_pressure"] = max(failures)
        derived["x_failure_breadth"] = sum(1 for value in failures if value > 0.1) / len(failures)

    return derived


#: Names of every derived feature, so the model input layout is fixed even
#: when a given frame cannot compute all of them.
DERIVED_FEATURES: tuple[str, ...] = (
    "x_remote_over_gateway",
    "x_path_minus_gateway_ms",
    "x_remote_minus_path_ms",
    "x_dns_share",
    "x_tls_share",
    "x_wifi_weakness",
    "x_wifi_pressure",
    "x_link_headroom",
    "x_failure_pressure",
    "x_failure_breadth",
)

#: Layer a derived feature points at, used by RCA attribution.
DERIVED_LAYER: dict[str, Layer] = {
    "x_remote_over_gateway": "remote_https",
    "x_path_minus_gateway_ms": "path",
    "x_remote_minus_path_ms": "remote_https",
    "x_dns_share": "dns",
    "x_tls_share": "remote_https",
    "x_wifi_weakness": "wifi",
    "x_wifi_pressure": "wifi",
    "x_link_headroom": "wifi",
    "x_failure_pressure": "gateway",
    "x_failure_breadth": "gateway",
}
