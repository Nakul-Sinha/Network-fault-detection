"""Collector construction and capability reporting."""

from __future__ import annotations

from ..config import NetPulseConfig
from ..core.ratelimit import ProbeBudget
from .base import Collector
from .captive import CaptivePortalCollector
from .dns import DnsCollector
from .gateway import GatewayCollector
from .https_probe import HttpsCollector
from .path import PathCollector
from .system import SystemCollector
from .wifi import WifiCollector

#: Construction order is also the sampling order within a round, so the
#: passive, always-available layers land in the sample before the slower
#: active probes.
COLLECTOR_TYPES: tuple[type[Collector], ...] = (
    SystemCollector,
    WifiCollector,
    GatewayCollector,
    DnsCollector,
    HttpsCollector,
    CaptivePortalCollector,
    PathCollector,
)


def build_budget(config: NetPulseConfig) -> ProbeBudget:
    return ProbeBudget(
        https_per_minute=config.probes.max_https_per_minute,
        dns_per_minute=config.probes.max_dns_per_minute,
        icmp_per_minute=config.probes.max_icmp_per_minute,
        traceroute_per_hour=config.probes.max_traceroute_per_hour,
    )


def build_collectors(config: NetPulseConfig, budget: ProbeBudget) -> list[Collector]:
    return [collector_type(config, budget) for collector_type in COLLECTOR_TYPES]


def capability_report(config: NetPulseConfig | None = None) -> list[dict[str, object]]:
    """Describe what this host can and cannot measure.

    Used by ``netpulse doctor``, by the onboarding screen and by the exported
    diagnostic bundle, so a support conversation starts from facts about the
    machine rather than guesses.
    """
    config = config or NetPulseConfig()
    budget = build_budget(config)
    return [collector.status() for collector in build_collectors(config, budget)]
