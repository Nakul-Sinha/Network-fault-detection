# Answers to the PRD open questions

PRD section 16 lists eight open questions. Building the thing settled most of
them. Where the build settled one, the answer and its reason are below; where
it did not, that is said plainly rather than guessed at.

---

### 1. Final product name and trademark

**Open.** The working title NetPulse Local is used throughout the code, the
config directory names and the container image. Nothing has been searched for
trademark conflicts, and nothing should be published under the name until it
has been. The name appears in enough places that a change is a rename commit
rather than a rewrite: the package is `netpulse`, the paths come from
`netpulse/paths.py`, and the UI title is one string.

---

### 2. Apache-2.0 or GPL for the open-source core

**Answered: Apache-2.0.**

PRD 14.1 allows either Apache-2.0 or MIT for the core, and the question was
really about whether copyleft would frighten a future Pro dual-licence.
Apache-2.0 over MIT for the explicit patent grant, which matters for anything
touching network measurement. Copyleft was not chosen because PRD 14.1 wants
a freemium binary on top of an open core, and that is a harder conversation
under GPL than it needs to be for a project at this stage.

---

### 3. Is a Python sidecar acceptable for public beta, or ONNX-only from day one

**Answered, and the question turned out to be the wrong shape.**

There is no sidecar, because there is no second runtime. The whole agent is
Python and the models are numpy: an autoencoder ensemble of a few hundred
floats, and two logistic regressions in a readable JSON file. Nothing needs
ONNX to run in microseconds.

The reasoning is in [ADR 0001](adr/0001-python-reference-implementation.md).
The short version: the risk in this product is not the runtime, and keeping
the scorer in one language is what lets the evaluation harness drive the
exact object the agent runs.

---

### 4. Default HTTPS allowlist endpoints, and legal review

**Answered in part.**

The defaults are `www.cloudflare.com/cdn-cgi/trace` and
`www.google.com/generate_204`. Both are endpoints designed to be fetched by
software: a trace endpoint intended for diagnostics, and a connectivity check
endpoint that every Android device on the planet requests. One request every
90 seconds each is far below anything either operator would notice.

What makes this safe is not the specific choice, though. It is that the
targets are configurable, the whole collector has an off switch, the rate is
capped in code rather than implied by the cadence, and every probe is written
to a ledger the user can read. `docs/privacy.md` lists all of it.

**Still open:** no lawyer has looked at the terms of service of either
endpoint. Before a public launch, either get that review or move the default
to endpoints the project runs itself, which is the cleaner answer anyway.

---

### 5. Store the SSID in plain text, or HMAC with a per-install salt

**Answered: HMAC with a per-install salt.**

A 32 byte salt is generated once per install and kept in the owner-only state
directory. SSIDs and BSSIDs are hashed with it before they go anywhere near
the database, and the salt is never included in an export.

This keeps what the agent needs, which is only the ability to notice that the
network changed or the device roamed, and discards what it does not, which is
the name itself. The stored value is useful on that machine and useless off
it.

The database has no text column at all, which is what makes this structural
rather than a matter of discipline. Identifiers are stored as numbers derived
from the salted hash.

---

### 6. Server SKU: a separate binary, or the same one with a headless flag

**Answered: the same binary.**

`netpulse run --headless`, or `NETPULSE_HEADLESS=1`, which is what the
container image sets. There is no separate build, no separate package and no
divergent code path.

The reason is that the difference between the two is small and shrinking: the
server SKU has no tray, and F17 already asks for a localhost web UI, which
the desktop build wants anyway. Two binaries would mean two things to test,
two things to release and one of them getting less attention.

---

### 7. Pricing for Pro

**Open, and deliberately untouched.**

Nothing in the code refers to tiers, licences or entitlements. There is no
licence key check, no feature flag for a paid capability, and no telemetry
that could support a funnel. PRD 14.1 sketches a split between Community and
Pro, and every line of that split is a product decision that should be made
with dogfood data rather than in advance.

What the build does preserve is the ability to make that decision later
without rework: retention is a config value, the export path is already
structured, and multi-profile support is a scheduler change.

---

### 8. Accepting community model weight contributions

**Answered: not yet, and the mechanism should come before the policy.**

Model files carry a feature layout and are rejected on load if it does not
match, so a mismatched file cannot be applied silently. But a model is still
executable policy: it decides when to interrupt someone. Accepting one from a
stranger means accepting their judgement about that.

The prerequisite is not a review pipeline, it is a shared benchmark. The
evaluation harness and the fault-injection corpus are that benchmark, and
`netpulse eval --gate` already runs it, so a contributed model can be held to
the same five PRD 3.2 targets as the shipped one. Until there is a corpus
from real installs rather than synthetic faults, passing those gates says
less than it should.

Until then: contributions to the corpus and to the scenario set are welcome
and are the more valuable thing. Weights stay built from the shipped corpus
by `netpulse train`.

---

## Things the build settled that the PRD did not ask about

**Where to draw the line on probe safety.** Probe URLs are validated against
a scheme, port and address allowlist at config load, so loopback, link-local
and metadata endpoints are rejected. That was not in the PRD, and without it
the config file is a scanning primitive.

**What an alert is, for measurement purposes.** PRD 3.2 caps noisy alerts at
two a day without defining one. The harness counts incidents the agent
actually opened, with the notification cooldown applied, because that is what
a user would be shown. Counting threshold crossings would have made the
number meaningless.

**How long a corpus has to be to measure a daily rate.** Long enough that one
spurious alert does not move the number by an order of magnitude. A
seventy-five minute scenario cannot do it, so the false-alarm gate runs
against an eight hour soak across the evening peak.
