"""The agent: collectors in, scores, incidents and notifications out.

This is the process that actually runs on a user machine. It owns the
lifecycle of everything else: the collector workers, the feature store, the
model ladder, notification policy, retention and the persisted model state.

The shape is deliberately simple. Collector workers write whatever they
measure into an assembler; a scoring tick on a fixed cadence turns the
freshly measured values into one Sample and pushes it through the ladder.
Decoupling the two means a collector on a ten minute cadence and one on a
fifteen second cadence contribute to the same timeline without either
blocking the other, and a slow traceroute cannot delay a health update.
"""

from __future__ import annotations

import contextlib
import json
import threading
import time
from typing import Any

from ..collectors.base import Collector, CollectorResult
from ..collectors.registry import build_budget, build_collectors
from ..config import NetPulseConfig, load_config
from ..logging_setup import get_logger
from ..ml.scorer import Scorer, ScoreResult
from ..notify.notifier import Notifier, from_incident
from ..paths import ensure_dirs, model_dir
from ..privacy import redact
from ..store.db import Database
from ..store.models import Incident, Label, Sample
from ..store.repository import Repository

log = get_logger(__name__)

STATE_FILE = "scorer-state.json"
#: How often maintenance runs: retention, state save, worker watchdog.
MAINTENANCE_INTERVAL_S = 300.0
#: How often model state is written to disk. Losing at most this much
#: learning to a hard power loss is an acceptable trade for not writing a
#: few hundred kilobytes every fifteen seconds.
STATE_SAVE_INTERVAL_S = 900.0


class SampleAssembler:
    """Collects feature values between scoring ticks.

    Only values measured since the previous tick are emitted. Carrying a
    value forward is the feature pipeline's job and it applies a per-layer
    staleness bound, so duplicating values here would defeat that.
    """

    def __init__(self) -> None:
        self._values: dict[str, float] = {}
        self._lock = threading.Lock()
        self.total_values = 0

    def update(self, values: dict[str, float]) -> None:
        if not values:
            return
        with self._lock:
            self._values.update(values)
            self.total_values += len(values)

    def take(self) -> Sample:
        with self._lock:
            values = self._values
            self._values = {}
        return Sample(ts=time.time(), values=values)

    def pending(self) -> int:
        with self._lock:
            return len(self._values)


