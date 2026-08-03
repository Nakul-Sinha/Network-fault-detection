# Explanation (Simple Overview)

A plain-language version of [`Research.md`](./Research.md).  
**Product idea:** a local app on your laptop that warns you *before* Zoom/games/SaaS get laggy, and tells you *why*—without sending your traffic to the cloud.

---

## 1. Problem

When video freezes or the internet “feels slow,” the cause is rarely a clean “cable unplugged” event. It’s usually a mix of:

- Wi‑Fi retries / weak signal  
- DNS problems  
- ISP / path congestion  
- Your own device or VPN getting overloaded  

Big enterprise tools (Datadog, ThousandEyes, etc.) are built for companies with fleets of servers and NOC teams. They are expensive, cloud-heavy, and not aimed at home users or small offices.

What’s missing: a **software-only**, **privacy-first** tool that:

1. Watches several layers at once on *your* machine  
2. **Predicts** trouble 5–15 minutes ahead (not only after it breaks)  
3. Explains the likely cause in normal English  

---

## 2. Solution

Build a **Local Predictive Network Health Agent**—an app that runs on your laptop/server and:

| What it does | How |
| --- | --- |
| Collects signals | Wi‑Fi stats, OS network health, DNS timing, traceroute, small HTTPS “canary” probes |
| Scores health | Combines simple rules + ML on-device |
| Forecasts risk | Estimates chance of lag/degradation in the next 5–15 minutes |
| Explains | “Likely Wi‑Fi” / “Likely DNS” / “Likely ISP path” + a suggested fix |
| Protects privacy | Keeps data local; no packet payloads by default; no mandatory cloud |

**We are not building:** an enterprise network monitor, a malware IDS, or anything that needs special hardware.

---

## 3. Research Summary

We reviewed papers from arXiv, IEEE, ACM, USENIX, and industry tools. The useful ideas fall into a few buckets:

| Research area | What we take from it | Example papers |
| --- | --- | --- |
| Online anomaly detection | Learn “normal” without labeled attacks; run continuously | Kitsune, Donut, Putina & Rossi |
| Early service degradation | Catch trouble from early flow/probe symptoms before full failure | Bicski & Pekar (intra-/inter-flow) |
| Time-series ML | Detect odd patterns across many metrics together | TranAD, Anomaly Transformer |
| Predict rare bad events | Forecast *extreme* outages/lag, not average wiggles | Outage-Watch, PreFix |
| Active probing | Lightweight always-on checks (downsized for home) | Pingmesh, NetBouncer |
| Root-cause reasoning | Rank likely failing layers with probabilities | Sherlock, NetMedic, 007 |
| Concept drift | Networks change (new AP, evening congestion)—models must adapt | LEAF, label-free drift work |

**Big takeaway:** almost every *piece* exists in research, but mostly for data centers, security, or enterprise ops—not a consumer laptop product that fuses Wi‑Fi + DNS + path + app probes, predicts lag, and explains it simply.

---

## 4. Novel Approach

**Name:** Hybrid Multi-Layer Predictive Health (**HMPH**)

**What’s new isn’t one magic algorithm.** It’s the *productized combination*:

1. **Cross-layer fusion** — one timeline of Wi‑Fi + OS + DNS + path + HTTPS probes (optional flows later)  
2. **Prediction, not only detection** — “risk of bad QoE in 5–15 min,” inspired by extreme-event forecasting  
3. **Privacy-local ML** — train/score on-device; don’t upload your browsing traffic  
4. **Drift-aware** — baselines update when home/SMB networks change  
5. **Non-expert RCA** — turn model scores into plain sentences + fixes  

Honest pitch: *we productize multi-layer predictive health on the end host*—not “we invented networking ML.”

---

## 5. Architecture

```
UI (health score, forecast, plain-language RCA)
        │
        ▼
Agent core (scheduler, permissions, probe limits)
        │
   ┌────┴────────────────────────────┐
   ▼         ▼          ▼            ▼
 Wi‑Fi/OS   DNS/Path   HTTPS probes  (optional flows)
   └────┬──────────┬────┘
        ▼
 Feature store (local SQLite / time-series DB)
        │
   L0–L1 rules/stats  →  L2 unsupervised ML  →  L3 predictor
        │
        ▼
   L4 explanation templates → notifications
```

**Model ladder (keep it light on a laptop):**

| Layer | Role |
| --- | --- |
| **L0–L1** | Fast rules & stats (thresholds, EWMA, seasonal baselines) |
| **L2** | Small online unsupervised model (“is this unusual?”) |
| **L3** | Lightweight predictor (“will it get bad soon?”) |
| **L4** | Attribution + plain-language templates (“why / what to try”) |

**Rules of the house:** software-only, collectors can be turned off per layer, probes are rate-limited, MVP works fully offline.

---

*For citations, datasets, and full paper notes, see [`Research.md`](./Research.md). For build plan and requirements, see [`PRD.md`](./PRD.md).*
