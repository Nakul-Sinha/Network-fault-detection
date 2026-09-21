"""Synthetic HTTPS probe with per-phase timing.

The probe is written against ``socket`` and ``ssl`` rather than an HTTP
client library because the phase split is the whole point: separating DNS
from TCP from TLS from time-to-first-byte is what lets RCA say "your name
resolution is fine, the handshake is what got slow". A high-level client
reports one number and hides exactly the signal this product needs.

Only the head of the response is read, and it is discarded immediately. No
body, no headers and no payload byte is ever stored (PRD N2).
"""

from __future__ import annotations

import contextlib
import socket
import ssl
import time
from dataclasses import dataclass
from urllib.parse import urlparse

from ..config import assert_safe_probe_url
from .base import Capability, Collector, CollectorResult, percentile

USER_AGENT = "NetPulse-Local/0.1 (+https://github.com/Nakul-Sinha/Network-fault-detection)"
READ_BYTES = 256


@dataclass(slots=True)
class PhaseTimings:
    """Milliseconds spent in each phase of one synthetic request."""

    dns_ms: float | None = None
    tcp_ms: float | None = None
    tls_ms: float | None = None
    ttfb_ms: float | None = None
    total_ms: float | None = None
    ok: bool = False
    error: str = ""
    status: int | None = None

    def as_dict(self) -> dict[str, float | None]:
        return {
            "dns_ms": self.dns_ms,
            "tcp_ms": self.tcp_ms,
            "tls_ms": self.tls_ms,
            "ttfb_ms": self.ttfb_ms,
            "total_ms": self.total_ms,
        }


class HttpsCollector(Collector):
    name = "https"
    layer = "remote_https"
    active = True

    def interval_s(self) -> float:
        return float(self.config.probes.https_interval_s)

    def enabled(self) -> bool:
        return self.config.collectors.https

    def detect_capability(self) -> Capability:
        if not self.config.probes.https_targets:
            return Capability(False, "no HTTPS probe targets are configured")
        return Capability(True)

    def _collect(self) -> CollectorResult:
        result = CollectorResult()
        samples: list[PhaseTimings] = []
        attempted = 0

        for url in self.config.probes.https_targets:
            if self.budget is not None and not self.budget.acquire("https"):
                result.probe(self.name, url, False, None, "probe budget reached")
                continue
            attempted += 1
            timings = probe_url(url, timeout_s=self.config.probes.timeout_s)
            samples.append(timings)
            result.probe(
                self.name,
                url,
                timings.ok,
                timings.total_ms,
                timings.error or f"HTTP {timings.status}",
            )

        if attempted == 0:
            return CollectorResult(ok=True, skipped_reason="probe budget reached")

        failures = sum(1 for sample in samples if not sample.ok)
        result.set("https_fail_rate", failures / attempted)

        successful = [sample for sample in samples if sample.ok]
        if successful:
            _set_phase(result, "https_dns_ms", [s.dns_ms for s in successful])
            _set_phase(result, "https_tcp_ms", [s.tcp_ms for s in successful])
            _set_phase(result, "https_tls_ms", [s.tls_ms for s in successful])
            _set_phase(result, "https_ttfb_ms", [s.ttfb_ms for s in successful])
            _set_phase(result, "https_total_ms", [s.total_ms for s in successful])
        else:
            ceiling = self.config.probes.timeout_s * 1000.0
            result.set("https_total_ms", ceiling)
            result.set("https_ttfb_ms", ceiling)
        return result


def probe_url(url: str, *, timeout_s: float = 5.0) -> PhaseTimings:
    """Time one HTTPS request, phase by phase.

    The URL is re-validated here rather than trusted from config, because
    this function is also reachable from the CLI and from tests.
    """
    timings = PhaseTimings()
    try:
        assert_safe_probe_url(url, require_tls=False)
    except ValueError as exc:
        timings.error = str(exc)
        return timings

    parsed = urlparse(url)
    host = parsed.hostname or ""
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    target = parsed.path or "/"
    if parsed.query:
        target = f"{target}?{parsed.query}"

    started = time.perf_counter()
    sock: socket.socket | None = None
    try:
        mark = time.perf_counter()
        addresses = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
        timings.dns_ms = (time.perf_counter() - mark) * 1000.0

        family, socktype, proto, _canon, address = addresses[0]
        sock = socket.socket(family, socktype, proto)
        sock.settimeout(timeout_s)
        mark = time.perf_counter()
        sock.connect(address)
        timings.tcp_ms = (time.perf_counter() - mark) * 1000.0

        if parsed.scheme == "https":
            context = ssl.create_default_context()
            mark = time.perf_counter()
            sock = context.wrap_socket(sock, server_hostname=host)
            timings.tls_ms = (time.perf_counter() - mark) * 1000.0
        else:
            timings.tls_ms = 0.0

        request = (
            f"GET {target} HTTP/1.1\r\n"
            f"Host: {host}\r\n"
            f"User-Agent: {USER_AGENT}\r\n"
            "Accept: */*\r\n"
            "Connection: close\r\n\r\n"
        ).encode("ascii", "ignore")
        mark = time.perf_counter()
        sock.sendall(request)
        head = sock.recv(READ_BYTES)
        timings.ttfb_ms = (time.perf_counter() - mark) * 1000.0
        timings.status = _status_code(head)
        # The response is deliberately not kept beyond this point.
        del head
        timings.ok = timings.status is not None and timings.status < 500
        if not timings.ok and timings.status is not None:
            timings.error = f"server returned HTTP {timings.status}"
    except ssl.SSLCertVerificationError as exc:
        timings.error = f"TLS certificate verification failed: {exc.verify_message or exc.reason}"
    except ssl.SSLError as exc:
        timings.error = f"TLS error: {exc.reason or exc}"
    except TimeoutError:
        timings.error = "timed out"
    except socket.gaierror as exc:
        timings.error = f"name resolution failed: {exc.args[0]}"
    except OSError as exc:
        timings.error = f"connection failed: {exc.strerror or exc}"
    finally:
        if sock is not None:
            with contextlib.suppress(OSError):
                sock.close()
        timings.total_ms = (time.perf_counter() - started) * 1000.0
    return timings


def _status_code(head: bytes) -> int | None:
    if not head.startswith(b"HTTP/"):
        return None
    try:
        return int(head.split(b" ", 2)[1])
    except (IndexError, ValueError):
        return None


def _set_phase(result: CollectorResult, feature: str, values: list[float | None]) -> None:
    present = [value for value in values if value is not None]
    if not present:
        return
    # The worst of the set is the one a user would feel, so the probe reports
    # the upper end rather than an average that hides a single slow target.
    result.set(feature, percentile(present, 0.9))
