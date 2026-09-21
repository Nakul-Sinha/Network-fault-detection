"""L4: layer attribution and the explanation a person actually reads.

The hard part of root-cause analysis from a single end host is not noticing
that something is wrong, it is deciding *which* layer to name. Latency
propagates outward: if the router is slow then the path is slow, and if the
path is slow then every remote service looks slow. A naive "largest anomaly
wins" would blame the remote service for a Wi-Fi problem every time, which
is exactly what the L1 scores do on the ISP-congestion scenario.

So attribution runs in two stages, following the layered dependency sketch
Sherlock (R16) and NetMedic (R17) use:

1. **Inheritance dampening.** Each layer's score is reduced by the portion
   its upstream layers already explain. What survives is the *excess*: the
   trouble that appeared at this layer and not before it.
2. **Cross-layer evidence.** The derived ratios then vote directly. A large
   ``x_path_minus_gateway_ms`` with a quiet gateway is positive evidence for
   the path; a large ``x_remote_minus_path_ms`` with a quiet path is positive
   evidence for the far end.

L0 suppression is applied last and is absolute: a layer the rules have
forbidden cannot be named no matter how it scored. That is what keeps the
agent from blaming an ISP for a hotel sign-in page.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..features.pipeline import FeatureFrame
from ..features.schema import ATTRIBUTABLE_LAYERS, HUMAN_LAYER_NAMES, Layer
from ..rca import remediation, templates
from .l0_rules import RuleHit, severity_rank, suppressed_layers
from .l1_stats import L1Result
from .l2_online import L2Result

#: Dependency chain from the innermost hop outward. A layer inherits latency
#: from everything before it in this list.
CHAIN: tuple[Layer, ...] = ("wifi", "os", "gateway", "path", "remote_https")

#: Fraction of an upstream anomaly that is assumed to explain a downstream
#: one. High on purpose: when the router is slow, almost all of the extra
#: time seen at the far end is that same slowness measured again.
INHERITANCE = 0.75

#: Weight of each evidence source in the pre-dampening layer score.
W_L1 = 1.0
W_L2 = 0.6
W_RULE = 1.4

#: A secondary layer is reported when it lands within this distance of the
#: primary (PRD 8.4 uses 0.15).
SECONDARY_MARGIN = 0.15


@dataclass(slots=True)
class Attribution:
    primary: str | None
    secondary: str | None
    confidence: float
    scores: dict[str, float]
    evidence: list[dict[str, Any]] = field(default_factory=list)
    suppressed: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "primary": self.primary,
            "primary_label": HUMAN_LAYER_NAMES.get(self.primary or "", None),
            "secondary": self.secondary,
            "confidence": round(self.confidence, 3),
            "scores": {k: round(v, 4) for k, v in sorted(self.scores.items())},
            "evidence": self.evidence,
            "suppressed": self.suppressed,
        }


def attribute(
    frame: FeatureFrame,
    l1: L1Result,
    l2: L2Result,
    hits: list[RuleHit],
) -> Attribution:
    """Score every layer and pick a primary and optional secondary cause."""
    raw = _raw_scores(frame, l1, l2, hits)
    excess = _dampen(raw)
    _apply_cross_layer_evidence(frame, excess)

    blocked = suppressed_layers(hits)
    for layer in blocked:
        excess[layer] = 0.0

    ranked = sorted(excess.items(), key=lambda item: -item[1])
    primary_layer, primary_score = ranked[0] if ranked else (None, 0.0)
    if primary_score <= 0.0:
        return Attribution(
            primary=None,
            secondary=None,
            confidence=0.0,
            scores=excess,
            suppressed=sorted(blocked),
        )

    secondary_layer = None
    if len(ranked) > 1 and ranked[1][1] > 0 and primary_score - ranked[1][1] <= SECONDARY_MARGIN:
        secondary_layer = ranked[1][0]

    total = sum(value for _, value in ranked if value > 0)
    confidence = primary_score / total if total > 0 else 0.0

    return Attribution(
        primary=primary_layer,
        secondary=secondary_layer,
        confidence=confidence,
        scores=excess,
        evidence=_evidence(frame, l1, hits, primary_layer),
        suppressed=sorted(blocked),
    )


def _raw_scores(
    frame: FeatureFrame, l1: L1Result, l2: L2Result, hits: list[RuleHit]
) -> dict[str, float]:
    scores: dict[str, float] = dict.fromkeys(ATTRIBUTABLE_LAYERS, 0.0)
    for layer in ATTRIBUTABLE_LAYERS:
        scores[layer] = W_L1 * l1.per_layer.get(layer, 0.0) + W_L2 * l2.per_bundle.get(layer, 0.0)
    for hit in hits:
        if hit.layer in scores:
            scores[hit.layer] += W_RULE * (severity_rank(hit.severity) + 1) / 4.0
    if frame.values.get("vpn_active", 0.0) < 1.0:
        # A layer that does not exist right now cannot be the cause.
        scores["vpn"] = 0.0
    if not frame.has_layer("wifi"):
        scores["wifi"] = 0.0
    return scores


def _dampen(raw: dict[str, float]) -> dict[str, float]:
    """Subtract the part of each score its upstream layers already explain."""
    excess = dict(raw)
    upstream_max = 0.0
    for layer in CHAIN:
        own = raw.get(layer, 0.0)
        excess[layer] = max(0.0, own - INHERITANCE * upstream_max)
        upstream_max = max(upstream_max, own)

    # DNS sits beside the chain rather than in it: resolution goes out over
    # the same first hops, so it inherits from them, but nothing inherits
    # from DNS.
    local_max = max(raw.get("wifi", 0.0), raw.get("gateway", 0.0))
    excess["dns"] = max(0.0, raw.get("dns", 0.0) - 0.5 * local_max)

    # A tunnel carries traffic over the path, so it inherits from the path,
    # and the far end inherits from the tunnel in turn.
    excess["vpn"] = max(0.0, raw.get("vpn", 0.0) - INHERITANCE * raw.get("path", 0.0))
    return excess


def _apply_cross_layer_evidence(frame: FeatureFrame, excess: dict[str, float]) -> None:
    """Let the cross-layer ratios vote on where the extra time appeared."""
    derived = frame.derived
    gateway = frame.values.get("gw_rtt_ms")
    quiet_gateway = gateway is not None and frame.aggregates.get("gw_rtt_ms_15m_z", 0.0) < 1.5

    path_extra = derived.get("x_path_minus_gateway_ms")
    if path_extra is not None and quiet_gateway:
        # Latency that exists past the router but not at it.
        excess["path"] = excess.get("path", 0.0) + min(0.6, path_extra / 120.0)

    remote_extra = derived.get("x_remote_minus_path_ms")
    quiet_path = frame.aggregates.get("path_rtt_ms_15m_z", 0.0) < 1.5
    if remote_extra is not None and quiet_path:
        excess["remote_https"] = excess.get("remote_https", 0.0) + min(0.6, remote_extra / 400.0)

    if frame.values.get("vpn_active", 0.0) >= 1.0 and remote_extra and quiet_path:
        # With a tunnel up, "extra time past the path" is at least as likely
        # to be the tunnel as the far end, so the two share the evidence.
        share = min(0.6, remote_extra / 400.0)
        excess["vpn"] = excess.get("vpn", 0.0) + share
        excess["remote_https"] = max(0.0, excess.get("remote_https", 0.0) - share * 0.5)
        retrans = frame.values.get("os_retrans_rate", 0.0)
        if retrans >= 0.03:
            excess["vpn"] += min(0.3, retrans * 3.0)

    dns_share = derived.get("x_dns_share")
    if dns_share is not None and dns_share > 0.35:
        excess["dns"] = excess.get("dns", 0.0) + min(0.5, (dns_share - 0.35) * 2.0)

    wifi_pressure = derived.get("x_wifi_pressure")
    if wifi_pressure is not None and wifi_pressure > 0.4:
        excess["wifi"] = excess.get("wifi", 0.0) + min(0.5, (wifi_pressure - 0.4) * 1.5)


def _evidence(
    frame: FeatureFrame, l1: L1Result, hits: list[RuleHit], layer: str | None
) -> list[dict[str, Any]]:
    """Chips shown under the incident card: what moved, and by how much."""
    evidence: list[dict[str, Any]] = []
    for hit in hits:
        if hit.layer == layer:
            evidence.extend(hit.evidence)

    ranked = sorted(
        ((name, score) for name, score in l1.per_feature.items() if score > 0.0),
        key=lambda item: -item[1],
    )
    from ..features.schema import FEATURE_BY_NAME, layer_of

    for name, score in ranked:
        if layer is not None and layer_of(name) != layer:
            continue
        spec = FEATURE_BY_NAME[name]
        evidence.append(
            {
                "label": spec.description,
                "feature": name,
                "value": round(frame.values.get(name, 0.0), 3),
                "unit": spec.unit,
                "comparison": f"{score:.1f} above this network's usual range",
            }
        )
        if len(evidence) >= 4:
            break
    return evidence


def explain(
    frame: FeatureFrame,
    attribution: Attribution,
    severity: str,
    hits: list[RuleHit],
    risk_15m: float,
) -> dict[str, Any]:
    """Turn an attribution into a rendered incident card."""
    layer = attribution.primary or "os"
    context = templates.TemplateContext(
        frame=frame,
        layer=layer,
        secondary=attribution.secondary,
        severity=severity,
        rule_ids=frozenset(hit.rule_id for hit in hits),
        risk_15m=risk_15m,
    )
    template = templates.select(context)
    rendered = template.render(context)
    return {
        "template_id": rendered["template_id"],
        "title": rendered["title"],
        "summary": rendered["summary"],
        "remediation": remediation.texts(rendered["remediation"]),
        "remediation_detail": remediation.resolve(rendered["remediation"]),
        "primary_layer": attribution.primary,
        "secondary_layer": attribution.secondary,
        "confidence": round(attribution.confidence, 3),
        "evidence": attribution.evidence,
    }
