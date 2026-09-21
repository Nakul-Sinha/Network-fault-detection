"""Shared fixtures.

Every test runs against a throwaway ``NETPULSE_HOME``, so nothing touches a
developer's real configuration, database or API token, and a failing test
cannot leave state behind that makes the next run pass.
"""

from __future__ import annotations

import os
import time
from collections.abc import Iterator

import pytest

from netpulse import paths, privacy
from netpulse.config import NetPulseConfig
from netpulse.store.db import Database
from netpulse.store.models import Sample
from netpulse.store.repository import Repository


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch) -> Iterator[str]:
    """Point every path at a temporary directory for the whole test."""
    home = tmp_path / "netpulse-home"
    monkeypatch.setenv("NETPULSE_HOME", str(home))
    # The salt is cached per process; a new home needs a new salt.
    privacy.reset_cache()
    paths.ensure_dirs()
    yield str(home)
    privacy.reset_cache()


@pytest.fixture(autouse=True)
def clean_env(monkeypatch) -> None:
    """Remove any NETPULSE_* override that could change test behaviour."""
    for key in list(os.environ):
        if key.startswith("NETPULSE_") and key != "NETPULSE_HOME":
            monkeypatch.delenv(key, raising=False)


@pytest.fixture
def config() -> NetPulseConfig:
    return NetPulseConfig()


@pytest.fixture
def database() -> Iterator[Database]:
    db = Database(paths.database_file())
    yield db
    db.close()


@pytest.fixture
def repo(database: Database) -> Repository:
    return Repository(database)


@pytest.fixture
def quiet_config() -> NetPulseConfig:
    """A config with every active collector off, for tests that must not probe."""
    return NetPulseConfig.model_validate(
        {
            "collectors": {
                "system": True,
                "wifi": False,
                "dns": False,
                "gateway": False,
                "path": False,
                "https": False,
                "captive": False,
            }
        }
    )


def make_sample(ts: float | None = None, **values: float) -> Sample:
    """Build a Sample from keyword feature values."""
    sample = Sample(ts=ts if ts is not None else time.time())
    for name, value in values.items():
        sample.set(name, value)
    return sample


def healthy_stream(
    count: int = 400, *, start: float = 1_780_000_000.0, step: float = 15.0
) -> list[Sample]:
    """A deterministic, boring stream used to warm models up in tests."""
    import random

    rng = random.Random(4)
    samples: list[Sample] = []
    for index in range(count):
        samples.append(
            make_sample(
                ts=start + index * step,
                gw_rtt_ms=3.0 + rng.gauss(0, 0.3),
                gw_jitter_ms=0.4 + abs(rng.gauss(0, 0.1)),
                gw_loss_rate=0.0,
                dns_p50_ms=20.0 + rng.gauss(0, 2),
                dns_p95_ms=35.0 + rng.gauss(0, 3),
                dns_fail_rate=0.0,
                https_ttfb_ms=60.0 + rng.gauss(0, 5),
                https_tcp_ms=25.0 + rng.gauss(0, 3),
                https_tls_ms=30.0 + rng.gauss(0, 3),
                https_total_ms=120.0 + rng.gauss(0, 8),
                https_fail_rate=0.0,
                wifi_rssi_dbm=-52.0 + rng.gauss(0, 1.5),
                wifi_tx_retry_rate=0.03,
                wifi_link_mbps=300.0,
                wifi_signal_pct=88.0,
                wifi_noise_dbm=-95.0,
                wifi_band_ghz=5.0,
                wifi_roam_count=0.0,
                path_rtt_ms=13.0 + rng.gauss(0, 1),
                path_hops=10.0,
                path_second_hop_ms=6.0,
                path_unresponsive_rate=0.0,
                path_changed=0.0,
                os_cpu_pct=16.0 + rng.gauss(0, 3),
                os_mem_pct=55.0,
                os_retrans_rate=0.005,
                os_rx_drop_rate=0.0,
                os_tx_drop_rate=0.0,
                os_tx_err_rate=0.0,
                os_link_mbps=300.0,
                os_iface_type=1.0,
                os_on_battery=0.0,
                vpn_active=0.0,
                vpn_mtu=0.0,
                captive_portal=0.0,
            )
        )
    return samples
