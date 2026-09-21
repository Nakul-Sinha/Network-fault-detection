"""Wi-Fi association collector.

There is no portable API for association state, so each platform gets its own
parser: ``netsh wlan show interfaces`` on Windows, ``iw`` with a
``/proc/net/wireless`` fallback on Linux, and ``airport`` with a
``system_profiler`` fallback on macOS. When none of them is usable the
collector reports itself unsupported and the rest of the agent carries on
without the Wi-Fi layer, which is the graceful degradation PRD 5.3 requires.

Network identity (SSID and BSSID) is never stored in the clear. It is hashed
with the per-install salt and kept only so the agent can notice a roam or a
network change.
"""

from __future__ import annotations

import re
import sys

from ..config import NetPulseConfig
from ..core.ratelimit import ProbeBudget
from ..privacy import hash_to_float
from . import netinfo
from .base import Capability, Collector, CollectorResult
from .shell import have, run

MACOS_AIRPORT = (
    "/System/Library/PrivateFrameworks/Apple80211.framework/Versions/Current/Resources/airport"
)


class WifiCollector(Collector):
    name = "wifi"
    layer = "wifi"
    active = False

    def __init__(self, config: NetPulseConfig, budget: ProbeBudget | None = None) -> None:
        super().__init__(config, budget)
        self._last_bssid_hash: float | None = None
        self._roam_count = 0
        self._prev_tx: tuple[int, int] | None = None

    def interval_s(self) -> float:
        return float(self.config.probes.wifi_interval_s)

    def enabled(self) -> bool:
        return self.config.collectors.wifi

    def detect_capability(self) -> Capability:
        if sys.platform == "win32":
            result = run(["netsh", "wlan", "show", "interfaces"], timeout=8.0, check_path=False)
            if result.missing:
                return Capability(False, "netsh is not available on this system")
            if "There is no wireless interface" in result.text:
                return Capability(False, "no wireless interface on this system")
            return Capability(True)
        if sys.platform.startswith("linux"):
            if have("iw"):
                return Capability(True)
            try:
                with open("/proc/net/wireless", encoding="ascii") as handle:
                    if len(handle.read().splitlines()) > 2:
                        return Capability(True)
            except OSError:
                pass
            return Capability(False, "no iw binary and no wireless device in /proc/net/wireless")
        if sys.platform == "darwin":
            if have(MACOS_AIRPORT) or have("system_profiler"):
                return Capability(
                    True,
                    "macOS may require Location permission to expose the SSID; "
                    "signal metrics still work without it",
                )
            return Capability(False, "no airport or system_profiler binary")
        return Capability(False, f"Wi-Fi metrics are not implemented for {sys.platform}")

    def _collect(self) -> CollectorResult:
        result = CollectorResult()
        route = netinfo.default_route()
        if route.interface_kind == netinfo.IFACE_ETHERNET:
            # On a wired link the radio may still be associated; reporting its
            # metrics would let RCA blame Wi-Fi for a problem on the cable.
            return CollectorResult(ok=True, skipped_reason="active link is ethernet")

        if sys.platform == "win32":
            fields = _parse_netsh(
                run(["netsh", "wlan", "show", "interfaces"], timeout=8.0, check_path=False).text
            )
        elif sys.platform.startswith("linux"):
            fields = _linux_fields(route.interface)
        elif sys.platform == "darwin":
            fields = _darwin_fields()
        else:  # pragma: no cover - guarded by detect_capability
            return CollectorResult(ok=True, skipped_reason="unsupported platform")

        if not fields:
            return CollectorResult(ok=True, skipped_reason="not associated with a network")

        for name in (
            "wifi_rssi_dbm",
            "wifi_signal_pct",
            "wifi_link_mbps",
            "wifi_noise_dbm",
            "wifi_channel",
            "wifi_band_ghz",
            "wifi_tx_retry_rate",
        ):
            if name in fields:
                result.set(name, fields[name])

        if "wifi_rssi_dbm" not in fields and "wifi_signal_pct" in fields:
            result.set("wifi_rssi_dbm", signal_pct_to_dbm(fields["wifi_signal_pct"]))
        if "wifi_band_ghz" not in fields and "wifi_channel" in fields:
            result.set("wifi_band_ghz", band_for_channel(int(fields["wifi_channel"])))

        result.set("wifi_roam_count", float(self._track_roam(fields.get("bssid_hash"))))
        return result

    def _track_roam(self, bssid_hash: float | None) -> int:
        if bssid_hash is None:
            return 0
        if self._last_bssid_hash is not None and bssid_hash != self._last_bssid_hash:
            self._roam_count += 1
            self._last_bssid_hash = bssid_hash
            return 1
        self._last_bssid_hash = bssid_hash
        return 0


