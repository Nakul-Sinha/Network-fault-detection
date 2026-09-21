"""Fault-injection corpus: synthetic but realistic multi-layer sample streams.

PRD 13.1 asks for a fault-injection lab and PRD 8.3 wants CI gates that
replay it. Real netem and DNS blackhole containers are the right tool for
release validation on Linux hosts, but they cannot run inside a unit test on
a developer laptop or on a Windows CI runner. This module is the portable
half: it generates the sample streams those faults would produce, with the
properties that actually matter for evaluation.

* every fault has a **precursor phase** before its **impact phase**, because
  a corpus where degradation appears instantly cannot measure lead time and
  would reward a detector that is merely fast rather than early
* the healthy baseline carries diurnal structure, so a model that flags every
  evening as an anomaly fails here rather than in production
* collectors emit on their real cadences, so the corpus exercises the same
  carry-forward and staleness paths as a live agent

Each run carries ground truth: the impact windows and the layer a correct
root-cause analysis should name.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass, field

from ..store.models import Sample

BASE_CADENCE_S = 15

#: Emission cadence per feature prefix, matching the shipped probe defaults.
CADENCE_S: tuple[tuple[str, int], ...] = (
    ("os_", 15),
    ("vpn_", 15),
    ("wifi_", 20),
    ("gw_", 20),
    ("dns_", 45),
    ("https_", 90),
    ("captive_", 300),
    ("path_", 600),
)


def cadence_for(feature: str) -> int:
    for prefix, seconds in CADENCE_S:
        if feature.startswith(prefix):
            return seconds
    return BASE_CADENCE_S


@dataclass(slots=True)
class ScenarioRun:
    """A generated stream plus the ground truth needed to score a detector."""

    name: str
    samples: list[Sample]
    bad_windows: list[tuple[float, float]] = field(default_factory=list)
    expected_layer: str | None = None
    description: str = ""
    start_ts: float = 0.0
    impact_start: float | None = None

    def is_bad(self, ts: float) -> bool:
        return any(start <= ts <= end for start, end in self.bad_windows)

    def duration_s(self) -> float:
        if not self.samples:
            return 0.0
        return self.samples[-1].ts - self.samples[0].ts


def _diurnal(hour_float: float) -> float:
    """Evening congestion curve in [0, 1], peaking around 21:00."""
    return 0.5 * (1.0 + math.sin((hour_float - 15.0) / 24.0 * 2.0 * math.pi))


def _healthy(t_hours: float, rng: random.Random) -> dict[str, float]:
    """One healthy observation of every feature the corpus models."""
    load = _diurnal(t_hours % 24)
    gw = 3.0 + 1.4 * load + rng.gauss(0.0, 0.35)
    tls = 30.0 + 7.0 * load + rng.gauss(0.0, 3.0)
    tcp = 24.0 + 6.0 * load + rng.gauss(0.0, 3.0)
    ttfb = 58.0 + 22.0 * load + rng.gauss(0.0, 6.0)
    dns_p50 = 21.0 + 6.0 * load + rng.gauss(0.0, 3.0)
    path = 13.0 + 5.0 * load + rng.gauss(0.0, 1.2)
    return {
        "os_iface_type": 1.0,
        "os_link_mbps": 300.0,
        "os_rx_drop_rate": max(0.0, rng.gauss(0.0004, 0.0003)),
        "os_tx_drop_rate": max(0.0, rng.gauss(0.0003, 0.0002)),
        "os_tx_err_rate": 0.0,
        "os_retrans_rate": max(0.0, rng.gauss(0.006, 0.003)),
        "os_cpu_pct": max(2.0, rng.gauss(16.0, 5.0)),
        "os_mem_pct": max(10.0, rng.gauss(55.0, 4.0)),
        "os_on_battery": 0.0,
        "vpn_active": 0.0,
        "vpn_mtu": 0.0,
        "wifi_rssi_dbm": rng.gauss(-52.0, 1.6),
        "wifi_signal_pct": 88.0 + rng.gauss(0.0, 3.0),
        "wifi_link_mbps": 300.0,
        "wifi_tx_retry_rate": max(0.0, rng.gauss(0.03, 0.012)),
        "wifi_noise_dbm": rng.gauss(-95.0, 2.0),
        "wifi_channel": 44.0,
        "wifi_band_ghz": 5.0,
        "wifi_roam_count": 0.0,
        "dns_p50_ms": max(1.0, dns_p50),
        "dns_p95_ms": max(1.0, dns_p50 * 1.8 + rng.gauss(0.0, 4.0)),
        "dns_fail_rate": 0.0,
        "gw_rtt_ms": max(0.4, gw),
        "gw_jitter_ms": max(0.05, rng.gauss(0.45, 0.2)),
        "gw_loss_rate": 0.0,
        "path_hops": 10.0,
        "path_rtt_ms": max(1.0, path),
        "path_second_hop_ms": max(0.8, gw + 2.0 + rng.gauss(0.0, 0.6)),
        "path_unresponsive_rate": 0.0,
        "path_changed": 0.0,
        "https_dns_ms": max(0.5, rng.gauss(7.0, 2.0)),
        "https_tcp_ms": max(1.0, tcp),
        "https_tls_ms": max(1.0, tls),
        "https_ttfb_ms": max(2.0, ttfb),
        "https_total_ms": max(5.0, tcp + tls + ttfb + 10.0),
        "https_fail_rate": 0.0,
        "captive_portal": 0.0,
    }


def _ramp(elapsed: float, span: float) -> float:
    """Fraction of the way through a phase, clamped to [0, 1]."""
    if span <= 0:
        return 1.0
    return max(0.0, min(1.0, elapsed / span))


# --------------------------------------------------------------------- faults
# Each fault takes the healthy observation and the two progress fractions
# (precursor 0..1, impact 0..1) and mutates the observation in place.


def _wifi_fade(values: dict[str, float], pre: float, imp: float) -> None:
    values["wifi_rssi_dbm"] -= 24.0 * pre + 12.0 * imp
    values["wifi_tx_retry_rate"] = min(0.9, values["wifi_tx_retry_rate"] + 0.22 * pre + 0.3 * imp)
    values["wifi_link_mbps"] = max(6.0, 300.0 * (1.0 - 0.6 * pre - 0.3 * imp))
    values["os_retrans_rate"] = min(1.0, values["os_retrans_rate"] + 0.05 * imp)
    values["https_ttfb_ms"] *= 1.0 + 1.6 * imp
    values["gw_rtt_ms"] *= 1.0 + 1.2 * pre + 2.5 * imp
    values["gw_jitter_ms"] *= 1.0 + 4.0 * imp


def _wifi_interference(values: dict[str, float], pre: float, imp: float) -> None:
    values["wifi_tx_retry_rate"] = min(0.95, values["wifi_tx_retry_rate"] + 0.3 * pre + 0.4 * imp)
    values["wifi_noise_dbm"] += 22.0 * pre + 10.0 * imp
    values["gw_jitter_ms"] *= 1.0 + 6.0 * pre + 8.0 * imp
    values["gw_loss_rate"] = min(0.4, 0.15 * imp)
    values["https_ttfb_ms"] *= 1.0 + 1.1 * imp


def _dns_slow(values: dict[str, float], pre: float, imp: float) -> None:
    values["dns_p50_ms"] *= 1.0 + 6.0 * pre + 12.0 * imp
    values["dns_p95_ms"] *= 1.0 + 9.0 * pre + 18.0 * imp
    values["https_dns_ms"] *= 1.0 + 6.0 * pre + 12.0 * imp
    values["https_total_ms"] += values["https_dns_ms"]


def _dns_blackhole(values: dict[str, float], pre: float, imp: float) -> None:
    values["dns_p50_ms"] *= 1.0 + 10.0 * pre
    values["dns_fail_rate"] = min(1.0, 0.25 * pre + 1.0 * imp)
    if imp > 0.2:
        values["dns_p50_ms"] = 5000.0
        values["dns_p95_ms"] = 5000.0
        values["https_fail_rate"] = 1.0
        values["https_ttfb_ms"] = 5000.0


def _gateway_congestion(values: dict[str, float], pre: float, imp: float) -> None:
    values["gw_rtt_ms"] *= 1.0 + 5.0 * pre + 14.0 * imp
    values["gw_jitter_ms"] *= 1.0 + 8.0 * pre + 20.0 * imp
    values["gw_loss_rate"] = min(0.45, 0.06 * pre + 0.3 * imp)
    values["path_rtt_ms"] *= 1.0 + 2.0 * pre + 5.0 * imp
    values["https_ttfb_ms"] *= 1.0 + 1.5 * pre + 3.5 * imp


def _gateway_down(values: dict[str, float], pre: float, imp: float) -> None:
    values["gw_loss_rate"] = min(1.0, 0.3 * pre + 1.0 * imp)
    values["gw_rtt_ms"] *= 1.0 + 8.0 * pre
    if imp > 0.05:
        values["gw_rtt_ms"] = 5000.0
        values["gw_loss_rate"] = 1.0
        values["dns_fail_rate"] = 1.0
        values["https_fail_rate"] = 1.0
        values["path_unresponsive_rate"] = 1.0


def _isp_path_congestion(values: dict[str, float], pre: float, imp: float) -> None:
    # The LAN stays clean; everything beyond the second hop degrades.
    values["path_rtt_ms"] *= 1.0 + 4.0 * pre + 9.0 * imp
    values["path_second_hop_ms"] *= 1.0 + 3.0 * pre + 7.0 * imp
    values["path_unresponsive_rate"] = min(0.4, 0.1 * imp)
    values["https_ttfb_ms"] *= 1.0 + 2.0 * pre + 5.0 * imp
    values["https_tcp_ms"] *= 1.0 + 1.5 * pre + 3.0 * imp
    values["dns_p50_ms"] *= 1.0 + 1.2 * pre + 2.0 * imp


def _remote_service_slow(values: dict[str, float], pre: float, imp: float) -> None:
    # Only the application leg moves: name resolution, LAN and path are fine.
    values["https_ttfb_ms"] *= 1.0 + 3.5 * pre + 9.0 * imp
    values["https_total_ms"] *= 1.0 + 2.5 * pre + 6.0 * imp
    values["https_fail_rate"] = min(0.5, 0.25 * imp)


def _tls_handshake_slow(values: dict[str, float], pre: float, imp: float) -> None:
    values["https_tls_ms"] *= 1.0 + 5.0 * pre + 11.0 * imp
    values["https_total_ms"] += values["https_tls_ms"]
    values["https_ttfb_ms"] *= 1.0 + 0.8 * imp


def _vpn_overload(values: dict[str, float], pre: float, imp: float) -> None:
    values["vpn_active"] = 1.0
    values["vpn_mtu"] = 1380.0
    values["https_ttfb_ms"] *= 1.0 + 2.5 * pre + 6.0 * imp
    values["https_tcp_ms"] *= 1.0 + 2.0 * pre + 4.0 * imp
    values["os_retrans_rate"] = min(1.0, values["os_retrans_rate"] + 0.03 * pre + 0.09 * imp)
    values["path_rtt_ms"] *= 1.0 + 1.5 * pre + 3.0 * imp


def _device_pressure(values: dict[str, float], pre: float, imp: float) -> None:
    values["os_cpu_pct"] = min(100.0, values["os_cpu_pct"] + 55.0 * pre + 28.0 * imp)
    values["os_mem_pct"] = min(99.0, values["os_mem_pct"] + 20.0 * pre + 15.0 * imp)
    values["os_retrans_rate"] = min(1.0, values["os_retrans_rate"] + 0.04 * pre + 0.12 * imp)
    values["os_tx_drop_rate"] = min(1.0, values["os_tx_drop_rate"] + 0.05 * imp)
    values["https_ttfb_ms"] *= 1.0 + 1.2 * imp


def _captive_portal(values: dict[str, float], pre: float, imp: float) -> None:
    if imp <= 0.0:
        return
    values["captive_portal"] = 1.0
    values["https_fail_rate"] = 1.0
    values["dns_fail_rate"] = 0.5
    values["https_ttfb_ms"] = 3000.0
    values["path_unresponsive_rate"] = 0.9


def _evening_congestion(values: dict[str, float], pre: float, imp: float) -> None:
    """A benign regime change: everything slows a little, nothing breaks.

    Included so the corpus can measure false alarms, not just detections.
    """
    drift = 0.45 * (pre + imp) / 2.0
    values["gw_rtt_ms"] *= 1.0 + drift
    values["https_ttfb_ms"] *= 1.0 + drift
    values["dns_p50_ms"] *= 1.0 + drift


@dataclass(frozen=True, slots=True)
class ScenarioSpec:
    name: str
    expected_layer: str | None
    description: str
    mutate: object = None
    precursor_s: int = 600
    impact_s: int = 900
    benign: bool = False


SCENARIOS: tuple[ScenarioSpec, ...] = (
    ScenarioSpec(
        "healthy", None, "Nothing wrong; used to measure false alarms.", None, benign=True
    ),
    ScenarioSpec(
        "evening_congestion",
        None,
        "Benign regime change: everything slows slightly at peak hours.",
        _evening_congestion,
        benign=True,
    ),
    ScenarioSpec(
        "wifi_fade", "wifi", "Signal fades as the user walks away from the AP.", _wifi_fade
    ),
    ScenarioSpec(
        "wifi_interference",
        "wifi",
        "Retries and noise rise while signal strength stays fine.",
        _wifi_interference,
    ),
    ScenarioSpec("dns_slow", "dns", "Resolver latency climbs without failing.", _dns_slow),
    ScenarioSpec("dns_blackhole", "dns", "Resolver stops answering entirely.", _dns_blackhole),
    ScenarioSpec(
        "gateway_congestion",
        "gateway",
        "The router queues badly: RTT, jitter and loss all rise.",
        _gateway_congestion,
    ),
    ScenarioSpec("gateway_down", "gateway", "The router stops answering.", _gateway_down),
    ScenarioSpec(
        "isp_path_congestion",
        "path",
        "The LAN is clean; latency appears past the second hop.",
        _isp_path_congestion,
    ),
    ScenarioSpec(
        "remote_service_slow",
        "remote_https",
        "Only the application leg degrades; DNS, LAN and path are fine.",
        _remote_service_slow,
    ),
    ScenarioSpec(
        "tls_handshake_slow",
        "remote_https",
        "The TLS handshake stage dominates the request time.",
        _tls_handshake_slow,
    ),
    ScenarioSpec(
        "vpn_overload", "vpn", "A saturated tunnel adds latency and retransmits.", _vpn_overload
    ),
    ScenarioSpec(
        "device_pressure",
        "os",
        "The host itself is starved: CPU, memory and retransmits climb.",
        _device_pressure,
    ),
    ScenarioSpec(
        "captive_portal",
        "dns",
        "A sign-in page intercepts everything at once.",
        _captive_portal,
        precursor_s=0,
    ),
)

SCENARIOS_BY_NAME: dict[str, ScenarioSpec] = {spec.name: spec for spec in SCENARIOS}


def generate(
    spec: ScenarioSpec | str,
    *,
    seed: int = 0,
    warmup_s: int = 2700,
    recovery_s: int = 600,
    start_hour: float = 19.0,
    start_ts: float = 1_780_000_000.0,
) -> ScenarioRun:
    """Generate one scenario run.

    The stream is warm-up, then precursor, then impact, then recovery. The
    warm-up is long enough for the baselines to leave their cold start, which
    is what a fair evaluation requires: scoring a model during its own
    warm-up measures nothing.
    """
    if isinstance(spec, str):
        spec = SCENARIOS_BY_NAME[spec]
    rng = random.Random(seed if seed else hash(spec.name) & 0xFFFF)

    precursor_start = warmup_s
    impact_start = warmup_s + spec.precursor_s
    impact_end = impact_start + spec.impact_s
    total = impact_end + recovery_s

    samples: list[Sample] = []
    last_emitted: dict[str, float] = {}

    for offset in range(0, total + 1, BASE_CADENCE_S):
        ts = start_ts + offset
        hours = start_hour + offset / 3600.0
        values = _healthy(hours, rng)

        if spec.mutate is not None:
            pre = (
                _ramp(offset - precursor_start, spec.precursor_s)
                if offset >= precursor_start
                else 0.0
            )
            if offset >= impact_start:
                pre = 1.0
                imp = _ramp(offset - impact_start, max(1, spec.impact_s // 3))
            else:
                imp = 0.0
            if offset > impact_end:
                # Recovery: fault clears immediately, as a fix would.
                pre = imp = 0.0
            if pre > 0.0 or imp > 0.0:
                spec.mutate(values, pre, imp)  # type: ignore[operator]

        sample = Sample(ts=ts)
        for name, value in values.items():
            cadence = cadence_for(name)
            previous = last_emitted.get(name)
            if previous is not None and ts - previous < cadence:
                continue
            last_emitted[name] = ts
            sample.set(name, value)
        if sample.values:
            samples.append(sample)

    bad_windows: list[tuple[float, float]] = []
    impact_ts: float | None = None
    if spec.mutate is not None and not spec.benign:
        impact_ts = start_ts + impact_start
        bad_windows.append((impact_ts, start_ts + impact_end))

    return ScenarioRun(
        name=spec.name,
        samples=samples,
        bad_windows=bad_windows,
        expected_layer=spec.expected_layer,
        description=spec.description,
        start_ts=start_ts,
        impact_start=impact_ts,
    )


def generate_corpus(
    *, seeds: tuple[int, ...] = (11, 23, 37), names: tuple[str, ...] | None = None
) -> list[ScenarioRun]:
    """Generate the whole corpus, several seeds per scenario."""
    specs = (
        SCENARIOS
        if names is None
        else tuple(SCENARIOS_BY_NAME[name] for name in names if name in SCENARIOS_BY_NAME)
    )
    return [generate(spec, seed=seed) for spec in specs for seed in seeds]
