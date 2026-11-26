"""A child process appending records to audit.jsonl or log.jsonl (a script, never imported).

Arguments: ``<audit|log> <state dir> <go file> <tag> <count>``. It prints ``READY``, waits (bounded)
for the ``go`` file so every worker starts at once, appends ``count`` records whose identity is
``<tag>-<i>`` (a long value, so a torn or interleaved write cannot go unnoticed), prints ``DONE``.
The parent checks the file by content, never by the order of the lines.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

from review_respond.audit import AuditLog, JsonlFile, LogWriter
from review_respond.store import open_state_dir

PAD = "x" * 200


def identity(tag: str, index: int) -> str:
    return f"{tag}-{index}-{PAD}"[:256]


def main(argv: list[str]) -> int:
    kind, state_dir, go, tag, count = argv[0], Path(argv[1]), Path(argv[2]), argv[3], int(argv[4])
    state = open_state_dir(state_dir)
    sys.stdout.write("READY\n")
    sys.stdout.flush()
    deadline = time.monotonic() + 20
    while not go.exists():
        if time.monotonic() > deadline:
            return 3
        time.sleep(0.002)
    if kind == "audit":
        audit = AuditLog(
            JsonlFile(state, "audit.jsonl"), actor="human:tester", decision_ids=[f"d-{tag}"] * 8
        )
        for i in range(count):
            attempt = audit.attempted(
                "reply", "example.com/o/r#1", inputs={"pending_id": "rp-aaaaaaaa"}
            )
            audit.outcome(
                attempt,
                "reply",
                "example.com/o/r#1",
                {"status": "ok", "comment_id": f"{tag}-{i}", "exit_code": 0},
            )
    else:
        log = LogWriter(JsonlFile(state, "log.jsonl"))
        for i in range(count):
            log.warning("probe_event", key=identity(tag, i))
    sys.stdout.write("DONE\n")
    sys.stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
