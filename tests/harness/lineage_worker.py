"""A child process that moves the fork lineage (run as a script, never imported), for the race
tests of tests/test_lineage_race.py.

Protocol like ``rounds_worker``: ``READY`` once the state is open and the lease held; it then waits
for the ``go`` file; then ``RESULT <detail>`` or ``FAILED <code>`` (a ``CliError``). Arguments:
state directory, go file, the lease key to hold, the action and its argument (a round id and, for
``record``, the agent id).
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

from review_respond import lineage
from review_respond.errors import CliError
from review_respond.gateway import AgentSpawn, Gateway
from review_respond.store import Store, open_state_dir


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
    state_dir, go, key, action = Path(argv[0]), Path(argv[1]), argv[2], argv[3]
    args = argv[4:]
    store = Store.open(open_state_dir(state_dir), write=True, close_state=True)
    lease = store.try_acquire(key, heartbeat=None)
    if lease is None:
        say("FAILED no_lease")
        return 0
    say("READY")
    wait_for(go)
    try:
        now = store.clock.now().timestamp()
        if action == "switch":
            with lease.transaction() as conn:
                got = lineage.switch_base(conn, args[0], now=now)
            say(f"RESULT {'changed' if got.changed else 'unchanged'} {got.base}")
        elif action == "record":
            with lease.transaction() as conn:
                row = conn.execute(
                    "SELECT pr, base_agent_id FROM rounds WHERE id = ?", (args[0],)
                ).fetchone()
                lineage.record_fork(
                    conn, pr=row[0], round_id=args[0], agent_id=args[1], source=row[1], now=now
                )
            say("RESULT recorded")
        elif action == "teardown":
            done = lineage.teardown_pass(lease, AgentSpawn(Gateway(dict(os.environ))))
            say("RESULT " + ",".join(f"{d.agent_id}:{d.outcome}" for d in done))
        else:
            say(f"FAILED unknown_action {action}")
    except CliError as error:
        say(f"FAILED {error.code}")
    lease.release()
    store.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