class Agent:
    """The long-running supervisor."""

    def __init__(
        self,
        config: NetPulseConfig | None = None,
        *,
        database: Database | None = None,
        notifier: Notifier | None = None,
    ) -> None:
        ensure_dirs()
        self.config = config or load_config()
        self.db = database or Database(_database_path())
        self.repo = Repository(self.db)
        self.budget = build_budget(self.config)
        self.collectors: list[Collector] = build_collectors(self.config, self.budget)
        self.assembler = SampleAssembler()
        self.scorer = Scorer(self.config)
        self.notifier = notifier or Notifier(self.config)

        from .scheduler import WorkerSupervisor

        self.supervisor = WorkerSupervisor(self.collectors, self.config, self._on_result)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.started_at: float | None = None
        self.ticks = 0
        self._last_maintenance = 0.0
        self._last_state_save = 0.0
        self._restore_state()
        self._restore_pause_state()

    # ------------------------------------------------------------ lifecycle

    def start(self, *, background: bool = True) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self.started_at = time.time()
        self.supervisor.start()
        log.info(
            "agent started with %d collectors, scoring every %ds",
            len(self.collectors),
            self.config.probes.system_interval_s,
        )
        if background:
            self._thread = threading.Thread(target=self._loop, name="netpulse-agent", daemon=True)
            self._thread.start()
        else:
            self._loop()

    def stop(self, timeout: float = 15.0) -> None:
        log.info("agent stopping")
        self._stop.set()
        self.supervisor.stop(timeout=timeout)
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            self._thread = None
        self._save_state()
        log.info("agent stopped after %d scoring ticks", self.ticks)

    def _loop(self) -> None:
        interval = float(self.config.probes.system_interval_s)
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception:
                # The scoring loop is the one thread that must never die.
                log.exception("scoring tick failed")
            if self._stop.wait(interval):
                break

    # ----------------------------------------------------------------- tick

    def tick(self) -> ScoreResult | None:
        """One scoring cycle. Returns None when nothing has been measured."""
        sample = self.assembler.take()
        if not sample.values and self.ticks > 0:
            # Nothing new: usually every collector is paused or unsupported.
            return None
        self.ticks += 1
        self.repo.add_sample(sample)

        result = self.scorer.observe(sample)
        self.repo.add_score(result.score)
        self._persist_incidents(result)
        self._persist_drift(result)
        self._maybe_maintain()
        return result

    def _on_result(self, collector: Collector, result: CollectorResult) -> None:
        """Called from a collector worker thread."""
        self.assembler.update(result.values)
        for record in result.probes:
            record.detail = redact(record.detail)
            self.repo.log_probe(record)

    def _persist_incidents(self, result: ScoreResult) -> None:
        if result.opened is not None:
            incident = self.repo.open_incident(result.opened)
            log.info(
                "incident %s opened: %s (%s, confidence %.2f)",
                incident.id,
                incident.title,
                incident.primary_layer,
                incident.confidence,
            )
            notification = from_incident(incident, result.score.severity)
            if self.notifier.notify(notification):
                incident.notified = True
                self.repo.update_incident(incident)
        elif self.scorer.active_incident is not None and self.scorer.active_incident.id:
            self.repo.update_incident(self.scorer.active_incident)

        if result.closed:
            closing = self.repo.active_incident()
            if closing is not None:
                closing.ended_at = time.time()
                self.repo.update_incident(closing)
                log.info("incident %s closed", closing.id)

    def _persist_drift(self, result: ScoreResult) -> None:
        for decision in result.drift:
            self.repo.add_drift_event(decision.to_event(time.time()))

    # ---------------------------------------------------------- maintenance

    def _maybe_maintain(self) -> None:
        now = time.time()
        if now - self._last_maintenance < MAINTENANCE_INTERVAL_S:
            return
        self._last_maintenance = now
        restarted = self.supervisor.check()
        if restarted:
            log.info("restarted collectors: %s", ", ".join(restarted))
        try:
            stats = self.db.rollup_and_prune(
                downsample_after_hours=self.config.retention.downsample_after_hours,
                raw_days=self.config.retention.raw_days,
                rollup_days=self.config.retention.rollup_days,
            )
            if stats["raw_deleted"]:
                log.debug("retention: %s", stats)
        except Exception:
            log.exception("retention pass failed")
        if now - self._last_state_save >= STATE_SAVE_INTERVAL_S:
            self._save_state()

    def _save_state(self) -> None:
        self._last_state_save = time.time()
        try:
            path = model_dir() / STATE_FILE
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps(self.scorer.save_state(), separators=(",", ":")), encoding="utf-8"
            )
        except (OSError, TypeError, ValueError) as exc:
            log.warning("could not save model state: %s", exc)

    def _restore_state(self) -> None:
        path = model_dir() / STATE_FILE
        if not path.exists():
            return
        try:
            self.scorer.load_state(json.loads(path.read_text(encoding="utf-8")))
            log.info("restored model state from %s", path)
        except (OSError, ValueError) as exc:
            log.warning("could not restore model state: %s", exc)

    def _restore_pause_state(self) -> None:
        if self.db.kv_get("paused") == "1":
            self.budget.pause()
            log.info("probing is paused from a previous session")
        if self.db.kv_get("learning") == "0":
            self.scorer.set_learning(False)

    # -------------------------------------------------------------- control

    def pause(self) -> None:
        """Stop every active probe (F9). Passive collectors keep running."""
        self.budget.pause()
        self.db.kv_set("paused", "1")
        log.info("probing paused")

    def resume(self) -> None:
        self.budget.resume()
        self.db.kv_set("paused", "0")
        log.info("probing resumed")

    def set_learning(self, enabled: bool) -> None:
        self.scorer.set_learning(enabled)
        self.db.kv_set("learning", "1" if enabled else "0")
        log.info("learning %s", "enabled" if enabled else "paused")

    def add_label(self, verdict: str, note: str = "", window_s: float = 900.0) -> Label:
        """Record user feedback about the recent past (F8)."""
        now = time.time()
        active = self.repo.active_incident()
        label = Label(
            ts=now,
            verdict=verdict,
            window_start=now - window_s,
            window_end=now,
            note=note[:500],
            incident_id=active.id if active else None,
        )
        return self.repo.add_label(label)

    def wipe(self) -> None:
        """Delete every stored observation (PRD 12.1)."""
        self.db.wipe()
        path = model_dir() / STATE_FILE
        with contextlib.suppress(OSError):
            path.unlink(missing_ok=True)
        self.scorer = Scorer(self.config)
        log.warning("all stored data wiped on request")

    # --------------------------------------------------------------- status

    def status(self) -> dict[str, Any]:
        score = self.repo.latest_score()
        incident = self.repo.active_incident()
        uptime = time.time() - self.started_at if self.started_at else 0.0
        return {
            "version": _version(),
            "uptime_s": round(uptime, 1),
            "ticks": self.ticks,
            "paused": self.budget.paused,
            "learning": self.scorer.learn,
            "score": score.to_dict() if score else None,
            "incident": incident.to_dict() if incident else None,
            "model": self.scorer.status(),
            "budget": self.budget.stats(),
            "collectors": self.supervisor.status(),
            "capabilities": [collector.status() for collector in self.collectors],
            "notifications": self.notifier.status(),
            "store": self.repo.summary(),
            "pending_values": self.assembler.pending(),
        }

    def health_summary(self) -> dict[str, Any]:
        """The compact payload the tray flyout and the CLI status both use."""
        score = self.repo.latest_score()
        incident = self.repo.active_incident()
        return {
            "health": round(score.health, 1) if score else None,
            "severity": score.severity if score else "unknown",
            "risk_5m": round(score.risk_5m, 3) if score else None,
            "risk_15m": round(score.risk_15m, 3) if score else None,
            "primary_layer": score.primary_layer if score else None,
            "warming_up": score.warming_up if score else True,
            "coverage": round(score.coverage, 2) if score else 0.0,
            "paused": self.budget.paused,
            "incident": incident.to_dict() if incident else None,
            "updated_at": score.ts if score else None,
        }


def _database_path() -> str:
    from ..paths import database_file

    return str(database_file())


def _version() -> str:
    from .. import __version__

    return __version__


def run_once(config: NetPulseConfig | None = None) -> dict[str, Any]:
    """Collect one round from every collector and score it.

    Used by ``netpulse check`` and by the installer smoke test: it proves the
    whole path works on this machine without leaving a process behind.
    """
    agent = Agent(config)
    try:
        for collector in agent.collectors:
            result = collector.collect()
            agent._on_result(collector, result)
        agent.tick()
        return agent.health_summary()
    finally:
        agent.db.close()


def build_incident_payload(incident: Incident) -> dict[str, Any]:
    return incident.to_dict()
