"""L3: the predictive head, "will this get bad in the next 5 or 15 minutes".

This is the layer the product is actually sold on. L1 and L2 answer "is this
unusual now"; L3 answers the question a user cares about before their call
starts. Two things follow from that, and both come straight out of
Outage-Watch (R7) and PreFix (R14):

* the target is a **rare, extreme** event, not an average wiggle. Degradation
  windows are a small fraction of any realistic corpus, so training weights
  the positive class rather than letting the model learn to always say no.
* the inputs are weighted toward **slopes and spreads**, not levels. A
  latency that is high and steady is a network the user has already adapted
  to; a latency that is climbing is the one that will break the call.

The model is a calibrated logistic regression, which PRD 15 explicitly
endorses over a transformer for the first release: it trains in a second,
runs in microseconds, serialises to a readable JSON file a reviewer can
inspect, and its coefficients are directly usable as attribution evidence. A
distilled TranAD-class model can replace it behind this same interface once
there is a labelled corpus from real installs to justify one.

When no trained model is present the predictor falls back to a transparent
heuristic, so a fresh install forecasts from its first warm minute instead of
waiting for a download.
"""

from __future__ import annotations

import itertools
import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

from ..features.pipeline import DERIVED_FEATURES, FeatureFrame
from ..features.schema import ATTRIBUTABLE_LAYERS
from ..logging_setup import get_logger
from .l1_stats import L1Result
from .l2_online import L2Result

log = get_logger(__name__)

HORIZONS: tuple[int, ...] = (5, 15)

#: Features whose slope and z-score carry the lookahead signal. Kept short on
#: purpose: a wide input layer on a small corpus overfits, and every entry
#: here has to be explainable to a user when it drives an alert.
TREND_FEATURES: tuple[str, ...] = (
    "gw_rtt_ms",
    "gw_jitter_ms",
    "gw_loss_rate",
    "dns_p50_ms",
    "dns_fail_rate",
    "https_ttfb_ms",
    "https_tls_ms",
    "https_fail_rate",
    "path_rtt_ms",
    "wifi_rssi_dbm",
    "wifi_tx_retry_rate",
    "os_retrans_rate",
)


def _layout() -> tuple[str, ...]:
    names: list[str] = ["l1_anomaly", "l2_anomaly", "coverage"]
    names += [f"l1_{layer}" for layer in ATTRIBUTABLE_LAYERS]
    names += [f"l2_{layer}" for layer in ATTRIBUTABLE_LAYERS]
    for feature in TREND_FEATURES:
        names.append(f"{feature}_5m_z")
        names.append(f"{feature}_5m_slope_n")
        names.append(f"{feature}_15m_z")
    names += list(DERIVED_FEATURES)
    names += ["vpn_active", "captive_portal", "on_battery", "is_wifi"]
    return tuple(names)


#: Fixed input layout. Persisted with the weights so a model file can never
#: be silently applied to a different feature order.
FEATURE_LAYOUT: tuple[str, ...] = _layout()
LAYOUT_INDEX: dict[str, int] = {name: i for i, name in enumerate(FEATURE_LAYOUT)}


def extract(frame: FeatureFrame, l1: L1Result, l2: L2Result) -> np.ndarray:
    """Build the fixed-length input vector for one frame."""
    vector = np.zeros(len(FEATURE_LAYOUT), dtype=np.float64)

    vector[LAYOUT_INDEX["l1_anomaly"]] = l1.anomaly
    vector[LAYOUT_INDEX["l2_anomaly"]] = l2.anomaly
    vector[LAYOUT_INDEX["coverage"]] = frame.coverage

    for layer in ATTRIBUTABLE_LAYERS:
        vector[LAYOUT_INDEX[f"l1_{layer}"]] = l1.per_layer.get(layer, 0.0)
        vector[LAYOUT_INDEX[f"l2_{layer}"]] = l2.per_bundle.get(layer, 0.0)

    for feature in TREND_FEATURES:
        z5 = frame.aggregates.get(f"{feature}_5m_z", 0.0)
        z15 = frame.aggregates.get(f"{feature}_15m_z", 0.0)
        slope = frame.aggregates.get(f"{feature}_5m_slope", 0.0)
        level = abs(frame.aggregates.get(f"{feature}_5m_mean", 0.0))
        # Slope is normalised by the level so it reads as "percent change per
        # minute", which is comparable across a 3 ms RTT and a 300 ms TTFB.
        vector[LAYOUT_INDEX[f"{feature}_5m_z"]] = _clip(z5)
        vector[LAYOUT_INDEX[f"{feature}_15m_z"]] = _clip(z15)
        vector[LAYOUT_INDEX[f"{feature}_5m_slope_n"]] = _clip(slope / max(level, 1e-3), 20.0)

    for name in DERIVED_FEATURES:
        vector[LAYOUT_INDEX[name]] = _clip(frame.derived.get(name, 0.0), 1000.0)

    vector[LAYOUT_INDEX["vpn_active"]] = frame.values.get("vpn_active", 0.0)
    vector[LAYOUT_INDEX["captive_portal"]] = frame.values.get("captive_portal", 0.0)
    vector[LAYOUT_INDEX["on_battery"]] = frame.values.get("os_on_battery", 0.0)
    vector[LAYOUT_INDEX["is_wifi"]] = 1.0 if frame.values.get("os_iface_type", 2.0) == 1.0 else 0.0
    return vector


