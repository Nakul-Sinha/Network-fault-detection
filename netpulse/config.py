"""Configuration model, defaults, and TOML/env loading.

Defaults encode the policy the PRD makes non-negotiable: privacy-local
storage (N1, N2), enforced probe budgets (N5), and cadences inside the ranges
in PRD section 6.3. Anything a user can change lives in one TOML file so that
the privacy posture of an install is auditable by reading a single document.
"""

from __future__ import annotations

import ipaddress
import os
import socket
import tomllib
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlparse

from pydantic import BaseModel, Field, field_validator, model_validator

from . import paths

ENV_PREFIX = "NETPULSE_"

LAYERS = ("wifi", "os", "dns", "gateway", "path", "remote_https", "vpn")
Severity = Literal["info", "watch", "risk", "critical"]


class CollectorToggles(BaseModel):
    """Per-layer kill switches (F11). A disabled collector emits nothing."""

    model_config = {"extra": "forbid"}

    system: bool = True
    wifi: bool = True
    dns: bool = True
    gateway: bool = True
    path: bool = True
    https: bool = True
    captive: bool = True
    flows: bool = False  # F12, opt-in only, metadata never payloads


class ProbeConfig(BaseModel):
    """Probe cadence, targets and hard budgets.

    Intervals are the nominal period; the scheduler adds jitter so repeated
    runs never synchronise onto the same second. ``max_per_minute`` is a
    circuit breaker enforced in code, independent of the interval, so a bad
    config cannot turn the agent into a traffic generator.
    """

    model_config = {"extra": "forbid"}

    system_interval_s: int = Field(15, ge=5, le=300)
    wifi_interval_s: int = Field(20, ge=5, le=300)
    dns_interval_s: int = Field(45, ge=10, le=600)
    gateway_interval_s: int = Field(20, ge=5, le=300)
    https_interval_s: int = Field(90, ge=30, le=900)
    path_interval_s: int = Field(600, ge=120, le=7200)
    captive_interval_s: int = Field(300, ge=60, le=3600)

    jitter_fraction: float = Field(0.15, ge=0.0, le=0.5)

    dns_targets: list[str] = Field(
        default_factory=lambda: ["example.com", "cloudflare.com", "wikipedia.org"]
    )
    https_targets: list[str] = Field(
        default_factory=lambda: [
            "https://www.cloudflare.com/cdn-cgi/trace",
            "https://www.google.com/generate_204",
        ]
    )
    captive_probe_url: str = "http://detectportal.firefox.com/success.txt"
    path_target: str = "1.1.1.1"

    max_https_per_minute: int = Field(4, ge=1, le=30)
    max_dns_per_minute: int = Field(8, ge=1, le=60)
    max_icmp_per_minute: int = Field(20, ge=1, le=120)
    max_traceroute_per_hour: int = Field(12, ge=1, le=60)

    timeout_s: float = Field(5.0, ge=0.5, le=30.0)

    @field_validator("https_targets")
    @classmethod
    def _validate_https_targets(cls, value: list[str]) -> list[str]:
        if not value:
            raise ValueError("at least one HTTPS probe target is required")
        for url in value:
            assert_safe_probe_url(url, require_tls=True)
        return value

    @field_validator("captive_probe_url")
    @classmethod
    def _validate_captive(cls, value: str) -> str:
        assert_safe_probe_url(value, require_tls=False)
        return value

    @field_validator("dns_targets")
    @classmethod
    def _validate_dns(cls, value: list[str]) -> list[str]:
        for name in value:
            if not name or len(name) > 253 or "/" in name or " " in name:
                raise ValueError("invalid DNS probe name: " + repr(name))
        return value


class PrivacyConfig(BaseModel):
    """PRD section 12.1. Every field here defaults to the quieter option."""

    model_config = {"extra": "forbid"}

    hash_ssid: bool = True
    hash_dns_names: bool = True
    store_payloads: bool = False  # an explicit, permanently false invariant
    product_analytics: bool = False
    crash_reports: bool = False

    @field_validator("store_payloads")
    @classmethod
    def _no_payloads(cls, value: bool) -> bool:
        if value:
            raise ValueError(
                "payload capture is not implemented and cannot be enabled; "
                "NetPulse Local stores timings and counters only"
            )
        return value


class RetentionConfig(BaseModel):
    model_config = {"extra": "forbid"}

    raw_days: int = Field(7, ge=1, le=365)
    downsample_after_hours: int = Field(48, ge=1, le=8760)
    rollup_days: int = Field(90, ge=1, le=3650)
    vacuum_interval_hours: int = Field(24, ge=1, le=720)


