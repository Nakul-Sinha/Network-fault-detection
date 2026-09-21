"""Feature pipeline and the model ladder.

The pipeline tests check arithmetic against a plain reference implementation,
because the single-pass window was written for speed and a fast wrong answer
is worse than a slow right one.
"""

from __future__ import annotations

import json
import math
import random
import statistics

import pytest

from netpulse.eval.scenarios import SCENARIOS_BY_NAME, generate
from netpulse.features.pipeline import (
    DERIVED_FEATURES,
    FeaturePipeline,
    RollingWindow,
    derive_cross_layer,
)
from netpulse.ml import l0_rules, l4_rca
from netpulse.ml.drift import DriftMonitor, population_stability_index
from netpulse.ml.l1_stats import L1Baselines, combine
from netpulse.ml.l2_online import L2Ensemble, OnlineAutoencoder
from netpulse.ml.l3_predict import (
    FEATURE_LAYOUT,
    L3Predictor,
    LogisticModel,
    band_for,
    extract,
    heuristic_risk,
    train_logistic,
)
from netpulse.ml.scorer import Scorer
from netpulse.rca import remediation, templates
from netpulse.store.models import Sample

from .conftest import healthy_stream, make_sample

# ------------------------------------------------------------- rolling window


def test_window_statistics_match_a_plain_reference():
    window = RollingWindow()
    rng = random.Random(3)
    timestamps = [1000.0 + index * 15 for index in range(80)]
    values = [rng.gauss(50, 5) for _ in timestamps]
    for ts, value in zip(timestamps, values, strict=True):
        window.push(ts, value)

    out: dict[str, float] = {}
    window.summarise("f", timestamps[-1], values[-1], out)

    for seconds, label in ((60, "1m"), (300, "5m"), (900, "15m")):
        expected = [
            value
            for ts, value in zip(timestamps, values, strict=True)
            if ts >= timestamps[-1] - seconds
        ]
        assert out[f"f_{label}_mean"] == pytest.approx(statistics.fmean(expected))
        if len(expected) > 1:
            assert out[f"f_{label}_std"] == pytest.approx(statistics.stdev(expected), rel=1e-6)


def test_window_slope_detects_a_rise():
    window = RollingWindow()
    for index in range(40):
        window.push(1000.0 + index * 15, 10.0 + index)
    out: dict[str, float] = {}
    window.summarise("f", 1000.0 + 39 * 15, 49.0, out)
    # One unit per sample at 15 second spacing is four per minute.
    assert out["f_5m_slope"] == pytest.approx(4.0, rel=0.05)


def test_window_slope_is_zero_when_flat():
    window = RollingWindow()
    for index in range(40):
        window.push(1000.0 + index * 15, 7.0)
    out: dict[str, float] = {}
    window.summarise("f", 1000.0 + 39 * 15, 7.0, out)
    assert out["f_5m_slope"] == pytest.approx(0.0, abs=1e-9)
    assert out["f_5m_z"] == pytest.approx(0.0, abs=1e-6)


def test_window_prunes_by_age_and_compacts():
    window = RollingWindow(max_age_s=100)
    for index in range(500):
        window.push(1000.0 + index, 1.0)
    assert len(window) <= 101
    assert window._head < RollingWindow.COMPACT_AFTER


def test_z_score_has_a_spread_floor():
    """A perfectly flat feature must not turn a wobble into a huge z."""
    window = RollingWindow()
    for index in range(40):
        window.push(1000.0 + index * 15, 100.0)
    out: dict[str, float] = {}
    window.summarise("f", 1000.0 + 39 * 15, 101.0, out)
    assert abs(out["f_5m_z"]) < 1.0


# ---------------------------------------------------------------- pipeline


def test_carry_forward_fills_gaps_between_slow_collectors():
    pipeline = FeaturePipeline()
    pipeline.push(make_sample(ts=1000.0, path_rtt_ms=14.0, gw_rtt_ms=3.0))
    frame = pipeline.push(make_sample(ts=1015.0, gw_rtt_ms=3.1))
    assert frame.values["path_rtt_ms"] == 14.0, "the slow collector value carries forward"
    assert frame.fresh == {"gw_rtt_ms"}


