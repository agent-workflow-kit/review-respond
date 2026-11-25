"""Exact-PID bookkeeping for processes a test starts, and their cleanup.

Every process is recorded at launch with its start time (from ``/proc``) so a reused PID is never
signalled. Cleanup signals exactly the recorded process groups, and only groups that are not the
test runner's own. Nothing here selects a target by name or pattern.
"""

from __future__ import annotations

import os
import signal
from dataclasses import dataclass
from pathlib import Path


def start_time(pid: int) -> str | None:
    """Field 22 of ``/proc/<pid>/stat`` (start time), or None when the process is gone."""
    try:
        text = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return None
    # The command name (field 2) may hold spaces and parentheses: split after the last ')'.
    return text.rsplit(")", 1)[1].split()[19]


@dataclass(frozen=True)
class Launched:
    pid: int
    pgid: int
    started: str | None


class ProcRegistry:
    def __init__(self) -> None:
        self.launched: list[Launched] = []

    def record(self, pid: int) -> Launched:
        """Record a process the test just started. Call right after spawning, before waiting."""
        try:
            pgid = os.getpgid(pid)
        except ProcessLookupError:
            pgid = pid
        item = Launched(pid=pid, pgid=pgid, started=start_time(pid))
        self.launched.append(item)
        return item

    def on_spawn(self, pid: int) -> None:
        """The ``Gateway(on_spawn=...)`` callback."""
        self.record(pid)

    def alive(self) -> list[Launched]:
        return [
            p for p in self.launched if p.started is not None and start_time(p.pid) == p.started
        ]

    def cleanup(self) -> list[Launched]:
        """Kill exactly the recorded groups whose leader is still the process recorded at launch."""
        killed = []
        own = os.getpgrp()
        for item in self.alive():
            if item.pgid == own:
                continue
            try:
                os.killpg(item.pgid, signal.SIGKILL)
            except ProcessLookupError:
                continue
            killed.append(item)
        return killed
