"""Agent lifecycle, notification policy, the loopback API, export and the CLI."""

from __future__ import annotations

import json
import time
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from netpulse import paths
from netpulse.api.server import TOKEN_HEADER, _is_loopback_host, create_app, load_or_create_token
from netpulse.cli import build_parser, main
from netpulse.collectors.base import Capability, Collector, CollectorResult
from netpulse.core.agent import Agent, SampleAssembler
from netpulse.core.scheduler import CollectorWorker, WorkerSupervisor
from netpulse.export.bundle import audit_bundle, build_bundle
from netpulse.notify.notifier import (
    LogBackend,
    Notification,
    Notifier,
    RecordingBackend,
    _applescript_escape,
    _xml_escape,
    default_backend,
    from_incident,
)
from netpulse.store.models import Incident

from .conftest import healthy_stream, make_sample

BASE_URL = "http://127.0.0.1:8787"


# ------------------------------------------------------------------ fixtures


class StubCollector(Collector):
    """A collector with no side effects, for lifecycle tests."""

    name = "stub"
    layer = "os"
    active = False

    def __init__(self, config, budget=None, value: float = 3.0, interval: float = 0.05) -> None:
        super().__init__(config, budget)
        self.calls = 0
        self.value = value
        self.interval = interval

    def interval_s(self) -> float:
        return self.interval

    def enabled(self) -> bool:
        return True

    def detect_capability(self) -> Capability:
        return Capability(True)

    def _collect(self) -> CollectorResult:
        self.calls += 1
        result = CollectorResult()
        result.set("gw_rtt_ms", self.value)
        result.probe("stub", "192.168.0.1", True, 1.0, "detail 10.0.0.5")
        return result


@pytest.fixture
def agent(quiet_config) -> Agent:
    instance = Agent(quiet_config, notifier=Notifier(quiet_config, RecordingBackend()))
    yield instance
    instance.stop(timeout=3.0)
    instance.db.close()


@pytest.fixture
def client(agent) -> TestClient:
    app = create_app(agent, agent.config)
    return TestClient(app, base_url=BASE_URL)


def _token(client: TestClient) -> dict[str, str]:
    return {TOKEN_HEADER: client.app.state.token}


# ------------------------------------------------------------------- notify


def test_notifier_respects_severity_floor(config):
    notifier = Notifier(config, RecordingBackend())
    assert not notifier.notify(_note("info"))
    assert notifier.notify(_note("risk"))


def test_notifier_cooldown_is_per_layer(config):
    backend = RecordingBackend()
    notifier = Notifier(config, backend)
    base = _midday()
    assert notifier.notify(_note("risk", "wifi"), now=base)
    assert not notifier.notify(_note("risk", "wifi"), now=base + 600)
    assert notifier.notify(_note("risk", "dns"), now=base + 600)
    assert len(backend.sent) == 2


def test_escalation_beats_the_cooldown(config):
    notifier = Notifier(config, RecordingBackend())
    base = _midday()
    notifier.notify(_note("risk", "wifi"), now=base)
    assert notifier.notify(_note("critical", "wifi"), now=base + 60)


def test_cooldown_expires(config):
    notifier = Notifier(config, RecordingBackend())
    base = _midday()
    notifier.notify(_note("risk", "wifi"), now=base)
    cooldown = config.notifications.cooldown_minutes * 60 + 1
    assert notifier.notify(_note("risk", "wifi"), now=base + cooldown)


def test_quiet_hours_hold_back_non_critical(config):
    notifier = Notifier(config, RecordingBackend())
    night = _at_hour(23)
    assert not notifier.notify(_note("risk", "path"), now=night)
    assert notifier.notify(_note("critical", "gateway"), now=night)


def test_quiet_hours_wrap_midnight(config):
    notifier = Notifier(config, RecordingBackend())
    assert notifier._in_quiet_hours(_at_hour(23))
    assert notifier._in_quiet_hours(_at_hour(3))
    assert not notifier._in_quiet_hours(_at_hour(12))


def test_disabled_notifications_send_nothing():
    from netpulse.config import NetPulseConfig

    config = NetPulseConfig.model_validate({"notifications": {"enabled": False}})
    notifier = Notifier(config, RecordingBackend())
    assert not notifier.notify(_note("critical"))
    assert notifier.status()["suppressed"] == 1


