# Architecture

How NetPulse Local actually works, and why it is shaped this way.

`Research.md` explains the thinking behind the approach and `PRD.md` sets the
requirements. This document is the built system.

---

## 1. The shape of it

```
 collectors (7 threads)            scoring tick (every 15s)
 ─────────────────────             ────────────────────────
 system   passive  15s  ┐
 wifi     passive  20s  │
 gateway  active   20s  │          ┌──────────────┐
 dns      active   45s  ├─ values ─▶│  assembler   │
 https    active   90s  │          └──────┬───────┘
 captive  active  300s  │                 │ Sample (only what is new)
 path     active  600s  ┘                 ▼
                                   ┌──────────────┐
                                   │   pipeline   │ carry-forward, windows,
                                   └──────┬───────┘ cross-layer ratios
                                          ▼ FeatureFrame
             ┌────────────────────────────────────────────────┐
             │  L0 rules   →  L1 stats  →  L2 online  →  L3    │
             │  hard facts    seasonal     unsupervised  head  │
             └───────────────────────┬────────────────────────┘
                                     ▼
                              L4 attribution
                                     │
                    ┌────────────────┼────────────────┐
                    ▼                ▼                ▼
                 SQLite         notifications      web UI / CLI
```

Collectors and scoring are decoupled through the assembler. A traceroute on a
ten minute cadence and a counter read on a fifteen second cadence land on the
same timeline, and a slow probe can never delay a health update.

The assembler emits only values measured since the last tick. Carrying a
value forward is the pipeline's job, and it applies a per-layer staleness
bound that duplicating in the assembler would defeat.

---

## 2. Collectors

Each collector owns one layer, declares whether it can run here, and returns
a sparse feature set plus the probe records describing any traffic it
generated.

| Collector | Layer | Active | Source |
| --- | --- | --- | --- |
| `system` | os, vpn | no | psutil counters; `GetTcpStatistics` on Windows, `/proc/net/snmp` on Linux, `netstat` on macOS |
| `wifi` | wifi | no | `netsh wlan`, `iw` with a `/proc/net/wireless` fallback, `airport` with a `system_profiler` fallback |
| `gateway` | gateway | yes | the platform `ping` binary |
| `dns` | dns | yes | `getaddrinfo` through the platform resolver |
| `https` | remote_https | yes | `socket` and `ssl` directly, timed per phase |
| `captive` | context | yes | a known-answer HTTP request |
| `path` | path | yes | `tracert` or `traceroute` |

Three decisions inside that table are load-bearing.

**Nothing needs privilege.** Gateway probing goes through the `ping` binary
rather than a raw socket, because raw ICMP needs administrator rights on
Windows and a capability on Linux. PRD 5.3 requires the agent to work as a
normal user, and it does.

**The HTTPS probe is hand-written against `socket` and `ssl`** rather than
using an HTTP client, because the phase split is the entire point. Separating
DNS from TCP from TLS from time-to-first-byte is what lets the explanation
say *which stage* got slow. A high-level client reports one number and hides
exactly the signal this product needs. Only the response head is read, and it
is discarded immediately.

**A collector that cannot run says so.** `netpulse doctor` prints the
capability report, and a layer that is unavailable is hidden in the UI with
its reason, rather than silently reported as healthy.

### Probe budgets

Every active collector asks a token bucket before it puts anything on the
wire. The budget is enforced in code, independent of the configured cadence,
so a bad config fails rather than turning the agent into a traffic generator.
Cadence is cross-checked against the budget at config load time, so an
aggressive interval is rejected before the agent starts.

`netpulse pause` empties every bucket, and the probe counter in the UI stops
moving, which is how PRD Appendix B asks for pause to be verified.

Traceroute and the captive check also run **on suspicion**: when severity
rises, they are pulled forward rather than waiting for their cadence. The
budget still applies, so this can only move an allowed probe earlier.

---

## 3. The feature pipeline

Collectors run on cadences that differ by two orders of magnitude, so a raw
sample is always partial. The pipeline makes samples comparable:

1. **Carry-forward with a staleness bound.** A ten minute traceroute does not
   leave a hole in every intermediate frame, but a two minute old RSSI is
   dropped rather than believed. A stale value is worse than an absent one.
