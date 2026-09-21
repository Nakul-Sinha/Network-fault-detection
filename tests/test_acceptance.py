"""PRD Appendix B acceptance checklist, and the PRD 3.2 evaluation gates.

Each test here maps to a line an engineer would otherwise have to check by
hand before a release. Turning the checklist into executable assertions is
the point: a checklist that lives only in a document gets skipped, and the
items it covers are exactly the ones that are embarrassing to get wrong.

The fault-injection scenarios stand in for netem and DNS blackhole
containers. Those remain the right tool for release validation on a Linux
host; these run everywhere, including a Windows CI runner, which is what
makes them run at all.
"""

from __future__ import annotations

import time
import zipfile

import pytest

from netpulse import paths
from netpulse.config import NetPulseConfig
from netpulse.core.agent import Agent
from netpulse.eval.metrics import DEFAULT_GATES
from netpulse.eval.replay import replay_corpus, replay_run
from netpulse.eval.scenarios import SCENARIOS_BY_NAME, generate, generate_soak
from netpulse.export.bundle import audit_bundle, build_bundle
from netpulse.ml.scorer import Scorer
from netpulse.notify.notifier import Notifier, RecordingBackend

from .conftest import healthy_stream, make_sample

pytestmark = pytest.mark.acceptance


def _first_incident(run, config: NetPulseConfig | None = None):
    scorer = Scorer(config or NetPulseConfig())
    for sample in run.samples:
        result = scorer.observe(sample)
        if result.opened is not None:
            return result.opened, sample.ts
    return None, None


# ------------------------------- "Fresh install shows a score in < 5 minutes"


def test_a_fresh_install_produces_a_score_quickly(quiet_config):
    """The agent must say something useful long before its models are warm."""
    agent = Agent(quiet_config, notifier=Notifier(quiet_config, RecordingBackend()))
    try:
        agent.assembler.update({"gw_rtt_ms": 3.0, "dns_p50_ms": 20.0, "https_ttfb_ms": 60.0})
        result = agent.tick()
        assert result is not None
        summary = agent.health_summary()
        assert summary["health"] is not None
        assert summary["severity"] in ("info", "watch", "risk", "critical")
        assert summary["warming_up"] is True, "and it must say it is still learning"
    finally:
        agent.stop(timeout=2.0)
        agent.db.close()


# ------------------------- "With DNS blackhole injection, primary layer = DNS"


def test_dns_blackhole_is_attributed_to_dns_within_two_minutes():
    run = generate(SCENARIOS_BY_NAME["dns_blackhole"])
    incident, opened_at = _first_incident(run)
    assert incident is not None, "a total resolver failure must raise an incident"
    assert incident.primary_layer == "dns"
    assert opened_at - run.impact_start <= 120, "within two minutes of impact"


# ------------------------------ "With Wi-Fi RSSI drop fixture, RCA mentions Wi-Fi"


def test_wifi_fade_names_wifi_in_plain_language():
    run = generate(SCENARIOS_BY_NAME["wifi_fade"])
    incident, _ = _first_incident(run)
    assert incident is not None
    assert incident.primary_layer == "wifi"
    assert "Wi-Fi" in incident.title or "Wi-Fi" in incident.summary
    assert incident.remediation, "an incident card must suggest something to try"


# ---------------------- "With netem delay on the HTTPS path only, layer != Wi-Fi"


def test_path_congestion_is_not_blamed_on_wifi():
    run = generate(SCENARIOS_BY_NAME["isp_path_congestion"])
    incident, _ = _first_incident(run)
    assert incident is not None
    assert incident.primary_layer != "wifi"
    assert incident.primary_layer in ("path", "remote_https")


def test_remote_slowdown_is_not_blamed_on_the_local_network():
    run = generate(SCENARIOS_BY_NAME["remote_service_slow"])
    incident, _ = _first_incident(run)
    assert incident is not None
    assert incident.primary_layer == "remote_https"


def test_captive_portal_is_never_reported_as_an_isp_fault():
    """PRD 11.2: never blame the ISP without path evidence."""
    run = generate(SCENARIOS_BY_NAME["captive_portal"])
    incident, _ = _first_incident(run)
    assert incident is not None
    assert incident.primary_layer not in ("path", "remote_https")


