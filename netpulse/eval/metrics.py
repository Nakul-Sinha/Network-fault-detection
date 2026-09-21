"""Evaluation metrics for the replay harness.

The headline number for this product is **lead time**, not accuracy. A
detector that fires the instant a call breaks is worth very little; the
promise is a warning 5 to 15 minutes early. So lead time is measured first,
as a distribution rather than an average, because the median is what PRD 3.2
sets a target against and the tail is what decides whether the product feels
trustworthy.

Alert rate per day is tracked with equal weight. PRD 3.2 caps default noisy
alerts at two per day, and a model that hits every precision target while
firing hourly has failed.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any


@dataclass
class ScenarioOutcome:
    """What the harness observed for one scenario run."""

    name: str
    expected_layer: str | None
    benign: bool
    detected: bool = False
    lead_time_s: float | None = None
    predicted_layer: str | None = None
    layer_correct: bool | None = None
    alerts: int = 0
    duration_s: float = 0.0
    peak_risk_bad: float = 0.0
    peak_risk_quiet: float = 0.0
    incident_title: str = ""
    frames: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "scenario": self.name,
            "benign": self.benign,
            "detected": self.detected,
            "lead_time_s": round(self.lead_time_s, 1) if self.lead_time_s is not None else None,
            "expected_layer": self.expected_layer,
            "predicted_layer": self.predicted_layer,
            "layer_correct": self.layer_correct,
            "alerts": self.alerts,
            "alerts_per_day": round(self.alerts_per_day(), 2),
            "peak_risk_bad": round(self.peak_risk_bad, 3),
            "peak_risk_quiet": round(self.peak_risk_quiet, 3),
            "incident_title": self.incident_title,
            "frames": self.frames,
        }

    def alerts_per_day(self) -> float:
        if self.duration_s <= 0:
            return 0.0
        return self.alerts * 86400.0 / self.duration_s


@dataclass
class EvaluationReport:
    """Aggregate of every scenario outcome, plus the PRD gate verdicts."""

    outcomes: list[ScenarioOutcome] = field(default_factory=list)
    calibration: dict[str, Any] = field(default_factory=dict)

    # ------------------------------------------------------------ aggregates

    def faults(self) -> list[ScenarioOutcome]:
        return [outcome for outcome in self.outcomes if not outcome.benign]

    def benign(self) -> list[ScenarioOutcome]:
        return [outcome for outcome in self.outcomes if outcome.benign]

    def detection_rate(self) -> float:
        faults = self.faults()
        if not faults:
            return 0.0
        return sum(1 for outcome in faults if outcome.detected) / len(faults)

    def lead_times(self) -> list[float]:
        return [
            outcome.lead_time_s
            for outcome in self.faults()
            if outcome.detected and outcome.lead_time_s is not None
        ]

    def median_lead_time_s(self) -> float:
        values = sorted(self.lead_times())
        if not values:
            return 0.0
        middle = len(values) // 2
        if len(values) % 2:
            return values[middle]
        return (values[middle - 1] + values[middle]) / 2.0

    def lead_time_cdf(self) -> list[dict[str, float]]:
        """Points for the lead-time CDF chart in the internal dashboard."""
        values = sorted(self.lead_times())
        if not values:
            return []
        return [
            {"lead_time_s": value, "fraction": (index + 1) / len(values)}
            for index, value in enumerate(values)
        ]

    def layer_accuracy(self) -> float:
        judged = [outcome for outcome in self.faults() if outcome.layer_correct is not None]
        if not judged:
            return 0.0
        return sum(1 for outcome in judged if outcome.layer_correct) / len(judged)

    def precision(self) -> float:
        """Fraction of scenarios with alerts where an alert was warranted."""
        alerting = [outcome for outcome in self.outcomes if outcome.alerts > 0]
        if not alerting:
            return 0.0
        return sum(1 for outcome in alerting if not outcome.benign) / len(alerting)

    def benign_alerts_per_day(self) -> float:
        benign = self.benign()
        if not benign:
            return 0.0
        return sum(outcome.alerts_per_day() for outcome in benign) / len(benign)

    def summary(self) -> dict[str, Any]:
        return {
            "scenarios": len(self.outcomes),
            "faults": len(self.faults()),
            "detection_rate": round(self.detection_rate(), 3),
            "median_lead_time_s": round(self.median_lead_time_s(), 1),
            "median_lead_time_min": round(self.median_lead_time_s() / 60.0, 2),
            "layer_accuracy": round(self.layer_accuracy(), 3),
            "precision": round(self.precision(), 3),
            "benign_alerts_per_day": round(self.benign_alerts_per_day(), 2),
            "calibration": self.calibration,
        }

    # ----------------------------------------------------------------- gates

    def gates(self, targets: dict[str, float] | None = None) -> list[dict[str, Any]]:
        """Check the report against the PRD 3.2 MVP targets."""
        thresholds = dict(DEFAULT_GATES)
        if targets:
            thresholds.update(targets)
        summary = self.summary()
        checks = [
            ("median_lead_time_s", summary["median_lead_time_s"], "at least", "s"),
            ("detection_rate", summary["detection_rate"], "at least", ""),
            ("layer_accuracy", summary["layer_accuracy"], "at least", ""),
            ("precision", summary["precision"], "at least", ""),
            ("benign_alerts_per_day", summary["benign_alerts_per_day"], "at most", "/day"),
        ]
        results: list[dict[str, Any]] = []
        for name, actual, direction, unit in checks:
            target = thresholds[name]
            passed = actual >= target if direction == "at least" else actual <= target
            results.append(
                {
                    "gate": name,
                    "actual": actual,
                    "target": target,
                    "direction": direction,
                    "unit": unit,
                    "passed": bool(passed),
                }
            )
        return results

    def passed(self, targets: dict[str, float] | None = None) -> bool:
        return all(gate["passed"] for gate in self.gates(targets))

    def to_dict(self) -> dict[str, Any]:
        return {
            "summary": self.summary(),
            "gates": self.gates(),
            "scenarios": [outcome.to_dict() for outcome in self.outcomes],
            "lead_time_cdf": self.lead_time_cdf(),
        }


#: PRD 3.2 MVP column, expressed as machine-checkable thresholds.
DEFAULT_GATES: dict[str, float] = {
    "median_lead_time_s": 300.0,  # forecast lead time, median, at least 5 minutes
    "detection_rate": 0.9,
    "layer_accuracy": 0.6,  # "user RCA agreement (right layer?)"
    "precision": 0.5,  # precision of elevated-risk alerts
    "benign_alerts_per_day": 2.0,  # default noisy alerts per day
}


def reliability_bins(
    predictions: list[float], outcomes: list[float], bins: int = 10
) -> list[dict[str, float]]:
    """Reliability diagram rows: predicted probability versus observed rate."""
    rows: list[dict[str, float]] = []
    for index in range(bins):
        low = index / bins
        high = (index + 1) / bins
        selected = [
            (prediction, outcome)
            for prediction, outcome in zip(predictions, outcomes, strict=True)
            if low <= prediction < high or (index == bins - 1 and prediction == 1.0)
        ]
        if not selected:
            continue
        rows.append(
            {
                "bin_low": low,
                "bin_high": high,
                "count": len(selected),
                "predicted": sum(p for p, _ in selected) / len(selected),
                "observed": sum(o for _, o in selected) / len(selected),
            }
        )
    return rows


def brier(predictions: list[float], outcomes: list[float]) -> float:
    if not predictions:
        return 0.0
    return sum(
        (prediction - outcome) ** 2
        for prediction, outcome in zip(predictions, outcomes, strict=True)
    ) / len(predictions)


def expected_calibration_error(
    predictions: list[float], outcomes: list[float], bins: int = 10
) -> float:
    rows = reliability_bins(predictions, outcomes, bins)
    total = sum(row["count"] for row in rows)
    if not total:
        return 0.0
    return sum(row["count"] * abs(row["predicted"] - row["observed"]) for row in rows) / total


def format_report(report: EvaluationReport) -> str:
    """Human-readable console output for ``netpulse eval``."""
    summary = report.summary()
    lines: list[str] = []
    lines.append("Scenario results")
    lines.append(f"  {'scenario':<22}{'kind':<8}{'lead':>8}{'layer':>14}{'alerts/day':>12}  title")
    for outcome in report.outcomes:
        kind = "benign" if outcome.benign else "fault"
        lead = (
            f"{outcome.lead_time_s / 60:.1f}m"
            if outcome.lead_time_s is not None
            else ("miss" if not outcome.benign else "-")
        )
        layer = outcome.predicted_layer or "-"
        mark = "" if outcome.layer_correct is not False else " x"
        lines.append(
            f"  {outcome.name:<22}{kind:<8}{lead:>8}{layer + mark:>14}"
            f"{outcome.alerts_per_day():>12.1f}  {outcome.incident_title[:40]}"
        )

    lines.append("")
    lines.append("Summary")
    lines.append(f"  detection rate        {summary['detection_rate'] * 100:.0f}%")
    lines.append(f"  median lead time      {summary['median_lead_time_min']:.1f} min")
    lines.append(f"  layer accuracy        {summary['layer_accuracy'] * 100:.0f}%")
    lines.append(f"  alert precision       {summary['precision'] * 100:.0f}%")
    lines.append(f"  benign alerts/day     {summary['benign_alerts_per_day']:.2f}")
    if report.calibration:
        lines.append(f"  brier score           {report.calibration.get('brier', 0):.4f}")
        lines.append(f"  calibration error     {report.calibration.get('ece', 0):.4f}")

    lines.append("")
    lines.append("Gates (PRD 3.2, MVP column)")
    for gate in report.gates():
        mark = "PASS" if gate["passed"] else "FAIL"
        lines.append(
            f"  [{mark}] {gate['gate']:<24} {gate['actual']:>8.3g}{gate['unit']}"
            f"  ({gate['direction']} {gate['target']:g}{gate['unit']})"
        )
    return "\n".join(lines)


def percentile(values: list[float], fraction: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    position = fraction * (len(ordered) - 1)
    low = int(math.floor(position))
    high = min(low + 1, len(ordered) - 1)
    weight = position - low
    return ordered[low] * (1 - weight) + ordered[high] * weight
