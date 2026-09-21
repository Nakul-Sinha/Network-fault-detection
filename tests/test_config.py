"""Configuration validation.

These are the tests that matter most for safety: the config layer is where
"the agent must not scan arbitrary hosts" and "the API must not leave
loopback" stop being prose and become code.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from netpulse import paths
from netpulse.config import (
    DEFAULT_CONFIG_TOML,
    NetPulseConfig,
    assert_safe_probe_url,
    load_config,
    write_default_config,
)


def test_defaults_are_valid():
    config = NetPulseConfig()
    assert config.api.host == "127.0.0.1"
    assert config.privacy.store_payloads is False
    assert config.collectors.flows is False, "flow collection must be opt-in"


def test_default_toml_parses_and_round_trips():
    import tomllib

    parsed = tomllib.loads(DEFAULT_CONFIG_TOML)
    assert NetPulseConfig.model_validate(parsed).api.port == 8787


@pytest.mark.parametrize(
    "host",
    ["0.0.0.0", "192.168.1.5", "::", "example.com"],
)
def test_api_host_must_be_loopback(host):
    with pytest.raises(ValidationError):
        NetPulseConfig.model_validate({"api": {"host": host}})


@pytest.mark.parametrize("host", ["127.0.0.1", "127.0.0.53", "::1"])
def test_loopback_hosts_are_accepted(host):
    assert NetPulseConfig.model_validate({"api": {"host": host}}).api.host == host


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "ftp://example.com/x",
        "https://127.0.0.1/x",
        "https://169.254.169.254/latest/meta-data",
        "https://224.0.0.1/x",
        "https://example.com:9999/x",
        "https://[::1]/x",
        "http://example.com/x",  # HTTPS targets must use TLS
    ],
)
def test_unsafe_probe_urls_are_rejected(url):
    with pytest.raises(ValueError):
        assert_safe_probe_url(url, require_tls=True)


@pytest.mark.parametrize(
    "url",
    ["https://example.com/", "https://example.com:443/x", "https://example.com:8443/x"],
)
def test_safe_probe_urls_are_accepted(url):
    assert_safe_probe_url(url, require_tls=True)


def test_probe_budget_is_cross_checked_against_cadence():
    """An aggressive cadence must fail at load time, not at runtime."""
    with pytest.raises(ValidationError, match="probes/min"):
        NetPulseConfig.model_validate(
            {
                "probes": {
                    "https_interval_s": 30,
                    "https_targets": [
                        "https://a.example/",
                        "https://b.example/",
                        "https://c.example/",
                    ],
                    "max_https_per_minute": 4,
                }
            }
        )


def test_payload_capture_cannot_be_enabled():
    with pytest.raises(ValidationError, match="payload capture"):
        NetPulseConfig.model_validate({"privacy": {"store_payloads": True}})


def test_risk_bands_must_be_ordered():
    with pytest.raises(ValidationError):
        NetPulseConfig.model_validate(
            {"ml": {"risk_bands": {"watch": 0.9, "risk": 0.5, "critical": 0.95}}}
        )


def test_unknown_keys_are_rejected():
    """A typo in a config file should be loud, not silently ignored."""
    with pytest.raises(ValidationError):
        NetPulseConfig.model_validate({"probes": {"htps_interval_s": 30}})


def test_env_overrides_nested_keys(monkeypatch):
    monkeypatch.setenv("NETPULSE_API__PORT", "9123")
    monkeypatch.setenv("NETPULSE_NOTIFICATIONS__ENABLED", "false")
    monkeypatch.setenv("NETPULSE_RETENTION__RAW_DAYS", "3")
    config = load_config()
    assert config.api.port == 9123
    assert config.notifications.enabled is False
    assert config.retention.raw_days == 3


def test_headless_env_flag(monkeypatch):
    monkeypatch.setenv("NETPULSE_HEADLESS", "1")
    assert load_config().headless is True


def test_config_file_is_read(tmp_path):
    path = tmp_path / "custom.toml"
    path.write_text("[api]\nport = 9999\n[ml]\nwarmup_minutes = 5\n", encoding="utf-8")
    config = load_config(path)
    assert config.api.port == 9999
    assert config.ml.warmup_minutes == 5


def test_missing_config_file_falls_back_to_defaults(tmp_path):
    config = load_config(tmp_path / "does-not-exist.toml")
    assert config.api.port == 8787


def test_write_default_config_creates_once():
    first = write_default_config()
    assert first.exists()
    first.write_text("# edited\n", encoding="utf-8")
    write_default_config()
    assert first.read_text(encoding="utf-8") == "# edited\n", "must not clobber user edits"


def test_redacted_config_has_no_token_value(monkeypatch):
    paths.write_private(paths.api_token_file(), "super-secret-token-value")
    data = NetPulseConfig().redacted()
    assert data["api"]["token_present"] is True
    assert "super-secret-token-value" not in str(data)