def _clip(value: float, limit: float = 12.0) -> float:
    if value != value or value in (float("inf"), float("-inf")):
        return 0.0
    return max(-limit, min(limit, value))


def sigmoid(x: np.ndarray | float) -> Any:
    return 1.0 / (1.0 + np.exp(-np.clip(x, -30.0, 30.0)))


@dataclass
class LogisticModel:
    """Standardised logistic regression with a readable serialisation."""

    weights: np.ndarray
    bias: float
    mean: np.ndarray
    scale: np.ndarray
    layout: tuple[str, ...] = FEATURE_LAYOUT
    horizon_min: int = 5
    metrics: dict[str, float] | None = None

    def predict(self, x: np.ndarray) -> float:
        standardised = (x - self.mean) / self.scale
        return float(sigmoid(float(standardised @ self.weights) + self.bias))

    def predict_batch(self, X: np.ndarray) -> np.ndarray:
        standardised = (X - self.mean) / self.scale
        return np.asarray(sigmoid(standardised @ self.weights + self.bias))

    def contributions(self, x: np.ndarray, top: int = 5) -> list[tuple[str, float]]:
        """Signed per-feature contribution to the logit, largest first.

        This is what turns a probability into evidence a person can read.
        """
        standardised = (x - self.mean) / self.scale
        products = standardised * self.weights
        order = np.argsort(-np.abs(products))[:top]
        return [(self.layout[int(i)], float(products[int(i)])) for i in order]

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": 1,
            "kind": "logistic",
            "horizon_min": self.horizon_min,
            "layout": list(self.layout),
            "weights": [round(float(w), 6) for w in self.weights],
            "bias": round(float(self.bias), 6),
            "mean": [round(float(v), 6) for v in self.mean],
            "scale": [round(float(v), 6) for v in self.scale],
            "metrics": self.metrics or {},
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> LogisticModel:
        layout = tuple(data.get("layout", FEATURE_LAYOUT))
        if layout != FEATURE_LAYOUT:
            raise ValueError(
                "model was trained on a different feature layout; retrain with "
                "'netpulse train' rather than using a mismatched file"
            )
        return cls(
            weights=np.array(data["weights"], dtype=np.float64),
            bias=float(data["bias"]),
            mean=np.array(data["mean"], dtype=np.float64),
            scale=np.array(data["scale"], dtype=np.float64),
            layout=layout,
            horizon_min=int(data.get("horizon_min", 5)),
            metrics=data.get("metrics") or {},
        )


