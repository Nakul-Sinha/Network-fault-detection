"""Default route, active interface and VPN detection across platforms.

Almost every collector needs the same two facts: which interface currently
carries the default route, and what its gateway address is. Resolving that
once and caching it briefly keeps the gateway, Wi-Fi and system collectors
consistent with one another within a sampling round.
"""

from __future__ import annotations

import ipaddress
import socket
import struct
import sys
import threading
import time
from dataclasses import dataclass

import psutil

from ..logging_setup import get_logger
from .shell import run

log = get_logger(__name__)

CACHE_TTL_S = 20.0

#: Interface name fragments that indicate a tunnel rather than a physical NIC.
VPN_HINTS = (
    "tun",
    "tap",
    "wg",
    "ppp",
    "utun",
    "ipsec",
    "vpn",
    "wireguard",
    "openvpn",
    "nordlynx",
    "zt",
    "tailscale",
    "proton",
)

WIFI_HINTS = ("wi-fi", "wifi", "wlan", "wlp", "airport", "en0", "wl")
ETHERNET_HINTS = ("ethernet", "eth", "enp", "eno", "ens", "lan")

IFACE_ETHERNET = 0.0
IFACE_WIFI = 1.0
IFACE_OTHER = 2.0


@dataclass(slots=True)
class RouteInfo:
    gateway: str | None = None
    interface: str | None = None
    local_ip: str | None = None
    is_vpn: bool = False
    vpn_interface: str | None = None
    vpn_mtu: int = 0
    interface_kind: float = IFACE_OTHER

    def as_dict(self) -> dict[str, object]:
        return {
            "gateway": self.gateway,
            "interface": self.interface,
            "local_ip": self.local_ip,
            "is_vpn": self.is_vpn,
            "vpn_interface": self.vpn_interface,
            "vpn_mtu": self.vpn_mtu,
            "interface_kind": self.interface_kind,
        }


_cache: tuple[float, RouteInfo] | None = None
_lock = threading.Lock()


def default_route(*, force: bool = False) -> RouteInfo:
    """Discover the default route, cached for :data:`CACHE_TTL_S` seconds."""
    global _cache
    with _lock:
        now = time.monotonic()
        if not force and _cache is not None and now - _cache[0] < CACHE_TTL_S:
            return _cache[1]
        info = _discover()
        _cache = (now, info)
        return info


def invalidate() -> None:
    global _cache
    with _lock:
        _cache = None


def _discover() -> RouteInfo:
    info = RouteInfo()
    try:
        if sys.platform.startswith("linux"):
            info.gateway, info.interface = _linux_default_route()
        elif sys.platform == "darwin":
            info.gateway, info.interface = _darwin_default_route()
        elif sys.platform == "win32":
            info.gateway, info.interface, info.local_ip = _windows_default_route()
    except Exception as exc:  # defensive: a route parse must never kill a cycle
        log.debug("default route discovery failed: %s", exc)

    if info.local_ip is None:
        info.local_ip = _local_ip_via_udp()
    if info.interface is None and info.local_ip:
        info.interface = _interface_for_ip(info.local_ip)
    info.interface_kind = classify_interface(info.interface)
    _annotate_vpn(info)
    return info


def _linux_default_route() -> tuple[str | None, str | None]:
    """Parse /proc/net/route, which needs no external tool and no privilege."""
    try:
        with open("/proc/net/route", encoding="ascii") as handle:
            lines = handle.read().splitlines()
    except OSError:
        return None, None
    best: tuple[int, str, str] | None = None
    for line in lines[1:]:
        fields = line.split()
        if len(fields) < 11:
            continue
        iface, destination, gateway, _flags, _refcnt, _use, metric = fields[:7]
        if destination != "00000000":
            continue
        try:
            metric_value = int(metric)
            gateway_ip = socket.inet_ntoa(struct.pack("<L", int(gateway, 16)))
        except (ValueError, OSError):
            continue
        if best is None or metric_value < best[0]:
            best = (metric_value, gateway_ip, iface)
    if best is None:
        return None, None
    return best[1], best[2]