# --------------------------------------------------------------------- Windows


def _parse_netsh(text: str) -> dict[str, float]:
    """Parse ``netsh wlan show interfaces``.

    Windows 11 reports ``Rssi`` directly; older builds only report
    ``Signal`` as a percentage, which is mapped with the documented linear
    relationship in :func:`signal_pct_to_dbm`.
    """
    raw: dict[str, str] = {}
    for line in text.splitlines():
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        key = key.strip().lower()
        if key and key not in raw:
            raw[key] = value.strip()

    if raw.get("state", "").lower() not in ("connected", "associated"):
        return {}

    fields: dict[str, float] = {}
    _put_float(fields, "wifi_rssi_dbm", raw.get("rssi"))
    _put_float(fields, "wifi_signal_pct", raw.get("signal"), strip="%")
    _put_float(fields, "wifi_link_mbps", raw.get("receive rate (mbps)"))
    if "wifi_link_mbps" not in fields:
        _put_float(fields, "wifi_link_mbps", raw.get("transmit rate (mbps)"))
    _put_float(fields, "wifi_channel", raw.get("channel"))
    band = raw.get("band", "")
    if band:
        _put_float(fields, "wifi_band_ghz", band.replace("GHz", "").replace("ghz", ""))
    bssid = raw.get("ap bssid") or raw.get("bssid")
    if bssid:
        fields["bssid_hash"] = hash_to_float(bssid)
    return fields


# ----------------------------------------------------------------------- Linux


def _linux_fields(interface: str | None) -> dict[str, float]:
    fields: dict[str, float] = {}
    if have("iw"):
        name = interface or _first_wireless_interface()
        if name:
            fields.update(_parse_iw_link(run(["iw", "dev", name, "link"], timeout=6.0).text))
            fields.update(
                _parse_iw_station(run(["iw", "dev", name, "station", "dump"], timeout=6.0).text)
            )
    if "wifi_rssi_dbm" not in fields:
        fields.update(_parse_proc_wireless())
    return fields


def _first_wireless_interface() -> str | None:
    try:
        with open("/proc/net/wireless", encoding="ascii") as handle:
            lines = handle.read().splitlines()
    except OSError:
        return None
    for line in lines[2:]:
        name = line.split(":")[0].strip()
        if name:
            return name
    return None


def _parse_iw_link(text: str) -> dict[str, float]:
    fields: dict[str, float] = {}
    if "Not connected" in text:
        return fields
    match = re.search(r"Connected to ([0-9a-fA-F:]{17})", text)
    if match:
        fields["bssid_hash"] = hash_to_float(match.group(1))
    match = re.search(r"signal:\s*(-?\d+)\s*dBm", text)
    if match:
        fields["wifi_rssi_dbm"] = float(match.group(1))
    match = re.search(r"tx bitrate:\s*([\d.]+)\s*MBit/s", text)
    if match:
        fields["wifi_link_mbps"] = float(match.group(1))
    match = re.search(r"freq:\s*(\d+)", text)
    if match:
        frequency = int(match.group(1))
        fields["wifi_band_ghz"] = band_for_frequency(frequency)
        fields["wifi_channel"] = float(channel_for_frequency(frequency))
    return fields


def _parse_iw_station(text: str) -> dict[str, float]:
    """Derive the transmit retry rate from ``iw station dump`` counters."""
    fields: dict[str, float] = {}
    packets = _search_int(text, r"tx packets:\s*(\d+)")
    retries = _search_int(text, r"tx retries:\s*(\d+)")
    failed = _search_int(text, r"tx failed:\s*(\d+)")
    if packets and packets > 0 and retries is not None:
        fields["wifi_tx_retry_rate"] = min(1.0, (retries + (failed or 0)) / packets)
    return fields


