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
from collections import deque
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

    Stores ``(timestamp, value)`` pairs and prunes by age rather than by
    count, because the sampling cadence changes with battery state.
    """

    __slots__ = ("_points", "max_age_s")

    def __init__(self, max_age_s: float = MAX_WINDOW_S) -> None:
        self.max_age_s = max_age_s
        self._points: deque[tuple[float, float]] = deque()

    def push(self, ts: float, value: float) -> None:
        self._points.append((ts, value))
        self.prune(ts)

    def prune(self, now: float) -> None:
        cutoff = now - self.max_age_s
        while self._points and self._points[0][0] < cutoff:
            self._points.popleft()

    def window(self, now: float, seconds: float) -> list[float]:
        cutoff = now - seconds
        return [value for ts, value in self._points if ts >= cutoff]

    def pairs(self, now: float, seconds: float) -> list[tuple[float, float]]:
        cutoff = now - seconds
        return [(ts, value) for ts, value in self._points if ts >= cutoff]

    def last(self) -> float | None:
        return self._points[-1][1] if self._points else None

    def __len__(self) -> int:
        return len(self._points)


def mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def stddev(values: list[float]) -> float:
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
            current = values[name]
            for seconds in WINDOWS:
                sample = window.window(now, seconds)
                if not sample:
                    continue
                label = f"{name}_{seconds // 60}m"
                aggregates[f"{label}_mean"] = mean(sample)
                aggregates[f"{label}_std"] = stddev(sample)
                if seconds >= 300:
                    aggregates[f"{label}_slope"] = slope_per_minute(window.pairs(now, seconds))
                    aggregates[f"{label}_z"] = robust_z(current, sample)
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
            window._points.clear()
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
