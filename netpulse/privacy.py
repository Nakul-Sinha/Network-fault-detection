"""Identifier hashing and redaction helpers.

PRD open question 5 asked whether to store the SSID in plain text or as an
HMAC with a per-install salt. This module implements the second answer: a
random 32 byte salt is generated once per install, kept in the owner-only
state directory, and never leaves the machine. Two samples from the same
network hash the same way on this host and differently on any other, which is
enough for the agent to notice "the network changed" without the stored data
naming the network.
"""

from __future__ import annotations

import hmac
import os
import re
from hashlib import blake2b, sha256
from pathlib import Path

from . import paths

SALT_BYTES = 32
_salt_cache: bytes | None = None

#: Patterns scrubbed from free text (command output, error strings) before it
#: is written to the probe ledger or an exported bundle.
_REDACTIONS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\b(?:[0-9a-fA-F]{2}[:-]){5}[0-9a-fA-F]{2}\b"), "<mac>"),
    (re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b"), "<ip>"),
    (
        re.compile(r"(?i)\b(token|key|secret|password|passphrase|psk)\b\s*[:=]\s*\S+"),
        r"\1=<redacted>",
    ),
    (re.compile(r"(?i)\bssid\s*[:=]\s*.+"), "SSID=<redacted>"),
)


def install_salt(path: Path | None = None) -> bytes:
    """Return the per-install salt, creating it on first use."""
    global _salt_cache
    if _salt_cache is not None and path is None:
        return _salt_cache
    target = path or (paths.state_dir() / "install-salt")
    try:
        raw = target.read_bytes()
        if len(raw) >= SALT_BYTES:
            if path is None:
                _salt_cache = raw
            return raw
    except OSError:
        pass
    raw = os.urandom(SALT_BYTES)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(raw)
        if os.name != "nt":
            target.chmod(0o600)
    except OSError:
        # A read-only state directory is survivable: the salt becomes
        # per-process instead of per-install, which only affects continuity of
        # network identity across restarts.
        pass
    if path is None:
        _salt_cache = raw
    return raw


def hash_identifier(value: str | None, *, length: int = 12) -> str | None:
    """Stable, salted, truncated hash of a network identifier."""
    if value is None:
        return None
    text = value.strip()
    if not text:
        return None
    digest = hmac.new(install_salt(), text.encode("utf-8", "replace"), sha256).hexdigest()
    return digest[:length]


def hash_to_float(value: str | None) -> float:
    """Map an identifier onto a stable float, for storage in a REAL column.

    The feature table has no text columns by design, so identity changes are
    represented as a number: the value itself is meaningless, only equality
    between consecutive samples matters.
    """
    if not value:
        return 0.0
    digest = blake2b(
        value.encode("utf-8", "replace"), key=install_salt()[:16], digest_size=6
    ).digest()
    return float(int.from_bytes(digest, "big"))


def redact(text: str, *, limit: int = 400) -> str:
    """Scrub MAC addresses, IP literals, secrets and SSIDs out of free text."""
    if not text:
        return ""
    cleaned = text
    for pattern, replacement in _REDACTIONS:
        cleaned = pattern.sub(replacement, cleaned)
    cleaned = " ".join(cleaned.split())
    return cleaned[:limit]


def reset_cache() -> None:
    """Drop the cached salt. Used by tests that relocate NETPULSE_HOME."""
    global _salt_cache
    _salt_cache = None