def test_stale_values_are_dropped_not_carried():
    pipeline = FeaturePipeline()
    pipeline.push(make_sample(ts=1000.0, wifi_rssi_dbm=-50.0, path_rtt_ms=14.0))
    frame = pipeline.push(make_sample(ts=1000.0 + 3000.0, gw_rtt_ms=3.0))
    assert "wifi_rssi_dbm" not in frame.values, "a 50 minute old RSSI is worse than none"
    assert "path_rtt_ms" not in frame.values


def test_coverage_reflects_what_is_measurable():
    pipeline = FeaturePipeline()
    frame = pipeline.push(make_sample(ts=1000.0, gw_rtt_ms=3.0))
    assert 0.0 < frame.coverage < 0.2
    assert frame.layer_coverage["gateway"] > 0
    assert frame.layer_coverage["wifi"] == 0.0

    full = pipeline.push(healthy_stream(1)[0])
    assert full.coverage > 0.8


def test_derived_features_isolate_the_slow_leg():
    """The point of the cross-layer ratios: which leg added the latency."""
    remote = derive_cross_layer({"gw_rtt_ms": 3.0, "path_rtt_ms": 14.0, "https_ttfb_ms": 600.0})
    assert remote["x_remote_minus_path_ms"] > 500
    assert remote["x_path_minus_gateway_ms"] < 15

    isp = derive_cross_layer({"gw_rtt_ms": 3.0, "path_rtt_ms": 300.0, "https_ttfb_ms": 340.0})
    assert isp["x_path_minus_gateway_ms"] > 250
    assert isp["x_remote_minus_path_ms"] < 60


def test_derived_wifi_weakness_scales_with_signal():
    strong = derive_cross_layer({"wifi_rssi_dbm": -40.0})["x_wifi_weakness"]
    weak = derive_cross_layer({"wifi_rssi_dbm": -90.0})["x_wifi_weakness"]
    assert strong == 0.0
    assert weak == 1.0


def test_derived_feature_names_are_declared():
    produced = set(
        derive_cross_layer(
            {
                "gw_rtt_ms": 3.0,
                "path_rtt_ms": 14.0,
                "https_ttfb_ms": 60.0,
                "dns_p50_ms": 20.0,
                "https_tcp_ms": 25.0,
                "https_tls_ms": 30.0,
                "wifi_rssi_dbm": -50.0,
                "wifi_tx_retry_rate": 0.05,
                "wifi_link_mbps": 300.0,
                "gw_loss_rate": 0.0,
                "dns_fail_rate": 0.0,
                "https_fail_rate": 0.0,
            }
        )
    )
    assert produced <= set(DERIVED_FEATURES)
    assert len(produced) >= 9


def test_pipeline_reset_clears_state():
    pipeline = FeaturePipeline()
    for sample in healthy_stream(20):
        pipeline.push(sample)
    pipeline.reset()
    assert pipeline.samples_seen == 0
    frame = pipeline.push(make_sample(ts=9999.0, gw_rtt_ms=3.0))
    assert "wifi_rssi_dbm" not in frame.values


# ----------------------------------------------------------------- L0 rules


def test_gateway_unreachable_is_critical_and_suppresses_downstream():
    frame = FeaturePipeline().push(
        make_sample(ts=1.0, gw_loss_rate=1.0, dns_fail_rate=1.0, https_fail_rate=1.0)
    )
    hits = l0_rules.evaluate(frame)
    ids = {hit.rule_id for hit in hits}
    assert "l0.gateway_unreachable" in ids
    suppressed = l0_rules.suppressed_layers(hits)
    assert {"path", "remote_https"} <= suppressed
    assert l0_rules.risk_floor(hits) >= 0.9


def test_captive_portal_suppresses_every_other_layer():
    frame = FeaturePipeline().push(make_sample(ts=1.0, captive_portal=1.0, https_fail_rate=1.0))
    hits = l0_rules.evaluate(frame)
    assert "l0.captive_portal" in {hit.rule_id for hit in hits}
    assert "path" in l0_rules.suppressed_layers(hits)
    assert "remote_https" in l0_rules.suppressed_layers(hits)


