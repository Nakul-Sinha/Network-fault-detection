"""NetPulse Local: a privacy-first predictive network health agent.

The package implements the Hybrid Multi-Layer Predictive Health (HMPH)
architecture described in ``Research.md`` section 6 and specified in
``PRD.md``: cross-layer collectors feed a local feature store, a ladder of
models (L0 rules, L1 statistics, L2 online unsupervised, L3 predictive head)
scores current and near-future health, and an L4 attribution stage turns the
scores into plain-language incident cards.

Everything stays on the machine by default. No packet payloads are captured
and no network features leave the host unless the operator exports a bundle
themselves.
"""

__version__ = "0.1.0"
__all__ = ["__version__"]
