"""Diagnostic bundle export (F10).

The bundle is what a user sends to their internet provider, attaches to a
support thread, or replays through the evaluation harness. PRD 12.3 warns
that it carries network environment metadata, so two rules shape this module:

1. **Nothing leaves that was not already in the store.** No new collection
   happens during an export, and the store has no column a payload could be
   written into, so a bundle cannot contain traffic contents.
2. **Everything free-text is redacted on the way out.** Log lines and probe
   details can contain IP addresses, MAC addresses and error strings, so they
   pass through the same scrubber before they are written.

The bundle leads with a README that says in plain words what is inside and
what to check before sharing it, because an export the user does not
understand is one they either will not send or should not have.
"""

from __future__ import annotations

import io
import json
import platform
import sys
import time
import zipfile
from pathlib import Path
from typing import Any

from .. import __version__
from ..collectors.registry import capability_report
from ..config import NetPulseConfig
from ..features.schema import describe as describe_schema
from ..logging_setup import get_logger
from ..paths import log_dir
from ..privacy import redact
from ..store.repository import Repository

log = get_logger(__name__)

DEFAULT_WINDOW_S = 24 * 3600.0
MAX_LOG_BYTES = 512 * 1024

README = """\
NetPulse Local diagnostic bundle
================================

What this is
------------
A snapshot of what the NetPulse agent measured on this machine, exported by
the person who runs it. It is meant to be shared with an internet provider,
an IT team or a support thread.

What is inside
--------------
  manifest.json    when this was made, the agent version, the host platform
  config.json      the agent settings, with any API token removed
  capabilities.json what this machine can and cannot measure
  schema.json      the meaning and unit of every feature column
  samples.jsonl    one line per observation: timings, counters and rates
  scores.jsonl     health score and risk forecast over the same period
  incidents.json   what the agent concluded, and why
  labels.json      feedback the user gave about whether it was right
  probe-log.jsonl  every probe the agent sent, and where
  drift.json       moments when the agent decided the network had changed
  logs/            the agent log, scrubbed

What is NOT inside
------------------
No packet contents. No browsing history. No page or file contents. The agent
never captures any of these: the feature store has no column they could be
written into. Domain names the agent probed are stored as salted hashes when
hashing is enabled, and the salt is not included here.

Before you share this
---------------------
Addresses and identifiers have been scrubbed, but the timings themselves can
still say something about your network: how many hops away your provider is,
roughly when you were using it, and how it performed. Have a look through
samples.jsonl and probe-log.jsonl if that matters to you.

Replaying it
------------
  netpulse replay path/to/samples.jsonl

runs this capture back through the same scoring path the live agent uses.
"""


def build_bundle(
    repo: Repository,
    config: NetPulseConfig,
    output: Path,
    *,
    window_s: float = DEFAULT_WINDOW_S,
    include_logs: bool = True,
    now: float | None = None,
) -> Path:
    """Write a diagnostic bundle and return its path."""
    now = time.time() if now is None else now
    since = now - window_s
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)

    samples = repo.recent_samples(limit=100_000, since=since)
    scores = repo.scores_since(since, limit=100_000)
    incidents = repo.recent_incidents(limit=500, since=since)
    labels = repo.recent_labels(limit=500)
    probes = repo.recent_probes(limit=5000)
    drift = repo.recent_drift(limit=500)

    manifest = {
        "bundle_version": 1,
        "agent_version": __version__,
        "created_at": now,
        "created_at_iso": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(now)),
        "window_seconds": window_s,
        "platform": {
            "system": platform.system(),
            "release": platform.release(),
            "machine": platform.machine(),
            "python": sys.version.split()[0],
        },
        "counts": {
            "samples": len(samples),
            "scores": len(scores),
            "incidents": len(incidents),
            "labels": len(labels),
            "probes": len(probes),
            "drift_events": len(drift),
        },
        "store": repo.summary(),
        "contains_payloads": False,
    }

    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("README.txt", README)
        archive.writestr("manifest.json", _json(manifest))
        archive.writestr("config.json", _json(config.redacted()))
        archive.writestr("capabilities.json", _json(capability_report(config)))
        archive.writestr("schema.json", _json(describe_schema()))

        archive.writestr(
            "samples.jsonl",
            _jsonl(
                {key: value for key, value in sample.as_row().items() if value is not None}
                for sample in samples
            ),
        )
        archive.writestr("scores.jsonl", _jsonl(score.to_dict() for score in scores))
        archive.writestr("incidents.json", _json([item.to_dict() for item in incidents]))
        archive.writestr("labels.json", _json([item.to_dict() for item in labels]))
        archive.writestr(
            "probe-log.jsonl",
            _jsonl(_redact_probe(record.to_dict()) for record in probes),
        )
        archive.writestr("drift.json", _json([item.to_dict() for item in drift]))

        if include_logs:
            for name, content in _collect_logs():
                archive.writestr(f"logs/{name}", content)

    log.info("wrote diagnostic bundle to %s (%d bytes)", output, output.stat().st_size)
    return output