def test_remote_is_not_blamed_while_dns_is_failing():
    """Failing HTTPS is a symptom of failing DNS, not a separate cause."""
    frame = FeaturePipeline().push(
        make_sample(ts=1.0, https_fail_rate=1.0, dns_fail_rate=1.0, gw_loss_rate=0.0)
    )
    ids = {hit.rule_id for hit in l0_rules.evaluate(frame)}
    assert "l0.remote_unreachable" not in ids
    assert "l0.dns_blackhole" in ids


def test_remote_is_blamed_when_everything_local_is_healthy():
    frame = FeaturePipeline().push(
        make_sample(ts=1.0, https_fail_rate=1.0, dns_fail_rate=0.0, gw_loss_rate=0.0)
    )
    assert "l0.remote_unreachable" in {hit.rule_id for hit in l0_rules.evaluate(frame)}


def test_retransmissions_follow_the_route_onto_the_vpn():
    on_vpn = FeaturePipeline().push(make_sample(ts=1.0, os_retrans_rate=0.2, vpn_active=1.0))
    off_vpn = FeaturePipeline().push(make_sample(ts=1.0, os_retrans_rate=0.2, vpn_active=0.0))
    assert [
        h.layer for h in l0_rules.evaluate(on_vpn) if h.rule_id.endswith("retransmissions")
    ] == ["vpn"]
    assert [
        h.layer for h in l0_rules.evaluate(off_vpn) if h.rule_id.endswith("retransmissions")
    ] == ["os"]


def test_healthy_frame_fires_no_rules():
    frame = FeaturePipeline().push(healthy_stream(1)[0])
    assert l0_rules.evaluate(frame) == []


def test_severity_ordering():
    assert l0_rules.severity_rank("critical") > l0_rules.severity_rank("risk")
    assert l0_rules.max_severity("info", "risk", "watch") == "risk"


# ------------------------------------------------------------------ L1 stats


def test_l1_stays_quiet_on_a_healthy_stream():
    pipeline, l1 = FeaturePipeline(), L1Baselines(warmup_samples=100)
    peak = 0.0
    for sample in healthy_stream(400):
        result = l1.observe(pipeline.push(sample))
        if l1.warm:
            peak = max(peak, result.anomaly)
    assert peak < 0.5, f"a boring network should not alarm L1 (peak {peak:.2f})"


def test_l1_detects_a_sustained_fault_and_names_the_layer():
    pipeline, l1 = FeaturePipeline(), L1Baselines(warmup_samples=100)
    for sample in healthy_stream(300):
        l1.observe(pipeline.push(sample))
    result = None
    for index in range(20):
        bad = healthy_stream(1, start=1_780_000_000.0 + (300 + index) * 15)[0]
        bad.set("dns_p50_ms", 900.0)
        bad.set("dns_p95_ms", 1500.0)
        result = l1.observe(pipeline.push(bad))
    assert result is not None
    assert result.anomaly > 0.5
    assert max(result.per_layer, key=result.per_layer.get) == "dns"


def test_l1_tolerates_a_gradual_diurnal_rise():
    """The spread floor exists so evening congestion is not an incident."""
    pipeline, l1 = FeaturePipeline(), L1Baselines(warmup_samples=100)
    base = healthy_stream(500)
    for index, sample in enumerate(base):
        scale = 1.0 + 0.45 * min(1.0, index / len(base))
        sample.set("gw_rtt_ms", sample.values["gw_rtt_ms"] * scale)
        sample.set("https_ttfb_ms", sample.values["https_ttfb_ms"] * scale)
        result = l1.observe(pipeline.push(sample))
        if l1.warm and index > 300:
            assert result.anomaly < 0.6, f"benign drift alarmed at sample {index}"


def test_l1_state_survives_serialisation():
    pipeline, l1 = FeaturePipeline(), L1Baselines(warmup_samples=50)
    for sample in healthy_stream(120):
        l1.observe(pipeline.push(sample))
    restored = L1Baselines.from_dict(json.loads(json.dumps(l1.to_dict())))
    assert restored.samples_seen == l1.samples_seen
    assert restored.warm is l1.warm
    name = "gw_rtt_ms"
    assert restored.baselines[name].global_moments.mean == pytest.approx(
        l1.baselines[name].global_moments.mean
    )


