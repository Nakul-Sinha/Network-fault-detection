"""The model ladder assembled into one object.

Everything above this module, the agent loop, the replay harness and the
tests, sees a single call: give me a sample, get back a score, an
explanation and any incident transition. Keeping that seam narrow is what
lets the evaluation harness replay a corpus through exactly the code path a
live agent runs, rather than through a re-implementation that drifts away
from it.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from ..config import NetPulseConfig
from ..features.pipeline import FeaturePipeline
from ..logging_setup import get_logger
from ..store.models import Incident, Sample, Score
from . import l0_rules, l3_predict, l4_rca
from .drift import DriftDecision, DriftMonitor
from .l1_stats import L1Baselines
from .l2_online import L2Ensemble
from .l3_predict import L3Predictor

log = get_logger(__name__)

#: Consecutive frames at or above the alert band before an incident opens.
#: Ninety seconds at the default cadence. A five to fifteen minute forecast
#: has no need to react in thirty seconds, and the shorter window let isolated
#: noise spikes open incidents on a perfectly healthy network.
OPEN_AFTER = 6
#: Consecutive quiet frames before an incident closes. Deliberately larger
#: than OPEN_AFTER so a flapping network produces one incident, not twenty.
CLOSE_AFTER = 8
#: Health is smoothed so the number a user is watching does not flicker.
HEALTH_BETA = 0.4
#: Risk is smoothed for the same reason and a stronger one: a probability of
#: trouble in the next fifteen minutes that swings between 0.1 and 1.0 from
#: one sample to the next is not a forecast, it is noise with a percent sign.
#: The resulting lag is a few tens of seconds against lead times in minutes.
RISK_BETA = 0.35


@dataclass
class ScoreResult:
    """Everything one pass through the ladder produced."""

    score: Score
    card: dict[str, Any] | None = None
    rule_hits: list[l0_rules.RuleHit] = field(default_factory=list)
    drift: list[DriftDecision] = field(default_factory=list)
    opened: Incident | None = None
    closed: bool = False
    l3_source: str = "heuristic"

    def to_dict(self) -> dict[str, Any]:
        return {
            "score": self.score.to_dict(),
            "card": self.card,
            "rules": [hit.to_dict() for hit in self.rule_hits],
            "drift": [{"feature": d.feature, "psi": round(d.psi, 4)} for d in self.drift],
            "l3_source": self.l3_source,
        }


class Scorer:
    """L0 to L4 in order, plus incident lifecycle and health smoothing."""

    def __init__(
        self,
        config: NetPulseConfig | None = None,
        *,
        predictor: L3Predictor | None = None,
        learn: bool = True,
    ) -> None:
        self.config = config or NetPulseConfig()
        self.learn = learn
        self.pipeline = FeaturePipeline()
        warmup_samples = max(
            10,
            int(self.config.ml.warmup_minutes * 60 / max(1, self.config.probes.system_interval_s)),
        )
        self.l1 = L1Baselines(warmup_samples=warmup_samples)
        self.l2 = L2Ensemble(
            hidden_ratio=self.config.ml.l2_hidden_ratio,
            learning_rate=self.config.ml.l2_learning_rate,
        )
        self.l3 = predictor if predictor is not None else L3Predictor.load()
        self.drift = DriftMonitor(threshold=self.config.ml.drift_psi_threshold)

        self.health = 100.0
        self.risk_5m = 0.0
        self.risk_15m = 0.0
        self.active_incident: Incident | None = None
        self._above = 0
        self._below = 0
        self._last_card: dict[str, Any] | None = None

    # ------------------------------------------------------------------ main

    def observe(self, sample: Sample) -> ScoreResult:
        frame = self.pipeline.push(sample)
        hits = l0_rules.evaluate(frame)

        l1_result = self.l1.observe(frame) if self.learn else _frozen_l1(self.l1, frame)
        l2_result = self.l2.observe(frame, learn=self.learn)
        l3_result = self.l3.predict(frame, l1_result, l2_result)

        floor = l0_rules.risk_floor(hits)
        # The L0 floor bypasses smoothing on purpose: an unreachable gateway
        # is not a forecast to be eased into, it is a fact.
        self.risk_5m += RISK_BETA * (l3_predict.clamp_probability(l3_result.risk_5m) - self.risk_5m)
        self.risk_15m += RISK_BETA * (
            l3_predict.clamp_probability(l3_result.risk_15m) - self.risk_15m
        )
        risk_5m = max(floor, self.risk_5m)
        risk_15m = max(floor, self.risk_15m)

        attribution = l4_rca.attribute(frame, l1_result, l2_result, hits)
        severity = self._severity(risk_15m, hits)
        self._update_health(l1_result.anomaly, l2_result.anomaly, risk_5m, floor)

        score = Score(
            ts=frame.ts,
            health=self.health,
            risk_5m=risk_5m,
            risk_15m=risk_15m,
            severity=severity,
            l1_anomaly=l1_result.anomaly,
            l2_anomaly=l2_result.anomaly,
            primary_layer=attribution.primary,
            secondary_layer=attribution.secondary,
            layer_scores=attribution.scores,
            warming_up=not self.l1.warm,
            coverage=frame.coverage,
        )

        card = None
        if attribution.primary is not None and l0_rules.severity_rank(severity) >= 1:
            card = l4_rca.explain(frame, attribution, severity, hits, risk_15m)
            self._last_card = card

        decisions = self.drift.observe(frame, now=frame.ts) if self.learn else []
        for decision in decisions:
            self._apply_drift(decision)

        opened, closed = self._update_incident(score, card, frame.ts)

        return ScoreResult(
            score=score,
            card=card,
            rule_hits=hits,
            drift=decisions,
            opened=opened,
            closed=closed,
            l3_source=l3_result.source,
        )

    # ------------------------------------------------------------- internals

    def _severity(self, risk_15m: float, hits: list[l0_rules.RuleHit]) -> str:
        band = l3_predict.band_for(risk_15m, self.config.ml.risk_bands)
        worst = l0_rules.worst(hits)
        if worst is not None:
            band = l0_rules.max_severity(band, worst.severity)
        return band

    def _update_health(self, l1: float, l2: float, risk: float, floor: float) -> None:
        """Blend the present and the forecast into a single 0 to 100 number.

        The present dominates: a user looking at a health score wants to know
        how their network is right now, with the forecast pulling the number
        down early rather than defining it.
        """
        penalty = max(floor, min(1.0, 0.65 * l1 + 0.25 * l2 + 0.35 * risk))
        target = 100.0 * (1.0 - penalty)
        self.health += HEALTH_BETA * (target - self.health)
        self.health = max(0.0, min(100.0, self.health))

    def _apply_drift(self, decision: DriftDecision) -> None:
        """Relearn what "normal" means for a feature, never what bad means."""
        self.l1.reset_feature(decision.feature)
        from ..features.schema import layer_of

        self.l2.reset(layer_of(decision.feature))

    def _update_incident(
        self, score: Score, card: dict[str, Any] | None, now: float
    ) -> tuple[Incident | None, bool]:
        alerting = l0_rules.severity_rank(score.severity) >= l0_rules.severity_rank("risk")
        opened: Incident | None = None
        closed = False

        if alerting:
            self._below = 0
            self._above += 1
        else:
            self._above = 0
            self._below += 1

        if self.active_incident is None:
            if self._above >= OPEN_AFTER and card is not None:
                opened = Incident(
                    started_at=now,
                    primary_layer=score.primary_layer or "os",
                    secondary_layer=score.secondary_layer,
                    severity=score.severity,
                    title=str(card["title"]),
                    summary=str(card["summary"]),
                    confidence=float(card.get("confidence", 0.0)),
                    evidence=list(card.get("evidence", [])),
                    remediation=list(card.get("remediation", [])),
                    template_id=str(card.get("template_id", "")),
                    peak_risk=score.risk_15m,
                )
                self.active_incident = opened
            return opened, closed

        incident = self.active_incident
        incident.peak_risk = max(incident.peak_risk, score.risk_15m)
        if l0_rules.severity_rank(score.severity) > l0_rules.severity_rank(incident.severity):
            incident.severity = score.severity
            if card is not None:
                # Escalation rewrites the card: the story changed.
                incident.title = str(card["title"])
                incident.summary = str(card["summary"])
                incident.primary_layer = score.primary_layer or incident.primary_layer
                incident.template_id = str(card.get("template_id", ""))
                incident.evidence = list(card.get("evidence", []))
                incident.remediation = list(card.get("remediation", []))

        if self._below >= CLOSE_AFTER:
            incident.ended_at = now
            self.active_incident = None
            closed = True
        return opened, closed

    # ---------------------------------------------------------------- state

    def status(self) -> dict[str, Any]:
        return {
            "warming_up": not self.l1.warm,
            "l1_samples": self.l1.samples_seen,
            "l1_warmup_target": self.l1.warmup_samples,
            "l2_warm": self.l2.warm,
            "l3_trained": self.l3.trained,
            "health": round(self.health, 1),
            "risk_5m": round(self.risk_5m, 4),
            "risk_15m": round(self.risk_15m, 4),
            "learning": self.learn,
            "active_incident": self.active_incident.to_dict() if self.active_incident else None,
        }

    def save_state(self) -> dict[str, Any]:
        return {
            "version": 1,
            "saved_at": time.time(),
            "health": self.health,
            "l1": self.l1.to_dict(),
            "l2": self.l2.to_dict(),
        }

    def load_state(self, data: dict[str, Any]) -> None:
        try:
            self.health = float(data.get("health", 100.0))
            if "l1" in data:
                self.l1 = L1Baselines.from_dict(data["l1"])
            if "l2" in data:
                self.l2 = L2Ensemble.from_dict(data["l2"])
        except (KeyError, TypeError, ValueError) as exc:
            log.warning("could not restore model state, starting fresh: %s", exc)

    def set_learning(self, enabled: bool) -> None:
        self.learn = enabled


def _frozen_l1(l1: L1Baselines, frame: Any) -> Any:
    """Score against L1 without updating it, for paused learning (F9)."""
    from .l1_stats import L1Result, aggregate_by_layer, combine

    scores = l1.score_only(frame)
    return L1Result(
        anomaly=combine(scores.values()),
        per_feature=scores,
        per_layer=aggregate_by_layer(scores),
        shifted=[],
        warm=l1.warm,
    )
