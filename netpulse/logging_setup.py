"""Logging configuration for the agent.

Two sinks: a human-readable console stream and a rotating file under the
state directory. The file handler is what ships inside a diagnostic bundle,
so the formatter deliberately avoids anything resembling user content.
"""

from __future__ import annotations

import logging
import logging.handlers
import sys

from . import paths

LOG_FORMAT = "%(asctime)s %(levelname)-7s %(name)-28s %(message)s"
DATE_FORMAT = "%Y-%m-%dT%H:%M:%S%z"
MAX_BYTES = 2 * 1024 * 1024
BACKUP_COUNT = 3

_configured = False


def setup_logging(level: str = "INFO", *, to_file: bool = True) -> None:
    """Install console and rotating-file handlers exactly once."""
    global _configured
    if _configured:
        set_level(level)
        return

    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    formatter = logging.Formatter(LOG_FORMAT, DATE_FORMAT)

    console = logging.StreamHandler(stream=sys.stderr)
    console.setLevel(_as_level(level))
    console.setFormatter(formatter)
    root.addHandler(console)

    if to_file:
        try:
            paths.log_dir().mkdir(parents=True, exist_ok=True)
            handler = logging.handlers.RotatingFileHandler(
                paths.log_dir() / "netpulse.log",
                maxBytes=MAX_BYTES,
                backupCount=BACKUP_COUNT,
                encoding="utf-8",
            )
            handler.setLevel(logging.DEBUG)
            handler.setFormatter(formatter)
            root.addHandler(handler)
        except OSError:
            root.warning("could not open the log file; continuing with console logging only")

    # uvicorn installs its own noisy access logger; keep it at warning level.
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    _configured = True


def set_level(level: str) -> None:
    resolved = _as_level(level)
    for handler in logging.getLogger().handlers:
        if isinstance(handler, logging.StreamHandler) and not isinstance(
            handler, logging.handlers.RotatingFileHandler
        ):
            handler.setLevel(resolved)


def _as_level(level: str) -> int:
    return getattr(logging, str(level).upper(), logging.INFO)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
