"""Generate the demo site's data by running the real scoring pipeline.

The web demo is a replay, not a mock. Every number it shows was produced by
``netpulse.ml.scorer.Scorer`` observing the fault-injection corpus, which is
the same object the installed agent runs. That matters: a hand-written demo
would drift from the product within a week and would be quietly making claims
the code does not support.

    python scripts/build_demo_data.py

Writes one JSON file per scenario into ``web/public/data/`` plus an index.

The output is reproducible on any machine, which CI checks by regenerating
and diffing. Getting there needed one thing that is easy to miss: the model
derives hour of day from *local* time, correctly, because seasonality is
about the user's day rather than UTC. That makes the generator's output
depend on the timezone it runs in, so the recordings are anchored to a fixed
calendar date at a fixed local hour instead of to the corpus default. A
machine in Kolkata and a runner in UTC then feed the model the same hour and
produce identical files.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from netpulse.config import NetPulseConfig  # noqa: E402
from netpulse.eval.replay import replay_corpus  # noqa: E402
from netpulse.eval.scenarios import SCENARIOS, ScenarioRun, generate  # noqa: E402
from netpulse.ml import l0_rules  # noqa: E402
from netpulse.ml.scorer import Scorer  # noqa: E402

OUTPUT = ROOT / "web" / "public" / "data"

#: Recordings are anchored to this local date and time. The hour matches the
#: one scenarios.generate builds its diurnal curve around, so the values and
#: the model's sense of time of day agree wherever this runs.
ANCHOR = datetime(2026, 6, 15, 19, 0, 0)

#: Human-facing titles. The scenario names are internal identifiers; these
#: are what someone who has never read the code should see.
TITLES: dict[str, tuple[str, str]] = {
    "healthy": (
        "A quiet network",
        "Nothing wrong. This is what the agent looks like when it has nothing to say, "
        "which is most of the time and is the hardest case to get right.",
    ),
    "evening_congestion": (
        "Evening congestion",
        "Everything slows by about 45 percent at peak hours and nothing breaks. "
        "A detector that flags this is one you uninstall within a week.",
    ),
    "wifi_fade": (
        "Walking away from the access point",
        "Signal fades, retries climb, the link rate collapses. The classic "
        "cause of a call breaking up, and the one users most often misdiagnose.",
    ),
    "wifi_interference": (
        "A crowded Wi-Fi channel",
        "Signal strength stays fine but a third of frames need retransmitting. "
        "Distance is not the problem here, and the advice differs accordingly.",
    ),
    "dns_slow": (
        "A slow resolver",
        "Name lookups climb from 20 ms to several hundred. Pages feel slow to "
        "start even though they transfer normally once open.",
    ),
    "dns_blackhole": (
        "DNS stops answering",
        "Total resolver failure. Everything downstream fails too, so the "
        "interesting question is whether the agent blames the cause or a symptom.",
    ),
    "gateway_congestion": (
        "The router is struggling",
        "Round trips to your own router rise from 3 ms to tens of milliseconds "
        "with heavy jitter. The problem is inside the building.",
    ),
    "gateway_down": (
        "The router stops responding",
        "Nothing past it can be reached or measured. The agent has to say so "
        "without inventing conclusions about layers it can no longer see.",
    ),
    "isp_path_congestion": (
        "Congestion past the router",
        "The local network stays clean; latency appears beyond the second hop. "
        "This is the case a single-layer tool always gets wrong.",
    ),
    "remote_service_slow": (
        "The far end is slow",
        "DNS, the router and the path are all healthy. Only the service being "
        "contacted is slow, so the answer is that nothing is wrong with your network.",
    ),
    "tls_handshake_slow": (
        "Slow TLS handshakes",
        "The encrypted setup at the start of each connection dominates. Phase "
        "timing is the only way to see this at all.",
    ),
    "vpn_overload": (
        "A saturated VPN tunnel",
        "Extra latency appears inside the tunnel rather than on the path to it, "
        "and retransmissions rise with it.",
    ),
    "device_pressure": (
        "The laptop itself is the bottleneck",
        "CPU is pinned and the network stack is starved. The network is fine; the machine is not.",
    ),
    "captive_portal": (
        "A hotel sign-in page",
        "Something intercepts every request at once. Every layer looks broken, "
        "and blaming the internet provider would be confidently wrong.",
    ),
}

LAYER_LABELS = {
    "wifi": "Wi-Fi",
    "os": "This device",
    "dns": "DNS",
    "gateway": "Your router",
    "path": "Path to internet",
    "remote_https": "Remote services",
    "vpn": "VPN",
}

#: Feature values surfaced in the demo's evidence strip, with the units a
#: person reads rather than the internal names.
WATCHED = [
    ("wifi_rssi_dbm", "Wi-Fi signal", "dBm", 0),
    ("wifi_tx_retry_rate", "Wi-Fi retries", "%", 0, 100),
    ("gw_rtt_ms", "Router round trip", "ms", 1),
    ("gw_loss_rate", "Router packet loss", "%", 0, 100),
    ("dns_p50_ms", "Name lookup", "ms", 0),
    ("dns_fail_rate", "Lookup failures", "%", 0, 100),
    ("path_rtt_ms", "Path latency", "ms", 0),
    ("https_ttfb_ms", "Time to first byte", "ms", 0),
    ("https_tls_ms", "TLS handshake", "ms", 0),
    ("os_cpu_pct", "CPU", "%", 0),
    ("os_retrans_rate", "TCP retransmits", "%", 1, 100),
]


def _round(value: float | None, places: int = 3) -> float | None:
    if value is None:
        return None
    return round(float(value), places)


def build_scenario(run: ScenarioRun) -> dict:
    """Replay one scenario through the real scorer and capture every frame."""
    scorer = Scorer(NetPulseConfig())
    frames: list[dict] = []
    first_alert_index: int | None = None
    warm_from: int | None = None
    # Collectors report on cadences an order of magnitude apart, so most ticks
    # carry only a handful of fresh values. The agent's own pipeline carries
    # the rest forward, and the demo has to show the same thing or the signal
    # strip would be empty on nine ticks out of ten.
    latest: dict[str, float] = {}

    for sample in run.samples:
        result = scorer.observe(sample)
        index = len(frames)

        if warm_from is None and not result.score.warming_up:
            warm_from = index

        alerting = l0_rules.severity_rank(result.score.severity) >= l0_rules.severity_rank("risk")
        if alerting and first_alert_index is None and warm_from is not None:
            first_alert_index = index

        latest.update(sample.values)
        values = {}
        for entry in WATCHED:
            name, _label, _unit, places = entry[0], entry[1], entry[2], entry[3]
            scale = entry[4] if len(entry) > 4 else 1
            raw = latest.get(name)
            if raw is None:
                continue
            values[name] = _round(raw * scale, places)

        frames.append(
            {
                "t": round(sample.ts - run.start_ts),
                "h": _round(result.score.health, 1),
                "r5": _round(result.score.risk_5m),
                "r15": _round(result.score.risk_15m),
                "sev": result.score.severity,
                "cov": _round(result.score.coverage, 2),
                "warm": result.score.warming_up,
                "layers": {
                    key: _round(value, 3)
                    for key, value in result.score.layer_scores.items()
                    if value and value > 0.005
                },
                "v": values,
                "card": _card(result.card) if result.card else None,
                "open": bool(result.opened),
            }
        )

    impact_index = None
    if run.impact_start is not None:
        impact_index = min(
            range(len(frames)),
            key=lambda i: abs(run.start_ts + frames[i]["t"] - run.impact_start),
        )

    lead_s = None
    if impact_index is not None and first_alert_index is not None:
        lead_s = max(0, frames[impact_index]["t"] - frames[first_alert_index]["t"])

    title, description = TITLES.get(run.name, (run.name, run.description))
    return {
        "name": run.name,
        "title": title,
        "description": description,
        "expectedLayer": run.expected_layer,
        "expectedLayerLabel": LAYER_LABELS.get(run.expected_layer or ""),
        "benign": not run.bad_windows,
        "warmFrom": warm_from,
        "impactIndex": impact_index,
        "alertIndex": first_alert_index,
        "leadSeconds": lead_s,
        "frames": frames,
    }


def _card(card: dict) -> dict:
    return {
        "title": card["title"],
        "summary": card["summary"],
        "layer": card.get("primary_layer"),
        "layerLabel": LAYER_LABELS.get(card.get("primary_layer") or ""),
        "confidence": _round(card.get("confidence", 0.0), 2),
        "remediation": list(card.get("remediation", []))[:3],
        "evidence": [
            {
                "label": item.get("label", ""),
                "value": item.get("value"),
                "unit": item.get("unit", ""),
                "comparison": item.get("comparison", ""),
            }
            for item in card.get("evidence", [])[:4]
        ],
    }


def main() -> int:
    OUTPUT.mkdir(parents=True, exist_ok=True)
    index: list[dict] = []

    for spec in SCENARIOS:
        run = generate(spec, start_ts=ANCHOR.timestamp())
        print(f"  replaying {spec.name} ({len(run.samples)} samples) ...", end="", flush=True)
        data = build_scenario(run)
        path = OUTPUT / f"{spec.name}.json"
        path.write_text(json.dumps(data, separators=(",", ":")), encoding="utf-8")
        size_kb = path.stat().st_size / 1024
        lead = f"{data['leadSeconds'] / 60:.1f} min" if data["leadSeconds"] else "-"
        print(f" {size_kb:5.0f} KB  lead {lead}")
        index.append(
            {
                "name": data["name"],
                "title": data["title"],
                "description": data["description"],
                "benign": data["benign"],
                "expectedLayer": data["expectedLayer"],
                "expectedLayerLabel": data["expectedLayerLabel"],
                "leadSeconds": data["leadSeconds"],
                "frames": len(data["frames"]),
            }
        )

    # The headline figures come from the evaluation harness, not from these
    # recordings. The recordings are anchored to a fixed hour so they are
    # byte-reproducible, and each marks its own first warning, which is right
    # for the per-scenario timeline but yields a slightly different median
    # from the one CI gates on. Publishing a number nobody enforces is
    # publishing a number nobody should believe.
    print("\n  running the evaluation harness for the headline figures ...")
    report = replay_corpus(seeds=(11,), include_soak=True, soak_hours=8.0)
    official = report.summary()

    summary = {
        "scenarios": index,
        "generatedFrom": "netpulse.ml.scorer.Scorer over netpulse.eval.scenarios",
        "stats": {
            "faults": official["faults"],
            "detectionRate": official["detection_rate"],
            "medianLeadSeconds": official["median_lead_time_s"],
            "layerAccuracy": official["layer_accuracy"],
            "precision": official["precision"],
            "benignAlertsPerDay": official["benign_alerts_per_day"],
            "source": "netpulse eval, the same figures CI gates on",
        },
    }
    (OUTPUT / "index.json").write_text(json.dumps(summary, indent=1), encoding="utf-8")
    print(f"\nwrote {len(index)} scenarios to {OUTPUT}")
    print(
        f"headline: {official['median_lead_time_min']} min median lead, "
        f"{official['detection_rate'] * 100:.0f}% detected, "
        f"{official['layer_accuracy'] * 100:.0f}% layer accuracy, "
        f"{official['benign_alerts_per_day']}/day benign alerts"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
