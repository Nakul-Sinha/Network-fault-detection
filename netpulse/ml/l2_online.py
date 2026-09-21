"""L2: an online ensemble of small autoencoders.

This is the Kitsune idea (R1) moved from intrusion detection to health: keep
one tiny autoencoder per feature bundle, train it continuously on whatever
the network is currently doing, and treat reconstruction error as "this does
not look like normal for this machine". Nothing is labelled, and nothing
needs to be.

Two departures from the paper, both for product reasons:

* the bundles are the **layers**, not a learned feature clustering. A
  learned clustering would score marginally better, but per-layer bundles
  mean the ensemble's internals are directly usable as attribution evidence:
  "the Wi-Fi bundle is what stopped reconstructing" is a sentence L4 can turn
  into an explanation, and an arbitrary cluster index is not.
* error is normalised against the model's own recent error distribution, so
  the score means "unusual compared with how well I usually reconstruct",
  which is stable across machines with very different absolute latencies.

Sizing is deliberately small: a bundle of six features trains a 6-4-6
network. The whole ensemble is a few hundred floats, trains in microseconds
per sample, and serialises to a small JSON blob.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from ..features.pipeline import FeatureFrame
from ..features.schema import LAYER_FEATURES, MODELLED_FEATURES, Layer

#: SGD steps an autoencoder takes before its reconstruction error means
#: anything. Error statistics gathered during this phase describe the model
#: converging, not the network behaving, so they are discarded. At the
#: default learning rate the error curve is flat by this point.
CONVERGENCE_SAMPLES = 120
#: Further samples used to characterise the settled error distribution before
#: the model is allowed to call anything anomalous.
CALIBRATION_SAMPLES = 90
#: Floor on the running error scale, so a perfectly reconstructable bundle
#: does not divide a tiny error by a tinier scale.
ERROR_FLOOR = 1e-3
#: Score above which a warm model stops learning. Without this an online
#: autoencoder quietly absorbs a sustained fault as the new normal within a
#: few samples, and then reports that everything is fine while the user
#: watches their call fall apart.
FREEZE_SCORE = 0.2
#: Upper bound on consecutive frozen steps. A network really can change for
#: good (a new router, a new ISP), so after about an hour of continuous
#: disagreement the model accepts the new regime rather than protesting
#: forever. The drift monitor is the deliberate path for the same outcome.
MAX_FROZEN_STEPS = 240


def _sigmoid(x: np.ndarray) -> np.ndarray:
    return 1.0 / (1.0 + np.exp(-np.clip(x, -30.0, 30.0)))


@dataclass
class Normaliser:
    """Running min-max scaling into [0, 1].

    Autoencoders on raw network features are hopeless: RTT is single digits
    and TTFB is hundreds, so the loss is dominated by whichever feature
    happens to have the largest units.
    """

    size: int
    lo: np.ndarray = field(default=None)  # type: ignore[assignment]
    hi: np.ndarray = field(default=None)  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.lo is None:
            self.lo = np.full(self.size, np.inf, dtype=np.float64)
        if self.hi is None:
            self.hi = np.full(self.size, -np.inf, dtype=np.float64)

    def observe(self, x: np.ndarray) -> None:
        self.lo = np.minimum(self.lo, x)
        self.hi = np.maximum(self.hi, x)

    def apply(self, x: np.ndarray) -> np.ndarray:
        span = self.hi - self.lo
        span = np.where(np.isfinite(span) & (span > 1e-9), span, 1.0)
        lo = np.where(np.isfinite(self.lo), self.lo, 0.0)
        return np.clip((x - lo) / span, 0.0, 1.0)

    def to_dict(self) -> dict[str, Any]:
        return {
            "size": self.size,
            "lo": [None if not math.isfinite(v) else float(v) for v in self.lo],
            "hi": [None if not math.isfinite(v) else float(v) for v in self.hi],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Normaliser:
        size = int(data["size"])
        lo = np.array([np.inf if v is None else float(v) for v in data["lo"]], dtype=np.float64)
        hi = np.array([-np.inf if v is None else float(v) for v in data["hi"]], dtype=np.float64)
        return cls(size=size, lo=lo, hi=hi)


class OnlineAutoencoder:
    """A single hidden layer autoencoder trained by online SGD."""

    def __init__(
        self, size: int, *, hidden_ratio: float = 0.6, learning_rate: float = 0.4, seed: int = 0
    ) -> None:
        self.size = size
        self.hidden = max(1, min(size, math.ceil(size * hidden_ratio)))
        self.learning_rate = learning_rate
        rng = np.random.default_rng(seed)
        limit = math.sqrt(6.0 / (size + self.hidden))
        self.w_encode = rng.uniform(-limit, limit, (size, self.hidden))
        self.b_encode = np.zeros(self.hidden)
        self.w_decode = rng.uniform(-limit, limit, (self.hidden, size))
        self.b_decode = np.zeros(size)
        self.normaliser = Normaliser(size)
        self.trained = 0
        self.frozen_steps = 0
        # Running mean and variance of reconstruction error, used to turn a
        # raw RMSE into a comparable score.
        self.error_mean = 0.0
        self.error_var = 0.0

    @property
    def warm(self) -> bool:
        return self.trained >= CONVERGENCE_SAMPLES + CALIBRATION_SAMPLES

    def _forward(self, x: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        hidden = _sigmoid(x @ self.w_encode + self.b_encode)
        output = _sigmoid(hidden @ self.w_decode + self.b_decode)
        return hidden, output

    def score(self, raw: np.ndarray) -> float:
        """Reconstruction RMSE for one observation, without learning."""
        x = self.normaliser.apply(raw)
        _hidden, output = self._forward(x)
        return float(np.sqrt(np.mean((output - x) ** 2)))

    def observe(self, raw: np.ndarray) -> float:
        """Score an observation, then learn from it unless it looks wrong.

        Scoring happens against the pre-update model, so a frame is always
        judged by what the model believed before it saw the frame.
        """
        if not self.warm:
            # During warm-up everything is treated as normal, which is what
            # lets the scale and the weights settle at all.
            self.normaliser.observe(raw)
        x = self.normaliser.apply(raw)
        hidden, output = self._forward(x)
        error = output - x
        rmse = float(np.sqrt(np.mean(error**2)))

        if self._should_freeze(rmse):
            self.frozen_steps += 1
            return rmse
        self.frozen_steps = 0
        if self.warm:
            self.normaliser.observe(raw)

        # Backprop through two sigmoid layers.
        delta_out = error * output * (1.0 - output)
        grad_w_decode = np.outer(hidden, delta_out)
        delta_hidden = (delta_out @ self.w_decode.T) * hidden * (1.0 - hidden)
        grad_w_encode = np.outer(x, delta_hidden)

        self.w_decode -= self.learning_rate * grad_w_decode
        self.b_decode -= self.learning_rate * delta_out
        self.w_encode -= self.learning_rate * grad_w_encode
        self.b_encode -= self.learning_rate * delta_hidden

        self.trained += 1
        self._update_error_stats(rmse)
        return rmse

    def _should_freeze(self, rmse: float) -> bool:
        if not self.warm or self.frozen_steps >= MAX_FROZEN_STEPS:
            return False
        return self.normalised_error(rmse) >= FREEZE_SCORE

    def _update_error_stats(self, rmse: float) -> None:
        """Track the settled error distribution, ignoring the training ramp.

        Folding the convergence transient into these statistics was the bug
        that made the whole ensemble silent: a large early error inflated the
        mean and variance so far that no real fault could ever exceed the
        threshold derived from them.
        """
        if self.trained <= CONVERGENCE_SAMPLES:
            return
        settled = self.trained - CONVERGENCE_SAMPLES
        alpha = max(0.01, min(0.2, 1.0 / settled))
        previous = self.error_mean
        self.error_mean += alpha * (rmse - previous)
        self.error_var = (1.0 - alpha) * (self.error_var + alpha * (rmse - previous) ** 2)

    def error_scale(self) -> float:
        """The reconstruction error this model considers unremarkable."""
        spread = math.sqrt(max(0.0, self.error_var))
        return max(ERROR_FLOOR, self.error_mean + 3.0 * spread, self.error_mean * 1.5)

    def normalised_error(self, rmse: float) -> float:
        """Map an RMSE onto [0, 1] against this model's own error history.

        The score is the ratio to the model's usual ceiling rather than a log
        of it: three times the usual error saturates. Expressing it as a
        ratio keeps the number comparable between a bundle that reconstructs
        almost perfectly and one with an irreducible noise floor.
        """
        if not self.warm:
            return 0.0
        scale = self.error_scale()
        if rmse <= scale:
            return 0.0
        return float(min(1.0, (rmse / scale - 1.0) / 2.0))

    def to_dict(self) -> dict[str, Any]:
        return {
            "size": self.size,
            "hidden": self.hidden,
            "learning_rate": self.learning_rate,
            "w_encode": self.w_encode.tolist(),
            "b_encode": self.b_encode.tolist(),
            "w_decode": self.w_decode.tolist(),
            "b_decode": self.b_decode.tolist(),
            "normaliser": self.normaliser.to_dict(),
            "trained": self.trained,
            "frozen_steps": self.frozen_steps,
            "error_mean": self.error_mean,
            "error_var": self.error_var,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> OnlineAutoencoder:
        model = cls(int(data["size"]), learning_rate=float(data.get("learning_rate", 0.4)))
        model.hidden = int(data["hidden"])
        model.w_encode = np.array(data["w_encode"], dtype=np.float64)
        model.b_encode = np.array(data["b_encode"], dtype=np.float64)
        model.w_decode = np.array(data["w_decode"], dtype=np.float64)
        model.b_decode = np.array(data["b_decode"], dtype=np.float64)
        model.normaliser = Normaliser.from_dict(data["normaliser"])
        model.trained = int(data.get("trained", 0))
        model.frozen_steps = int(data.get("frozen_steps", 0))
        model.error_mean = float(data.get("error_mean", 0.0))
        model.error_var = float(data.get("error_var", 0.0))
        return model


@dataclass(slots=True)
class L2Result:
    anomaly: float
    per_bundle: dict[str, float]
    raw_errors: dict[str, float]
    warm: bool

    def top_bundle(self) -> str | None:
        if not self.per_bundle:
            return None
        best = max(self.per_bundle.items(), key=lambda item: item[1])
        return best[0] if best[1] > 0.0 else None


class L2Ensemble:
    """One autoencoder per layer bundle, plus an output model over them."""

    def __init__(self, *, hidden_ratio: float = 0.6, learning_rate: float = 0.4) -> None:
        self.bundles: dict[str, tuple[str, ...]] = {}
        self.models: dict[str, OnlineAutoencoder] = {}
        for index, (layer, names) in enumerate(sorted(LAYER_FEATURES.items())):
            modelled = tuple(name for name in names if name in MODELLED_FEATURES)
            if len(modelled) < 2:
                continue
            self.bundles[layer] = modelled
            self.models[layer] = OnlineAutoencoder(
                len(modelled),
                hidden_ratio=hidden_ratio,
                learning_rate=learning_rate,
                seed=index + 1,
            )
        self.output = OnlineAutoencoder(
            max(2, len(self.models)),
            hidden_ratio=hidden_ratio,
            learning_rate=learning_rate,
            seed=99,
        )
        self.samples_seen = 0

    @property
    def warm(self) -> bool:
        return self.output.warm

    def _bundle_vector(self, frame: FeatureFrame, names: tuple[str, ...]) -> np.ndarray | None:
        """Vector for one bundle, or None when the layer is not measurable.

        A bundle is skipped entirely rather than zero-filled: feeding zeros
        for an absent Wi-Fi radio would teach the model that "no Wi-Fi" is
        normal and then flag the moment a radio appears.
        """
        values = [frame.values.get(name) for name in names]
        if any(value is None for value in values):
            return None
        return np.array(values, dtype=np.float64)

    def observe(self, frame: FeatureFrame, *, learn: bool = True) -> L2Result:
        self.samples_seen += 1
        raw_errors: dict[str, float] = {}
        per_bundle: dict[str, float] = {}
        bundle_scores: list[float] = []
        relative: list[float] = []

        for layer in sorted(self.models):
            vector = self._bundle_vector(frame, self.bundles[layer])
            model = self.models[layer]
            if vector is None:
                bundle_scores.append(0.0)
                relative.append(1.0)
                continue
            rmse = model.observe(vector) if learn else model.score(vector)
            raw_errors[layer] = rmse
            normalised = model.normalised_error(rmse)
            if normalised > 0.0:
                per_bundle[layer] = normalised
            bundle_scores.append(normalised)
            # The output model sees the *ratio* of each bundle's error to its
            # own usual error, not the thresholded score. Thresholded scores
            # are zero almost always, which would leave the output model
            # nothing to learn the normal joint behaviour from.
            relative.append(min(5.0, rmse / model.error_scale()))

        output_vector = np.array(
            relative + [0.0] * (self.output.size - len(relative)), dtype=np.float64
        )[: self.output.size]
        output_rmse = (
            self.output.observe(output_vector) if learn else self.output.score(output_vector)
        )
        combined = self.output.normalised_error(output_rmse)
        # The output model catches unusual *combinations*; the strongest
        # single bundle still matters on its own, so take the stronger signal.
        anomaly = max(combined, max(bundle_scores) if bundle_scores else 0.0)

        return L2Result(
            anomaly=float(min(1.0, anomaly)),
            per_bundle=per_bundle,
            raw_errors=raw_errors,
            warm=self.warm,
        )

    def reset(self, layer: Layer | None = None) -> None:
        """Rebuild one bundle, or all of them, after a drift event."""
        targets = [layer] if layer is not None else list(self.models)
        for name in targets:
            if name in self.models:
                previous = self.models[name]
                self.models[name] = OnlineAutoencoder(
                    previous.size, learning_rate=previous.learning_rate
                )

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": 1,
            "samples_seen": self.samples_seen,
            "bundles": {layer: list(names) for layer, names in self.bundles.items()},
            "models": {layer: model.to_dict() for layer, model in self.models.items()},
            "output": self.output.to_dict(),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> L2Ensemble:
        instance = cls()
        instance.samples_seen = int(data.get("samples_seen", 0))
        for layer, payload in data.get("models", {}).items():
            if layer in instance.models:
                instance.models[layer] = OnlineAutoencoder.from_dict(payload)
        if "output" in data:
            instance.output = OnlineAutoencoder.from_dict(data["output"])
        return instance
