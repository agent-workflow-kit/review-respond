"""A child process that records one comment read into the handled set (run as a script, never
imported), for the race tests of tests/test_handled.py.

Protocol, one line per message on stdout, every wait bounded: ``READY`` once the state is open and
the lease held; then it waits for the ``go`` file (the parent creates it when every worker is
ready, so the writes really overlap); then ``DONE`` after the read was recorded, or
``FAILED <code>`` when recording raised a ``CliError``.

Arguments: state directory, go file, PR reference, a JSON file with the read
(``{"complete": bool, "comments": [ReadComment fields], "verdicts": {id: [eligible, reason]}}``),
and the lease key to hold (each worker holds its own, as a ``pass`` and a ``pr:`` holder would).
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

from review_respond.detect import CommentRead, ReadComment, get_pr
from review_respond.eligibility import Verdict
from review_respond.errors import CliError
from review_respond.handled import HandledSet
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
    state_dir, go, pr, doc_file, key = Path(argv[0]), Path(argv[1]), argv[2], Path(argv[3]), argv[4]
    doc = json.loads(doc_file.read_text())
    store = Store.open(open_state_dir(state_dir), write=True, close_state=True)
    lease = store.try_acquire(key, heartbeat=None)
    if lease is None:
        say("FAILED no_lease")
        return 0
    say("READY")
    wait_for(go)
    stored = get_pr(store.conn, pr)
    assert stored is not None
    read = CommentRead(
        pr=pr,
        head_sha="a" * 40,
        complete=doc["complete"],
        comments=tuple(ReadComment(**c) for c in doc["comments"]),
        verdicts={cid: Verdict(*v) for cid, v in doc["verdicts"].items()},
    )
    try:
        HandledSet(lease).record_read(stored, read)
    except CliError as error:
        say(f"FAILED {error.code}")
    else:
        say("DONE")
    lease.release()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
