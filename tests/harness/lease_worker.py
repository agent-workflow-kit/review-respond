"""A child process for lease and compare-and-set race tests (run as a script, never imported).

Protocol, one line per message on stdout, every wait bounded: ``READY`` once started; then it
waits for the ``go`` file to exist (the parent creates it when every worker is ready, so the
attempts are really concurrent); then one result line:

- ``acquire``: ``ACQUIRED`` (it then holds the lease until its stdin reaches EOF, so a slow loser
  can never win a lease the winner already let go of) or ``HELD``;
- ``cas``: ``WON`` or ``LOST`` for one compare-and-set of ``<table>.<id>`` at the given rev.

The parent decides by these lines and the database, never by the order of any log.
"""

from __future__ import annotations

import os
import select
import sys
import time
from pathlib import Path

from review_respond.store import Store, cas_update, open_state_dir


def wait_for(path: Path, seconds: float = 20.0) -> None:
    deadline = time.monotonic() + seconds
    while not path.exists():
        if time.monotonic() > deadline:
            raise SystemExit("go file never appeared")
        time.sleep(0.005)


def say(text: str) -> None:
    sys.stdout.write(text + "\n")
    sys.stdout.flush()


def hold_until_eof(limit: float = 60.0) -> None:
    deadline = time.monotonic() + limit
    while time.monotonic() < deadline:
        ready, _, _ = select.select([0], [], [], 0.5)
        if ready and not os.read(0, 4096):
            return


def main(argv: list[str]) -> int:
    mode, state_dir, go = argv[0], Path(argv[1]), Path(argv[2])
    store = Store.open(open_state_dir(state_dir), write=True, close_state=True)
    say("READY")
    wait_for(go)
    if mode == "acquire":
        key = argv[3]
        lease = store.try_acquire(key, heartbeat=None)
        if lease is None:
            say("HELD")
            return 0
        say("ACQUIRED")
        hold_until_eof()
        lease.release()
        return 0
    if mode == "cas":
        table, row_id, rev, value = argv[3], argv[4], int(argv[5]), argv[6]
        with store.transaction() as conn:
            won = cas_update(conn, table, "id", row_id, rev, {"value": value})
        say("WON" if won else "LOST")
        return 0
    raise SystemExit(f"unknown mode {mode}")


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
