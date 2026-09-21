"""L0: hard rules and safety interlocks.

These fire before any model runs and they are deliberately boring. Their job
is to handle the cases where a learned score would be both slower and less
trustworthy than a threshold: the gateway is simply unreachable, a captive
portal is intercepting everything, the link is down.

L0 also owns *suppression*. A captive portal makes every layer look broken at
once, so while one is detected the rules forbid blaming the path or the
remote service. PRD 11.2 puts this plainly: never blame the ISP without path
evidence.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..features.pipeline import FeatureFrame
from ..features.schema import Layer

SEVERITY_ORDER = ("info", "watch", "risk", "critical")


def severity_rank(severity: str) -> int:
    try:
        return SEVERITY_ORDER.index(severity)
    except ValueError:
        return 0


def max_severity(*severities: str) -> str:
    return max(severities, key=severity_rank) if severities else "info"


@dataclass(slots=True)
class RuleHit:
    """One fired rule, carrying its own evidence for the incident card."""

    rule_id: str
    layer: Layer
    severity: str
    message: str
    evidence: list[dict[str, object]] = field(default_factory=list)
    #: Layers this rule forbids blaming while it is active.
    suppresses: tuple[Layer, ...] = ()
    #: Forced floor on the risk forecast, for conditions that are already bad.
    risk_floor: float = 0.0

    def to_dict(self) -> dict[str, object]:
        return {
            "rule_id": self.rule_id,
            "layer": self.layer,
            "severity": self.severity,
            "message": self.message,
            "evidence": self.evidence,
        }


def _evidence(label: str, value: float, unit: str, comparison: str) -> dict[str, object]:
    return {"label": label, "value": round(value, 3), "unit": unit, "comparison": comparison}


def evaluate(frame: FeatureFrame) -> list[RuleHit]:
    """Run every L0 rule against one frame, in priority order."""
    hits: list[RuleHit] = []
    values = frame.values

    captive = values.get("captive_portal", 0.0)
    if captive >= 1.0:
        hits.append(
            RuleHit(
                rule_id="l0.captive_portal",
                layer="dns",
                severity="critical",
                message="A sign-in page is intercepting network traffic.",
                evidence=[_evidence("Captive portal check", 1.0, "", "intercepted")],
                # Everything downstream of the portal is unmeasurable, so no
                # other layer may be blamed while this holds.
                suppresses=("path", "remote_https", "dns", "gateway", "wifi", "os", "vpn"),
                risk_floor=0.9,
            )
        )

    loss = values.get("gw_loss_rate")
    if loss is not None and loss >= 0.99:
        hits.append(
            RuleHit(
                rule_id="l0.gateway_unreachable",
                layer="gateway",
                severity="critical",
                message="Your router is not answering at all.",
                evidence=[_evidence("Gateway replies", 0.0, "%", "none of 4 probes answered")],
                suppresses=("path", "remote_https", "dns"),
                risk_floor=0.95,
            )
        )
    elif loss is not None and loss >= 0.5:
        hits.append(
            RuleHit(
                rule_id="l0.gateway_loss",
                layer="gateway",
                severity="risk",
                message="Your router is dropping a large share of probes.",
                evidence=[_evidence("Gateway packet loss", loss * 100, "%", "above 50%")],
                risk_floor=0.7,
            )
        )

    dns_fail = values.get("dns_fail_rate")
    if dns_fail is not None and dns_fail >= 0.99:
        hits.append(
            RuleHit(
                rule_id="l0.dns_blackhole",
                layer="dns",
                severity="critical",
                message="Name lookups are failing completely.",
                evidence=[_evidence("DNS failures", 100.0, "%", "every lookup failed")],
                suppresses=("remote_https",),
                risk_floor=0.9,
            )
        )
    elif dns_fail is not None and dns_fail >= 0.34:
        hits.append(
            RuleHit(
                rule_id="l0.dns_failures",
                layer="dns",
                severity="risk",
                message="A third or more of name lookups are failing.",
                evidence=[_evidence("DNS failures", dns_fail * 100, "%", "above 33%")],
                risk_floor=0.6,
            )
        )

    https_fail = values.get("https_fail_rate")
    # Failing HTTPS only implicates the far end when the two things it
    # depends on, the router and name resolution, are both healthy.
    # Otherwise the failure is a symptom of those and not a cause.
    local_healthy = (loss is None or loss < 0.5) and (dns_fail is None or dns_fail < 0.2)
    if https_fail is not None and https_fail >= 0.99 and local_healthy:
        hits.append(
            RuleHit(
                rule_id="l0.remote_unreachable",
                layer="remote_https",
                severity="critical",
                message="Test connections to the internet are all failing while your router "
                "still answers.",
                evidence=[
                    _evidence("HTTPS probe failures", 100.0, "%", "every probe failed"),
                    _evidence("Gateway loss", (loss or 0.0) * 100, "%", "router still answering"),
                ],
                risk_floor=0.9,
            )
        )

    rssi = values.get("wifi_rssi_dbm")
    if rssi is not None and rssi <= -82.0:
        hits.append(
            RuleHit(
                rule_id="l0.wifi_signal_floor",
                layer="wifi",
                severity="risk",
                message="Wi-Fi signal is at the level where calls usually break up.",
                evidence=[_evidence("Wi-Fi signal", rssi, "dBm", "at or below -82 dBm")],
                risk_floor=0.55,
            )
        )
    elif rssi is not None and rssi <= -75.0:
        hits.append(
            RuleHit(
                rule_id="l0.wifi_signal_weak",
                layer="wifi",
                severity="watch",
                message="Wi-Fi signal is weak.",
                evidence=[_evidence("Wi-Fi signal", rssi, "dBm", "at or below -75 dBm")],
            )
        )

    retries = values.get("wifi_tx_retry_rate")
    if retries is not None and retries >= 0.35:
        hits.append(
            RuleHit(
                rule_id="l0.wifi_retries",
                layer="wifi",
                severity="risk",
                message="A large share of Wi-Fi frames are being retransmitted.",
                evidence=[_evidence("Wi-Fi retries", retries * 100, "%", "above 35%")],
                risk_floor=0.55,
            )
        )

    retrans = values.get("os_retrans_rate")
    if retrans is not None and retrans >= 0.08:
        # Retransmissions are measured on the host, but when a tunnel is
        # carrying the traffic they are far more likely to be the tunnel
        # than the network adapter, so the rule follows the route.
        on_vpn = values.get("vpn_active", 0.0) >= 1.0
        hits.append(
            RuleHit(
                rule_id="l0.tcp_retransmissions",
                layer="vpn" if on_vpn else "os",
                severity="watch",
                message=(
                    "Traffic inside the VPN tunnel is being retransmitted heavily."
                    if on_vpn
                    else "This device is retransmitting an unusual share of TCP segments."
                ),
                evidence=[_evidence("TCP retransmissions", retrans * 100, "%", "above 8%")],
            )
        )

    return hits


def suppressed_layers(hits: list[RuleHit]) -> set[Layer]:
    """Layers that must not be named as a cause given the rules that fired."""
    suppressed: set[Layer] = set()
    for hit in hits:
        for layer in hit.suppresses:
            if layer != hit.layer:
                suppressed.add(layer)
    return suppressed


def risk_floor(hits: list[RuleHit]) -> float:
    return max((hit.risk_floor for hit in hits), default=0.0)


def worst(hits: list[RuleHit]) -> RuleHit | None:
    if not hits:
        return None
    return max(hits, key=lambda hit: (severity_rank(hit.severity), hit.risk_floor))