def _parse_proc_wireless() -> dict[str, float]:
    """Fallback for hosts with no iw binary: the legacy wireless stats file."""
    try:
        with open("/proc/net/wireless", encoding="ascii") as handle:
            lines = handle.read().splitlines()
    except OSError:
        return {}
    for line in lines[2:]:
        parts = line.split()
        if len(parts) < 5:
            continue
        try:
            quality = float(parts[2].rstrip("."))
            level = float(parts[3].rstrip("."))
            noise = float(parts[4].rstrip("."))
        except ValueError:
            continue
        fields = {"wifi_rssi_dbm": level, "wifi_signal_pct": min(100.0, quality * 100.0 / 70.0)}
        if noise < 0:
            fields["wifi_noise_dbm"] = noise
        return fields
    return {}


# ----------------------------------------------------------------------- macOS


def _darwin_fields() -> dict[str, float]:
    if have(MACOS_AIRPORT):
        fields = _parse_airport(run([MACOS_AIRPORT, "-I"], timeout=6.0, check_path=False).text)
        if fields:
            return fields
    return _parse_system_profiler(
        run(["system_profiler", "SPAirPortDataType", "-detailLevel", "basic"], timeout=12.0).text
    )


def _parse_airport(text: str) -> dict[str, float]:
    raw: dict[str, str] = {}
    for line in text.splitlines():
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        raw[key.strip().lower()] = value.strip()
    fields: dict[str, float] = {}
    _put_float(fields, "wifi_rssi_dbm", raw.get("agrctlrssi"))
    _put_float(fields, "wifi_noise_dbm", raw.get("agrctlnoise"))
    _put_float(fields, "wifi_link_mbps", raw.get("lasttxrate"))
    _put_float(fields, "wifi_channel", (raw.get("channel") or "").split(",")[0])
    if raw.get("bssid"):
        fields["bssid_hash"] = hash_to_float(raw["bssid"])
    return fields


def _parse_system_profiler(text: str) -> dict[str, float]:
    fields: dict[str, float] = {}
    section = text.split("Current Network Information", 1)
    body = section[1] if len(section) > 1 else text
    match = re.search(r"Signal\s*/\s*Noise:\s*(-?\d+)\s*dBm\s*/\s*(-?\d+)\s*dBm", body)
    if match:
        fields["wifi_rssi_dbm"] = float(match.group(1))
        fields["wifi_noise_dbm"] = float(match.group(2))
    match = re.search(r"Transmit Rate:\s*([\d.]+)", body)
    if match:
        fields["wifi_link_mbps"] = float(match.group(1))
    match = re.search(r"Channel:\s*(\d+)", body)
    if match:
        fields["wifi_channel"] = float(match.group(1))
    return fields


# ----------------------------------------------------------------------- shared


def signal_pct_to_dbm(percent: float) -> float:
    """Map a Windows signal quality percentage onto dBm.

    Windows documents quality as a linear scale where 0% is -100 dBm and
    100% is -50 dBm, so the inverse is ``dBm = pct / 2 - 100``.
    """
    return max(-100.0, min(-50.0, percent / 2.0 - 100.0))


def band_for_channel(channel: int) -> float:
    if 1 <= channel <= 14:
        return 2.4
    if 32 <= channel <= 177:
        return 5.0
    return 6.0


def band_for_frequency(mhz: int) -> float:
    if mhz < 3000:
        return 2.4
    if mhz < 5960:
        return 5.0
    return 6.0


def channel_for_frequency(mhz: int) -> int:
    if mhz == 2484:
        return 14
    if 2412 <= mhz <= 2472:
        return (mhz - 2407) // 5
    if 5160 <= mhz <= 5885:
        return (mhz - 5000) // 5
    if 5955 <= mhz <= 7115:
        return (mhz - 5950) // 5
    return 0


def _put_float(target: dict[str, float], key: str, raw: str | None, strip: str = "") -> None:
    if not raw:
        return
    cleaned = raw.strip()
    if strip:
        cleaned = cleaned.replace(strip, "")
    cleaned = cleaned.split()[0] if cleaned.split() else ""
    try:
        target[key] = float(cleaned)
    except ValueError:
        return


def _search_int(text: str, pattern: str) -> int | None:
    match = re.search(pattern, text)
    return int(match.group(1)) if match else None
