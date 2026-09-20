"""Device and operating-system collector.

Passive only: interface counters, TCP retransmission counters, CPU, memory,
power source and VPN state. Nothing here touches the network, so it keeps
working when the link is down and it costs no probe budget.
"""

from __future__ import annotations

import sys
import time
from typing import ClassVar

import psutil

from ..config import NetPulseConfig
from ..core.ratelimit import ProbeBudget
from . import netinfo
from .base import Capability, Collector, CollectorResult
from .shell import run

#: Retransmission counters need an external tool on Windows and macOS, so they
#: are sampled every Nth round rather than every round.
RETRANS_EVERY = 4


class SystemCollector(Collector):
    name = "system"
    layer = "os"
    active = False

    def __init__(self, config: NetPulseConfig, budget: ProbeBudget | None = None) -> None:
        super().__init__(config, budget)
        self._prev_counters: tuple[float, object] | None = None
        self._prev_retrans: tuple[int, int] | None = None
        self._round = 0

    def interval_s(self) -> float:
        return float(self.config.probes.system_interval_s)

    def enabled(self) -> bool:
        return self.config.collectors.system

    def detect_capability(self) -> Capability:
        try:
            psutil.net_io_counters(pernic=True)
        except Exception as exc:
            return Capability(False, f"per-interface counters are unavailable: {exc}")
        return Capability(True)

    def _collect(self) -> CollectorResult:
        result = CollectorResult()
        route = netinfo.default_route()
        self._round += 1

        result.set("os_iface_type", route.interface_kind)
        result.set("vpn_active", 1.0 if route.is_vpn else 0.0)
        result.set("vpn_mtu", float(route.vpn_mtu))

        self._collect_link(result, route.interface)
        self._collect_counters(result, route.interface)
        self._collect_host(result)
        if self._round % RETRANS_EVERY == 1:
            self._collect_retransmissions(result)
        return result

    # ------------------------------------------------------------- sections

    def _collect_link(self, result: CollectorResult, interface: str | None) -> None:
        if not interface:
            return
        try:
            stats = psutil.net_if_stats().get(interface)
        except Exception:
            return
        if stats is None:
            return
        # psutil reports 0 for adapters that do not expose a negotiated speed.
        if getattr(stats, "speed", 0):
            result.set("os_link_mbps", float(stats.speed))

    def _collect_counters(self, result: CollectorResult, interface: str | None) -> None:
        """Turn monotonically increasing NIC counters into per-interval rates."""
        if not interface:
            return
        try:
            counters = psutil.net_io_counters(pernic=True).get(interface)
        except Exception:
            return
        if counters is None:
            return
        now = time.monotonic()
        previous = self._prev_counters
        self._prev_counters = (now, counters)
        if previous is None or previous[1] is None:
            return
        prior = previous[1]

        rx_packets = counters.packets_recv - getattr(prior, "packets_recv", 0)
        tx_packets = counters.packets_sent - getattr(prior, "packets_sent", 0)
        rx_drop = counters.dropin - getattr(prior, "dropin", 0)
        tx_drop = counters.dropout - getattr(prior, "dropout", 0)
        tx_err = counters.errout - getattr(prior, "errout", 0)

        # A counter reset (adapter reconnect, driver reload) shows up as a
        # negative delta; skip the round rather than emit a nonsense rate.
        if min(rx_packets, tx_packets, rx_drop, tx_drop, tx_err) < 0:
            return
        if rx_packets > 0:
            result.set("os_rx_drop_rate", rx_drop / rx_packets)
        if tx_packets > 0:
            result.set("os_tx_drop_rate", tx_drop / tx_packets)
            result.set("os_tx_err_rate", tx_err / tx_packets)

    def _collect_host(self, result: CollectorResult) -> None:
        try:
            # interval=None returns the value since the previous call, which is
            # exactly the sampling interval and costs no sleep.
            result.set("os_cpu_pct", psutil.cpu_percent(interval=None))
            result.set("os_mem_pct", psutil.virtual_memory().percent)
        except Exception:
            pass
        try:
            battery = psutil.sensors_battery()
        except Exception:
            battery = None
        if battery is not None:
            result.set("os_on_battery", 0.0 if battery.power_plugged else 1.0)
        else:
            result.set("os_on_battery", 0.0)

    def _collect_retransmissions(self, result: CollectorResult) -> None:
        counters = read_tcp_retransmissions()
        if counters is None:
            return
        out_segs, retrans_segs = counters
        previous = self._prev_retrans
        self._prev_retrans = counters
        if previous is None:
            return
        delta_out = out_segs - previous[0]
        delta_retrans = retrans_segs - previous[1]
        if delta_out <= 0 or delta_retrans < 0:
            return
        result.set("os_retrans_rate", delta_retrans / delta_out)


