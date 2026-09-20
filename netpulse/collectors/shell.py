"""Guarded subprocess execution for platform collectors.

Several signals (Wi-Fi association state, traceroute, TCP retransmit
counters) have no portable Python API and must come from an OS tool. Every
such call goes through :func:`run` so that the agent keeps three properties:
no shell interpolation, a hard timeout, and no console window flashing on
Windows.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from dataclasses import dataclass

from ..logging_setup import get_logger

log = get_logger(__name__)

_NO_WINDOW = 0x08000000  # CREATE_NO_WINDOW


@dataclass(slots=True)
class CommandResult:
    ok: bool
    returncode: int
    stdout: str
    stderr: str
    timed_out: bool = False
    missing: bool = False

    @property
    def text(self) -> str:
        return self.stdout if self.stdout else self.stderr


def have(executable: str) -> bool:
    """True when the executable is resolvable on PATH."""
    return shutil.which(executable) is not None


def run(args: list[str], *, timeout: float = 8.0, check_path: bool = True) -> CommandResult:
    """Run a command with no shell, a timeout and suppressed console windows."""
    if not args:
        raise ValueError("run() needs at least the executable name")
    if check_path and not have(args[0]):
        return CommandResult(False, 127, "", f"{args[0]} not found on PATH", missing=True)

    kwargs: dict[str, object] = {}
    if sys.platform == "win32":
        kwargs["creationflags"] = _NO_WINDOW
    env = dict(os.environ)
    # Force a parseable, locale-independent output from POSIX tools.
    env.setdefault("LC_ALL", "C")
    env.setdefault("LANG", "C")

    try:
        # argv list, never a shell string: no interpolation is possible here.
        completed = subprocess.run(
            args,
            capture_output=True,
            timeout=timeout,
            text=True,
            errors="replace",
            env=env,
            **kwargs,  # type: ignore[arg-type]
        )
    except subprocess.TimeoutExpired:
        log.debug("command timed out after %.1fs: %s", timeout, args[0])
        return CommandResult(False, -1, "", f"{args[0]} timed out", timed_out=True)
    except (OSError, ValueError) as exc:
        log.debug("command failed to start: %s (%s)", args[0], exc)
        return CommandResult(False, -1, "", str(exc), missing=True)

    return CommandResult(
        ok=completed.returncode == 0,
        returncode=completed.returncode,
        stdout=completed.stdout or "",
        stderr=completed.stderr or "",
    )
