"""A child process that moves one round (run as a script, never imported), for the race tests of
tests/test_rounds_race.py.

Protocol, one line per message on stdout, every wait bounded: ``READY`` once the state is open and
the lease held; then it waits for the ``go`` file (the parent creates it when every worker is
ready, so the moves really overlap); then ``RESULT <changed|unchanged> <state>`` after the move,
or ``FAILED <code>`` when it raised a ``CliError``.

Arguments: state directory, go file, the lease key to hold, the round id and the action.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

from review_respond.errors import CliError
from review_respond.outcomes import RunEnd
from review_respond.rounds import Rounds, get_round
from review_respond.store import Store, open_state_dir

HEAD = "e" * 40


def wait_for(path: Path, seconds: float = 20.0) -> None:
    deadline = time.monotonic() + seconds
    while not path.exists():
        if time.monotonic() > deadline:
            raise SystemExit("go file never appeared")
        time.sleep(0.005)


def say(text: str) -> None:
    sys.stdout.write(text + "\n")
    sys.stdout.flush()


def main(argv: list[str]) -> int:
    state_dir, go, key, rid, action = Path(argv[0]), Path(argv[1]), argv[2], argv[3], argv[4]
    store = Store.open(open_state_dir(state_dir), write=True, close_state=True)
    lease = store.try_acquire(key, heartbeat=None)
    if lease is None:
        say("FAILED no_lease")
        return 0
    rounds = Rounds(lease)
    say("READY")
    wait_for(go)
    try:
        if action.startswith("settle_"):
            with lease.transaction() as conn:
                changed = rounds.settle_pending(conn, rid, action.removeprefix("settle_"))
        else:
            moves = {
                "start": lambda: rounds.start(rid, ("gh:rc:c1",), head_sha=HEAD),
                "cancel_flagged": lambda: rounds.cancel_flagged(rid),
                "reconcile_lost": lambda: rounds.reconcile_lost(rid),
                "to_pending": lambda: rounds.to_pending(rid),
                "to_escalated": lambda: rounds.to_escalated(
                    rid, reason="q", summary="s", counted=True
                ),
                "to_failed": lambda: rounds.to_failed(rid, "timeout", counted=True),
                "to_canceled": lambda: rounds.to_canceled(rid, counted=True),
                "finish_failed": lambda: rounds.finish_run(rid, RunEnd(command="wait", exit=18)),
            }
            changed = moves[action]().changed
    except CliError as error:
        say(f"FAILED {error.code}")
    else:
        row = get_round(store.conn, rid)
        say(f"RESULT {'changed' if changed else 'unchanged'} {row.state if row else 'none'}")
    lease.release()
    store.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
