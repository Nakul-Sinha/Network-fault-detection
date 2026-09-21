# Evaluation and model card

How this thing is measured, what it currently scores, and what those numbers
do and do not mean.

Run it yourself:

```bash
netpulse eval          # replay the corpus and print the report
netpulse eval --gate   # exit non-zero if a PRD target regressed
```

---

## What is being measured

The headline metric is **lead time**, not accuracy. A detector that fires the
instant a call breaks is worth very little; the promise is a warning minutes
early. So lead time comes first, as a distribution rather than an average.

The second metric that carries real weight is the **false-alarm rate**. A
model can hit every precision target and still be unusable if it fires
hourly. PRD 15 lists "models cry wolf" as the top product risk, and it is the
one that gets software uninstalled.

The five gates are the PRD 3.2 MVP column, expressed so CI can check them:

| Gate | Target | What it protects |
| --- | --- | --- |
| Median lead time | at least 300 s | the product promise |
| Detection rate | at least 0.9 | not missing real faults |
| Layer attribution | at least 0.6 | naming the right thing |
| Alert precision | at least 0.5 | alerts being worth reading |
| Benign alerts per day | at most 2.0 | not crying wolf |

---

## Current results

Replaying the full corpus through the shipped model:

```
Summary
  detection rate        100%
  median lead time      6.2 min
  layer accuracy        100%
  alert precision       100%
  benign alerts/day     0.00
  brier score           0.0744
  calibration error     0.0258

Gates (PRD 3.2, MVP column)
  [PASS] median_lead_time_s            375s  (at least 300s)
  [PASS] detection_rate                  1  (at least 0.9)
  [PASS] layer_accuracy                  1  (at least 0.6)
  [PASS] precision                       1  (at least 0.5)
  [PASS] benign_alerts_per_day           0/day  (at most 2/day)
```

Per scenario, lead time is the gap between the first sustained alert and the
moment degradation actually begins:

| Scenario | Lead | Layer named | Card |
| --- | --- | --- | --- |
| wifi_fade | 6.2 min | wifi | Wi-Fi quality is dropping |
| wifi_interference | 6.2 min | wifi | Wi-Fi is busy or noisy |
| dns_slow | 7.0 min | dns | Name lookups are slow |
| dns_blackhole | 7.8 min | dns | Name lookups are failing |
| gateway_congestion | 8.0 min | gateway | Your router is slow to respond |
| gateway_down | 8.0 min | gateway | Your router is not responding |
| isp_path_congestion | 5.0 min | path | Congestion past your router |
| remote_service_slow | 5.5 min | remote_https | The far end is slow to answer |
| tls_handshake_slow | 6.0 min | remote_https | Secure connections are slow to set up |
| vpn_overload | 8.2 min | vpn | Your VPN looks like the bottleneck |
| device_pressure | 5.8 min | os | This device looks like the problem |
| captive_portal | 0.0 min | dns | A sign-in page is intercepting |
| healthy | no alert | | |
| evening_congestion | no alert | | |
| soak, 8 hours | no alert | | |

Captive portal shows zero lead time on purpose. A sign-in page appears
without warning; there is nothing to forecast, only something to recognise.

---

## How the corpus works

Real netem and DNS blackhole containers are the right tool for release
validation on a Linux host. They cannot run inside a unit test on a laptop or
on a Windows CI runner, so the portable half of the fault-injection lab
generates the sample streams those faults would produce.

Three properties make it an evaluation target rather than a demo.

**Every fault has a precursor phase before its impact phase.** A corpus where
degradation appears instantly cannot measure lead time at all, and would
reward a detector that is merely fast instead of early.

**The healthy baseline has diurnal structure**, and two scenarios are benign
by construction: a quiet network and an evening congestion drift where
everything slows by up to 45 percent and nothing breaks. A model that treats
peak hour as a fault fails here rather than in production.

**Features are emitted on the real probe cadences**, so the corpus exercises
the same carry-forward and staleness paths a live agent uses.