def train_logistic(
    X: np.ndarray,
    y: np.ndarray,
    *,
    horizon_min: int,
    epochs: int = 400,
    learning_rate: float = 0.25,
    l2: float = 1e-3,
    positive_weight: float | None = None,
) -> LogisticModel:
    """Fit a class-weighted logistic regression by full-batch gradient descent.

    ``positive_weight`` defaults to the inverse class ratio, which is the
    extreme-event emphasis Outage-Watch argues for: without it a corpus that
    is 85 percent healthy trains a model whose best strategy is to say
    "fine" forever.
    """
    if X.ndim != 2 or X.shape[0] != y.shape[0]:
        raise ValueError("X and y shapes do not match")

    mean = X.mean(axis=0)
    scale = X.std(axis=0)
    scale = np.where(scale < 1e-6, 1.0, scale)
    Z = (X - mean) / scale

    positives = float(y.sum())
    negatives = float(len(y) - positives)
    if positive_weight is None:
        positive_weight = (negatives / positives) if positives > 0 else 1.0
    sample_weight = np.where(y > 0.5, positive_weight, 1.0)
    sample_weight = sample_weight / sample_weight.mean()

    weights = np.zeros(Z.shape[1], dtype=np.float64)
    bias = 0.0
    for _ in range(epochs):
        predictions = np.asarray(sigmoid(Z @ weights + bias))
        error = (predictions - y) * sample_weight
        grad_w = Z.T @ error / len(y) + l2 * weights
        grad_b = float(error.mean())
        weights -= learning_rate * grad_w
        bias -= learning_rate * grad_b

    # Prior correction. Re-weighting the positive class is what makes the
    # model discriminate a rare event at all, but it also trains the model on
    # a fictional base rate, so its probabilities come out systematically
    # high. For a re-weighted logistic regression the correction is exact:
    # subtract the log of the weight ratio from the bias. Without this the
    # head is a worse forecaster than the heuristic it replaces, firing on
    # quiet networks while reporting confident probabilities.
    if positive_weight > 0:
        bias -= math.log(positive_weight)

    return LogisticModel(
        weights=weights,
        bias=bias,
        mean=mean,
        scale=scale,
        horizon_min=horizon_min,
    )


def heuristic_risk(l1: L1Result, l2: L2Result, frame: FeatureFrame, horizon_min: int) -> float:
    """Transparent fallback used until a trained model is available.

    Deliberately simple and deliberately documented: a user asking "why did
    it warn me" on a fresh install deserves an answer that is not "the model
    said so".
    """
    base = 0.6 * l1.anomaly + 0.4 * l2.anomaly
    trend = 0.0
    for feature in TREND_FEATURES:
        level = abs(frame.aggregates.get(f"{feature}_5m_mean", 0.0))
        slope = frame.aggregates.get(f"{feature}_5m_slope", 0.0)
        if level > 1e-6:
            trend = max(trend, slope / level)
    # A 10 percent per minute climb is a strong lookahead signal.
    trend_term = max(0.0, min(1.0, trend / 0.10))
    horizon_gain = 1.0 if horizon_min >= 15 else 0.85
    risk = (0.7 * base + 0.3 * trend_term) * horizon_gain
    return float(max(0.0, min(1.0, risk)))


@dataclass(slots=True)
class L3Result:
    risk_5m: float
    risk_15m: float
    source: str
    contributions: list[tuple[str, float]]

    def to_dict(self) -> dict[str, Any]:
        return {
            "risk_5m": round(self.risk_5m, 4),
            "risk_15m": round(self.risk_15m, 4),
            "source": self.source,
            "contributions": [(name, round(value, 4)) for name, value in self.contributions],
        }


class L3Predictor:
    """Holds one model per horizon and falls back when none is loaded."""

    def __init__(self, models: dict[int, LogisticModel] | None = None) -> None:
        self.models: dict[int, LogisticModel] = models or {}

    @property
    def trained(self) -> bool:
        return bool(self.models)

    def predict(self, frame: FeatureFrame, l1: L1Result, l2: L2Result) -> L3Result:
        if not self.models:
            return L3Result(
                risk_5m=heuristic_risk(l1, l2, frame, 5),
                risk_15m=heuristic_risk(l1, l2, frame, 15),
                source="heuristic",
                contributions=[],
            )
        vector = extract(frame, l1, l2)
        risks: dict[int, float] = {}
        contributions: list[tuple[str, float]] = []
        for horizon in HORIZONS:
            model = self.models.get(horizon)
            if model is None:
                risks[horizon] = heuristic_risk(l1, l2, frame, horizon)
                continue
            risks[horizon] = model.predict(vector)
            if horizon == 15 or not contributions:
                contributions = model.contributions(vector)
        return L3Result(
            risk_5m=risks.get(5, 0.0),
            risk_15m=risks.get(15, 0.0),
            source="model",
            contributions=contributions,
        )

    # ------------------------------------------------------------ loading

    @classmethod
    def load(cls, path: Path | None = None) -> L3Predictor:
        """Load a bundle, preferring an explicit path then the shipped default."""
        candidates = [path] if path else []
        candidates.append(default_model_path())
        for candidate in candidates:
            if candidate is None or not candidate.exists():
                continue
            try:
                return cls.from_bundle(json.loads(candidate.read_text(encoding="utf-8")))
            except (OSError, ValueError, KeyError) as exc:
                log.warning("could not load L3 model from %s: %s", candidate, exc)
        return cls()

    @classmethod
    def from_bundle(cls, data: dict[str, Any]) -> L3Predictor:
        models: dict[int, LogisticModel] = {}
        for key, payload in data.get("models", {}).items():
            models[int(key)] = LogisticModel.from_dict(payload)
        return cls(models)

    def to_bundle(self) -> dict[str, Any]:
        return {
            "version": 1,
            "layout_hash": layout_hash(),
            "models": {str(horizon): model.to_dict() for horizon, model in self.models.items()},
        }

    def save(self, path: Path) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.to_bundle(), indent=1), encoding="utf-8")
        return path


