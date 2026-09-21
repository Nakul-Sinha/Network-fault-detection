"""Feature store, retention, and the privacy primitives underneath it."""

from __future__ import annotations

import time

import pytest

from netpulse import paths, privacy
from netpulse.features.schema import (
    FEATURE_NAMES,
    MODELLED_FEATURES,
    FeatureSpec,
    clip,
    describe,
    layer_of,
)
from netpulse.store.db import SCHEMA_VERSION, Database
from netpulse.store.models import DriftEvent, Incident, Label, ProbeRecord, Sample, Score
from netpulse.store.repository import Repository

from .conftest import make_sample

# ------------------------------------------------------------------ schema


def test_feature_names_are_unique_and_sql_safe():
    assert len(set(FEATURE_NAMES)) == len(FEATURE_NAMES)
    for name in FEATURE_NAMES:
        assert name.replace("_", "").isalnum(), name
        assert not name[0].isdigit()


def test_no_feature_can_hold_free_text():
    """The privacy guarantee is structural: every column is numeric."""
    for row in describe():
        assert row["unit"] in (
            "enum",
            "bool",
            "ms",
            "percent",
            "fraction",
            "count",
            "dBm",
            "Mbit/s",
            "GHz",
            "bytes",
            "hour",
            "day",
        ), row


def test_clip_respects_declared_bounds():
    assert clip("wifi_rssi_dbm", -500.0) == -100.0
    assert clip("dns_fail_rate", 5.0) == 1.0
    assert clip("dns_fail_rate", -1.0) == 0.0
    assert clip("gw_rtt_ms", 12345.0) == 12345.0, "unbounded features pass through"


def test_context_features_are_excluded_from_modelling():
    assert "hour_of_day" not in MODELLED_FEATURES
    assert "captive_portal" not in MODELLED_FEATURES
    assert "gw_rtt_ms" in MODELLED_FEATURES


def test_layer_of_matches_prefix_convention():
    assert layer_of("wifi_rssi_dbm") == "wifi"
    assert layer_of("https_ttfb_ms") == "remote_https"
    assert layer_of("gw_rtt_ms") == "gateway"


def test_feature_spec_is_immutable():
    """The schema is shared mutable-looking state; freezing it is deliberate."""
    spec = FeatureSpec("x", "os", "ms", True, "d")
    with pytest.raises((AttributeError, TypeError)):
        spec.name = "y"  # type: ignore[misc]


# ------------------------------------------------------------------- store


def test_migration_sets_version_and_is_idempotent(database: Database):
    assert database.user_version() == SCHEMA_VERSION
    assert database.migrate() == SCHEMA_VERSION


def test_schema_columns_match_the_feature_schema(database: Database):
    rows = database.query("PRAGMA table_info(samples)")
    columns = {row["name"] for row in rows}
    assert columns == {"ts", *FEATURE_NAMES}


def test_newer_schema_is_refused(database: Database):
    database.execute(f"PRAGMA user_version={SCHEMA_VERSION + 5}")
    with pytest.raises(RuntimeError, match="newer NetPulse"):
        database.migrate()


def test_sample_round_trip(repo: Repository):
    sample = make_sample(ts=1000.0, gw_rtt_ms=4.5, wifi_rssi_dbm=-55.0)
    repo.add_sample(sample)
    stored = repo.last_sample()
    assert stored is not None
    assert stored.values["gw_rtt_ms"] == pytest.approx(4.5)
    assert "dns_p50_ms" not in stored.values, "absent features stay absent, not zero"


def test_sparse_samples_do_not_become_zeros(repo: Repository):
    repo.add_sample(make_sample(ts=1.0, gw_rtt_ms=3.0))
    row = repo.db.query_one("SELECT dns_p50_ms FROM samples WHERE ts = 1.0")
    assert row["dns_p50_ms"] is None


def test_score_and_incident_round_trip(repo: Repository):
    repo.add_score(
        Score(
            ts=10.0,
            health=71.5,
            risk_5m=0.3,
            risk_15m=0.6,
            severity="risk",
            primary_layer="dns",
            layer_scores={"dns": 0.8},
        )
    )
    latest = repo.latest_score()
    assert latest is not None and latest.layer_scores == {"dns": 0.8}

    incident = repo.open_incident(
        Incident(
            started_at=10.0,
            primary_layer="dns",
            severity="risk",
            title="Slow lookups",
            summary="x",
            remediation=["flush dns"],
            evidence=[{"label": "DNS", "value": 900}],
        )
    )
    assert incident.id is not None
    assert repo.active_incident() is not None

    incident.ended_at = 30.0
    repo.update_incident(incident)
    assert repo.active_incident() is None
    assert repo.get_incident(incident.id).remediation == ["flush dns"]


def test_label_attaches_to_active_incident(repo: Repository):
    incident = repo.open_incident(
        Incident(started_at=1.0, primary_layer="wifi", severity="risk", title="t", summary="s")
    )
    repo.add_label(
        Label(ts=2.0, verdict="bad", window_start=0.0, window_end=2.0, incident_id=incident.id)
    )
    assert repo.get_incident(incident.id).user_label == "bad"


