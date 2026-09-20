"""Typed read and write helpers over the feature store.

Everything above the store, the agent loop, the API and the export bundle,
talks to this module rather than to SQL, so the schema stays changeable.
"""

from __future__ import annotations

import time
from typing import Any

from ..features.schema import FEATURE_NAMES
from .db import Database
from .models import DriftEvent, Incident, Label, ProbeRecord, Sample, Score, dumps, loads

_SAMPLE_COLUMNS = ", ".join(f'"{name}"' for name in FEATURE_NAMES)
_SAMPLE_PLACEHOLDERS = ", ".join("?" for _ in range(len(FEATURE_NAMES) + 1))

VALID_VERDICTS = ("bad", "ok", "unsure")


class Repository:
    def __init__(self, db: Database) -> None:
        self.db = db

    # ----------------------------------------------------------- samples

    def add_sample(self, sample: Sample) -> None:
        values = [sample.ts] + [sample.values.get(name) for name in FEATURE_NAMES]
        self.db.execute(
            f"INSERT OR REPLACE INTO samples (ts, {_SAMPLE_COLUMNS}) "
            f"VALUES ({_SAMPLE_PLACEHOLDERS})",
            values,
        )

    def add_samples(self, samples: list[Sample]) -> None:
        rows = [[s.ts] + [s.values.get(name) for name in FEATURE_NAMES] for s in samples]
        self.db.executemany(
            f"INSERT OR REPLACE INTO samples (ts, {_SAMPLE_COLUMNS}) "
            f"VALUES ({_SAMPLE_PLACEHOLDERS})",
            rows,
        )

    def recent_samples(self, limit: int = 240, since: float | None = None) -> list[Sample]:
        if since is None:
            rows = self.db.query("SELECT * FROM samples ORDER BY ts DESC LIMIT ?", (limit,))
            return [Sample.from_row(dict(r)) for r in reversed(rows)]
        rows = self.db.query(
            "SELECT * FROM samples WHERE ts >= ? ORDER BY ts ASC LIMIT ?", (since, limit)
        )
        return [Sample.from_row(dict(r)) for r in rows]

    def last_sample(self) -> Sample | None:
        row = self.db.query_one("SELECT * FROM samples ORDER BY ts DESC LIMIT 1")
        return Sample.from_row(dict(row)) if row else None

    def sample_count(self) -> int:
        row = self.db.query_one("SELECT COUNT(*) AS n FROM samples")
        return int(row["n"]) if row else 0

    # ------------------------------------------------------------ scores

    def add_score(self, score: Score) -> None:
        self.db.execute(
            "INSERT OR REPLACE INTO scores (ts, health, risk_5m, risk_15m, severity, "
            "l1_anomaly, l2_anomaly, primary_layer, secondary_layer, layer_scores, "
            "warming_up, coverage) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                score.ts,
                score.health,
                score.risk_5m,
                score.risk_15m,
                score.severity,
                score.l1_anomaly,
                score.l2_anomaly,
                score.primary_layer,
                score.secondary_layer,
                dumps(score.layer_scores),
                int(score.warming_up),
                score.coverage,
            ),
        )

    def latest_score(self) -> Score | None:
        row = self.db.query_one("SELECT * FROM scores ORDER BY ts DESC LIMIT 1")
        return _score_from_row(row) if row else None

    def scores_since(self, since: float, limit: int = 2000) -> list[Score]:
        rows = self.db.query(
            "SELECT * FROM scores WHERE ts >= ? ORDER BY ts ASC LIMIT ?", (since, limit)
        )
        return [_score_from_row(row) for row in rows]

    # --------------------------------------------------------- incidents

    def open_incident(self, incident: Incident) -> Incident:
        cursor = self.db.execute(
            "INSERT INTO incidents (started_at, ended_at, primary_layer, secondary_layer, "
            "severity, title, summary, confidence, evidence, remediation, template_id, "
            "user_label, notified, peak_risk) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                incident.started_at,
                incident.ended_at,
                incident.primary_layer,
                incident.secondary_layer,
                incident.severity,
                incident.title,
                incident.summary,
                incident.confidence,
                dumps(incident.evidence),
                dumps(incident.remediation),
                incident.template_id,
                incident.user_label,
                int(incident.notified),
                incident.peak_risk,
            ),
        )
        incident.id = int(cursor.lastrowid or 0)
        return incident

    def update_incident(self, incident: Incident) -> None:
        if incident.id is None:
            raise ValueError("cannot update an incident without an id")
        self.db.execute(
            "UPDATE incidents SET ended_at=?, primary_layer=?, secondary_layer=?, severity=?, "
            "title=?, summary=?, confidence=?, evidence=?, remediation=?, template_id=?, "
            "user_label=?, notified=?, peak_risk=? WHERE id=?",
            (
                incident.ended_at,
                incident.primary_layer,
                incident.secondary_layer,
                incident.severity,
                incident.title,
                incident.summary,
                incident.confidence,
                dumps(incident.evidence),
                dumps(incident.remediation),
                incident.template_id,
                incident.user_label,
                int(incident.notified),
                incident.peak_risk,
                incident.id,
            ),
        )

    def active_incident(self) -> Incident | None:
        row = self.db.query_one(
            "SELECT * FROM incidents WHERE ended_at IS NULL ORDER BY started_at DESC LIMIT 1"
        )
        return _incident_from_row(row) if row else None

    def recent_incidents(self, limit: int = 50, since: float | None = None) -> list[Incident]:
        if since is None:
            rows = self.db.query(
                "SELECT * FROM incidents ORDER BY started_at DESC LIMIT ?", (limit,)
            )
        else:
            rows = self.db.query(
                "SELECT * FROM incidents WHERE started_at >= ? ORDER BY started_at DESC LIMIT ?",
                (since, limit),
            )
        return [_incident_from_row(row) for row in rows]

    def get_incident(self, incident_id: int) -> Incident | None:
        row = self.db.query_one("SELECT * FROM incidents WHERE id = ?", (incident_id,))
        return _incident_from_row(row) if row else None

    # ------------------------------------------------------------ labels

    def add_label(self, label: Label) -> Label:
        if label.verdict not in VALID_VERDICTS:
            raise ValueError(f"verdict must be one of {VALID_VERDICTS}, got {label.verdict!r}")
        cursor = self.db.execute(
            "INSERT INTO labels (ts, verdict, window_start, window_end, note, incident_id) "
            "VALUES (?,?,?,?,?,?)",
            (
                label.ts,
                label.verdict,
                label.window_start,
                label.window_end,
                label.note,
                label.incident_id,
            ),
        )
        label.id = int(cursor.lastrowid or 0)
        if label.incident_id is not None:
            self.db.execute(
                "UPDATE incidents SET user_label = ? WHERE id = ?",
                (label.verdict, label.incident_id),
            )
        return label

    def recent_labels(self, limit: int = 100) -> list[Label]:
        rows = self.db.query("SELECT * FROM labels ORDER BY ts DESC LIMIT ?", (limit,))
        return [
            Label(
                id=int(r["id"]),
                ts=float(r["ts"]),
                verdict=str(r["verdict"]),
                window_start=float(r["window_start"]),
                window_end=float(r["window_end"]),
                note=str(r["note"] or ""),
                incident_id=r["incident_id"],
            )
            for r in rows
        ]

    # --------------------------------------------------------- probe log

    def log_probe(self, record: ProbeRecord) -> None:
        self.db.execute(
            "INSERT INTO probe_log (ts, collector, target, ok, duration_ms, detail) "
            "VALUES (?,?,?,?,?,?)",
            (
                record.ts,
                record.collector,
                record.target,
                int(record.ok),
                record.duration_ms,
                record.detail[:200],
            ),
        )

    def recent_probes(self, limit: int = 100) -> list[ProbeRecord]:
        rows = self.db.query("SELECT * FROM probe_log ORDER BY ts DESC LIMIT ?", (limit,))
        return [
            ProbeRecord(
                id=int(r["id"]),
                ts=float(r["ts"]),
                collector=str(r["collector"]),
                target=str(r["target"]),
                ok=bool(r["ok"]),
                duration_ms=r["duration_ms"],
                detail=str(r["detail"] or ""),
            )
            for r in rows
        ]

    def probe_counts(self, since: float) -> dict[str, int]:
        rows = self.db.query(
            "SELECT collector, COUNT(*) AS n FROM probe_log WHERE ts >= ? GROUP BY collector",
            (since,),
        )
        return {str(r["collector"]): int(r["n"]) for r in rows}

    # -------------------------------------------------------------- drift

    def add_drift_event(self, event: DriftEvent) -> None:
        self.db.execute(
            "INSERT INTO drift_events (ts, feature, psi, action) VALUES (?,?,?,?)",
            (event.ts, event.feature, event.psi, event.action),
        )

    def recent_drift(self, limit: int = 50) -> list[DriftEvent]:
        rows = self.db.query("SELECT * FROM drift_events ORDER BY ts DESC LIMIT ?", (limit,))
        return [
            DriftEvent(
                id=int(r["id"]),
                ts=float(r["ts"]),
                feature=str(r["feature"]),
                psi=float(r["psi"]),
                action=str(r["action"]),
            )
            for r in rows
        ]

    # --------------------------------------------------------------- misc

    def summary(self) -> dict[str, Any]:
        stats = self.db.stats()
        stats["active_incident"] = self.active_incident() is not None
        stats["probes_last_hour"] = sum(self.probe_counts(time.time() - 3600).values())
        return stats