def default_model_path() -> Path:
    return Path(__file__).with_name("weights") / "l3_default.json"


def layout_hash() -> str:
    """Short digest of the feature layout, stored alongside trained weights."""
    from hashlib import blake2s

    joined = "|".join(FEATURE_LAYOUT).encode("utf-8")
    return blake2s(joined, digest_size=6).hexdigest()


def label_windows(
    timestamps: list[float], bad_windows: list[tuple[float, float]], horizon_min: int
) -> np.ndarray:
    """Label each timestamp with "degradation begins within the horizon".

    A frame already inside a bad window is excluded from the positive class
    by the caller; what the head is asked to learn is the approach, not the
    arrival.
    """
    horizon_s = horizon_min * 60.0
    labels = np.zeros(len(timestamps), dtype=np.float64)
    for index, ts in enumerate(timestamps):
        for start, end in bad_windows:
            if ts < start <= ts + horizon_s:
                labels[index] = 1.0
                break
            if start <= ts <= end:
                labels[index] = 1.0
                break
    return labels


def evaluate_binary(scores: np.ndarray, labels: np.ndarray, threshold: float) -> dict[str, float]:
    """Precision, recall and alert rate at one threshold."""
    predicted = scores >= threshold
    positives = labels > 0.5
    true_positive = float(np.sum(predicted & positives))
    false_positive = float(np.sum(predicted & ~positives))
    false_negative = float(np.sum(~predicted & positives))
    precision = true_positive / (true_positive + false_positive) if predicted.any() else 0.0
    recall = true_positive / (true_positive + false_negative) if positives.any() else 0.0
    f1 = 2 * precision * recall / (precision + recall) if (precision + recall) > 0 else 0.0
    return {
        "precision": precision,
        "recall": recall,
        "f1": f1,
        "alert_fraction": float(np.mean(predicted)),
    }


def brier_score(scores: np.ndarray, labels: np.ndarray) -> float:
    """Mean squared error of the probabilities, the calibration headline."""
    if len(scores) == 0:
        return 0.0
    return float(np.mean((scores - labels) ** 2))


def reliability(scores: np.ndarray, labels: np.ndarray, bins: int = 10) -> list[dict[str, float]]:
    """Reliability diagram data: predicted versus observed frequency."""
    out: list[dict[str, float]] = []
    edges = np.linspace(0.0, 1.0, bins + 1)
    for low, high in itertools.pairwise(edges):
        mask = (scores >= low) & (scores < high if high < 1.0 else scores <= 1.0)
        count = int(mask.sum())
        if count == 0:
            continue
        out.append(
            {
                "bin_low": float(low),
                "bin_high": float(high),
                "count": count,
                "predicted": float(scores[mask].mean()),
                "observed": float(labels[mask].mean()),
            }
        )
    return out


def expected_calibration_error(scores: np.ndarray, labels: np.ndarray, bins: int = 10) -> float:
    rows = reliability(scores, labels, bins)
    total = sum(row["count"] for row in rows)
    if not total:
        return 0.0
    return float(
        sum(row["count"] * abs(row["predicted"] - row["observed"]) for row in rows) / total
    )


def band_for(risk: float, bands: dict[str, float]) -> str:
    """Map a probability onto the traffic-light band shown in the UI."""
    if risk >= bands.get("critical", 0.75):
        return "critical"
    if risk >= bands.get("risk", 0.5):
        return "risk"
    if risk >= bands.get("watch", 0.25):
        return "watch"
    return "info"


def clamp_probability(value: float) -> float:
    if math.isnan(value):
        return 0.0
    return max(0.0, min(1.0, value))