def test_l1_score_only_does_not_learn():
    pipeline, l1 = FeaturePipeline(), L1Baselines(warmup_samples=10)
    for sample in healthy_stream(60):
        l1.observe(pipeline.push(sample))
    before = l1.baselines["gw_rtt_ms"].global_moments.count
    frame = pipeline.push(make_sample(ts=9e9, gw_rtt_ms=900.0))
    l1.score_only(frame)
    assert l1.baselines["gw_rtt_ms"].global_moments.count == before


def test_l1_reset_feature_forgets_only_that_feature():
    pipeline, l1 = FeaturePipeline(), L1Baselines(warmup_samples=10)
    for sample in healthy_stream(60):
        l1.observe(pipeline.push(sample))
    l1.reset_feature("gw_rtt_ms")
    assert l1.baselines["gw_rtt_ms"].global_moments.count == 0
    assert l1.baselines["dns_p50_ms"].global_moments.count > 0


def test_combine_is_bounded_and_monotonic():
    assert combine([]) == 0.0
    assert combine([1.0]) < combine([3.0])
    assert combine([10.0, 10.0, 10.0]) == 1.0


# ------------------------------------------------------------------- L2


def test_autoencoder_converges_on_a_stationary_signal():
    import numpy as np

    rng = np.random.default_rng(0)
    model = OnlineAutoencoder(5, seed=1)
    errors = []
    for _ in range(600):
        vector = np.array([50 + rng.normal(0, 1), 0.03, 300.0, 3 + rng.normal(0, 0.2), 20.0])
        errors.append(model.observe(vector))
    assert statistics.fmean(errors[-100:]) < statistics.fmean(errors[:100]) / 2


def test_l2_error_statistics_exclude_the_training_ramp():
    """The bug that made the whole ensemble silent."""
    import numpy as np

    model = OnlineAutoencoder(4, seed=2)
    for _ in range(60):
        model.observe(np.array([1.0, 2.0, 3.0, 4.0]))
    assert model.error_mean == 0.0, "no statistics before convergence"
    assert not model.warm


def test_l2_freezes_learning_while_anomalous():
    import numpy as np

    model = OnlineAutoencoder(4, seed=3)
    rng = np.random.default_rng(1)
    for _ in range(400):
        model.observe(np.array([1.0, 2.0, 3.0, 4.0]) + rng.normal(0, 0.01, 4))
    assert model.warm
    before = model.w_encode.copy()
    for _ in range(5):
        model.observe(np.array([900.0, 900.0, 900.0, 900.0]))
    assert model.frozen_steps > 0
    assert np.allclose(before, model.w_encode), "a warm model must not learn a fault as normal"


def test_l2_quiet_on_healthy_and_serialisable():
    pipeline, l2 = FeaturePipeline(), L2Ensemble()
    peak = 0.0
    for sample in healthy_stream(400):
        result = l2.observe(pipeline.push(sample))
        if l2.warm:
            peak = max(peak, result.anomaly)
    assert peak < 0.4
    restored = L2Ensemble.from_dict(json.loads(json.dumps(l2.to_dict())))
    assert restored.output.trained == l2.output.trained


def test_l2_skips_a_bundle_it_cannot_fully_measure():
    pipeline, l2 = FeaturePipeline(), L2Ensemble()
    frame = pipeline.push(make_sample(ts=1.0, gw_rtt_ms=3.0))
    result = l2.observe(frame)
    assert "gateway" not in result.raw_errors, "a partial bundle is skipped, not zero-filled"


# -------------------------------------------------------------------- L3


def test_feature_layout_is_stable_and_unique():
    assert len(set(FEATURE_LAYOUT)) == len(FEATURE_LAYOUT)
    assert FEATURE_LAYOUT[0] == "l1_anomaly"