An eight hour soak of healthy traffic across the evening peak is what the
false-alarm gate is measured against. Two alerts a day cannot be estimated
honestly from a 75 minute scenario, where one spurious alert extrapolates to
19 a day and one fewer extrapolates to zero.

---

## Model card, L3 predictive head

**What it is.** Two calibrated logistic regressions, one per horizon (5 and
15 minutes), over 76 features: the L1 and L2 anomaly scores, per-layer
scores, z-scores and normalised slopes for 12 trend features, the ten
cross-layer ratios, and four context flags.

**What it predicts.** The probability that user-visible degradation begins
within the horizon, or is already under way.

**Training data.** The synthetic fault-injection corpus: 14 scenarios across
several seeds, plus six hour healthy soaks at five different starting hours.
Roughly 16,000 frames.

**Held-out results**, on whole runs from unseen seeds:

| Horizon | Brier | Calibration error | Precision at 0.5 | Recall at 0.5 |
| --- | --- | --- | --- | --- |
| 5 min | 0.024 | 0.012 | 0.93 | 0.95 |
| 15 min | 0.090 | 0.031 | 0.98 | 0.71 |

**Three choices that keep those numbers honest.**

The corpus is split by **seed, not by row**. Neighbouring frames of a time
series share rolling windows, so a random split trains and tests on what is
effectively the same moment and reports an accuracy the model does not have.

Labels describe the **approach, not the arrival**. A frame is positive when
degradation begins within the horizon. Training only on frames inside the bad
window teaches detection wearing a forecast's clothes.

Healthy soak runs are **part of the training corpus**. The scenario list is
twelve faults to two benign runs, a prior nothing like a real install, and a
head trained on that learns that trouble is the normal state of a network.

**Class weighting and its correction.** The positive class is up-weighted,
which is the Outage-Watch emphasis on rare extreme events, and that needs a
prior correction to be usable. Re-weighting trains the model on a fictional
base rate, so probabilities come out systematically high. Subtracting the log
of the weight ratio from the bias is the exact fix. Without it the trained
head is a *worse* forecaster than the heuristic it replaces: it fires on
quiet networks while reporting confident probabilities.

**When there is no model**, a documented heuristic takes over, so a fresh
install forecasts from its first warm minute rather than waiting.

---

## Limits you should know about

**The corpus is synthetic.** Fault shapes are hand-written from the
literature and from what these failures look like in practice, not sampled
from real incidents. The numbers above say the system behaves correctly on
faults of the kind it was designed for. They are not a claim about how it
performs on your network. PRD 8.1 makes the same point: product metrics need
a self-collected labelled corpus, and that comes from dogfooding.

**One vantage point.** The agent sees the network from one machine. It can
say latency appeared past your router; it cannot prove whose fault it is.
This is an inherent limit, not an implementation gap, and both the incident
copy and PRD 12.3 say so.

**The first evening is the weakest.** Hour-of-day baselines need to see each
hour before they are useful. Until then the agent widens its uncertainty for
an unseen hour rather than guessing, which is why the false-alarm rate is
good on the soak, but forecasts genuinely sharpen over the first few days.

**Layer accuracy of 100 percent is a corpus result, not a guarantee.** Each
scenario injects one fault with a clear signature. Real degradation is often
two things at once, where the honest answer is a primary and a secondary
layer, which the attribution reports but this metric does not capture.

---

## Reproducing and extending

```bash
netpulse eval --json > report.json      # every scenario, plus the lead-time CDF
netpulse eval --seeds 11 23 37          # more seeds, tighter estimates
netpulse eval --no-soak                 # skip the long quiet run
netpulse train                          # retrain and rewrite the shipped model
netpulse replay samples.jsonl           # a real capture through the same path
```

That last one closes the loop. The `samples.jsonl` inside an exported bundle
feeds straight back through the scoring path the live agent runs, so "it
would have caught this" is something that can be checked rather than claimed.

New scenarios go in `netpulse/eval/scenarios.py` as a mutation function and
a `ScenarioSpec`. Give the fault a precursor phase, or it will not measure
what this harness is for.
