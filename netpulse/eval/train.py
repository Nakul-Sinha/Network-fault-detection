"""Offline training for the L3 predictive head.

PRD 8.3 puts training offline and shipping the result: the agent never trains
L3 on a user's machine in the MVP, because that would need labelled
degradation windows the user has not provided. What ships is a model fitted
here, on the fault-injection corpus, and exported as a readable JSON file.

The corpus is split by **seed**, not by row. Splitting a time series by row
leaks: neighbouring frames share rolling windows, so a random split trains
and tests on what is effectively the same moment and reports an accuracy the
model does not have. Holding out whole runs is the honest version.

Labels use the approach rather than the arrival: a frame is positive when
degradation *begins* within the horizon. A model trained on frames inside the
bad window learns to recognise an outage in progress, which is detection
wearing a forecast's clothes.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

from ..config import NetPulseConfig
from ..logging_setup import get_logger
from ..ml.l3_predict import (
    HORIZONS,
    L3Predictor,
    LogisticModel,
    evaluate_binary,
    extract,
    train_logistic,
)
from ..ml.l3_predict import (
    brier_score as _brier,
)
from ..ml.l3_predict import (
    expected_calibration_error as _ece,
)
from ..ml.scorer import Scorer
from .scenarios import SCENARIOS, ScenarioRun, generate, generate_soak

log = get_logger(__name__)

TRAIN_SEEDS: tuple[int, ...] = (11, 23, 37, 51, 67)
HOLDOUT_SEEDS: tuple[int, ...] = (83, 97)

#: Hours of healthy traffic added per seed. The scenario list is twelve faults
#: to two benign runs, a prior nothing like a real install, and a head trained
#: on it learns that trouble is the normal state of a network. Soak runs
#: restore a realistic class balance; the positive-class weighting in
#: train_logistic is what keeps the rare class from being ignored, which is
#: the division of labour Outage-Watch argues for.
SOAK_HOURS_PER_SEED = 6.0


@dataclass
class TrainingSet:
    X: np.ndarray
    y: dict[int, np.ndarray]
    groups: list[str]

    def __len__(self) -> int:
        return int(self.X.shape[0])


def collect(runs: list[ScenarioRun], config: NetPulseConfig | None = None) -> TrainingSet:
    """Run the corpus through the ladder and record inputs and labels.

    Frames are collected with the same Scorer the agent runs, so the L1 and
    L2 inputs the head is trained on are exactly the ones it will see live.
    """
    config = config or NetPulseConfig()
    rows: list[np.ndarray] = []
    labels: dict[int, list[float]] = {horizon: [] for horizon in HORIZONS}
    groups: list[str] = []

    for run in runs:
        # An untrained predictor: the head must not be an input to itself.
        scorer = Scorer(config, predictor=L3Predictor())
        for sample in run.samples:
            frame = scorer.pipeline.push(sample)
            hits_l1 = scorer.l1.observe(frame)
            hits_l2 = scorer.l2.observe(frame)
            if not scorer.l1.warm:
                continue
            rows.append(extract(frame, hits_l1, hits_l2))
            groups.append(run.name)
            for horizon in HORIZONS:
                labels[horizon].append(_label(run, sample.ts, horizon * 60.0))

    if not rows:
        raise RuntimeError("no frames collected; the corpus produced nothing to train on")
    return TrainingSet(
        X=np.vstack(rows),
        y={horizon: np.array(values, dtype=np.float64) for horizon, values in labels.items()},
        groups=groups,
    )


def _label(run: ScenarioRun, ts: float, horizon_s: float) -> float:
    """1 when user-visible degradation starts within the horizon, or is on."""
    for start, end in run.bad_windows:
        if start <= ts <= end:
            return 1.0
        if ts < start <= ts + horizon_s:
            return 1.0
    return 0.0


def train(
    *,
    train_seeds: tuple[int, ...] = TRAIN_SEEDS,
    holdout_seeds: tuple[int, ...] = HOLDOUT_SEEDS,
    config: NetPulseConfig | None = None,
    epochs: int = 600,
) -> tuple[L3Predictor, dict[str, Any]]:
    """Fit one model per horizon and evaluate it on held-out runs."""
    log.info("generating training corpus (%d seeds)", len(train_seeds))
    training_runs = [generate(spec, seed=seed) for spec in SCENARIOS for seed in train_seeds]
    training_runs += [
        generate_soak(hours=SOAK_HOURS_PER_SEED, seed=seed, start_hour=hour)
        for seed, hour in zip(train_seeds, (2.0, 8.0, 14.0, 18.0, 21.0), strict=False)
    ]
    holdout_runs = [generate(spec, seed=seed) for spec in SCENARIOS for seed in holdout_seeds]
    holdout_runs += [
        generate_soak(hours=SOAK_HOURS_PER_SEED, seed=seed, start_hour=hour)
        for seed, hour in zip(holdout_seeds, (5.0, 16.0), strict=False)
    ]

    training = collect(training_runs, config)
    holdout = collect(holdout_runs, config)
    log.info("collected %d training frames, %d holdout frames", len(training), len(holdout))

    models: dict[int, LogisticModel] = {}
    metrics: dict[str, Any] = {"train_frames": len(training), "holdout_frames": len(holdout)}

    for horizon in HORIZONS:
        y_train = training.y[horizon]
        y_holdout = holdout.y[horizon]
        model = train_logistic(training.X, y_train, horizon_min=horizon, epochs=epochs)
        scores = model.predict_batch(holdout.X)

        horizon_metrics = {
            "positive_rate_train": round(float(y_train.mean()), 4),
            "positive_rate_holdout": round(float(y_holdout.mean()), 4),
            "brier": round(_brier(scores, y_holdout), 4),
            "ece": round(_ece(scores, y_holdout), 4),
        }
        for threshold in (0.25, 0.5, 0.75):
            binary = evaluate_binary(scores, y_holdout, threshold)
            horizon_metrics[f"at_{threshold}"] = {
                key: round(value, 4) for key, value in binary.items()
            }
        model.metrics = {
            key: value for key, value in horizon_metrics.items() if isinstance(value, float)
        }
        models[horizon] = model
        metrics[f"h{horizon}"] = horizon_metrics
        log.info(
            "horizon %dm: brier %.4f, precision@0.5 %.3f, recall@0.5 %.3f",
            horizon,
            horizon_metrics["brier"],
            horizon_metrics["at_0.5"]["precision"],
            horizon_metrics["at_0.5"]["recall"],
        )

    return L3Predictor(models), metrics


def train_and_save(path: Path | None = None, **kwargs: Any) -> tuple[Path, dict[str, Any]]:
    """Train and write the bundle to the shipped default location."""
    from ..ml.l3_predict import default_model_path

    predictor, metrics = train(**kwargs)
    target = path or default_model_path()
    predictor.save(target)
    log.info("wrote %s", target)
    return target, metrics


def top_features(model: LogisticModel, count: int = 12) -> list[tuple[str, float]]:
    """Largest absolute coefficients, for the model card in the docs."""
    order = np.argsort(-np.abs(model.weights))[:count]
    return [(model.layout[int(i)], round(float(model.weights[int(i)]), 4)) for i in order]
