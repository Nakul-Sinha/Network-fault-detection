# Contributing

```bash
git clone https://github.com/Nakul-Sinha/Network-fault-detection
cd Network-fault-detection
pip install -e ".[dev]"

make test     # unit, integration, and the PRD acceptance checklist
make lint     # ruff, plus a parse check on the workflow files
make eval     # replay the fault-injection corpus
make gate     # same, but fails if a PRD target regressed
```

`make gate` is what CI blocks merges on. Run it before opening a pull
request if you touched anything in `netpulse/ml/` or `netpulse/features/`.

---

## The one rule worth stating

**A change that moves a number has to show the number.**

This project is a set of claims: minutes of warning, the right layer named,
alerts worth reading. Those claims are measured by `netpulse eval`, and a
change that affects them should say what happened to them in the pull
request. Not because the gates would catch it, though they will, but because
trading lead time for precision is a decision someone should make on purpose.

---

## Where things live

| | |
| --- | --- |
| `netpulse/collectors/` | one module per layer, plus the platform parsers |
| `netpulse/features/` | the canonical feature schema and the rolling-window pipeline |
| `netpulse/ml/` | L0 rules, L1 statistics, L2 autoencoders, L3 forecast, L4 attribution |
| `netpulse/rca/` | plain-language templates and the remediation catalogue |
| `netpulse/eval/` | fault-injection corpus, replay harness, metrics, training |
| `netpulse/api/` | loopback API and the web UI |
| `docs/` | architecture, privacy, operations, evaluation, decision records |

[`docs/architecture.md`](docs/architecture.md) explains how it fits together,
including the parts that took several attempts.

---

## Adding a feature to the schema

`netpulse/features/schema.py` is the single source of truth. The sample table
DDL, the model input vector and the export headers are all generated from it,
so adding an entry there propagates everywhere.

Two constraints are not negotiable:

- **Every feature is a number.** A timing, a counter, a rate or a coarse
  categorical. This is what makes the privacy guarantee structural rather
  than a matter of discipline, and there is a test asserting it against the
  live schema.
- **Set `higher_is_worse` correctly.** It drives the sign convention in
  scoring and attribution. Getting it wrong means a rising RSSI is reported
  as a problem.

## Adding a collector

Subclass `Collector`, implement `interval_s`, `enabled` and `_collect`, and
override `detect_capability` if it can be unavailable. Register it in
`netpulse/collectors/registry.py`.

Anything that puts traffic on the wire must ask the probe budget first and
record what it sent in the probe ledger. That ledger is a promise to the
user, and a collector that skips it breaks it.

Platform output goes in tests as a **pinned fixture**, not read from the
machine running the tests. That is the only way the Windows parser gets
exercised on Linux, and it caught a real bug in the macOS parser the first
time it was done.

## Adding an RCA template

`netpulse/rca/templates.py`. The voice is set by PRD 11.2: expand jargon in
the same sentence, hedge rather than assert, at most three imperative
remediations, and never claim the ISP is at fault beyond what the path
evidence supports.

Templates are selected by layer then by specificity, so a specific condition
beats the generic card. Every remediation id must exist in the catalogue, and
a test checks that.

## Adding an evaluation scenario

`netpulse/eval/scenarios.py`. Give the fault a **precursor phase** before its
impact phase, or it measures nothing useful: a corpus where degradation
appears instantly cannot measure lead time at all, and rewards a detector
that is merely fast rather than early.

Contributions to the corpus are the most valuable thing here. The models are
only as good as what they are measured against, and the current corpus is
synthetic by necessity.

---

## Model weights

Not accepted from contributors yet, and the reasoning is in
[`docs/open-questions.md`](docs/open-questions.md). A model decides when to
interrupt someone, and until the corpus comes from real installs rather than
synthetic faults, passing the gates says less than it should.

Retrain locally with `netpulse train`.

---

## Style

Ruff handles formatting and linting; `make lint` is the whole standard.

Beyond that, one preference: comments should explain **why**, especially
where the obvious implementation is wrong. Several of the trickier parts of
this codebase look arbitrary without their reason, and the reason is usually
a bug that was found the hard way. If you fix something subtle, leave the
reason behind for whoever reads it next.

## Commits

Conventional-ish prefixes (`feat:`, `fix:`, `docs:`, `test:`, `ci:`) and a
body that says why rather than what. The diff already says what.