def test_captive_portal_card_appears_once_the_portal_is_seen():
    """The portal check runs on a slow cadence, so the first incident may
    honestly name DNS. Once the probe has run, the card must say sign-in.

    This is why the agent pulls the portal check forward on suspicion: on its
    ordinary cadence it can take minutes to find out why DNS broke.
    """
    run = generate(SCENARIOS_BY_NAME["captive_portal"])
    scorer = Scorer(NetPulseConfig())
    titles = []
    for sample in run.samples:
        result = scorer.observe(sample)
        if result.card and sample.values.get("captive_portal", 0.0) >= 1.0:
            titles.append(result.card["title"].lower())
    assert titles, "the portal scenario must produce a card while the portal is up"
    assert any("sign-in" in title or "sign in" in title for title in titles)


# ------------------------ "Disk after an hour contains no packet payloads"


def test_the_database_holds_no_text_at_all(repo):
    """The strongest form of the promise: there is nowhere to put a payload."""
    for sample in healthy_stream(200):
        repo.add_sample(sample)
    columns = repo.db.query("PRAGMA table_info(samples)")
    assert all(column["type"] == "REAL" or column["name"] == "ts" for column in columns)

    rows = repo.db.query("SELECT * FROM samples LIMIT 50")
    for row in rows:
        for value in tuple(row):
            assert value is None or isinstance(
                value, int | float
            ), "a sample row must never contain text"


def test_probe_log_stores_configured_targets_not_observed_traffic(repo, quiet_config):
    agent = Agent(
        quiet_config, database=repo.db, notifier=Notifier(quiet_config, RecordingBackend())
    )
    try:
        for collector in agent.collectors:
            if collector.name == "system":
                agent._on_result(collector, collector.collect())
        for record in repo.recent_probes():
            assert record.collector in {c.name for c in agent.collectors}
    finally:
        agent.stop(timeout=2.0)


# ------------------------ "Pause stops all probes (verified by probe counter)"


def test_pause_stops_every_probe(quiet_config):
    config = NetPulseConfig()
    agent = Agent(config, notifier=Notifier(config, RecordingBackend()))
    try:
        agent.pause()
        for collector in agent.collectors:
            if not collector.active:
                continue
            collector._capability = type(collector.capability)(True)
            result = collector.collect()
            assert result.values == {}, f"{collector.name} probed while paused"
        assert agent.budget.stats()["total_granted"] == 0
    finally:
        agent.stop(timeout=2.0)
        agent.db.close()


# ------------------- "Export ZIP opens and redacts secrets (tokens)"


def test_export_opens_and_redacts(repo, config):
    from netpulse.store.models import ProbeRecord

    paths.write_private(paths.api_token_file(), "tok_SUPERSECRETVALUE_0123456789")
    for sample in healthy_stream(30):
        repo.add_sample(sample)
    repo.log_probe(
        ProbeRecord(
            ts=time.time(),
            collector="https",
            target="https://example.com",
            ok=False,
            detail="tls error from 203.0.113.9 token=abcdef123456",
        )
    )
    target = paths.data_dir() / "acceptance.zip"
    build_bundle(repo, config, target)

    with zipfile.ZipFile(target) as archive:
        blob = "".join(archive.read(name).decode("utf-8", "replace") for name in archive.namelist())
    assert "tok_SUPERSECRETVALUE_0123456789" not in blob
    assert "abcdef123456" not in blob
    assert "203.0.113.9" not in blob
    assert audit_bundle(target)["ok"]


# --------------------------------- "Idle CPU budget held under default probes"


def test_scoring_is_cheap_enough_for_the_cpu_budget(config):
    """PRD 3.2 asks for under 3 percent idle CPU on a laptop.

    The scoring path runs once per sampling interval, so the budget is a
    statement about how long one pass may take. At a fifteen second cadence,
    three percent of one core is 450 ms; this asserts an order of magnitude
    better than that, which leaves room for slower machines than this one.
    """
    scorer = Scorer(config)
    samples = healthy_stream(300)
    for sample in samples[:200]:
        scorer.observe(sample)

    started = time.perf_counter()
    for sample in samples[200:]:
        scorer.observe(sample)
    per_frame_ms = (time.perf_counter() - started) / 100 * 1000
    assert per_frame_ms < 45.0, f"{per_frame_ms:.1f} ms per frame is too slow"


