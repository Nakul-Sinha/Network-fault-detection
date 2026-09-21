"""Replay harness: run a corpus through the real scorer and measure it.

The harness deliberately drives :class:`~netpulse.ml.scorer.Scorer`, the same
object the live agent uses, rather than a re-implementation. An evaluation
that measures a parallel copy of the scoring logic eventually measures
something the product does not do.

Two things are measured that a plain classification score would miss:

* **lead time**, the gap between the first sustained alert and the moment the
  degradation actually starts. This is the product promise, so it is the
  headline metric.
* **benign alert rate**, from scenarios where nothing is wrong and from a
  regime change that is real but harmless. A model can hit every precision
  target and still be unusable if it fires hourly.

Warm-up frames are excluded from scoring: judging a model during its own
warm-up measures the warm-up, not the model.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from ..config import NetPulseConfig
from ..logging_setup import get_logger
from ..ml.l3_predict import L3Predictor
from ..ml.scorer import Scorer
from ..store.models import Sample
from .metrics import EvaluationReport, ScenarioOutcome, brier, expected_calibration_error
from .scenarios import SCENARIOS, ScenarioRun, generate, generate_soak

log = get_logger(__name__)

#: An alert is an incident the agent opened, not a threshold crossing, and
#: repeats within the notification cooldown for the same layer collapse into
#: one. That is what a user would actually be shown, so it is what the
#: alert-rate gate counts.


def replay_run(
    run: ScenarioRun,
    *,
    config: NetPulseConfig | None = None,
    predictor: L3Predictor | None = None,
    collect_calibration: bool = False,
) -> tuple[ScenarioOutcome, list[tuple[float, float]]]:
    """Replay one scenario and return its outcome.

    The second element is ``(predicted_risk, actual_outcome)`` pairs for
    calibration, collected only when asked for because it is the bulk of the
    harness's memory use on a large corpus.
    """
    config = config or NetPulseConfig()
    scorer = Scorer(config, predictor=predictor)
    outcome = ScenarioOutcome(
        name=run.name,
        expected_layer=run.expected_layer,
        benign=not run.bad_windows,
        duration_s=run.duration_s(),
    )
    calibration: list[tuple[float, float]] = []

    cooldown_s = config.notifications.cooldown_minutes * 60.0
    last_alert_per_layer: dict[str, float] = {}
    first_alert_ts: float | None = None
    scored_from: float | None = None

    for sample in run.samples:
        result = scorer.observe(sample)
        if scorer.l1.warm and scored_from is None:
            scored_from = sample.ts
        if not scorer.l1.warm:
            continue

        outcome.frames += 1
        risk = result.score.risk_15m
        bad_now = run.is_bad(sample.ts)
        if bad_now:
            outcome.peak_risk_bad = max(outcome.peak_risk_bad, risk)
        elif not _in_precursor(run, sample.ts):
            outcome.peak_risk_quiet = max(outcome.peak_risk_quiet, risk)

        if collect_calibration:
            calibration.append((risk, 1.0 if _degrades_within(run, sample.ts, 900.0) else 0.0))

        if result.opened is not None:
            layer = result.opened.primary_layer
            previous = last_alert_per_layer.get(layer)
            if previous is None or sample.ts - previous >= cooldown_s:
                outcome.alerts += 1
                last_alert_per_layer[layer] = sample.ts
                if first_alert_ts is None:
                    first_alert_ts = sample.ts
            if not outcome.incident_title or bad_now:
                outcome.incident_title = result.opened.title
                outcome.predicted_layer = layer

    # Only the frames after warm-up count toward the alert rate.
    if scored_from is not None and run.samples:
        outcome.duration_s = max(1.0, run.samples[-1].ts - scored_from)

    if run.impact_start is not None and first_alert_ts is not None:
        outcome.detected = True
        outcome.lead_time_s = max(0.0, run.impact_start - first_alert_ts)
    if run.expected_layer is not None and outcome.predicted_layer is not None:
        outcome.layer_correct = outcome.predicted_layer == run.expected_layer
    return outcome, calibration


def _in_precursor(run: ScenarioRun, ts: float) -> bool:
    """True while a fault is building but has not yet started to hurt.

    Alerts here are the product working, not false alarms, so they are
    excluded from the quiet-period statistics.
    """
    if run.impact_start is None:
        return False
    return run.impact_start - 1800.0 <= ts < run.impact_start


def _degrades_within(run: ScenarioRun, ts: float, horizon_s: float) -> bool:
    return any(start <= ts <= end or ts < start <= ts + horizon_s for start, end in run.bad_windows)


def replay_corpus(
    runs: Iterable[ScenarioRun] | None = None,
    *,
    config: NetPulseConfig | None = None,
    predictor: L3Predictor | None = None,
    seeds: tuple[int, ...] = (11,),
    include_soak: bool = True,
    soak_hours: float = 8.0,
) -> EvaluationReport:
    """Replay a corpus and build the aggregate report."""
    if runs is None:
        runs = [generate(spec, seed=seed) for spec in SCENARIOS for seed in seeds]
        if include_soak:
            runs.append(generate_soak(hours=soak_hours))
    report = EvaluationReport()
    predictions: list[float] = []
    outcomes: list[float] = []

    for run in runs:
        outcome, calibration = replay_run(
            run, config=config, predictor=predictor, collect_calibration=True
        )
        report.outcomes.append(outcome)
        for prediction, actual in calibration:
            predictions.append(prediction)
            outcomes.append(actual)

    if predictions:
        report.calibration = {
            "brier": round(brier(predictions, outcomes), 4),
            "ece": round(expected_calibration_error(predictions, outcomes), 4),
            "n": len(predictions),
        }
    return report


def load_jsonl(path: Path) -> list[Sample]:
    """Load samples from a JSONL fixture, one object per line.

    This is the format an exported diagnostic bundle uses, so a user's real
    capture can be replayed through the same harness that runs the synthetic
    corpus. That is the bridge from "it passes our tests" to "it would have
    caught the thing that happened to you".
    """
    samples: list[Sample] = []
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            samples.append(Sample.from_row(row))
    return samples


def write_jsonl(path: Path, samples: Iterable[Sample]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        for sample in samples:
            row = {key: value for key, value in sample.as_row().items() if value is not None}
            handle.write(json.dumps(row, separators=(",", ":")) + "\n")
    return path


def report_to_json(report: EvaluationReport) -> str:
    return json.dumps(report.to_dict(), indent=2)


def summarise(report: EvaluationReport) -> dict[str, Any]:
    return report.summary()