class NotificationConfig(BaseModel):
    model_config = {"extra": "forbid"}

    enabled: bool = True
    quiet_hours_start: int = Field(22, ge=0, le=23)
    quiet_hours_end: int = Field(7, ge=0, le=23)
    cooldown_minutes: int = Field(30, ge=1, le=1440)
    min_severity: Severity = "risk"
    focus_mode: bool = False  # manual sensitivity boost before an important call


class ApiConfig(BaseModel):
    """Loopback-only control API (N7). The host is validated, not trusted."""

    model_config = {"extra": "forbid"}

    enabled: bool = True
    host: str = "127.0.0.1"
    port: int = Field(8787, ge=1024, le=65535)
    open_browser: bool = False

    @field_validator("host")
    @classmethod
    def _loopback_only(cls, value: str) -> str:
        try:
            address = ipaddress.ip_address(value)
        except ValueError as exc:
            raise ValueError("api.host must be a literal loopback IP address") from exc
        if not address.is_loopback:
            raise ValueError(
                "api.host must stay on loopback; NetPulse never binds the control API "
                "to a routable interface"
            )
        return value


class MlConfig(BaseModel):
    model_config = {"extra": "forbid"}

    warmup_minutes: int = Field(30, ge=1, le=1440)
    sensitivity: float = Field(0.5, ge=0.0, le=1.0)
    l2_learning_rate: float = Field(0.02, gt=0.0, le=1.0)
    l2_hidden_ratio: float = Field(0.6, gt=0.0, le=1.0)
    l3_enabled: bool = True
    l3_model_path: str | None = None
    online_l3_updates: bool = False  # v1 candidate, off by default
    drift_psi_threshold: float = Field(0.25, gt=0.0, le=2.0)
    risk_bands: dict[str, float] = Field(
        default_factory=lambda: {"watch": 0.25, "risk": 0.5, "critical": 0.75}
    )

    @field_validator("risk_bands")
    @classmethod
    def _bands_ordered(cls, value: dict[str, float]) -> dict[str, float]:
        missing = {"watch", "risk", "critical"} - set(value)
        if missing:
            raise ValueError("risk_bands missing keys: " + repr(sorted(missing)))
        if not 0 < value["watch"] < value["risk"] < value["critical"] < 1:
            raise ValueError("risk_bands must satisfy 0 < watch < risk < critical < 1")
        return value


class PowerConfig(BaseModel):
    model_config = {"extra": "forbid"}

    battery_backoff: bool = True
    battery_interval_multiplier: float = Field(2.0, ge=1.0, le=10.0)
    battery_threshold_pct: int = Field(30, ge=0, le=100)


class NetPulseConfig(BaseModel):
    """Root configuration object handed to every subsystem."""

    model_config = {"extra": "forbid"}

    profile: str = "default"
    headless: bool = False
    collectors: CollectorToggles = Field(default_factory=CollectorToggles)
    probes: ProbeConfig = Field(default_factory=ProbeConfig)
    privacy: PrivacyConfig = Field(default_factory=PrivacyConfig)
    retention: RetentionConfig = Field(default_factory=RetentionConfig)
    notifications: NotificationConfig = Field(default_factory=NotificationConfig)
    api: ApiConfig = Field(default_factory=ApiConfig)
    ml: MlConfig = Field(default_factory=MlConfig)
    power: PowerConfig = Field(default_factory=PowerConfig)

    @model_validator(mode="after")
    def _check_budget_consistency(self) -> NetPulseConfig:
        implied_https = 60 / self.probes.https_interval_s * len(self.probes.https_targets)
        if implied_https > self.probes.max_https_per_minute:
            raise ValueError(
                f"configured HTTPS cadence implies {implied_https:.1f} probes/min which "
                f"exceeds max_https_per_minute={self.probes.max_https_per_minute}"
            )
        implied_dns = 60 / self.probes.dns_interval_s * len(self.probes.dns_targets)
        if implied_dns > self.probes.max_dns_per_minute:
            raise ValueError(
                f"configured DNS cadence implies {implied_dns:.1f} probes/min which "
                f"exceeds max_dns_per_minute={self.probes.max_dns_per_minute}"
            )
        return self

    def redacted(self) -> dict[str, Any]:
        """Config as a dict that is safe to place in a diagnostic bundle."""
        data = self.model_dump(mode="json")
        data["api"]["token_present"] = paths.api_token_file().exists()
        return data


