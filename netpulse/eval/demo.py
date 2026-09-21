"""Replay a fault against a live agent, so you can watch one happen.

Waiting for a real network to break is a poor way to find out whether this
works. ``netpulse demo`` feeds a recorded fault through the same agent, the
same model ladder and the same web UI that a real install runs, at whatever
speed you ask for, so a Wi-Fi fade that takes eighty minutes in life takes
two minutes at your desk.

Nothing is faked. The collectors are switched off and their measurements
come from the corpus instead; everything downstream of that is the product.

Sample timestamps keep their real fifteen second spacing, because compressing
them would change what the rolling windows see and the demo would stop being
a demonstration of the real thing.

They are also anchored to the hour of day the recording was generated for,
not simply to now. The corpus bakes a diurnal curve into its values, and the
model reasons about hour of day, so dropping evening-shaped measurements into
a four in the morning baseline makes the two disagree. That showed up as a
DNS blackhole opening with a card about the far end being slow: not wrong
exactly, but a worse first sentence than the data deserves.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any

from ..config import NetPulseConfig
from ..logging_setup import get_logger
from ..ml.scorer import ScoreResult
from ..store.models import Sample
from .scenarios import SCENARIOS_BY_NAME, ScenarioRun, generate

log = get_logger(__name__)

#: Real seconds between samples in the corpus.
FRAME_SECONDS = 15.0
#: The local hour the corpus generates its diurnal curve around. Keep this in
#: step with the default in scenarios.generate.
SCENARIO_START_HOUR = 19.0


def demo_config(base: NetPulseConfig | None = None) -> NetPulseConfig:
    """A config with every collector off: the corpus is the only input."""
    config = base or NetPulseConfig()
    return config.model_copy(
        update={
            "collectors": config.collectors.model_copy(
                update={
                    "system": False,
                    "wifi": False,
                    "dns": False,
                    "gateway": False,
                    "path": False,
                    "https": False,
                    "captive": False,
                }
            )
        }
    )


def anchor_start(run: ScenarioRun, *, now: float | None = None) -> float:
    """Pick a start time whose local hour matches the one the run was built for.

    The most recent occurrence of that hour, so the recording still lands
    inside the last day and the timeline in the UI has somewhere to put it.
    """
    now = time.time() if now is None else now
    local = datetime.fromtimestamp(now)
    target = local.replace(
        hour=int(SCENARIO_START_HOUR) % 24,
        minute=int((SCENARIO_START_HOUR % 1) * 60),
        second=0,
        microsecond=0,
    )
    if target.timestamp() > now:
        target = target - timedelta(days=1)
    return target.timestamp()


def prepare_samples(run: ScenarioRun, *, start_at: float | None = None) -> list[Sample]:
    """Re-anchor a recording, keeping its spacing and its time of day."""
    start_at = anchor_start(run) if start_at is None else start_at
    rebased: list[Sample] = []
    for index, sample in enumerate(run.samples):
        rebased.append(Sample(ts=start_at + index * FRAME_SECONDS, values=dict(sample.values)))
    return rebased


class DemoDriver:
    """Feeds a recording into a live agent on a background thread."""

    def __init__(
        self,
        agent: Any,
        run: ScenarioRun,
        *,
        speed: float = 30.0,
        loop: bool = False,
        on_frame: Callable[[int, ScoreResult], None] | None = None,
    ) -> None:
        self.agent = agent
        self.run = run
        self.speed = max(1.0, speed)
        self.loop = loop
        self.on_frame = on_frame
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.frames_played = 0

    @property
    def interval(self) -> float:
        return FRAME_SECONDS / self.speed

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="netpulse-demo", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)

    def wait(self) -> None:
        if self._thread is not None:
            self._thread.join()

    def _run(self) -> None:
        while not self._stop.is_set():
            samples = prepare_samples(self.run)
            for index, sample in enumerate(samples):
                if self._stop.is_set():
                    return
                self.agent.assembler.update(sample.values)
                # The agent stamps its own sample with time.time(); override it
                # so the model sees the recording's spacing rather than the
                # accelerated wall clock.
                result = self.agent.tick(timestamp=sample.ts)
                self.frames_played += 1
                if result is not None and self.on_frame is not None:
                    self.on_frame(index, result)
                if self._stop.wait(self.interval):
                    return
            if not self.loop:
                return


def resolve_scenario(name: str | None) -> ScenarioRun:
    """Pick a scenario by name, defaulting to one that shows the point."""
    if not name:
        name = "wifi_fade"
    if name not in SCENARIOS_BY_NAME:
        available = ", ".join(sorted(SCENARIOS_BY_NAME))
        raise ValueError(f"unknown scenario {name!r}. Available: {available}")
    return generate(SCENARIOS_BY_NAME[name])


def scenario_names() -> list[str]:
    return sorted(SCENARIOS_BY_NAME)
