"""The canonical cross-layer feature schema.

One list of :class:`FeatureSpec` is the single source of truth for the sample
table DDL, the collector output contract, the feature vector fed to the model
ladder, and the column headers in an exported diagnostic bundle. Adding a
feature in one place therefore propagates everywhere, and the store migration
test catches any drift between the schema and the database.

Every feature is a timing, a counter, a rate or a coarse categorical. None of
them can hold a hostname, a URL or a packet payload, which is how PRD N2 is
enforced structurally rather than by review.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

Layer = Literal["wifi", "os", "dns", "gateway", "path", "remote_https", "vpn", "context"]

#: Layers the RCA stage is allowed to blame. ``context`` is excluded on
#: purpose: the hour of day is never the thing to go fix.
ATTRIBUTABLE_LAYERS: tuple[Layer, ...] = (
    "wifi",
    "os",
    "dns",
    "gateway",
    "path",
    "remote_https",
    "vpn",
)

HUMAN_LAYER_NAMES: dict[str, str] = {
    "wifi": "Wi-Fi",
    "os": "this device",
    "dns": "DNS",
    "gateway": "your router",
    "path": "the path to the internet",
    "remote_https": "the remote service",
    "vpn": "your VPN",
    "context": "context",
}


@dataclass(frozen=True, slots=True)
class FeatureSpec:
    """Description of one column in the feature vector.

    ``higher_is_worse`` drives the sign convention used by L1 z-scores and by
    RCA attribution, so a rising RSSI is never reported as a problem while a
    rising latency is.
    """

    name: str
    layer: Layer
    unit: str
    higher_is_worse: bool
    description: str
    categorical: bool = False
    clip_min: float | None = None
    clip_max: float | None = None


FEATURES: tuple[FeatureSpec, ...] = (
    # --- device and operating system -------------------------------------
    FeatureSpec(
        "os_iface_type",
        "os",
        "enum",
        False,
        "0 ethernet, 1 Wi-Fi, 2 other or unknown",
        categorical=True,
    ),
    FeatureSpec(
        "os_link_mbps", "os", "Mbit/s", False, "negotiated link speed of the active interface"
    ),
    FeatureSpec(
        "os_rx_drop_rate",
        "os",
        "fraction",
        True,
        "receive drops divided by receive packets over the interval",
        clip_min=0.0,
        clip_max=1.0,
    ),
    FeatureSpec(
        "os_tx_drop_rate",
        "os",
        "fraction",
        True,
        "transmit drops divided by transmit packets over the interval",
        clip_min=0.0,
        clip_max=1.0,
    ),
    FeatureSpec(
        "os_tx_err_rate",
        "os",
        "fraction",
        True,
        "transmit errors divided by transmit packets over the interval",
        clip_min=0.0,
        clip_max=1.0,
    ),
    FeatureSpec(
        "os_retrans_rate",
        "os",
        "fraction",
        True,
        "TCP segment retransmissions divided by segments sent",
        clip_min=0.0,
        clip_max=1.0,
    ),
    FeatureSpec(
        "os_cpu_pct",
        "os",
        "percent",
        True,
        "system-wide CPU utilisation",
        clip_min=0.0,
        clip_max=100.0,
    ),
    FeatureSpec(
        "os_mem_pct",
        "os",
        "percent",
        True,
        "system-wide memory utilisation",
        clip_min=0.0,
        clip_max=100.0,
    ),
    FeatureSpec(
        "os_on_battery",
        "os",
        "bool",
        False,
        "1 when the host is running on battery",
        categorical=True,
    ),
    # --- VPN --------------------------------------------------------------
    FeatureSpec(
        "vpn_active",
        "vpn",
        "bool",
        False,
        "1 when a tunnel interface carries the default route",
        categorical=True,
    ),
    FeatureSpec(
        "vpn_mtu", "vpn", "bytes", False, "MTU of the active tunnel interface, 0 when no tunnel"
    ),
    # --- Wi-Fi ------------------------------------------------------------
    FeatureSpec(
        "wifi_rssi_dbm",
        "wifi",
        "dBm",
        False,
        "received signal strength, closer to zero is stronger",
        clip_min=-100.0,
        clip_max=0.0,
    ),
    FeatureSpec(
        "wifi_signal_pct",
        "wifi",
        "percent",
        False,
        "signal quality as reported by the OS",
        clip_min=0.0,
        clip_max=100.0,
    ),
    FeatureSpec("wifi_link_mbps", "wifi", "Mbit/s", False, "current PHY rate of the association"),
    FeatureSpec(
        "wifi_tx_retry_rate",
        "wifi",
        "fraction",
        True,
        "transmit retries divided by transmitted frames",
        clip_min=0.0,
        clip_max=1.0,
    ),
    FeatureSpec(
        "wifi_noise_dbm",
        "wifi",
        "dBm",
        True,
        "noise floor where the platform exposes it",
        clip_min=-120.0,
        clip_max=0.0,
    ),
    FeatureSpec("wifi_channel", "wifi", "enum", False, "current channel number", categorical=True),
    FeatureSpec("wifi_band_ghz", "wifi", "GHz", False, "2.4, 5 or 6 depending on the association"),
    FeatureSpec(
        "wifi_roam_count", "wifi", "count", True, "BSSID changes observed since the previous sample"
    ),
    # --- DNS ---------------------------------------------------------------
    FeatureSpec("dns_p50_ms", "dns", "ms", True, "median resolution time over the probe set"),
    FeatureSpec("dns_p95_ms", "dns", "ms", True, "95th percentile resolution time"),
    FeatureSpec(
        "dns_fail_rate",
        "dns",
        "fraction",
        True,
        "fraction of DNS probes that failed or timed out",
        clip_min=0.0,
        clip_max=1.0,
    ),
    # --- gateway / first hop ------------------------------------------------
    FeatureSpec("gw_rtt_ms", "gateway", "ms", True, "round trip time to the default gateway"),
    FeatureSpec(
        "gw_jitter_ms",
        "gateway",
        "ms",
        True,
        "mean absolute deviation of gateway RTT within the probe burst",
    ),
    FeatureSpec(
        "gw_loss_rate",
        "gateway",
        "fraction",
        True,
        "fraction of gateway probes with no reply",
        clip_min=0.0,
        clip_max=1.0,
    ),
    # --- path to the internet ------------------------------------------------
    FeatureSpec("path_hops", "path", "count", False, "responsive hop count to the path target"),
    FeatureSpec("path_rtt_ms", "path", "ms", True, "RTT to the final responsive hop"),
    FeatureSpec(
        "path_second_hop_ms",
        "path",
        "ms",
        True,
        "RTT to the second hop, the usual first ISP device",
    ),
    FeatureSpec(
        "path_unresponsive_rate",
        "path",
        "fraction",
        True,
        "fraction of hops that did not answer",
        clip_min=0.0,
        clip_max=1.0,
    ),
    FeatureSpec(
        "path_changed",
        "path",
        "bool",
        True,
        "1 when the hop sequence differs from the previous trace",
        categorical=True,
    ),
    # --- synthetic HTTPS ------------------------------------------------------
    FeatureSpec(
        "https_dns_ms", "remote_https", "ms", True, "name resolution phase of the synthetic request"
    ),
    FeatureSpec("https_tcp_ms", "remote_https", "ms", True, "TCP connect phase"),
    FeatureSpec("https_tls_ms", "remote_https", "ms", True, "TLS handshake phase"),
    FeatureSpec("https_ttfb_ms", "remote_https", "ms", True, "time to first byte after request"),
    FeatureSpec("https_total_ms", "remote_https", "ms", True, "total synthetic request time"),
    FeatureSpec(
        "https_fail_rate",
        "remote_https",
        "fraction",
        True,
        "fraction of synthetic requests that failed",
        clip_min=0.0,
        clip_max=1.0,
    ),
    # --- context (never blamed, only conditioned on) --------------------------
    FeatureSpec(
        "captive_portal",
        "context",
        "bool",
        True,
        "1 when a captive portal intercepted the probe",
        categorical=True,
    ),
    FeatureSpec(
        "hour_of_day",
        "context",
        "hour",
        False,
        "local hour, used for seasonal baselines",
        categorical=True,
    ),
    FeatureSpec(
        "day_of_week", "context", "day", False, "0 Monday through 6 Sunday", categorical=True
    ),
)

FEATURE_NAMES: tuple[str, ...] = tuple(spec.name for spec in FEATURES)
FEATURE_INDEX: dict[str, int] = {name: i for i, name in enumerate(FEATURE_NAMES)}
FEATURE_BY_NAME: dict[str, FeatureSpec] = {spec.name: spec for spec in FEATURES}

#: Feature names grouped by layer, the bundling the L2 ensemble trains on.
LAYER_FEATURES: dict[str, tuple[str, ...]] = {
    layer: tuple(spec.name for spec in FEATURES if spec.layer == layer)
    for layer in dict.fromkeys(spec.layer for spec in FEATURES)
}

#: Features excluded from anomaly modelling because they carry no health
#: signal on their own; they are conditioning variables.
CONTEXT_FEATURES: frozenset[str] = frozenset(
    spec.name for spec in FEATURES if spec.layer == "context" or spec.name == "os_on_battery"
)

#: Features the model ladder scores. Categorical columns stay in the store for
#: display and for seasonal conditioning but never enter the anomaly models.
MODELLED_FEATURES: tuple[str, ...] = tuple(
    spec.name for spec in FEATURES if not spec.categorical and spec.name not in CONTEXT_FEATURES
)


def layer_of(feature: str) -> Layer:
    return FEATURE_BY_NAME[feature].layer


def clip(feature: str, value: float) -> float:
    """Clamp a value to the physically meaningful range for its feature."""
    spec = FEATURE_BY_NAME[feature]
    if spec.clip_min is not None:
        value = max(spec.clip_min, value)
    if spec.clip_max is not None:
        value = min(spec.clip_max, value)
    return value


def sql_column_definitions() -> str:
    """DDL fragment for the raw ``samples`` table, generated from the schema."""
    return ",\n    ".join(f'"{name}" REAL' for name in FEATURE_NAMES)


def describe() -> list[dict[str, object]]:
    """Machine-readable schema description, served by the API and the bundle."""
    return [
        {
            "name": spec.name,
            "layer": spec.layer,
            "unit": spec.unit,
            "higher_is_worse": spec.higher_is_worse,
            "categorical": spec.categorical,
            "description": spec.description,
        }
        for spec in FEATURES
    ]