def test_notification_text_is_escaped_for_its_interpreter():
    hostile = 'x"; Remove-Item -Recurse C:\\ #<&>'
    assert "<" not in _xml_escape(hostile)
    assert "&lt;" in _xml_escape(hostile)
    assert '\\"' in _applescript_escape(hostile)


def test_notification_from_incident_includes_one_remediation():
    incident = Incident(
        started_at=1.0,
        primary_layer="wifi",
        severity="risk",
        title="Weak signal",
        summary="Signal is low.",
        remediation=["Move closer", "Use cable"],
        id=7,
    )
    notification = from_incident(incident, "risk")
    assert notification.layer == "wifi"
    assert "Move closer" in notification.body
    assert "Use cable" not in notification.body, "only the top suggestion goes in a toast"


def test_default_backend_is_always_usable():
    assert default_backend().available()


def test_log_backend_never_fails():
    assert LogBackend().send(_note("critical")) is True


# -------------------------------------------------------------------- agent


def test_assembler_emits_only_fresh_values():
    assembler = SampleAssembler()
    assembler.update({"gw_rtt_ms": 3.0})
    first = assembler.take()
    assert first.values == {"gw_rtt_ms": 3.0}
    assert assembler.take().values == {}, "values are not repeated between ticks"


def test_assembler_is_last_write_wins():
    assembler = SampleAssembler()
    assembler.update({"gw_rtt_ms": 3.0})
    assembler.update({"gw_rtt_ms": 9.0})
    assert assembler.take().values == {"gw_rtt_ms": 9.0}


def test_agent_tick_persists_a_sample_and_a_score(agent):
    agent.assembler.update({"gw_rtt_ms": 3.0, "dns_p50_ms": 20.0})
    result = agent.tick()
    assert result is not None
    assert agent.repo.sample_count() == 1
    assert agent.repo.latest_score() is not None


def test_agent_tick_is_a_noop_without_new_data(agent):
    agent.assembler.update({"gw_rtt_ms": 3.0})
    agent.tick()
    assert agent.tick() is None


def test_agent_redacts_probe_details(agent, quiet_config):
    collector = StubCollector(quiet_config)
    agent._on_result(collector, collector.collect())
    stored = agent.repo.recent_probes(limit=1)[0]
    assert "10.0.0.5" not in stored.detail
    assert "<ip>" in stored.detail


def test_agent_pause_survives_a_restart(agent, quiet_config):
    agent.pause()
    assert agent.budget.paused
    revived = Agent(quiet_config, database=agent.db, notifier=agent.notifier)
    assert revived.budget.paused, "pause must persist across a restart"


def test_agent_learning_toggle_persists(agent, quiet_config):
    agent.set_learning(False)
    revived = Agent(quiet_config, database=agent.db, notifier=agent.notifier)
    assert revived.scorer.learn is False


def test_agent_label_attaches_to_the_open_incident(agent):
    incident = agent.repo.open_incident(
        Incident(
            started_at=time.time(), primary_layer="dns", severity="risk", title="t", summary="s"
        )
    )
    label = agent.add_label("bad", "call dropped")
    assert label.incident_id == incident.id


def test_agent_wipe_clears_the_store_and_state(agent):
    agent.assembler.update({"gw_rtt_ms": 3.0})
    agent.tick()
    agent._save_state()
    agent.wipe()
    assert agent.repo.sample_count() == 0
    assert not (paths.model_dir() / "scorer-state.json").exists()


def test_agent_state_survives_a_restart(agent, quiet_config):
    for sample in healthy_stream(120):
        agent.scorer.observe(sample)
    seen = agent.scorer.l1.samples_seen
    agent._save_state()
    revived = Agent(quiet_config, database=agent.db, notifier=agent.notifier)
    assert revived.scorer.l1.samples_seen == seen


def test_agent_status_reports_every_subsystem(agent):
    status = agent.status()
    for key in (
        "version",
        "paused",
        "learning",
        "model",
        "budget",
        "collectors",
        "capabilities",
        "notifications",
        "store",
    ):
        assert key in status, key