def _darwin_default_route() -> tuple[str | None, str | None]:
    result = run(["route", "-n", "get", "default"], timeout=4.0)
    gateway = interface = None
    for line in result.text.splitlines():
        stripped = line.strip()
        if stripped.startswith("gateway:"):
            gateway = stripped.split(":", 1)[1].strip()
        elif stripped.startswith("interface:"):
            interface = stripped.split(":", 1)[1].strip()
    return gateway, interface


def _windows_default_route() -> tuple[str | None, str | None, str | None]:
    """Parse ``route print -4`` for the lowest-metric default route."""
    result = run(["route", "print", "-4"], timeout=6.0, check_path=False)
    best: tuple[int, str, str] | None = None
    in_table = False
    for line in result.text.splitlines():
        stripped = line.strip()
        if stripped.startswith("Network Destination"):
            in_table = True
            continue
        if in_table and stripped.startswith("="):
            in_table = False
            continue
        if not in_table:
            continue
        fields = stripped.split()
        if len(fields) < 5 or fields[0] != "0.0.0.0":
            continue
        gateway, interface_ip, metric = fields[2], fields[3], fields[4]
        if gateway.lower() in ("on-link", "on_link"):
            continue
        try:
            metric_value = int(metric)
            ipaddress.ip_address(gateway)
            ipaddress.ip_address(interface_ip)
        except ValueError:
            continue
        if best is None or metric_value < best[0]:
            best = (metric_value, gateway, interface_ip)
    if best is None:
        return None, None, None
    return best[1], _interface_for_ip(best[2]), best[2]


def _local_ip_via_udp() -> str | None:
    """Find the source address the kernel would use, without sending a packet."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.settimeout(0.5)
        sock.connect(("198.51.100.1", 9))  # TEST-NET-2, connect on UDP sends nothing
        return str(sock.getsockname()[0])
    except OSError:
        return None
    finally:
        sock.close()


def _interface_for_ip(ip: str) -> str | None:
    try:
        addresses = psutil.net_if_addrs()
    except Exception:
        return None
    for name, entries in addresses.items():
        for entry in entries:
            if entry.family == socket.AF_INET and entry.address == ip:
                return name
    return None


def classify_interface(name: str | None) -> float:
    """Map an interface name onto the ``os_iface_type`` categorical."""
    if not name:
        return IFACE_OTHER
    lowered = name.lower()
    if any(hint in lowered for hint in VPN_HINTS):
        return IFACE_OTHER
    if any(hint in lowered for hint in WIFI_HINTS):
        return IFACE_WIFI
    if any(hint in lowered for hint in ETHERNET_HINTS):
        return IFACE_ETHERNET
    return IFACE_OTHER


def is_vpn_interface(name: str | None) -> bool:
    if not name:
        return False
    lowered = name.lower()
    return any(hint in lowered for hint in VPN_HINTS)


def _annotate_vpn(info: RouteInfo) -> None:
    """Flag a tunnel when one exists and is carrying traffic.

    The strong signal is a tunnel interface owning the default route. The
    weaker signal, a tunnel that is merely up, is still reported because a
    split-tunnel VPN can degrade a subset of traffic without owning the
    default route.
    """
    if is_vpn_interface(info.interface):
        info.is_vpn = True
        info.vpn_interface = info.interface
    try:
        stats = psutil.net_if_stats()
    except Exception:
        return
    for name, entry in stats.items():
        if not entry.isup or not is_vpn_interface(name):
            continue
        if info.vpn_interface is None:
            info.vpn_interface = name
            info.is_vpn = True
        if name == info.vpn_interface:
            info.vpn_mtu = int(getattr(entry, "mtu", 0) or 0)


def active_interface_stats() -> tuple[str | None, object | None]:
    """Return the active interface name and its psutil stats entry."""
    route = default_route()
    name = route.interface
    if not name:
        return None, None
    try:
        return name, psutil.net_if_stats().get(name)
    except Exception:
        return name, None