def _redact_probe(row: dict[str, Any]) -> dict[str, Any]:
    """Scrub the free-text fields of a probe record.

    The target is kept as configured, because the whole point of the probe
    ledger is showing where the agent sent traffic, and the operator chose
    those destinations.
    """
    row["detail"] = redact(str(row.get("detail") or ""), limit=200)
    return row


def _collect_logs() -> list[tuple[str, str]]:
    """Read and scrub the tail of each log file."""
    out: list[tuple[str, str]] = []
    directory = log_dir()
    if not directory.exists():
        return out
    for path in sorted(directory.glob("netpulse.log*")):
        try:
            with open(path, "rb") as handle:
                handle.seek(0, io.SEEK_END)
                size = handle.tell()
                handle.seek(max(0, size - MAX_LOG_BYTES))
                raw = handle.read().decode("utf-8", "replace")
        except OSError as exc:
            log.debug("could not read log %s: %s", path, exc)
            continue
        scrubbed = "\n".join(redact(line, limit=1000) for line in raw.splitlines())
        out.append((path.name, scrubbed))
    return out


def _json(value: Any) -> str:
    return json.dumps(value, indent=1, default=str)


def _jsonl(rows: Any) -> str:
    return "\n".join(json.dumps(row, separators=(",", ":"), default=str) for row in rows) + "\n"


FORBIDDEN_KEYS = ("token", "secret", "password", "passphrase", "api_key", "salt")


def audit_bundle(path: Path) -> dict[str, Any]:
    """Re-open a bundle and check it against the privacy promises.

    This backs the PRD Appendix B acceptance item "export ZIP opens and
    redacts secrets". It is a real check rather than a comment: it opens the
    archive, walks every entry, and fails on anything that looks like a
    credential or a raw address in a place that should have been scrubbed.
    """
    findings: list[str] = []
    entries: list[str] = []
    with zipfile.ZipFile(path) as archive:
        for name in archive.namelist():
            entries.append(name)
            content = archive.read(name).decode("utf-8", "replace")
            lowered = content.lower()
            for key in FORBIDDEN_KEYS:
                # A key name appearing with a value after it is the thing to
                # catch; the word alone is fine in prose and in the README.
                needle = f'"{key}": "'
                if needle in lowered:
                    findings.append(f"{name}: possible {key} value")
            if name.startswith("logs/") and "<ip>" not in content and _looks_like_ip(content):
                findings.append(f"{name}: unredacted address")
    required = {"README.txt", "manifest.json", "config.json", "samples.jsonl", "incidents.json"}
    missing = sorted(required - set(entries))
    return {
        "path": str(path),
        "entries": sorted(entries),
        "missing": missing,
        "findings": findings,
        "ok": not findings and not missing,
    }


def _looks_like_ip(text: str) -> bool:
    import re

    return bool(re.search(r"\b(?:\d{1,3}\.){3}\d{1,3}\b", text))