def test_agent_notifies_once_when_an_incident_opens(agent, monkeypatch):
    from netpulse.ml.scorer import ScoreResult
    from netpulse.store.models import Score

    incident = Incident(
        started_at=time.time(),
        primary_layer="gateway",
        severity="critical",
        title="Router down",
        summary="No replies.",
        remediation=["Restart it"],
    )
    score = Score(ts=time.time(), health=10, risk_5m=0.9, risk_15m=0.95, severity="critical")
    agent._persist_incidents(ScoreResult(score=score, opened=incident))
    assert len(agent.notifier.backend.sent) == 1
    assert agent.repo.active_incident() is not None


# ---------------------------------------------------------------- scheduler


def test_worker_runs_and_can_be_stopped(quiet_config):
    import threading

    collector = StubCollector(quiet_config)
    stop = threading.Event()
    seen: list[CollectorResult] = []
    worker = CollectorWorker(collector, quiet_config, lambda c, r: seen.append(r), stop)
    worker.start()
    deadline = time.monotonic() + 5.0
    while collector.calls < 2 and time.monotonic() < deadline:
        time.sleep(0.02)
    stop.set()
    worker.join(timeout=3.0)
    assert collector.calls >= 2
    assert seen
    assert not worker.is_alive()


def test_worker_interval_has_jitter(quiet_config):
    import threading

    collector = StubCollector(quiet_config, interval=30.0)
    worker = CollectorWorker(collector, quiet_config, lambda c, r: None, threading.Event())
    intervals = {round(worker.interval(), 6) for _ in range(20)}
    assert len(intervals) > 1, "every probe at the same instant is both rude and less useful"


def test_supervisor_does_not_spawn_before_start(quiet_config):
    supervisor = WorkerSupervisor([StubCollector(quiet_config)], quiet_config, lambda c, r: None)
    assert supervisor.check() == []
    assert supervisor.workers == {}


def test_supervisor_restarts_a_dead_worker(quiet_config):
    collector = StubCollector(quiet_config)
    supervisor = WorkerSupervisor([collector], quiet_config, lambda c, r: None)
    supervisor.start()
    time.sleep(0.1)
    supervisor.workers["stub"].stop_event = __import__("threading").Event()
    supervisor.workers["stub"]._consecutive_failures = 0
    # Simulate death by replacing the worker with one that has finished.
    supervisor.workers["stub"].join(timeout=0)
    supervisor.stop_event.set()
    supervisor.workers["stub"].join(timeout=2.0)
    supervisor.stop_event.clear()
    restarted = supervisor.check()
    assert "stub" in restarted
    supervisor.stop(timeout=2.0)


# ---------------------------------------------------------------------- API


@pytest.mark.parametrize(
    "host,ok",
    [
        ("127.0.0.1:8787", True),
        ("localhost:8787", True),
        ("[::1]:8787", True),
        ("evil.example.com", False),
        ("192.168.1.10:8787", False),
        ("", False),
    ],
)
def test_loopback_host_check(host, ok):
    assert _is_loopback_host(host) is ok


def test_api_requires_a_token(client):
    assert client.get("/api/health").status_code == 401
    assert client.get("/api/health", headers={TOKEN_HEADER: "nope"}).status_code == 401
    assert client.get("/api/health", headers=_token(client)).status_code == 200


def test_api_refuses_a_rebinding_host(client):
    headers = {**_token(client), "Host": "attacker.example.com"}
    assert client.get("/api/health", headers=headers).status_code == 421


@pytest.mark.parametrize(
    "path",
    [
        "/api/health",
        "/api/status",
        "/api/timeline?hours=1",
        "/api/incidents",
        "/api/probes",
        "/api/schema",
        "/api/config",
        "/api/drift",
    ],
)
def test_read_routes_respond(client, path):
    assert client.get(path, headers=_token(client)).status_code == 200


def test_api_security_headers_are_present(client):
    response = client.get("/", headers=_token(client))
    assert "frame-ancestors 'none'" in response.headers["Content-Security-Policy"]
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["Cache-Control"] == "no-store"


def test_ui_receives_the_token_by_injection(client):
    body = client.get("/").text
    assert client.app.state.token in body
    assert "__NETPULSE_TOKEN__" not in body


def test_api_pause_and_resume(client, agent):
    assert client.post("/api/pause", headers=_token(client)).json() == {"paused": True}
    assert agent.budget.paused
    assert client.post("/api/resume", headers=_token(client)).json() == {"paused": False}
    assert not agent.budget.paused