def _score_from_row(row: Any) -> Score:
    return Score(
        ts=float(row["ts"]),
        health=float(row["health"]),
        risk_5m=float(row["risk_5m"]),
        risk_15m=float(row["risk_15m"]),
        severity=str(row["severity"]),
        l1_anomaly=float(row["l1_anomaly"]),
        l2_anomaly=float(row["l2_anomaly"]),
        primary_layer=row["primary_layer"],
        secondary_layer=row["secondary_layer"],
        layer_scores=loads(row["layer_scores"], {}),
        warming_up=bool(row["warming_up"]),
        coverage=float(row["coverage"]),
    )


def _incident_from_row(row: Any) -> Incident:
    return Incident(
        id=int(row["id"]),
        started_at=float(row["started_at"]),
        ended_at=row["ended_at"],
        primary_layer=str(row["primary_layer"]),
        secondary_layer=row["secondary_layer"],
        severity=str(row["severity"]),
        title=str(row["title"]),
        summary=str(row["summary"]),
        confidence=float(row["confidence"]),
        evidence=loads(row["evidence"], []),
        remediation=loads(row["remediation"], []),
        template_id=str(row["template_id"] or ""),
        user_label=row["user_label"],
        notified=bool(row["notified"]),
        peak_risk=float(row["peak_risk"]),
    )
