# ADR 0001: Build the reference implementation in Python, not Rust and Tauri

**Status:** accepted
**Date:** 2026-09-21
**Supersedes:** the primary stack recommendation in PRD section 7.1

## Context

PRD 7.1 recommends Rust for the agent core, Tauri 2 with TypeScript for the
UI, and ONNX Runtime for inference, with a Python sidecar acceptable for an
alpha. The same section offers an explicit alternative: if the team is
Python-only, ship a PyInstaller agent for alpha and migrate the hot path to
Rust and ONNX before public v1.

The recommendation is sound for a funded team shipping signed desktop
installers. It is the wrong first move for getting this working end to end.

Three things drove the decision.

**The risk in this product is not the runtime.** Nothing here is
performance-bound. One scoring pass over the whole model ladder costs about
2 ms and runs once every 15 seconds, which is a rounding error against a
3 percent CPU budget. The hard parts are which features to collect, how to
tell a fault from an evening, and how to name the failing layer without
blaming the wrong one. None of those get easier in a different language, and
all of them need many iterations against an evaluation corpus.

**The ML work and the agent want to be the same code.** PRD 8.3 puts training
offline in Python and inference in the shipped agent. With two languages the
feature pipeline exists twice, and the two copies drift. Then the evaluation
harness measures the Python copy while users run the Rust one. Keeping the
scorer in one language means the replay harness drives literally the same
object the agent does, which is the difference between an evaluation and a
plausible-looking number.

**Tauri buys a window.** The MVP surface is a health score, a forecast, an
incident card, a timeline and a settings panel. PRD 11.3 says explicitly that
a functional tool UI is fine. F17 already asks for a localhost web UI for the
server SKU, so building that one well covers both personas with one
implementation. A native shell adds a signing and notarisation pipeline
before there is anything worth signing.

## Decision

The reference implementation is Python 3.11 or newer, one package.

- Agent core: threads, one per collector, plus a scoring loop.
- Models: numpy for the autoencoders and the logistic head. No PyTorch, no
  ONNX. The shipped model is a JSON file a reviewer can read.
- UI: the localhost web UI from F17, plain HTML, CSS and JavaScript, no build
  step. It serves both the desktop and the server persona.
- Notifications: native per platform, through small subprocess calls.
- Distribution: wheel, single-file binaries via PyInstaller, and a container.

Runtime dependencies are numpy, psutil, pydantic, fastapi and uvicorn. The
probe path is standard library only.

## Consequences

### Accepted costs

No tray icon or menu bar presence, which F1 asks for. The agent runs in the
background and the UI is a browser tab. A tray shell can be added later
against the same localhost API without touching the agent.

Idle RAM is roughly 80 to 120 MB against a target of under 250 MB.
Comfortably inside the budget, but a Rust agent would be a tenth of that.

Installers are not code-signed. PRD 14.3 lists signing and notarisation as a
shipping requirement; until that is done, releases publish SHA256 checksums
and the documentation says to verify them.

### What was gained

The whole system works end to end today: seven collectors on real hardware, a
model ladder that passes all five PRD 3.2 gates, a web UI, a CLI, exports, a
container, and CI that blocks a merge on a metric regression.

The evaluation harness drives the production scorer, so the numbers in
`docs/evaluation.md` describe the shipped code rather than a parallel
implementation of it.

## Revisiting this

Migrate the hot path when there is a reason, not a schedule. Concretely: if
dogfooding shows idle CPU above 2 percent or RAM above 200 MB on a real
laptop, or if the tray presence in F1 turns out to matter to users more than
the features it would delay.

The seam is already in the right place. `Scorer.observe` takes a sample and
returns a score, and everything above it talks through that one call. A Rust
agent could adopt the same feature schema, the same JSON model format and the
same localhost API, and the evaluation harness would keep working.
