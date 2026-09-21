"""Collector scheduling: jitter, battery backoff and a watchdog.

One thread per collector rather than a shared event loop. The collectors
block on subprocesses and sockets, so threads are the natural fit, and seven
mostly-sleeping threads cost nothing against the RAM budget in PRD 3.2. More
importantly, it makes PRD N8 straightforward: a collector that dies takes
its own thread down and the supervisor restarts it, leaving every other
layer running.

Two scheduling behaviours are policy rather than mechanism:

* **Jitter.** Probes are spread inside a fraction of their interval so the
  agent never emits a burst on the same second every cycle, which is both
  politer to the network and more useful statistically.
* **Battery backoff.** PRD N4 asks for battery awareness. On battery below
  the configured threshold, intervals stretch by a multiplier. Passive
  collectors are exempt, because reading a counter costs nothing.
"""

from __future__ import annotations

import random
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field

import psutil

from ..collectors.base import Collector, CollectorResult
from ..config import NetPulseConfig
from ..logging_setup import get_logger

log = get_logger(__name__)

#: Consecutive failures before a collector is given a longer rest. It is not
#: disabled: a transient failure (an adapter reconnecting) should not silence
#: a layer for the rest of the session.
BACKOFF_AFTER = 3
MAX_BACKOFF_MULTIPLIER = 8.0


def on_battery(threshold_pct: int) -> bool:
    """True when running on battery below the configured charge level."""
    try:
        battery = psutil.sensors_battery()
    except Exception:
        return False
    if battery is None or battery.power_plugged:
        return False
    return battery.percent <= threshold_pct


@dataclass
class WorkerStats:
    runs: int = 0
    failures: int = 0
    skips: int = 0
    restarts: int = 0
    triggered: int = 0
    last_run: float | None = None
    last_duration_ms: float = 0.0
    next_due: float = field(default_factory=time.time)


class CollectorWorker(threading.Thread):
    """Runs one collector on its own cadence until asked to stop."""

    def __init__(
        self,
        collector: Collector,
        config: NetPulseConfig,
        on_result: Callable[[Collector, CollectorResult], None],
        stop_event: threading.Event,
        *,
        rng: random.Random | None = None,
    ) -> None:
        super().__init__(name=f"netpulse-{collector.name}", daemon=True)
        self.collector = collector
        self.config = config
        self.on_result = on_result
        self.stop_event = stop_event
        self.stats = WorkerStats()
        self._rng = rng or random.Random()
        self._consecutive_failures = 0
        #: Set to run this collector immediately rather than at its next due
        #: time. PRD 6.3 allows the expensive probes to run "on suspicion",
        #: which is what this is for.
        self.wake = threading.Event()

    def interval(self) -> float:
        base = self.collector.interval_s()
        power = self.config.power
        if (
            power.battery_backoff
            and self.collector.active
            and on_battery(power.battery_threshold_pct)
        ):
            base *= power.battery_interval_multiplier
        if self._consecutive_failures >= BACKOFF_AFTER:
            multiplier = min(
                MAX_BACKOFF_MULTIPLIER, 2 ** (self._consecutive_failures - BACKOFF_AFTER + 1)
            )
            base *= multiplier
        jitter = self.config.probes.jitter_fraction
        return max(1.0, base * (1.0 + self._rng.uniform(-jitter, jitter)))

    def run(self) -> None:
        # Stagger the first run so seven collectors do not all fire at once
        # on startup, which would blow the probe budget in the first second.
        initial = self._rng.uniform(0.0, min(5.0, self.collector.interval_s() / 2.0))
        if self.stop_event.wait(initial):
            return

        while not self.stop_event.is_set():
            self.wake.clear()
            started = time.perf_counter()
            try:
                result = self.collector.collect()
            except Exception as exc:
                # Collector.collect already contains its own errors; reaching
                # here means something escaped that, so the worker survives it
                # rather than dying and silencing the layer.
                self._consecutive_failures += 1
                self.stats.failures += 1
                log.exception("collector %s raised past its guard: %s", self.collector.name, exc)
            else:
                self.stats.runs += 1
                self.stats.last_run = time.time()
                self.stats.last_duration_ms = (time.perf_counter() - started) * 1000.0
                if result.skipped_reason:
                    self.stats.skips += 1
                if result.ok:
                    self._consecutive_failures = 0
                else:
                    self._consecutive_failures += 1
                    self.stats.failures += 1
                try:
                    self.on_result(self.collector, result)
                except Exception:
                    log.exception("handling result from %s failed", self.collector.name)

            wait = self.interval()
            self.stats.next_due = time.time() + wait
            # Wake early either to stop, or because something asked for this
            # probe now. Waiting on both keeps shutdown immediate.
            deadline = time.monotonic() + wait
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                if self.stop_event.wait(min(remaining, 0.5)):
                    return
                if self.wake.is_set():
                    self.stats.triggered += 1
                    break

    def status(self) -> dict[str, object]:
        return {
            "collector": self.collector.name,
            "alive": self.is_alive(),
            "runs": self.stats.runs,
            "failures": self.stats.failures,
            "skips": self.stats.skips,
            "restarts": self.stats.restarts,
            "triggered": self.stats.triggered,
            "last_run": self.stats.last_run,
            "last_duration_ms": round(self.stats.last_duration_ms, 1),
            "next_due": self.stats.next_due,
            "interval_s": round(self.interval(), 1),
            "consecutive_failures": self._consecutive_failures,
        }