def test_extract_produces_the_declared_width():
    from netpulse.ml.l1_stats import L1Result
    from netpulse.ml.l2_online import L2Result

    frame = FeaturePipeline().push(healthy_stream(1)[0])
    vector = extract(
        frame,
        L1Result(anomaly=0.2, per_feature={}, per_layer={"dns": 0.3}, shifted=[], warm=True),
        L2Result(anomaly=0.1, per_bundle={"wifi": 0.2}, raw_errors={}, warm=True),
    )
    assert vector.shape == (len(FEATURE_LAYOUT),)
    assert not any(math.isnan(value) for value in vector)


def test_logistic_training_separates_two_clusters():
    import numpy as np

    rng = np.random.default_rng(0)
    width = len(FEATURE_LAYOUT)
    negatives = rng.normal(0, 0.2, (400, width))
    positives = rng.normal(2.0, 0.2, (100, width))
    X = np.vstack([negatives, positives])
    y = np.concatenate([np.zeros(400), np.ones(100)])
    model = train_logistic(X, y, horizon_min=5, epochs=300)
    scores = model.predict_batch(X)
    assert scores[y > 0.5].mean() > 0.7
    assert scores[y < 0.5].mean() < 0.3


def test_prior_correction_keeps_probabilities_honest():
    """Without it the head reports confident probabilities on quiet inputs."""
    import numpy as np

    rng = np.random.default_rng(1)
    width = len(FEATURE_LAYOUT)
    X = np.vstack([rng.normal(0, 1.0, (900, width)), rng.normal(1.2, 1.0, (100, width))])
    y = np.concatenate([np.zeros(900), np.ones(100)])
    model = train_logistic(X, y, horizon_min=15, epochs=300)
    predicted = model.predict_batch(X).mean()
    assert abs(predicted - y.mean()) < 0.15, "mean prediction must track the base rate"


def test_model_bundle_round_trips():
    import numpy as np

    model = LogisticModel(
        weights=np.zeros(len(FEATURE_LAYOUT)),
        bias=-1.0,
        mean=np.zeros(len(FEATURE_LAYOUT)),
        scale=np.ones(len(FEATURE_LAYOUT)),
        horizon_min=5,
    )
    predictor = L3Predictor({5: model})
    restored = L3Predictor.from_bundle(json.loads(json.dumps(predictor.to_bundle())))
    assert restored.trained
    assert restored.models[5].bias == pytest.approx(-1.0)


def test_mismatched_layout_is_refused():
    with pytest.raises(ValueError, match="feature layout"):
        LogisticModel.from_dict(
            {
                "layout": ["something", "else"],
                "weights": [0.0, 0.0],
                "bias": 0.0,
                "mean": [0.0, 0.0],
                "scale": [1.0, 1.0],
            }
        )


def test_shipped_model_loads_and_predicts():
    predictor = L3Predictor.load()
    assert predictor.trained, "a trained default model must ship with the package"
    assert sorted(predictor.models) == [5, 15]


def test_heuristic_fallback_is_used_without_a_model():
    from netpulse.ml.l1_stats import L1Result
    from netpulse.ml.l2_online import L2Result

    frame = FeaturePipeline().push(healthy_stream(1)[0])
    l1 = L1Result(anomaly=0.9, per_feature={}, per_layer={}, shifted=[], warm=True)
    l2 = L2Result(anomaly=0.8, per_bundle={}, raw_errors={}, warm=True)
    result = L3Predictor().predict(frame, l1, l2)
    assert result.source == "heuristic"
    assert result.risk_15m > result.risk_5m
    assert 0.0 <= result.risk_15m <= 1.0


def test_heuristic_is_low_when_nothing_is_wrong():
    from netpulse.ml.l1_stats import L1Result
    from netpulse.ml.l2_online import L2Result

    frame = FeaturePipeline().push(healthy_stream(1)[0])
    quiet = heuristic_risk(
        L1Result(anomaly=0.0, per_feature={}, per_layer={}, shifted=[], warm=True),
        L2Result(anomaly=0.0, per_bundle={}, raw_errors={}, warm=True),
        frame,
        15,
    )
    assert quiet < 0.1


@pytest.mark.parametrize(
    "risk,band", [(0.05, "info"), (0.3, "watch"), (0.6, "risk"), (0.9, "critical")]
)
def test_risk_bands(risk, band, config):
    assert band_for(risk, config.ml.risk_bands) == band


# ------------------------------------------------------------------- drift


