"""Path collector: a rate-limited traceroute toward the internet.

This is the only evidence the agent has for "the problem is past your
router", so PRD 11.2 forbids blaming the ISP without it. It is also the most
expensive probe, so it runs on the slowest cadence of any collector and takes
its own hourly budget.

Hop addresses are never stored. What is stored is the shape of the path: how
many hops answered, the RTT to the last responsive hop, the RTT to the second
hop (usually the first ISP device), and whether the hop sequence changed
since the previous trace.
"""

from __future__ import annotations

import re
import sys

from ..config import NetPulseConfig
from ..core.ratelimit import ProbeBudget
from ..privacy import hash_to_float
from .base import Capability, Collector, CollectorResult
from .shell import have, run

MAX_HOPS = 12
QUERIES_PER_HOP = 1

_IP = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
_MS = re.compile(r"([\d.]+)\s*ms", re.IGNORECASE)


class PathCollector(Collector):
    name = "path"
    layer = "path"
    active = True

    def __init__(self, config: NetPulseConfig, budget: ProbeBudget | None = None) -> None:
        super().__init__(config, budget)
        self._previous_signature: float | None = None

    def interval_s(self) -> float:
        return float(self.config.probes.path_interval_s)

    def enabled(self) -> bool:
        return self.config.collectors.path

    def detect_capability(self) -> Capability:
        binary = traceroute_binary()
        if binary is None:
            return Capability(
                False,
                "no tracert or traceroute binary on PATH; install one to enable path analysis",
            )
        if sys.platform.startswith("linux") and binary == "traceroute":
            return Capability(
                True,
                "traceroute uses UDP probes by default, which needs no capability",
            )
        return Capability(True)

    def _collect(self) -> CollectorResult:
        target = self.config.probes.path_target
        result = CollectorResult()
        if self.budget is not None and not self.budget.acquire("traceroute"):
            return CollectorResult(ok=True, skipped_reason="traceroute budget reached")

        binary = traceroute_binary()
        if binary is None:  # pragma: no cover - guarded by detect_capability
            return CollectorResult(ok=True, skipped_reason="no traceroute binary")

        command = traceroute_command(binary, target)
        # A full trace can legitimately take a while; the timeout is generous
        # because this probe only runs a few times an hour.
        completed = run(command, timeout=float(MAX_HOPS) * 3.0 + 10.0)
        hops = parse_traceroute(completed.text)
        if not hops:
            result.probe(self.name, target, False, None, "no hops parsed")
            return CollectorResult(ok=False, error="traceroute produced no parseable hops")

        responsive = [hop for hop in hops if hop.rtt_ms is not None]
        result.set("path_hops", float(len(responsive)))
        result.set("path_unresponsive_rate", 1.0 - len(responsive) / len(hops))
        if responsive:
            result.set("path_rtt_ms", responsive[-1].rtt_ms)
        second = next((hop for hop in hops if hop.index == 2 and hop.rtt_ms is not None), None)
        if second is not None:
            result.set("path_second_hop_ms", second.rtt_ms)

        signature = path_signature(hops)
        changed = self._previous_signature is not None and signature != self._previous_signature
        self._previous_signature = signature
        result.set("path_changed", 1.0 if changed else 0.0)

        result.probe(
            self.name,
            target,
            True,
            responsive[-1].rtt_ms if responsive else None,
            f"{len(responsive)}/{len(hops)} hops answered",
        )
        return result


class Hop:
    """One traceroute hop. The address is used for the change signature only."""

    __slots__ = ("address", "index", "rtt_ms")

    def __init__(self, index: int, address: str | None, rtt_ms: float | None) -> None:
        self.index = index
        self.address = address
        self.rtt_ms = rtt_ms

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"Hop({self.index}, rtt={self.rtt_ms})"


def traceroute_binary() -> str | None:
    if sys.platform == "win32":
        return "tracert" if have("tracert") else None
    for candidate in ("traceroute", "tracepath"):
        if have(candidate):
            return candidate
    return None


def traceroute_command(binary: str, target: str) -> list[str]:
    if binary == "tracert":
        return ["tracert", "-d", "-h", str(MAX_HOPS), "-w", "800", target]
    if binary == "tracepath":
        return ["tracepath", "-n", "-m", str(MAX_HOPS), target]
    return [
        "traceroute",
        "-n",
        "-m",
        str(MAX_HOPS),
        "-q",
        str(QUERIES_PER_HOP),
        "-w",
        "2",
        target,
    ]


def parse_traceroute(text: str) -> list[Hop]:
    """Parse tracert, traceroute and tracepath output into hops.

    All three print a leading hop number followed by timings and an address,
    with unresponsive hops marked by asterisks, so one tolerant parser covers
    every platform.
    """
    hops: list[Hop] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or not line[0].isdigit():
            continue
        parts = line.split(None, 1)
        try:
            index = int(parts[0].rstrip(":"))
        except ValueError:
            continue
        remainder = parts[1] if len(parts) > 1 else ""
        address_match = _IP.search(remainder)
        address = address_match.group(0) if address_match else None
        times = [float(value) for value in _MS.findall(remainder)]
        rtt = min(times) if times else None
        if rtt is None and address is None and "*" not in remainder:
            continue
        hops.append(Hop(index, address, rtt))
    return hops


def path_signature(hops: list[Hop]) -> float:
    """Stable, salted hash of the responsive hop addresses.

    Stored as a single number so the feature table keeps its "no text
    columns" property while still detecting a rerouted path.
    """
    joined = ">".join(hop.address or "*" for hop in hops)
    return hash_to_float(joined)