class WorkerSupervisor:
    """Starts workers and restarts any that die (PRD N8)."""

    def __init__(
        self,
        collectors: list[Collector],
        config: NetPulseConfig,
        on_result: Callable[[Collector, CollectorResult], None],
    ) -> None:
        self.collectors = collectors
        self.config = config
        self.on_result = on_result
        self.stop_event = threading.Event()
        self.workers: dict[str, CollectorWorker] = {}
        self._restarts: dict[str, int] = {}
        self.started = False

    def start(self) -> None:
        self.stop_event.clear()
        self.started = True
        for collector in self.collectors:
            self._spawn(collector)

    def _spawn(self, collector: Collector) -> None:
        worker = CollectorWorker(collector, self.config, self.on_result, self.stop_event)
        worker.stats.restarts = self._restarts.get(collector.name, 0)
        self.workers[collector.name] = worker
        worker.start()

    def check(self) -> list[str]:
        """Restart dead workers. Returns the names that were restarted."""
        restarted: list[str] = []
        if not self.started or self.stop_event.is_set():
            # Maintenance can run before start() in one-shot modes such as
            # `netpulse check`; it must not conjure workers nobody asked for.
            return restarted
        for collector in self.collectors:
            worker = self.workers.get(collector.name)
            if worker is not None and worker.is_alive():
                continue
            count = self._restarts.get(collector.name, 0) + 1
            self._restarts[collector.name] = count
            log.warning("collector worker %s died, restarting (%d)", collector.name, count)
            collector.refresh_capability()
            self._spawn(collector)
            restarted.append(collector.name)
        return restarted

    def trigger(self, *names: str) -> list[str]:
        """Ask the named collectors to run now instead of at their next due time.

        The probe budget still applies, so this cannot be used to exceed the
        rate limits; it only moves an allowed probe earlier.
        """
        woken: list[str] = []
        for name in names:
            worker = self.workers.get(name)
            if worker is not None and worker.is_alive():
                worker.wake.set()
                woken.append(name)
        return woken

    def stop(self, timeout: float = 10.0) -> None:
        self.started = False
        self.stop_event.set()
        deadline = time.monotonic() + timeout
        for worker in self.workers.values():
            remaining = max(0.1, deadline - time.monotonic())
            worker.join(timeout=remaining)

    def status(self) -> list[dict[str, object]]:
        return [worker.status() for worker in self.workers.values()]