def test_api_label_validates_the_verdict(client):
    good = client.post("/api/label", headers=_token(client), json={"verdict": "bad"})
    assert good.status_code == 200
    bad = client.post("/api/label", headers=_token(client), json={"verdict": "terrible"})
    assert bad.status_code == 422


def test_api_export_writes_into_the_agent_directory(client):
    response = client.post("/api/export", headers=_token(client), json={"hours": 1})
    assert response.status_code == 200
    written = Path(response.json()["path"])
    assert written.exists()
    assert paths.data_dir() in written.parents, "exports never escape the data directory"


def test_api_timeline_rejects_an_absurd_window(client):
    assert client.get("/api/timeline?hours=100000", headers=_token(client)).status_code == 422


def test_api_unknown_incident_is_404(client):
    assert client.get("/api/incidents/98765", headers=_token(client)).status_code == 404


def test_api_serves_static_assets(client):
    for path in ("/static/style.css", "/static/app.js", "/favicon.ico"):
        assert client.get(path).status_code == 200


def test_token_file_is_created_once():
    first = load_or_create_token()
    assert len(first) >= 32
    assert load_or_create_token() == first
    assert paths.api_token_file().exists()


# ------------------------------------------------------------------- export


def test_bundle_contains_the_promised_entries(repo, config):
    repo.add_sample(make_sample(ts=time.time(), gw_rtt_ms=3.0))
    target = paths.data_dir() / "bundle.zip"
    build_bundle(repo, config, target, window_s=3600)
    with zipfile.ZipFile(target) as archive:
        names = set(archive.namelist())
    assert {
        "README.txt",
        "manifest.json",
        "config.json",
        "capabilities.json",
        "schema.json",
        "samples.jsonl",
        "scores.jsonl",
        "incidents.json",
        "labels.json",
        "probe-log.jsonl",
        "drift.json",
    } <= names


def test_bundle_passes_its_own_privacy_audit(repo, config):
    from netpulse.store.models import ProbeRecord

    repo.add_sample(make_sample(ts=time.time(), gw_rtt_ms=3.0))
    repo.log_probe(
        ProbeRecord(
            ts=time.time(),
            collector="gateway",
            target="192.168.1.1",
            ok=False,
            detail="no route to 10.20.30.40 mac aa:bb:cc:dd:ee:ff",
        )
    )
    target = paths.data_dir() / "bundle.zip"
    build_bundle(repo, config, target)
    audit = audit_bundle(target)
    assert audit["ok"], audit["findings"]
    with zipfile.ZipFile(target) as archive:
        probes = archive.read("probe-log.jsonl").decode()
    assert "10.20.30.40" not in probes
    assert "aa:bb:cc:dd:ee:ff" not in probes


def test_bundle_declares_it_has_no_payloads(repo, config):
    target = paths.data_dir() / "bundle.zip"
    build_bundle(repo, config, target)
    with zipfile.ZipFile(target) as archive:
        manifest = json.loads(archive.read("manifest.json"))
        readme = archive.read("README.txt").decode()
    assert manifest["contains_payloads"] is False
    assert "No packet contents" in readme


def test_bundle_samples_are_replayable(repo, config, tmp_path):
    from netpulse.eval.replay import load_jsonl

    for sample in healthy_stream(20):
        repo.add_sample(sample)
    target = paths.data_dir() / "bundle.zip"
    build_bundle(repo, config, target, window_s=10 * 365 * 86400)
    with zipfile.ZipFile(target) as archive:
        extracted = tmp_path / "samples.jsonl"
        extracted.write_bytes(archive.read("samples.jsonl"))
    replayed = load_jsonl(extracted)
    assert len(replayed) == 20
    assert replayed[0].values["gw_rtt_ms"] > 0


def test_audit_flags_a_tampered_bundle(repo, config, tmp_path):
    target = tmp_path / "bad.zip"
    with zipfile.ZipFile(target, "w") as archive:
        archive.writestr("manifest.json", '{"api_key": "AKIAIOSFODNN7EXAMPLE"}')
    audit = audit_bundle(target)
    assert not audit["ok"]
    assert audit["missing"]


# ---------------------------------------------------------------------- CLI


