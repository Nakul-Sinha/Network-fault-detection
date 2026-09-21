"""Captive portal detection.

PRD 10.2 lists this as a should-have for one reason: a hotel or airport
portal makes every other layer look broken at once, and without this check
the agent would confidently blame the ISP for a login page. When the portal
flag is set, the L0 rules suppress path and remote attribution entirely.

The check is the standard one: request a URL whose correct response is known
in advance and see whether the answer was tampered with.
"""

from __future__ import annotations

import contextlib
import socket
import time
from urllib.parse import urlparse

from ..config import assert_safe_probe_url
from .base import Capability, Collector, CollectorResult

EXPECTED_BODY = b"success"
READ_BYTES = 512


class CaptivePortalCollector(Collector):
    name = "captive"
    layer = "context"
    active = True

    def interval_s(self) -> float:
        return float(self.config.probes.captive_interval_s)

    def enabled(self) -> bool:
        return self.config.collectors.captive

    def detect_capability(self) -> Capability:
        if not self.config.probes.captive_probe_url:
            return Capability(False, "no captive portal probe URL is configured")
        return Capability(True)

    def _collect(self) -> CollectorResult:
        result = CollectorResult()
        url = self.config.probes.captive_probe_url
        if self.budget is not None and not self.budget.acquire("https"):
            return CollectorResult(ok=True, skipped_reason="probe budget reached")

        intercepted, detail, elapsed_ms = detect_portal(url, timeout_s=self.config.probes.timeout_s)
        if intercepted is None:
            # Offline or unreachable: this probe cannot distinguish a portal
            # from a dead link, so it says nothing rather than guessing.
            result.probe(self.name, url, False, elapsed_ms, detail)
            return CollectorResult(ok=True, skipped_reason=detail)

        result.set("captive_portal", 1.0 if intercepted else 0.0)
        result.probe(self.name, url, True, elapsed_ms, detail)
        return result


def detect_portal(url: str, *, timeout_s: float = 5.0) -> tuple[bool | None, str, float | None]:
    """Return ``(intercepted, detail, elapsed_ms)``.

    ``intercepted`` is ``None`` when the probe could not reach anything at
    all, which is a different condition from "a portal answered".
    """
    try:
        assert_safe_probe_url(url, require_tls=False)
    except ValueError as exc:
        return None, str(exc), None

    parsed = urlparse(url)
    host = parsed.hostname or ""
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    target = parsed.path or "/"

    started = time.perf_counter()
    sock: socket.socket | None = None
    try:
        sock = socket.create_connection((host, port), timeout=timeout_s)
        request = (
            f"GET {target} HTTP/1.1\r\n"
            f"Host: {host}\r\n"
            "User-Agent: NetPulse-Local/0.1\r\n"
            "Connection: close\r\n\r\n"
        ).encode("ascii", "ignore")
        sock.sendall(request)
        head = sock.recv(READ_BYTES)
    except OSError as exc:
        elapsed = (time.perf_counter() - started) * 1000.0
        return None, f"captive probe unreachable: {exc.strerror or exc}", elapsed
    finally:
        if sock is not None:
            with contextlib.suppress(OSError):
                sock.close()

    elapsed = (time.perf_counter() - started) * 1000.0
    status = _status(head)
    body = head.split(b"\r\n\r\n", 1)[1] if b"\r\n\r\n" in head else b""

    if status is None:
        return True, "non-HTTP response to the portal probe", elapsed
    if status in (301, 302, 303, 307, 308):
        return True, f"portal probe redirected (HTTP {status})", elapsed
    if status != 200:
        return True, f"portal probe answered HTTP {status}", elapsed
    if EXPECTED_BODY not in body.lower():
        return True, "portal probe body did not match the expected response", elapsed
    return False, "no portal detected", elapsed


def _status(head: bytes) -> int | None:
    if not head.startswith(b"HTTP/"):
        return None
    try:
        return int(head.split(b" ", 2)[1])
    except (IndexError, ValueError):
        return None
