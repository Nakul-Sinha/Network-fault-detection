# ADR 0003: Deploy a demo of the agent, not the agent

**Status:** accepted
**Date:** 2026-09-22
**Relates to:** PRD 1.3, PRD N1, PRD 14.2

## Context

The project needs something a person can look at without installing
anything. A lab project nobody can see is a lab project nobody reads, and
PRD 14.2 puts a public page in the launch sequence.

The obvious move is to host the agent. It is the wrong move, and it is worth
writing down why, because the reasoning is not about effort.

**It would measure the wrong network.** The agent's entire job is to
characterise the network *from the machine it runs on*. Hosted, it would
report the round trip time to a datacenter gateway and the path out of a
cloud region. Every number would be real and every number would be about
somewhere the visitor has never been.

**The runtime does not fit.** Serverless functions are ephemeral and
stateless. The agent needs long-running collector threads, a scoring loop on
a fixed cadence, a SQLite file that survives between ticks, and the platform
`ping` and `traceroute` binaries. None of that exists there.

**It would contradict the product.** The reason this exists is that nothing
leaves the user's machine. An instance running on someone else's computer,
measuring someone else's network, is the thing the product is defined
against.

## Decision

Deploy a **replay** of the agent, as a static site.

`scripts/build_demo_data.py` runs `netpulse.ml.scorer.Scorer` over the
fault-injection corpus and records every frame: health, both risk horizons,
severity, per-layer attribution, the incident card, and the feature values
behind it. The site plays those recordings back.

Two rules keep it honest.

**It is generated, never hand-written.** Every number came from the same
object an installed agent runs. A hand-written demo would drift from the
product within a week and would quietly be making claims the code cannot
support. Regenerating is one command, and it belongs in the same change as
any model edit.

**It says what it is.** The page states plainly that it is a recording, that
the scenarios are synthetic, and that the real thing measures your own
network. The install section exists because the demo is an argument for
running it locally, not a substitute.

The site also leads with the thing that is hard to convey in prose: a
timeline marking when the agent warned and shading when things actually
broke, so the gap is something a reader sees rather than a figure they are
asked to trust.

## Consequences

Someone can evaluate the project in thirty seconds without installing
anything, and what they evaluate is the real decision-making rather than a
mock-up.

The demo data is checked in, about 3 MB across fourteen scenarios. That is
deliberate: the site has no build step and no server, so the recordings are
the artifact. It also means a reviewer can diff them when the model changes,
which is a useful property in itself.

The recordings go stale if someone changes the model ladder and does not
regenerate. `CONTRIBUTING.md` says to, and the data being generated from the
corpus makes a stale file obvious as soon as anyone looks.

`netpulse demo` covers the same ground locally and against a live agent, so
the two are not redundant: the site is for people deciding whether to try it,
and the command is for people who already have.