def test_psi_is_near_zero_for_identical_samples():
    rng = random.Random(0)
    reference = [rng.gauss(10, 1) for _ in range(500)]
    recent = [rng.gauss(10, 1) for _ in range(500)]
    assert population_stability_index(reference, recent) < 0.1


def test_psi_detects_a_shifted_distribution():
    rng = random.Random(0)
    reference = [rng.gauss(10, 1) for _ in range(500)]
    recent = [rng.gauss(30, 1) for _ in range(500)]
    assert population_stability_index(reference, recent) > 0.25


def test_drift_monitor_stays_quiet_on_a_stable_network():
    pipeline, monitor = FeaturePipeline(), DriftMonitor()
    decisions = []
    for sample in healthy_stream(900):
        decisions.extend(monitor.observe(pipeline.push(sample), now=sample.ts))
    assert decisions == []


def test_drift_monitor_notices_a_new_normal():
    pipeline, monitor = FeaturePipeline(), DriftMonitor()
    for sample in healthy_stream(700):
        monitor.observe(pipeline.push(sample), now=sample.ts)
    decisions: list = []
    for index in range(700):
        sample = healthy_stream(1, start=1_780_000_000.0 + (700 + index) * 15)[0]
        sample.set("gw_rtt_ms", 40.0)  # a new router, three times slower
        decisions.extend(monitor.observe(pipeline.push(sample), now=sample.ts))
    assert any(decision.feature == "gw_rtt_ms" for decision in decisions)


# ------------------------------------------------------------------ L4 RCA


def test_template_coverage_meets_the_prd():
    coverage = templates.coverage_report()
    assert sum(coverage.values()) >= 12, "PRD 10.1 asks for at least twelve templates"
    assert {"wifi", "dns", "gateway", "path", "remote_https", "vpn"} <= set(coverage)


def test_every_template_renders_without_a_missing_key():
    frame = FeaturePipeline().push(healthy_stream(1)[0])
    for template in templates.TEMPLATES:
        context = templates.TemplateContext(
            frame=frame, layer=template.layer, secondary=None, severity="risk", risk_15m=0.6
        )
        rendered = template.render(context)
        assert rendered["summary"]
        assert "{" not in rendered["summary"], template.id


def test_every_template_remediation_id_exists():
    for template in (*templates.TEMPLATES, templates.FALLBACK):
        for identifier in template.remediation:
            assert identifier in remediation.BY_ID, f"{template.id} -> {identifier}"


def test_remediation_is_capped_at_three():
    assert len(remediation.resolve([item.id for item in remediation.CATALOGUE])) == 3


def test_specific_template_beats_the_generic_one():
    frame = FeaturePipeline().push(
        make_sample(ts=1.0, wifi_rssi_dbm=-50.0, wifi_tx_retry_rate=0.4, wifi_band_ghz=5.0)
    )
    context = templates.TemplateContext(frame=frame, layer="wifi", secondary=None, severity="risk")
    assert templates.select(context).id == "wifi.interference"


def test_attribution_prefers_the_layer_where_latency_appeared():
    pipeline = FeaturePipeline()
    for sample in healthy_stream(200):
        pipeline.push(sample)
    frame = pipeline.push(
        make_sample(
            ts=1_780_000_000.0 + 201 * 15,
            gw_rtt_ms=3.0,
            path_rtt_ms=14.0,
            https_ttfb_ms=900.0,
            dns_p50_ms=20.0,
        )
    )
    from netpulse.ml.l1_stats import L1Result
    from netpulse.ml.l2_online import L2Result

    attribution = l4_rca.attribute(
        frame,
        L1Result(
            anomaly=0.8,
            per_feature={},
            per_layer={"remote_https": 0.8, "path": 0.1},
            shifted=[],
            warm=True,
        ),
        L2Result(anomaly=0.2, per_bundle={}, raw_errors={}, warm=True),
        [],
    )
    assert attribution.primary == "remote_https"


