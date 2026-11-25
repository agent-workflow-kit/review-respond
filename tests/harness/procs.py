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


def group_members(pgid: int) -> list[int]:
    """Pids whose process group and session are both ``pgid`` (read-only scan of ``/proc``).

    A group the registry created with ``start_new_session`` has pgid == sid == pid of its leader,
    so a process elsewhere cannot match by accident.
    """
    found = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            fields = (entry / "stat").read_text().rsplit(")", 1)[1].split()
        except OSError:
            continue
        if fields[0] != "Z" and int(fields[2]) == pgid and int(fields[3]) == pgid:
            found.append(int(entry.name))
    return found


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
        """Kill exactly the recorded groups, never one that is the runner's own.

        A group is killed when its leader is still the process recorded at launch, or when the
        leader is gone but members of the group the registry created remain (a grandchild that
        outlived its parent). A pid that now belongs to another process is never signalled.
        """
        killed = []
        own = os.getpgrp()
        for item in self.launched:
            if item.pgid == own:
                continue
            now = start_time(item.pid)
            leader_alive = item.started is not None and now == item.started
            if now is not None and not leader_alive:
                continue  # the pid was reused: not ours any more
            if not leader_alive and (item.pgid != item.pid or not group_members(item.pgid)):
                continue
            try:
                os.killpg(item.pgid, signal.SIGKILL)
            except ProcessLookupError:
                continue
            killed.append(item)
        return killed
