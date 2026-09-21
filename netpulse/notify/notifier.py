"""OS notifications, with the rules that keep the agent from being uninstalled.

PRD 15 lists "models cry wolf" as the top product risk, and PRD 11.4 sets
the countermeasures: a cooldown of at least thirty minutes per layer unless
severity jumps, quiet hours, and a severity floor. All three live here rather
than in the scorer, because they are product policy about interrupting a
person, not statements about the network.

Delivery is best effort by design. Every backend is a small subprocess call
that can fail for reasons the agent cannot fix (no notification daemon on a
headless box, a locked session, a missing binary). A failed notification is
logged and the incident is still recorded, because the incident is the
durable artefact and the toast is only a courtesy.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol

from ..config import NetPulseConfig
from ..logging_setup import get_logger
from ..ml.l0_rules import severity_rank

log = get_logger(__name__)

APP_NAME = "NetPulse"
_NO_WINDOW = 0x08000000


@dataclass
class Notification:
    title: str
    body: str
    severity: str
    layer: str
    incident_id: int | None = None
    ts: float = field(default_factory=time.time)


class Backend(Protocol):
    """A way of putting a message in front of the user."""

    name: str

    def available(self) -> bool: ...

    def send(self, notification: Notification) -> bool: ...


class WindowsBackend:
    name = "windows-toast"

    def available(self) -> bool:
        return sys.platform == "win32" and shutil.which("powershell") is not None

    def send(self, notification: Notification) -> bool:
        # WinRT toast through PowerShell. Strings are injected as here-string
        # literals and XML-escaped, so no notification content can break out
        # into the script.
        title = _xml_escape(notification.title)
        body = _xml_escape(notification.body)
        script = (
            "[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications,"
            " ContentType = WindowsRuntime] > $null;"
            "$xml = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent(1);"
            f"$xml.GetElementsByTagName('text')[0].AppendChild($xml.CreateTextNode('{title}'))"
            " > $null;"
            f"$xml.GetElementsByTagName('text')[1].AppendChild($xml.CreateTextNode('{body}'))"
            " > $null;"
            "$toast = [Windows.UI.Notifications.ToastNotification]::new($xml);"
            "[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier"
            f"('{APP_NAME}').Show($toast);"
        )
        return _run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
            creation_flags=_NO_WINDOW,
        )


class MacBackend:
    name = "osascript"

    def available(self) -> bool:
        return sys.platform == "darwin" and shutil.which("osascript") is not None

    def send(self, notification: Notification) -> bool:
        title = _applescript_escape(notification.title)
        body = _applescript_escape(notification.body)
        script = f'display notification "{body}" with title "{APP_NAME}" subtitle "{title}"'
        return _run(["osascript", "-e", script])


class LinuxBackend:
    name = "notify-send"

    def available(self) -> bool:
        return sys.platform.startswith("linux") and shutil.which("notify-send") is not None

    def send(self, notification: Notification) -> bool:
        urgency = "critical" if notification.severity == "critical" else "normal"
        return _run(
            [
                "notify-send",
                "--app-name",
                APP_NAME,
                "--urgency",
                urgency,
                notification.title,
                notification.body,
            ]
        )


class LogBackend:
    """Always available. The only backend a headless server has."""

    name = "log"

    def available(self) -> bool:
        return True

    def send(self, notification: Notification) -> bool:
        log.warning(
            "[%s] %s: %s", notification.severity.upper(), notification.title, notification.body
        )
        return True


class RecordingBackend:
    """Collects notifications instead of sending them. Used by the tests."""

    name = "recording"

    def __init__(self) -> None:
        self.sent: list[Notification] = []

    def available(self) -> bool:
        return True

    def send(self, notification: Notification) -> bool:
        self.sent.append(notification)
        return True


def _run(args: list[str], creation_flags: int = 0) -> bool:
    kwargs: dict[str, object] = {}
    if creation_flags:
        kwargs["creationflags"] = creation_flags
    try:
        result = subprocess.run(
            args,
            capture_output=True,
            timeout=10.0,
            text=True,
            **kwargs,  # type: ignore[arg-type]
        )
    except (OSError, subprocess.SubprocessError) as exc:
        log.debug("notification backend failed: %s", exc)
        return False
    if result.returncode != 0:
        log.debug("notification backend returned %d: %s", result.returncode, result.stderr[:200])
        return False
    return True


def _xml_escape(text: str) -> str:
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace("'", "&apos;")
        .replace('"', "&quot;")
    )


def _applescript_escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace('"', '\\"')


def default_backend() -> Backend:
    for candidate in (WindowsBackend(), MacBackend(), LinuxBackend()):
        if candidate.available():
            return candidate
    return LogBackend()


class Notifier:
    """Applies quiet hours, severity floor and per-layer cooldown."""

    def __init__(self, config: NetPulseConfig, backend: Backend | None = None) -> None:
        self.config = config
        self.backend = backend or default_backend()
        self._last_sent: dict[str, tuple[float, str]] = {}
        self._lock = threading.Lock()
        self.suppressed = 0
        self.delivered = 0

    def should_send(
        self, notification: Notification, *, now: float | None = None
    ) -> tuple[bool, str]:
        """Decide whether to interrupt, and say why not when the answer is no."""
        settings = self.config.notifications
        if not settings.enabled:
            return False, "notifications are disabled"

        if severity_rank(notification.severity) < severity_rank(settings.min_severity):
            return False, f"below the {settings.min_severity} threshold"

        now = time.time() if now is None else now
        if self._in_quiet_hours(now) and notification.severity != "critical":
            # Critical still gets through: an unreachable gateway at 3am is
            # worth knowing about when you wake up to a broken morning call.
            return False, "quiet hours"

        with self._lock:
            previous = self._last_sent.get(notification.layer)
        if previous is not None:
            elapsed = now - previous[0]
            escalated = severity_rank(notification.severity) > severity_rank(previous[1])
            cooldown = settings.cooldown_minutes * 60.0
            if elapsed < cooldown and not escalated:
                remaining = (cooldown - elapsed) / 60.0
                return False, f"cooldown for {notification.layer}, {remaining:.0f} min remaining"
        return True, ""

    def notify(self, notification: Notification, *, now: float | None = None) -> bool:
        allowed, reason = self.should_send(notification, now=now)
        if not allowed:
            self.suppressed += 1
            log.debug("suppressed notification (%s): %s", reason, notification.title)
            return False

        sent = self.backend.send(notification)
        if sent:
            self.delivered += 1
            with self._lock:
                self._last_sent[notification.layer] = (
                    time.time() if now is None else now,
                    notification.severity,
                )
        else:
            log.info("notification backend %s could not deliver", self.backend.name)
        return sent

    def _in_quiet_hours(self, now: float) -> bool:
        settings = self.config.notifications
        start = settings.quiet_hours_start
        end = settings.quiet_hours_end
        if start == end:
            return False
        hour = datetime.fromtimestamp(now).hour
        if start < end:
            return start <= hour < end
        # The window wraps midnight, which is the common case (22:00 to 07:00).
        return hour >= start or hour < end

    def status(self) -> dict[str, object]:
        with self._lock:
            last = {
                layer: {"ts": ts, "severity": severity}
                for layer, (ts, severity) in self._last_sent.items()
            }
        return {
            "backend": self.backend.name,
            "enabled": self.config.notifications.enabled,
            "quiet_hours": [
                self.config.notifications.quiet_hours_start,
                self.config.notifications.quiet_hours_end,
            ],
            "in_quiet_hours": self._in_quiet_hours(time.time()),
            "cooldown_minutes": self.config.notifications.cooldown_minutes,
            "min_severity": self.config.notifications.min_severity,
            "delivered": self.delivered,
            "suppressed": self.suppressed,
            "last_per_layer": last,
        }


def from_incident(incident: object, severity: str) -> Notification:
    """Build a notification from an incident record."""
    title = getattr(incident, "title", "Network health")
    summary = getattr(incident, "summary", "")
    remediation = getattr(incident, "remediation", []) or []
    body = summary
    if remediation:
        body = f"{summary}\n\nTry: {remediation[0]}"
    return Notification(
        title=str(title),
        body=str(body),
        severity=severity,
        layer=str(getattr(incident, "primary_layer", "os")),
        incident_id=getattr(incident, "id", None),
    )
