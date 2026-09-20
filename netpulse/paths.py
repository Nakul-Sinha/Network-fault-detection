"""Platform-appropriate locations for configuration, data and runtime state.

A single ``NETPULSE_HOME`` override collapses every location under one
directory, which is what the container image, the test suite and portable
"run from a USB stick" usage all rely on.
"""

from __future__ import annotations

import contextlib
import os
import sys
from pathlib import Path

APP_NAME = "NetPulse"
APP_SLUG = "netpulse"

_HOME_ENV = "NETPULSE_HOME"
_CONFIG_ENV = "NETPULSE_CONFIG"


def _windows_base(var: str, fallback: str) -> Path:
    raw = os.environ.get(var)
    if raw:
        return Path(raw)
    return Path.home() / fallback


def home_override() -> Path | None:
    """Return the ``NETPULSE_HOME`` override if one is set."""
    raw = os.environ.get(_HOME_ENV)
    return Path(raw).expanduser() if raw else None


def config_dir() -> Path:
    override = home_override()
    if override:
        return override / "config"
    if sys.platform == "win32":
        return _windows_base("APPDATA", "AppData/Roaming") / APP_NAME
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / APP_NAME
    xdg = os.environ.get("XDG_CONFIG_HOME")
    return (Path(xdg) if xdg else Path.home() / ".config") / APP_SLUG


def data_dir() -> Path:
    override = home_override()
    if override:
        return override / "data"
    if sys.platform == "win32":
        return _windows_base("LOCALAPPDATA", "AppData/Local") / APP_NAME
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / APP_NAME / "data"
    xdg = os.environ.get("XDG_DATA_HOME")
    return (Path(xdg) if xdg else Path.home() / ".local" / "share") / APP_SLUG


def state_dir() -> Path:
    """Directory for volatile runtime state: API token, pid file, model cache."""
    override = home_override()
    if override:
        return override / "state"
    if sys.platform in ("win32", "darwin"):
        return data_dir() / "state"
    xdg = os.environ.get("XDG_STATE_HOME")
    return (Path(xdg) if xdg else Path.home() / ".local" / "state") / APP_SLUG


def log_dir() -> Path:
    return state_dir() / "logs"


def config_file() -> Path:
    raw = os.environ.get(_CONFIG_ENV)
    if raw:
        return Path(raw).expanduser()
    return config_dir() / "config.toml"


def database_file() -> Path:
    return data_dir() / "netpulse.sqlite"


def api_token_file() -> Path:
    return state_dir() / "api-token"


def model_dir() -> Path:
    return state_dir() / "models"


def ensure_dirs() -> None:
    """Create every directory the agent writes to, with tight permissions."""
    for path in (config_dir(), data_dir(), state_dir(), log_dir(), model_dir()):
        path.mkdir(parents=True, exist_ok=True)
        _restrict(path)


def _restrict(path: Path) -> None:
    """Best-effort owner-only permissions on POSIX; a no-op on Windows."""
    if sys.platform == "win32":
        return
    with contextlib.suppress(OSError):
        path.chmod(0o700)


def write_private(path: Path, content: str) -> None:
    """Write a file readable only by the current user."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    if sys.platform != "win32":
        with contextlib.suppress(OSError):
            path.chmod(0o600)