def assert_safe_probe_url(url: str, *, require_tls: bool) -> None:
    """Reject probe targets that would turn the agent into a scanning tool.

    PRD section 12.2 requires the agent to validate probe URLs: block
    non-HTTP schemes, block loopback and link-local abuse, and keep the agent
    off arbitrary ports.
    """
    parsed = urlparse(url)
    if parsed.scheme not in ("https", "http"):
        raise ValueError("probe URL must be http or https: " + repr(url))
    if require_tls and parsed.scheme != "https":
        raise ValueError("HTTPS probe target must use TLS: " + repr(url))
    if not parsed.hostname:
        raise ValueError("probe URL has no host: " + repr(url))
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("probe URL has an invalid port: " + repr(url)) from exc
    if port is not None and port not in (80, 443, 8443):
        raise ValueError("probe URL port is not in the allowed set (80, 443, 8443): " + repr(url))

    try:
        address = ipaddress.ip_address(parsed.hostname)
    except ValueError:
        return
    if address.is_loopback or address.is_link_local or address.is_multicast:
        raise ValueError(
            "probe URL targets a loopback, link-local or multicast address: " + repr(url)
        )
    if address.is_reserved or address.is_unspecified:
        raise ValueError("probe URL targets a reserved address: " + repr(url))


def resolve_probe_host(hostname: str) -> str | None:
    """Resolve a probe hostname, returning None when resolution fails."""
    try:
        return socket.gethostbyname(hostname)
    except OSError:
        return None


def _coerce(raw: str) -> Any:
    lowered = raw.strip().lower()
    if lowered in ("true", "yes", "on"):
        return True
    if lowered in ("false", "no", "off"):
        return False
    try:
        return int(raw)
    except ValueError:
        pass
    try:
        return float(raw)
    except ValueError:
        pass
    if "," in raw:
        return [item.strip() for item in raw.split(",") if item.strip()]
    return raw


def _env_overrides() -> dict[str, Any]:
    """Map NETPULSE_API__PORT=9000 style variables onto nested config keys."""
    overrides: dict[str, Any] = {}
    for key, value in os.environ.items():
        if not key.startswith(ENV_PREFIX):
            continue
        suffix = key[len(ENV_PREFIX) :]
        if suffix in ("HOME", "CONFIG", "HEADLESS"):
            continue
        parts = [part.lower() for part in suffix.split("__")]
        cursor = overrides
        for part in parts[:-1]:
            nested = cursor.setdefault(part, {})
            if not isinstance(nested, dict):
                nested = {}
                cursor[part] = nested
            cursor = nested
        cursor[parts[-1]] = _coerce(value)
    if os.environ.get(ENV_PREFIX + "HEADLESS", "").strip().lower() in ("1", "true", "yes", "on"):
        overrides["headless"] = True
    return overrides


def _deep_merge(base: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in patch.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def load_config(path: os.PathLike[str] | str | None = None) -> NetPulseConfig:
    """Load configuration from defaults, then the TOML file, then environment."""
    config_path = paths.config_file() if path is None else Path(os.fspath(path))
    data: dict[str, Any] = {}
    try:
        with open(config_path, "rb") as handle:
            data = tomllib.load(handle)
    except FileNotFoundError:
        data = {}
    merged = _deep_merge(data, _env_overrides())
    return NetPulseConfig.model_validate(merged)


DEFAULT_CONFIG_TOML = """\
# NetPulse Local configuration.
# Every value below is a built-in default; change one and restart the agent.
# Delete this file to return to defaults.

profile = "default"

[collectors]
# Per-layer kill switches. Turning one off stops both probing and scoring for
# that layer; the rest of the agent degrades gracefully.
system = true
wifi = true
dns = true
gateway = true
path = true
https = true
captive = true
flows = false      # opt-in, metadata only, never payloads

[probes]
# Cadences stay inside the ranges in PRD section 6.3. The max_* values are
# hard budgets enforced in code, not merely implied by the cadence.
system_interval_s = 15
wifi_interval_s = 20
dns_interval_s = 45
gateway_interval_s = 20
https_interval_s = 90
path_interval_s = 600
max_https_per_minute = 4
max_dns_per_minute = 8
max_icmp_per_minute = 20
max_traceroute_per_hour = 12
# https_targets must use TLS and are checked by config.assert_safe_probe_url.
# https_targets = ["https://www.cloudflare.com/cdn-cgi/trace"]
# dns_targets = ["example.com", "cloudflare.com", "wikipedia.org"]

[privacy]
hash_ssid = true
hash_dns_names = true
product_analytics = false
crash_reports = false

[retention]
raw_days = 7
downsample_after_hours = 48
rollup_days = 90

[notifications]
enabled = true
quiet_hours_start = 22
quiet_hours_end = 7
cooldown_minutes = 30
min_severity = "risk"

[api]
enabled = true
host = "127.0.0.1"
port = 8787

[ml]
warmup_minutes = 30
sensitivity = 0.5

[power]
battery_backoff = true
"""


def write_default_config(path: os.PathLike[str] | str | None = None) -> Path:
    """Create the starter config when none exists. Returns its path."""
    target = paths.config_file() if path is None else Path(os.fspath(path))
    if not target.exists():
        paths.write_private(target, DEFAULT_CONFIG_TOML)
    return target