def test_invalid_verdict_is_rejected(repo: Repository):
    with pytest.raises(ValueError, match="verdict"):
        repo.add_label(Label(ts=1.0, verdict="maybe", window_start=0.0, window_end=1.0))


def test_probe_log_and_counts(repo: Repository):
    now = time.time()
    for index in range(5):
        repo.log_probe(
            ProbeRecord(ts=now - index, collector="dns", target="dns:abc", ok=index % 2 == 0)
        )
    assert repo.probe_counts(now - 60) == {"dns": 5}
    assert len(repo.recent_probes(limit=3)) == 3


def test_probe_detail_is_truncated(repo: Repository):
    repo.log_probe(ProbeRecord(ts=1.0, collector="x", target="t", ok=False, detail="y" * 5000))
    assert len(repo.recent_probes()[0].detail) <= 200


def test_retention_rolls_up_then_prunes(repo: Repository, database: Database):
    now = 1_800_000_000.0
    for index in range(60):
        repo.add_sample(make_sample(ts=now - 3 * 86400 + index * 15, gw_rtt_ms=3.0 + index))
    for index in range(10):
        repo.add_sample(make_sample(ts=now - index * 15, gw_rtt_ms=4.0))

    stats = database.rollup_and_prune(
        downsample_after_hours=48, raw_days=7, rollup_days=90, now=now
    )
    assert stats["rolled"] > 0
    assert stats["raw_deleted"] == 60
    assert repo.sample_count() == 10, "recent samples survive"
    rollups = database.query("SELECT COUNT(*) AS n FROM samples_rollup")[0]["n"]
    assert rollups > 0


def test_retention_keeps_averages_in_the_rollup(repo: Repository, database: Database):
    now = 1_800_000_000.0
    for index in range(20):
        repo.add_sample(make_sample(ts=now - 3 * 86400 + index, gw_rtt_ms=10.0))
    database.rollup_and_prune(downsample_after_hours=48, raw_days=7, rollup_days=90, now=now)
    row = database.query("SELECT gw_rtt_ms_avg, n FROM samples_rollup LIMIT 1")[0]
    assert row["gw_rtt_ms_avg"] == pytest.approx(10.0)
    assert row["n"] == 20


def test_wipe_removes_everything(repo: Repository, database: Database):
    repo.add_sample(make_sample(ts=1.0, gw_rtt_ms=3.0))
    repo.add_score(Score(ts=1.0, health=100, risk_5m=0, risk_15m=0, severity="info"))
    repo.open_incident(
        Incident(started_at=1.0, primary_layer="dns", severity="risk", title="t", summary="s")
    )
    repo.add_drift_event(DriftEvent(ts=1.0, feature="gw_rtt_ms", psi=0.4, action="reset"))
    database.wipe()
    assert repo.sample_count() == 0
    assert repo.latest_score() is None
    assert repo.recent_incidents() == []
    assert repo.recent_drift() == []


def test_kv_round_trip(database: Database):
    assert database.kv_get("paused", "0") == "0"
    database.kv_set("paused", "1")
    assert database.kv_get("paused") == "1"
    database.kv_set("paused", "0")
    assert database.kv_get("paused") == "0"


def test_sample_from_row_ignores_nulls():
    row = {"ts": 5.0, **dict.fromkeys(FEATURE_NAMES)}
    row["gw_rtt_ms"] = 2.0
    sample = Sample.from_row(row)
    assert sample.values == {"gw_rtt_ms": 2.0}


# ----------------------------------------------------------------- privacy


def test_identifier_hash_is_stable_and_salted():
    first = privacy.hash_identifier("HomeNet-5G")
    assert first == privacy.hash_identifier("HomeNet-5G")
    assert first != privacy.hash_identifier("OtherNet")
    assert "HomeNet" not in str(first)


def test_hash_changes_with_a_different_install_salt(tmp_path):
    first = privacy.hash_identifier("HomeNet-5G")
    privacy.reset_cache()
    (paths.state_dir() / "install-salt").unlink()
    second = privacy.hash_identifier("HomeNet-5G")
    assert first != second, "the salt must be per install, not global"


def test_hash_to_float_is_numeric_and_stable():
    value = privacy.hash_to_float("aa:bb:cc:dd:ee:ff")
    assert isinstance(value, float)
    assert value == privacy.hash_to_float("aa:bb:cc:dd:ee:ff")
    assert privacy.hash_to_float(None) == 0.0


@pytest.mark.parametrize(
    "text,forbidden",
    [
        ("gateway is 192.168.1.1 today", "192.168.1.1"),
        ("mac aa:bb:cc:dd:ee:ff seen", "aa:bb:cc:dd:ee:ff"),
        ("token=hunter2secret", "hunter2secret"),
        ("SSID: MyHomeNetwork", "MyHomeNetwork"),
    ],
)
def test_redaction_removes_identifiers(text, forbidden):
    assert forbidden not in privacy.redact(text)


def test_redaction_truncates():
    assert len(privacy.redact("x" * 5000, limit=100)) == 100