def read_tcp_retransmissions() -> tuple[int, int] | None:
    """Return ``(segments_out, segments_retransmitted)`` for the host.

    Linux reads /proc/net/snmp. Windows calls GetTcpStatistics through
    ctypes, because ``netstat -s`` can block for many seconds on a busy host
    and an agent that promises a small CPU budget cannot afford that. macOS
    falls back to the BSD netstat, which returns promptly.
    """
    if sys.platform.startswith("linux"):
        return _linux_retransmissions()
    if sys.platform == "win32":
        return _windows_retransmissions()
    if sys.platform == "darwin":
        return _parse_netstat_tcp(run(["netstat", "-s", "-p", "tcp"], timeout=6.0).text)
    return None


def _windows_retransmissions() -> tuple[int, int] | None:
    """Read MIB_TCPSTATS via iphlpapi.GetTcpStatistics."""
    try:
        import ctypes
        import ctypes.wintypes as wintypes
    except ImportError:  # pragma: no cover - Windows only
        return None

    class MibTcpStats(ctypes.Structure):
        _fields_: ClassVar = [
            (name, wintypes.DWORD)
            for name in (
                "dwRtoAlgorithm",
                "dwRtoMin",
                "dwRtoMax",
                "dwMaxConn",
                "dwActiveOpens",
                "dwPassiveOpens",
                "dwAttemptFails",
                "dwEstabResets",
                "dwCurrEstab",
                "dwInSegs",
                "dwOutSegs",
                "dwRetransSegs",
                "dwInErrs",
                "dwOutRsts",
                "dwNumConns",
            )
        ]

    stats = MibTcpStats()
    try:
        rc = ctypes.windll.iphlpapi.GetTcpStatistics(  # type: ignore[attr-defined]
            ctypes.byref(stats)
        )
    except (AttributeError, OSError):  # pragma: no cover - Windows only
        return None
    if rc != 0:
        return None
    return int(stats.dwOutSegs), int(stats.dwRetransSegs)


def _linux_retransmissions() -> tuple[int, int] | None:
    try:
        with open("/proc/net/snmp", encoding="ascii") as handle:
            lines = handle.read().splitlines()
    except OSError:
        return None
    headers: list[str] | None = None
    for line in lines:
        if not line.startswith("Tcp:"):
            continue
        fields = line.split()[1:]
        if headers is None:
            headers = fields
            continue
        try:
            row = dict(zip(headers, (int(value) for value in fields), strict=False))
        except ValueError:
            return None
        if "OutSegs" in row and "RetransSegs" in row:
            return row["OutSegs"], row["RetransSegs"]
    return None


def _parse_netstat_tcp(text: str) -> tuple[int, int] | None:
    """Pull segment counters out of BSD netstat statistics output.

    macOS prints ``N packets sent`` followed by an indented
    ``N data packets (N bytes) retransmitted`` line.
    """
    out_segs: int | None = None
    retrans: int | None = None
    for raw in text.splitlines():
        line = raw.strip()
        lowered = line.lower()
        if lowered.startswith("segments sent"):
            out_segs = _trailing_int(line)
        elif lowered.startswith("segments retransmitted"):
            retrans = _trailing_int(line)
        elif "packets sent" in lowered and out_segs is None:
            out_segs = _leading_int(line)
        elif "retransmitted" in lowered and retrans is None and "data packets" in lowered:
            retrans = _leading_int(line)
    if out_segs is None or retrans is None:
        return None
    return out_segs, retrans


def _trailing_int(line: str) -> int | None:
    parts = line.replace("=", " ").split()
    for token in reversed(parts):
        cleaned = token.replace(",", "")
        if cleaned.isdigit():
            return int(cleaned)
    return None


def _leading_int(line: str) -> int | None:
    for token in line.split():
        cleaned = token.replace(",", "")
        if cleaned.isdigit():
            return int(cleaned)
    return None