2. **Rolling windows at 1, 5 and 15 minutes** with mean, spread,
   least-squares slope and a spread-floored z-score. Slope is a first-class
   feature: a rising slope under a still-normal mean is the early-degradation
   precursor from `Research.md` 5.3.
3. **Cross-layer ratios**, which are what make attribution possible at all.
4. **Coverage**, so scoring can say it is judging on half the picture.

All of that runs in about 0.8 ms per frame. It was 34 ms when the windows
were computed with numpy: a 60 element array spends all its time in per-call
overhead rather than arithmetic, so the single reverse pass with running sums
is plain Python on purpose.

### Cross-layer ratios

A rising time-to-first-byte means nothing on its own. These are the features
that turn it into a diagnosis:

| Feature | Says |
| --- | --- |
| `x_path_minus_gateway_ms` | latency past the router but not at it |
| `x_remote_minus_path_ms` | latency past the path but not on it |
| `x_dns_share` | how much of the wait is name resolution |
| `x_tls_share` | handshake versus connection setup |
| `x_wifi_pressure` | weak signal and retries together |
| `x_failure_breadth` | how many layers are failing at once |

---

## 4. The model ladder

### L0, hard rules

Deliberately boring. They handle the cases where a learned score would be
both slower and less trustworthy than a threshold: the gateway is
unreachable, name lookups all fail, the signal is at the floor.

L0 also owns **suppression**, which is the part that keeps the product
honest. A captive portal makes every layer look broken at once, so while one
is detected the rules forbid blaming the path or the remote service. Failing
HTTPS is not evidence against the far end while DNS is failing, because it is
a symptom. Retransmissions follow the route onto the VPN when a tunnel is
carrying the traffic.

### L1, seasonal statistics

Answers "is this unusual *for this network, at this time of day*". Home and
small-office networks have strong diurnal structure, so a single global mean
would flag every weeknight as an anomaly.

Per feature: a bounded-learning-rate EWMA, 24 hour-of-day buckets used once
warm, and a two-sided CUSUM for sustained small shifts.

Four details matter more than the algorithm:

- **A deadband at z = 1.5**, because real host metrics are noisy enough that
  an undeadbanded z-score flags a quiet network several times an hour.
- **Persistence smoothing** over roughly four samples: degradation that
  matters persists, sampling noise does not.
- **A spread floor at a quarter of the baseline mean.** Network latency
  genuinely moves by tens of percent across a day; without this the ordinary
  evening rise from 3.0 to 4.4 ms reads as a four sigma event. An hour the
  install has not seen yet gets a wider floor still.
- **Freeze on anomaly.** A settled baseline stops learning while the
  observation looks wrong. Without it a sustained fault teaches the baseline
  to accept itself: one large jump inflates the variance, which widens the
  spread, which shrinks the next z-score, and a real outage fades from view
  within two minutes while it is still happening.

### L2, online unsupervised

The Kitsune idea (R1) moved from intrusion detection to health: one tiny
autoencoder per layer bundle, trained continuously, with reconstruction error
as the signal. Nothing is labelled.

The bundles are the **layers**, not a learned clustering. A learned
clustering would score marginally better, but "the Wi-Fi bundle stopped
reconstructing" is a sentence the explanation stage can use and a cluster
index is not.

Error is normalised against each model's own settled error distribution, so
the score means "unusual compared with how well I usually reconstruct", which
is comparable across machines with very different absolute latencies. Error
statistics only start once the model has converged: gathering them during the
training ramp inflates the threshold so far that nothing can ever exceed it,
which is a bug that made the whole ensemble silent for a while.

### L3, the predictive head

The layer the product is sold on. A calibrated logistic regression with
separate models for the 5 and 15 minute horizons.

PRD 15 endorses a linear model over a transformer for the first release, and
the reasons hold up: it trains in a minute, runs in microseconds, serialises
to a JSON file a reviewer can read, and its coefficients double as
attribution evidence. A distilled TranAD-class model can replace it behind
the same interface once there is a labelled corpus from real installs.

Two training choices keep the numbers honest:

- **The corpus is split by seed, not by row.** Neighbouring frames of a time
  series share rolling windows, so a random split trains and tests on what is
  effectively the same moment.
- **Labels describe the approach, not the arrival.** A frame is positive when
  degradation *begins* within the horizon. Training only on frames inside the
  bad window teaches detection wearing a forecast's clothes.

Class weighting is the Outage-Watch (R7) emphasis on rare extreme events, and
it needs a **prior correction** to be usable: re-weighting trains on a
fictional base rate, so probabilities come out systematically high. The exact
fix for a re-weighted logistic regression is to subtract the log of the
weight ratio from the bias. Without it the trained head is a worse forecaster
than the heuristic it replaces, firing on quiet networks while reporting
confident probabilities.

With no model present, a documented heuristic takes over, so a fresh install
forecasts from its first warm minute.

### L4, attribution and explanation

Noticing that something is wrong is the easy half. Naming the layer is the
hard half, because latency propagates outward: a slow router makes the path
look slow, and a slow path makes every remote service look slow. Ranking raw
anomaly scores blames the far end for a Wi-Fi problem every time.

So attribution runs the layered dependency sketch from Sherlock (R16) and
NetMedic (R17) in two stages:

1. **Inheritance dampening** subtracts the portion of each layer's score that
   its upstream layers already explain, along the chain
   wifi → os → gateway → path → remote_https. What survives is the excess
   that appeared at this layer and not before it.
2. **Cross-layer evidence** votes directly, using the ratios above.

L0 suppression applies last and is absolute: a forbidden layer cannot be
named however it scored.

Then 21 templates across seven layers turn the result into a sentence. Every
template is offline, because the MVP must work with no network and no model
download. The voice follows PRD 11.2: jargon expanded in the same sentence,
hedged language, at most three imperative remediations, and no claim about
the ISP that path evidence does not carry. Remediations describe what the
user should do and never change system state.

### Drift

Home networks genuinely change: a new router, a new provider, a move. A model
that treats every such change as a fault produces a week of false alarms and
then an uninstall.

Population Stability Index over quantile bins, checked periodically, with no
labels. The adaptation policy is **asymmetric** and that asymmetry is the
point: L1 baselines and L2 autoencoders are relearned for the drifted
feature, because they describe what is normal for this network. The L3 head
is never touched, because it encodes what degradation looks like in general.

---

## 5. Storage

One SQLite file holds everything: samples, scores, incidents, labels, the
probe ledger and drift events. SQLite rather than an embedded time-series
database because it is auditable with any `sqlite3` binary, exports
trivially, and a user who wants to check the privacy claim can read the whole
file themselves.

The privacy guarantee is **structural**. Every column in `samples` is REAL.
There is no column a hostname, a URL or a payload byte could be written into,
so the promise does not depend on anyone remembering it during review. The
test suite asserts this against the live schema.

Retention: raw samples for 48 hours, then five-minute roll-ups kept for 90
days, with `raw_days` as a hard ceiling. `netpulse wipe` removes everything.

---

## 6. Interfaces

**Localhost API and web UI.** Loopback-only bind validated in config, a token
from an owner-only file required on every API route, a Host header check
against loopback on every request (a loopback service that trusts `Host` is
open to DNS rebinding), and a content security policy confining the page to
its own origin. The UI receives its token by injection into the page the
server itself serves, so it never appears in a URL, a bookmark, a referrer or
browser history.

**CLI.** Local commands work standalone; store commands prefer a running
agent and fall back to the file, saying which; control commands go through
the API and record intent in the store when no agent is running, which is why
`pause` survives a reboot.

**Notifications.** Quiet hours, a severity floor and a per-layer cooldown,
with two carve-outs: escalation beats the cooldown, and critical beats quiet
hours.

---

## 7. What is deliberately not here

Per PRD 1.3 and 10.3: no packet capture, no deep inspection, no intrusion
detection, no cloud account, no auto-remediation that changes system DNS or
VPN settings, no mobile app, no BGP. The flow-metadata collector is declared
in config and defaults off; it is a v1 item and is not implemented.

See `docs/adr/` for the decisions that departed from the PRD, with reasons.
