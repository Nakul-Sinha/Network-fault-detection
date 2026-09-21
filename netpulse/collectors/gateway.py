"""Gateway reachability collector.

The first hop is the cleanest way to separate "the problem is inside my home"
from "the problem is past my router", so it gets its own layer and its own
probe. The measurement uses the platform ``ping`` binary rather than raw
sockets: raw ICMP needs administrator rights on Windows and a capability on
Linux, and PRD 5.3 requires the agent to work as a normal user.
"""

from __future__ import annotations

import re
import sys

from . import netinfo
from .base import Capability, Collector, CollectorResult, mean_absolute_deviation
from .shell import have, run

PING_COUNT = 4

_WINDOWS_TIME = re.compile(r"time[=<]\s*(\d+)\s*ms", re.IGNORECASE)
_POSIX_TIME = re.compile(r"time[=<]\s*([\d.]+)\s*ms", re.IGNORECASE)


class GatewayCollector(Collector):
    name = "gateway"
    layer = "gateway"
    active = True

    def interval_s(self) -> float:
        return float(self.config.probes.gateway_interval_s)

    def enabled(self) -> bool:
        return self.config.collectors.gateway

    def detect_capability(self) -> Capability:
        if not have("ping"):
            return Capability(False, "no ping binary on PATH")
        return Capability(True)

    def _collect(self) -> CollectorResult:
        route = netinfo.default_route()
        if not route.gateway:
            return CollectorResult(ok=True, skipped_reason="no default gateway is configured")

        result = CollectorResult()
        if self.budget is not None and not self.budget.acquire("icmp", PING_COUNT):
            result.probe(self.name, route.gateway, False, None, "probe budget reached")
            return CollectorResult(ok=True, skipped_reason="probe budget reached")

        timeout = self.config.probes.timeout_s
        command = ping_command(route.gateway, PING_COUNT, timeout)
        completed = run(command, timeout=timeout * PING_COUNT + 4.0)
        rtts = parse_ping_times(completed.text)

        loss = 1.0 - (len(rtts) / PING_COUNT)
        result.set("gw_loss_rate", max(0.0, min(1.0, loss)))
        if rtts:
            result.set("gw_rtt_ms", sum(rtts) / len(rtts))
            result.set("gw_jitter_ms", mean_absolute_deviation(rtts))
        else:
            # Unreachable gateway is an L0 condition, not a missing value.
            result.set("gw_rtt_ms", timeout * 1000.0)
            result.set("gw_jitter_ms", 0.0)

        result.probe(
            self.name,
            route.gateway,
            bool(rtts),
            (sum(rtts) / len(rtts)) if rtts else None,
            "" if rtts else "no replies",
        )
        return result


def ping_command(target: str, count: int, timeout_s: float) -> list[str]:
    """Build the platform-appropriate ping invocation.

    The flags differ in both name and unit: Windows takes a per-reply timeout
    in milliseconds, Linux takes whole seconds, and macOS takes milliseconds
    under a different flag.
    """
    if sys.platform == "win32":
        return ["ping", "-n", str(count), "-w", str(int(timeout_s * 1000)), target]
    if sys.platform == "darwin":
        return ["ping", "-c", str(count), "-W", str(int(timeout_s * 1000)), "-i", "0.2", target]
    return ["ping", "-n", "-c", str(count), "-W", str(max(1, int(timeout_s))), "-i", "0.2", target]


def parse_ping_times(text: str) -> list[float]:
    """Extract per-reply round trip times in milliseconds.

    Windows reports integer milliseconds and collapses sub-millisecond
    replies to ``time<1ms``, which is recorded as 0.5 ms so that a fast LAN
    does not read as a suspicious exact zero.
    """
    values: list[float] = []
    for line in text.splitlines():
        if "time" not in line.lower():
            continue
        if "<1ms" in line.replace(" ", "").lower():
            values.append(0.5)
            continue
        pattern = _WINDOWS_TIME if sys.platform == "win32" else _POSIX_TIME
        match = pattern.search(line)
        if match is None:
            match = _POSIX_TIME.search(line)
        if match:
            try:
                values.append(float(match.group(1)))
            except ValueError:
                continue
    return values