def test_parser_exposes_every_documented_command():
    parser = build_parser()
    actions = [a for a in parser._actions if a.dest == "command"]
    commands = set(actions[0].choices)
    assert {
        "run",
        "status",
        "check",
        "doctor",
        "pause",
        "resume",
        "learning",
        "label",
        "incidents",
        "export",
        "wipe",
        "config",
        "eval",
        "replay",
        "train",
        "version",
    } <= commands


def test_cli_version(capsys):
    assert main(["version"]) == 0
    assert "netpulse" in capsys.readouterr().out


def test_cli_doctor_reports_collectors(capsys):
    assert main(["doctor"]) == 0
    out = capsys.readouterr().out
    assert "Collectors" in out
    for name in ("system", "wifi", "gateway", "dns", "https", "path"):
        assert name in out


def test_cli_doctor_json(capsys):
    assert main(["--json", "doctor"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["version"]
    assert len(payload["collectors"]) == 7


def test_cli_config_init_then_show(capsys):
    assert main(["config", "--init"]) == 0
    assert paths.config_file().exists()
    capsys.readouterr()
    assert main(["--json", "config"]) == 0
    assert json.loads(capsys.readouterr().out)["api"]["host"] == "127.0.0.1"


def test_cli_status_without_data(capsys):
    assert main(["status"]) == 0
    assert "No readings yet" in capsys.readouterr().out


def test_cli_status_reads_the_store(capsys, repo):
    from netpulse.store.models import Score

    repo.add_score(Score(ts=time.time(), health=82.0, risk_5m=0.1, risk_15m=0.2, severity="watch"))
    repo.db.close()
    assert main(["status"]) == 0
    out = capsys.readouterr().out
    assert "82.0/100" in out
    assert "no agent running" in out


def test_cli_label_without_an_agent(capsys):
    assert main(["label", "bad", "call dropped"]) == 0
    assert "were 'bad'" in capsys.readouterr().out


def test_cli_pause_records_intent_without_an_agent(capsys):
    assert main(["pause"]) == 0
    assert "when the agent next starts" in capsys.readouterr().out
    from netpulse.store.db import Database

    db = Database(paths.database_file())
    assert db.kv_get("paused") == "1"
    db.close()


def test_cli_export_and_audit(capsys, repo):
    repo.add_sample(make_sample(ts=time.time(), gw_rtt_ms=3.0))
    repo.db.close()
    assert main(["export", "--hours", "1"]) == 0
    out = capsys.readouterr().out
    assert "Privacy audit: clean" in out


def test_cli_wipe_requires_confirmation(capsys, monkeypatch, repo):
    repo.add_sample(make_sample(ts=time.time(), gw_rtt_ms=3.0))
    repo.db.close()
    monkeypatch.setattr("builtins.input", lambda *_: "n")
    assert main(["wipe"]) == 0
    assert "Nothing was deleted" in capsys.readouterr().out


def test_cli_wipe_with_yes(capsys, repo):
    repo.add_sample(make_sample(ts=time.time(), gw_rtt_ms=3.0))
    repo.db.close()
    assert main(["wipe", "--yes"]) == 0
    assert "deleted" in capsys.readouterr().out


def test_cli_replay_of_a_captured_file(capsys, tmp_path):
    from netpulse.eval.replay import write_jsonl

    path = write_jsonl(tmp_path / "samples.jsonl", healthy_stream(60))
    assert main(["replay", str(path)]) == 0
    out = capsys.readouterr().out
    assert "Replayed 60 samples" in out
    assert "No incidents" in out


def test_cli_replay_of_a_missing_file_fails_cleanly(capsys):
    assert main(["replay", "no-such-file.jsonl"]) == 1
    assert "netpulse:" in capsys.readouterr().err


def test_cli_errors_are_sentences_not_tracebacks(capsys, monkeypatch):
    monkeypatch.setattr("netpulse.cli.load_config", _boom)
    assert main(["status"]) == 1
    assert "Traceback" not in capsys.readouterr().err


# ------------------------------------------------------------------ helpers


def _boom(*args, **kwargs):
    raise RuntimeError("config is unreadable")


def _note(severity: str, layer: str = "wifi") -> Notification:
    return Notification(
        title=f"{layer} problem", body="something happened", severity=severity, layer=layer
    )


def _midday() -> float:
    return _at_hour(14)


def _at_hour(hour: int) -> float:
    return time.mktime((2026, 9, 21, hour, 0, 0, 0, 0, -1))