# ------------------------------------------------ PRD 3.2 evaluation gates


@pytest.mark.slow
def test_evaluation_gates_pass_on_the_corpus():
    report = replay_corpus(seeds=(11,), include_soak=True, soak_hours=4.0)
    failures = [gate for gate in report.gates() if not gate["passed"]]
    assert not failures, f"regressed gates: {failures}"


def test_lead_time_beats_the_five_minute_target():
    """The product promise, measured on the layers where it matters most."""
    leads = []
    for name in ("wifi_fade", "dns_slow", "gateway_congestion", "isp_path_congestion"):
        outcome, _ = replay_run(generate(SCENARIOS_BY_NAME[name]))
        assert outcome.detected, f"{name} went undetected"
        leads.append(outcome.lead_time_s)
    median = sorted(leads)[len(leads) // 2]
    assert median >= DEFAULT_GATES["median_lead_time_s"], f"median lead time {median}s"


def test_a_quiet_network_raises_nothing():
    outcome, _ = replay_run(generate(SCENARIOS_BY_NAME["healthy"]))
    assert outcome.alerts == 0


def test_benign_congestion_is_not_an_incident():
    outcome, _ = replay_run(generate(SCENARIOS_BY_NAME["evening_congestion"]))
    assert outcome.alerts == 0, "a slower evening is not a fault"


def test_a_long_quiet_soak_stays_under_the_alert_budget():
    """PRD 3.2: at most two noisy alerts a day, by default.

    Measured over hours rather than minutes, and across the evening
    congestion peak, which is where a naive detector fires.
    """
    outcome, _ = replay_run(generate_soak(hours=6.0))
    assert (
        outcome.alerts_per_day() <= DEFAULT_GATES["benign_alerts_per_day"]
    ), f"{outcome.alerts} alerts in {outcome.duration_s / 3600:.1f} hours"


def test_every_fault_scenario_is_detected():
    missed = []
    for name, spec in SCENARIOS_BY_NAME.items():
        if spec.benign:
            continue
        outcome, _ = replay_run(generate(spec))
        if not outcome.detected:
            missed.append(name)
    assert not missed, f"undetected scenarios: {missed}"


def test_layer_attribution_meets_the_mvp_target():
    correct = total = 0
    for spec in SCENARIOS_BY_NAME.values():
        if spec.benign or spec.expected_layer is None:
            continue
        outcome, _ = replay_run(generate(spec))
        if outcome.layer_correct is None:
            continue
        total += 1
        correct += int(outcome.layer_correct)
    assert total > 0
    accuracy = correct / total
    assert accuracy >= DEFAULT_GATES["layer_accuracy"], f"{correct}/{total} layers correct"


def test_risk_forecasts_are_calibrated():
    report = replay_corpus(seeds=(11,), include_soak=False)
    brier = report.calibration["brier"]
    assert brier < 0.2, f"Brier score {brier} suggests poorly calibrated probabilities"
    assert report.calibration["ece"] < 0.15


# ---------------------------------------------- graceful degradation (5.3)


def test_the_agent_works_with_layers_switched_off():
    config = NetPulseConfig.model_validate(
        {"collectors": {"wifi": False, "path": False, "https": False, "captive": False}}
    )
    scorer = Scorer(config)
    for sample in healthy_stream(200):
        trimmed = make_sample(
            ts=sample.ts,
            **{
                name: value
                for name, value in sample.values.items()
                if not name.startswith(("wifi_", "path_", "https_"))
            },
        )
        result = scorer.observe(trimmed)
    assert result.score.coverage < 0.6
    assert result.score.health > 50, "a partial picture is still a picture"


def test_an_ethernet_host_is_never_blamed_on_wifi():
    scorer = Scorer(NetPulseConfig())
    result = None
    for sample in healthy_stream(200):
        wired = make_sample(
            ts=sample.ts,
            **{
                name: value for name, value in sample.values.items() if not name.startswith("wifi_")
            },
        )
        wired.set("os_iface_type", 0.0)
        result = scorer.observe(wired)
    assert result is not None
    assert result.score.layer_scores.get("wifi", 0.0) == 0.0
