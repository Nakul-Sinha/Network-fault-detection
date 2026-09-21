# NetPulse Local

Predicts network trouble minutes before you feel it, tells you which layer is
failing in plain language, and never sends anything off your machine.

This started as my Computer Networks lab project. The problem it goes after
is the one everyone recognises: a call freezes, a game lags, a page stalls,
and nothing tells you whether it was the Wi-Fi, DNS, the router or the
internet provider. Most tools tell you after it broke, and many of them ship
your traffic to somebody's cloud to do it.

```bash
pip install netpulse-local
netpulse doctor     # what can this machine measure?
netpulse run        # agent plus a web UI at http://127.0.0.1:8787/
```

---

## What it does

It watches seven layers at once, on your own machine:

| Layer | Measured by |
| --- | --- |
| Wi-Fi | signal, noise, retries, link rate, roaming |
| This device | interface counters, TCP retransmissions, CPU, memory |
| VPN | tunnel presence, MTU, retransmissions inside the tunnel |
| Your router | round trip time, jitter, packet loss |
| DNS | resolution time and failure rate |
| The path out | traceroute shape, latency past the first hop |
| Remote services | HTTPS timing split into DNS, TCP, TLS and first byte |

From those it produces a health score, a probability that things get bad in
the next 5 and 15 minutes, and a card that says what is wrong in a sentence:

> **Congestion past your router**
> Your own network looks fine: your router answers in 3 ms. Past it, the path
> out is adding about 240 ms. That points to your internet connection or your
> provider rather than anything in your home.
>
> What to try: wait a few minutes, this often clears on its own · export a
> diagnostic bundle for your provider · use a cable for anything important

Against the fault-injection corpus it currently detects every injected fault
with a **median 6.2 minutes of warning**, names the right layer **100%** of
the time, and raises **zero false alarms** across eight hours of healthy
traffic. Full numbers and the honest caveats are in
[docs/evaluation.md](docs/evaluation.md).

---

## Privacy, concretely

Not a policy, a property of the schema. Every column in the sample table is a
number:

```
$ sqlite3 ~/.local/share/netpulse/netpulse.sqlite ".schema samples"
CREATE TABLE samples (ts REAL PRIMARY KEY, "os_iface_type" REAL, ... );
```

There is nowhere to put a hostname, a URL or a packet byte, so the agent
could not store your browsing history if it tried. The test suite asserts
this against the live schema on every run.

Network names are hashed with a salt generated on your machine. Every probe
the agent sends is written to a ledger you can read. `netpulse wipe` deletes
the lot. Details, and how to verify all of it yourself, in
[docs/privacy.md](docs/privacy.md).

---

## How it works

Five stages, deliberately boring at the bottom and only as clever as it needs
to be at the top:

- **L0, rules.** Gateway unreachable, DNS blackholed, captive portal. Also
  suppression: while a sign-in page is intercepting the network, no other
  layer may be blamed.
- **L1, seasonal statistics.** Is this unusual *for this network, at this
  time of day*. Home networks have strong daily rhythms, so a single average
  would flag every weeknight.
- **L2, online autoencoders.** One small model per layer, trained
  continuously, no labels. Catches unusual combinations the per-feature
  statistics miss.
- **L3, the forecast.** A calibrated logistic regression per horizon, trained
  on slopes and spreads rather than levels, because a latency that is high
  and steady is one you have already adapted to and a latency that is
  climbing is the one that breaks the call.
- **L4, attribution.** Latency propagates outward, so ranking raw anomaly
  scores blames the far end for a Wi-Fi problem every time. Each layer's
  score is reduced by what its upstream layers already explain, then the
  cross-layer ratios vote.

[docs/architecture.md](docs/architecture.md) has the full picture, including
the parts that took several attempts to get right.

---

## Documentation

| | |
| --- | --- |
| [Install](docs/install.md) | every platform, plus running it all the time |
| [Architecture](docs/architecture.md) | how it works and why it is shaped this way |
| [Privacy](docs/privacy.md) | what is stored, and how to check |
| [Operations](docs/operations.md) | tuning, troubleshooting, what to do when it is wrong |
| [Evaluation](docs/evaluation.md) | how it is measured, current scores, model card |
| [Decisions](docs/adr/) | where the build departed from the plan, and why |
| [Open questions](docs/open-questions.md) | answers to the questions the PRD left open |

The original design documents are still here:
[Research.md](Research.md) for the literature this is built on,
[PRD.md](PRD.md) for the requirements, and
[explanation.md](explanation.md) for the plain-language version.

---

## Development

```bash
git clone https://github.com/Nakul-Sinha/Network-fault-detection
cd Network-fault-detection
pip install -e ".[dev]"

make test     # unit, integration and the PRD acceptance checklist
make lint
make eval     # replay the fault-injection corpus
make gate     # same, but fail if a PRD target regressed
```

`make gate` is what CI blocks merges on. A change that quietly trades lead
time for precision fails there rather than in someone's evening call.

The fault-injection corpus in `netpulse/eval/scenarios.py` is the portable
half of the lab from PRD 13.1. Every fault has a precursor phase before its
impact phase, because a corpus where degradation appears instantly cannot
measure lead time at all, and would reward a detector that is merely fast
instead of early.

---

## What this is not

Not an enterprise monitor, not an intrusion detector, not a packet capture
tool. It has one vantage point, your machine, so it can say trouble appeared
past your router but it cannot prove whose fault that is. The wording in
every incident card reflects that, and it always will.

---

Apache-2.0. Built on the research collected in [Research.md](Research.md):
Kitsune for online unsupervised detection, Donut for seasonal baselines,
Outage-Watch and PreFix for predicting rare events early, Sherlock and
NetMedic for layered diagnosis, LEAF for drift. The contribution here is not
a new algorithm, it is the composition, made to run on one laptop and explain
itself to whoever is using it.
