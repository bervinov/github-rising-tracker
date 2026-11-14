"""Minimal in-process scheduler: runs jobs one after another at fixed intervals."""

from __future__ import annotations

import logging
import signal
import time
from collections.abc import Callable
from dataclasses import dataclass, field

log = logging.getLogger(__name__)


@dataclass
class Job:
    """A named function and how often to run it."""

    name: str
    interval_s: float
    fn: Callable[[], object]
    next_at: float = 0.0


@dataclass
class Scheduler:
    """Runs due jobs serially; a failing job is logged and the loop continues."""

    jobs: list[Job]
    clock: Callable[[], float] = time.monotonic
    sleep: Callable[[float], None] = time.sleep
    tick_s: float = 5.0
    _stop: bool = field(default=False, init=False)

    def stop(self, *_: object) -> None:
        """Stop after the current job (used as the SIGTERM/SIGINT handler)."""
        log.info("stopping after the current job")
        self._stop = True

    def run_pending(self) -> list[str]:
        """Run every job that is due and return their names."""
        ran = []
        for job in self.jobs:
            if self._stop or self.clock() < job.next_at:
                continue
            started = self.clock()
            try:
                job.fn()
            except Exception:
                log.exception("job %s failed", job.name)
            # Counted from the start, so a job's own duration does not push its schedule back.
            job.next_at = started + job.interval_s
            ran.append(job.name)
        return ran

    def run_forever(self) -> None:
        """Loop until stopped."""
        signal.signal(signal.SIGTERM, self.stop)
        signal.signal(signal.SIGINT, self.stop)
        while not self._stop:
            self.run_pending()
            self.sleep(self.tick_s)
