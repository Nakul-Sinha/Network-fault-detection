"""Row types shared by the store, the model ladder and the API.

These are plain dataclasses rather than pydantic models: they sit on the hot
path (one Sample every few seconds) and they never parse untrusted input.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any

from ..features.schema import FEATURE_NAMES


@dataclass(slots=True)
class Sample:
    """One timestamped cross-layer observation.

    ``values`` is sparse: a collector that is disabled, unsupported on this
    platform, or temporarily failing simply leaves its features out, and the
    feature pipeline carries the last known value forward within a bounded
    staleness window.
    """

    ts: float = field(default_factory=time.time)
    values: dict[str, float] = field(default_factory=dict)
    sources: dict[str, str] = field(default_factory=dict)

    def set(self, name: str, value: float | None, source: str = "") -> None:
        if value is None:
            return
        self.values[name] = float(value)
        if source:
            self.sources[name] = source

    def merge(self, other: Sample) -> None:
        self.values.update(other.values)
        self.sources.update(other.sources)

    def get(self, name: str, default: float | None = None) -> float | None:
        return self.values.get(name, default)

    def as_row(self) -> dict[str, Any]:
        row: dict[str, Any] = {"ts": self.ts}
        for name in FEATURE_NAMES:
            row[name] = self.values.get(name)
        return row

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> Sample:
        values = {name: float(row[name]) for name in FEATURE_NAMES if row.get(name) is not None}
        return cls(ts=float(row["ts"]), values=values)


@dataclass(slots=True)
class Score:
    """Output of one pass through the model ladder."""

    ts: float
    health: float
    risk_5m: float
    risk_15m: float
    severity: str
    l1_anomaly: float = 0.0
    l2_anomaly: float = 0.0
    primary_layer: str | None = None
    secondary_layer: str | None = None
    layer_scores: dict[str, float] = field(default_factory=dict)
    warming_up: bool = False
    coverage: float = 1.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "ts": self.ts,
            "health": round(self.health, 2),
            "risk_5m": round(self.risk_5m, 4),
            "risk_15m": round(self.risk_15m, 4),
            "severity": self.severity,
            "l1_anomaly": round(self.l1_anomaly, 4),
            "l2_anomaly": round(self.l2_anomaly, 4),
            "primary_layer": self.primary_layer,
            "secondary_layer": self.secondary_layer,
            "layer_scores": {k: round(v, 4) for k, v in self.layer_scores.items()},
            "warming_up": self.warming_up,
            "coverage": round(self.coverage, 3),
        }


@dataclass(slots=True)
class Incident:
    """A grouped stretch of elevated risk with an explanation attached."""

    started_at: float
    primary_layer: str
    severity: str
    title: str
    summary: str
    id: int | None = None
    ended_at: float | None = None
    secondary_layer: str | None = None
    confidence: float = 0.0
    evidence: list[dict[str, Any]] = field(default_factory=list)
    remediation: list[str] = field(default_factory=list)
    template_id: str = ""
    user_label: str | None = None
    notified: bool = False
    peak_risk: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "primary_layer": self.primary_layer,
            "secondary_layer": self.secondary_layer,
            "severity": self.severity,
            "title": self.title,
            "summary": self.summary,
            "confidence": round(self.confidence, 3),
            "evidence": self.evidence,
            "remediation": self.remediation,
            "template_id": self.template_id,
            "user_label": self.user_label,
            "notified": self.notified,
            "peak_risk": round(self.peak_risk, 4),
            "active": self.ended_at is None,
        }


@dataclass(slots=True)
class Label:
    """User feedback (F8): was the network actually bad in this window?"""

    ts: float
    verdict: str  # "bad" | "ok" | "unsure"
    window_start: float
    window_end: float
    note: str = ""
    incident_id: int | None = None
    id: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "ts": self.ts,
            "verdict": self.verdict,
            "window_start": self.window_start,
            "window_end": self.window_end,
            "note": self.note,
            "incident_id": self.incident_id,
        }


@dataclass(slots=True)
class ProbeRecord:
    """One line of the transparent probe log.

    The agent sends active traffic, so it owes the user a ledger of exactly
    what it sent and where. Targets are stored as configured, never as
    resolved user traffic, and no response body is retained.
    """

    ts: float
    collector: str
    target: str
    ok: bool
    duration_ms: float | None = None
    detail: str = ""
    id: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "ts": self.ts,
            "collector": self.collector,
            "target": self.target,
            "ok": self.ok,
            "duration_ms": self.duration_ms,
            "detail": self.detail,
        }


@dataclass(slots=True)
class DriftEvent:
    ts: float
    feature: str
    psi: float
    action: str
    id: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "ts": self.ts,
            "feature": self.feature,
            "psi": round(self.psi, 4),
            "action": self.action,
        }


def dumps(value: Any) -> str:
    return json.dumps(value, separators=(",", ":"), default=str)


def loads(value: str | None, fallback: Any) -> Any:
    if not value:
        return fallback
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return fallback
