"""DNS timing collector.

Resolution time is measured with the platform resolver rather than by
building DNS packets, which keeps the agent dependency-free and, more
importantly, measures what applications actually experience: the OS cache,
the configured resolver and any VPN or captive-portal interception in front
of it.

Names are resolved from the configured probe list only. User traffic is never
observed, and the ledger records the configured name (hashed when
``privacy.hash_dns_names`` is on) rather than anything the user looked up.
"""

from __future__ import annotations

import socket
import time

from ..privacy import hash_identifier
from .base import Capability, Collector, CollectorResult, percentile

#: A resolution that takes longer than this is recorded as a failure; waiting
#: any longer tells us nothing new and burns the sampling interval.
MAX_WAIT_S = 5.0


class DnsCollector(Collector):
    name = "dns"
    layer = "dns"
    active = True

    def interval_s(self) -> float:
        return float(self.config.probes.dns_interval_s)

    def enabled(self) -> bool:
        return self.config.collectors.dns

    def detect_capability(self) -> Capability:
        if not self.config.probes.dns_targets:
            return Capability(False, "no DNS probe targets are configured")
        return Capability(True)

    def _collect(self) -> CollectorResult:
        result = CollectorResult()
        timings: list[float] = []
        failures = 0
        attempted = 0

        timeout = min(MAX_WAIT_S, self.config.probes.timeout_s)
        previous_timeout = socket.getdefaulttimeout()
        socket.setdefaulttimeout(timeout)
        try:
            for name in self.config.probes.dns_targets:
                if self.budget is not None and not self.budget.acquire("dns"):
                    result.probe(self.name, self._label(name), False, None, "probe budget reached")
                    continue
                attempted += 1
                elapsed_ms, ok, detail = _time_resolution(name)
                if ok and elapsed_ms is not None:
                    timings.append(elapsed_ms)
                else:
                    failures += 1
                result.probe(self.name, self._label(name), ok, elapsed_ms, detail)
        finally:
            socket.setdefaulttimeout(previous_timeout)

        if attempted == 0:
            return CollectorResult(ok=True, skipped_reason="probe budget reached")

        result.set("dns_fail_rate", failures / attempted)
        if timings:
            result.set("dns_p50_ms", percentile(timings, 0.5))
            result.set("dns_p95_ms", percentile(timings, 0.95))
        else:
            # Every lookup failed. Record the timeout ceiling so the model sees
            # a large latency rather than a gap it would carry forward.
            result.set("dns_p50_ms", timeout * 1000.0)
            result.set("dns_p95_ms", timeout * 1000.0)
        return result

    def _label(self, name: str) -> str:
        if self.config.privacy.hash_dns_names:
            return f"dns:{hash_identifier(name)}"
        return f"dns:{name}"


def _time_resolution(name: str) -> tuple[float | None, bool, str]:
    """Resolve one name and return ``(elapsed_ms, ok, detail)``.

    ``getaddrinfo`` is used rather than ``gethostbyname`` so that IPv6-only
    networks are measured correctly.
    """
    started = time.perf_counter()
    try:
        socket.getaddrinfo(name, 443, proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        return (time.perf_counter() - started) * 1000.0, False, f"resolution failed: {exc.args[0]}"
    except OSError as exc:
        return (time.perf_counter() - started) * 1000.0, False, f"resolver error: {exc.errno}"
    return (time.perf_counter() - started) * 1000.0, True, ""