def test_suppressed_layers_cannot_be_blamed():
    from netpulse.ml.l1_stats import L1Result
    from netpulse.ml.l2_online import L2Result

    frame = FeaturePipeline().push(make_sample(ts=1.0, captive_portal=1.0, https_fail_rate=1.0))
    hits = l0_rules.evaluate(frame)
    attribution = l4_rca.attribute(
        frame,
        L1Result(
            anomaly=0.9, per_feature={}, per_layer={"remote_https": 0.9}, shifted=[], warm=True
        ),
        L2Result(anomaly=0.0, per_bundle={}, raw_errors={}, warm=True),
        hits,
    )
    assert attribution.primary != "remote_https"
    assert attribution.scores["remote_https"] == 0.0


def test_explanation_is_plain_language():
    from netpulse.ml.l1_stats import L1Result
    from netpulse.ml.l2_online import L2Result

    frame = FeaturePipeline().push(
        make_sample(ts=1.0, wifi_rssi_dbm=-85.0, wifi_link_mbps=12.0, wifi_tx_retry_rate=0.4)
    )
    hits = l0_rules.evaluate(frame)
    attribution = l4_rca.attribute(
        frame,
        L1Result(
            anomaly=0.8,
            per_feature={"wifi_rssi_dbm": 3.0},
            per_layer={"wifi": 0.8},
            shifted=[],
            warm=True,
        ),
        L2Result(anomaly=0.3, per_bundle={"wifi": 0.4}, raw_errors={}, warm=True),
        hits,
    )
    card = l4_rca.explain(frame, attribution, "risk", hits, 0.7)
    assert card["primary_layer"] == "wifi"
    assert card["title"]
    assert 1 <= len(card["remediation"]) <= 3
    assert "dBm" in card["summary"] or "Wi-Fi" in card["summary"]


# ----------------------------------------------------------------- scorer


def test_scorer_stays_calm_on_a_healthy_stream(config):
    scorer = Scorer(config)
    opened = 0
    for sample in healthy_stream(600):
        result = scorer.observe(sample)
        opened += 1 if result.opened else 0
    assert opened == 0, "a healthy stream must not raise incidents"
    assert scorer.health > 80


def test_scorer_opens_and_closes_one_incident_per_episode(config):
    scorer = Scorer(config)
    run = generate(SCENARIOS_BY_NAME["gateway_down"])
    opened, closed = 0, 0
    for sample in run.samples:
        result = scorer.observe(sample)
        opened += 1 if result.opened else 0
        closed += 1 if result.closed else 0
    assert opened == 1, f"expected one incident, got {opened}"
    assert closed <= 1


def test_scorer_l0_floor_bypasses_smoothing(config):
    scorer = Scorer(config)
    for sample in healthy_stream(200):
        scorer.observe(sample)
    result = scorer.observe(
        make_sample(ts=1_780_000_000.0 + 201 * 15, gw_loss_rate=1.0, gw_rtt_ms=5000.0)
    )
    assert result.score.risk_15m >= 0.9, "a dead gateway is a fact, not a forecast"
    assert result.score.severity == "critical"


def test_scorer_state_round_trips(config):
    scorer = Scorer(config)
    for sample in healthy_stream(200):
        scorer.observe(sample)
    state = json.loads(json.dumps(scorer.save_state()))
    fresh = Scorer(config)
    fresh.load_state(state)
    assert fresh.l1.samples_seen == scorer.l1.samples_seen
    assert fresh.health == pytest.approx(scorer.health)


def test_scorer_survives_corrupt_state(config):
    scorer = Scorer(config)
    scorer.load_state({"l1": {"nonsense": True}})
    assert scorer.observe(healthy_stream(1)[0]) is not None


def test_paused_learning_does_not_update_baselines(config):
    scorer = Scorer(config)
    for sample in healthy_stream(100):
        scorer.observe(sample)
    scorer.set_learning(False)
    before = scorer.l1.baselines["gw_rtt_ms"].global_moments.count
    for sample in healthy_stream(20, start=1_780_000_000.0 + 100 * 15):
        scorer.observe(sample)
    assert scorer.l1.baselines["gw_rtt_ms"].global_moments.count == before


def test_scorer_handles_an_empty_sample(config):
    scorer = Scorer(config)
    result = scorer.observe(Sample(ts=1.0))
    assert result.score.coverage == 0.0
    assert result.score.warming_up is True
