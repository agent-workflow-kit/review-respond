"""Job placement: where a round's ``_job`` runs (docs/interface.md: Job, Round states). Stage 2.

A queued round gets its job from here, in one of two placements:

- jobsctl, when it is installed: ``jobsctl add`` to the ``jobs.queue`` queue with the unique key
  ``respond:<PR digest>:<round number>`` (so a second submission of the same round is idempotent),
  ``--max-retries 0``, ``--timeout none``, an explicit ``--cwd`` (the state directory),
  ``--kill-grace``, the correlation labels and the ``XDG_*`` locations and ``jobs.pass_env`` names.
- detached, without it: the same argv started as a session of its own, ``/dev/null`` for stdin and a
  private file under the state directory for stdout and stderr. Nothing waits for it.

The argv is review-respond's own command line (``<absolute review-respond> [--config <absolute
path>] _job <round id>``); no untrusted text is in it or in the environment. The environment is
built from scratch: ``PATH``, ``HOME``, ``LANG``, ``LC_ALL``, ``TZ``, ``XDG_CONFIG_HOME``,
``XDG_STATE_HOME``, ``XDG_RUNTIME_DIR`` (each when set) and the ``jobs.pass_env`` names. Where a
round's job is stored (``rounds.job_id``, ``rounds.detached_started_at``) is the placement that
round was submitted with: the queued reconcile (finalizer.py) reads that, not the present state of
the machine.

TODO(#25): ``respond`` places its round with ``Placement.submit`` too.
"""

from __future__ import annotations

import os
import re
import shutil
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from . import labels
from .audit import LogWriter
from .config import Config
from .deps import BY_TOOL, probe, safe_path
from .durations import parse_duration
from .errors import CliError
from .gateway import Gateway, GatewayError, Result
from .rounds import RoundRow, Rounds
from .store import StateDir, pr_digest

PROGRAM: Final = "review-respond"
JOBS_DIR: Final = "jobs"
JOB_RE: Final = re.compile(r"jb-[a-z2-7]{8}")
# A queued round older than this with no job stored is submitted again; a detached job that has
# not taken its lease this long after it started is gone (interface: reconcile_queued).
SUBMIT_GRACE_SECONDS: Final = 120.0
_FIXED_ENV: Final = (
    "PATH", "HOME", "LANG", "LC_ALL", "TZ", "XDG_CONFIG_HOME", "XDG_STATE_HOME", "XDG_RUNTIME_DIR"
)  # fmt: skip


@dataclass(frozen=True)
class Submitted:
    """How a round's job was placed: ``jobsctl`` (``job_id`` set) or ``detached``."""

    how: str
    job_id: str | None = None


def unique_key(pr: str, number: int) -> str:
    """The jobsctl unique key of a round: a PR reference can be long, its digest cannot."""
    return f"respond:{pr_digest(pr)}:{number}"


def program() -> list[str]:
    """The absolute command that runs review-respond: the installed script when this process was
    started through it (or one is on the PATH), else this interpreter with ``-m review_respond``."""
    argv0 = sys.argv[0] if sys.argv else ""
    if os.path.basename(argv0) == PROGRAM and os.path.isabs(os.path.realpath(argv0)):
        return [os.path.abspath(argv0)]
    found = shutil.which(PROGRAM, path=safe_path(os.environ.get("PATH")))
    if found is not None:
        return [os.path.abspath(found)]
    return [os.path.abspath(sys.executable), "-m", "review_respond"]


def job_argv(round_id: str, config: Path | None) -> list[str]:
    """``<absolute review-respond> [--config <absolute path>] _job <round id>``."""
    argv = program()
    if config is not None:
        argv += ["--config", os.path.abspath(config)]
    return [*argv, "_job", round_id]


def job_env(environ: Mapping[str, str], pass_env: list[str]) -> dict[str, str]:
    """The environment of a job, from scratch (never a ``REVIEW_RESPOND_*`` variable)."""
    env = {k: environ[k] for k in _FIXED_ENV if k in environ}
    if "PATH" in env:
        env["PATH"] = safe_path(env["PATH"])
    for name in pass_env:
        if name in environ and not name.upper().startswith("REVIEW_RESPOND_"):
            env[name] = environ[name]
    return env


def _lower_error(result: Result) -> dict[str, object]:
    """The sibling's own error object, or a placeholder when it printed none."""
    doc = result.json
    error = doc.get("error") if isinstance(doc, dict) else None
    if isinstance(error, dict):
        return dict(error)  # pyright: ignore[reportUnknownArgumentType]
    return {"code": "unknown", "message": f"jobsctl add exited {result.returncode}"}


def remove_output(state: StateDir, round_id: str, log: LogWriter | None = None) -> None:
    """Remove the output file of a detached job once its round ended (best effort)."""
    try:
        state.remove_private_file(JOBS_DIR, f"{round_id}.log")
    except (OSError, CliError, ValueError):
        if log is not None:
            log.warning("temp_file_left", round=round_id)


def _seconds(text: str) -> int:
    return max(1, int(parse_duration(text, flag="jobs.kill_grace").total_seconds()))


class Placement:
    """Submits the job of one round. ``gateway`` carries the environment of jobsctl (no
    credentials); ``environ`` is what the job's own environment is built from."""

    def __init__(
        self,
        settings: Config,
        state: StateDir,
        gateway: Gateway,
        *,
        config: Path | None,
        log: LogWriter | None,
        environ: Mapping[str, str] | None = None,
    ) -> None:
        self.cfg = settings
        self.state = state
        self.gw = gateway
        self.config = config
        self.log = log
        self.environ = os.environ if environ is None else environ
        self._jobsctl: bool | None = None

    def jobsctl_present(self) -> bool:
        """True when jobsctl is installed and answers ``--version`` (optional dependency).

        A present but too old jobsctl is ``tool_too_old`` (exit 12), like ``validate``: a queue
        that cannot take the documented arguments must not be used half-way. A program that is on
        the PATH but prints no readable version counts as absent, with the warning
        ``jobsctl_unusable``.
        """
        if self._jobsctl is None:
            status = probe(self.gw, BY_TOOL["jobsctl"], required=False)
            if status.code == "tool_too_old":
                raise CliError("tool_too_old", status.message)
            self._jobsctl = status.found is not None
            if status.unreadable and not self._jobsctl:
                # on the PATH but silent or garbled: not usable, the job runs detached
                self._warn("jobsctl_unusable")
        return self._jobsctl

    def _warn(self, code: str, round_id: str | None = None) -> None:
        if self.log is not None:
            self.log.warning(code, round=round_id)

    # -- submit --------------------------------------------------------------------------------

    def submit(self, rounds: Rounds, row: RoundRow) -> Submitted:
        """Place the job of the queued ``row`` and store where it went. ``lower_failed`` (exit 12)
        when jobsctl did not take the job (the round stays queued: the reconcile submits it again);
        ``internal`` when a detached job cannot be started."""
        if self.jobsctl_present():
            return self._jobsctl_add(rounds, row)
        return self._detach(rounds, row)

    def _jobsctl_add(self, rounds: Rounds, row: RoundRow) -> Submitted:
        found = labels.job_labels(pr=row.pr, round_id=row.id)
        if found.warnings:
            self._warn("label_skipped", row.id)
        env = {
            name: self.environ[name]
            for name in ("XDG_CONFIG_HOME", "XDG_STATE_HOME")
            if name in self.environ
        }
        try:
            result = self.gw.jobsctl.add(
                job_argv(row.id, self.config),
                queue=self.cfg.jobs.queue,
                unique_key=unique_key(row.pr, row.number),
                cwd=self.state.path,
                max_retries=0,
                timeout="none",
                kill_grace=f"{_seconds(self.cfg.jobs.kill_grace)}s",
                labels=found.labels,
                env=env,
                pass_env=[
                    n
                    for n in self.cfg.jobs.pass_env
                    if n not in env and not n.upper().startswith("REVIEW_RESPOND_")
                ],
                actor="auto:review-respond",
            )
        except GatewayError as exc:
            raise CliError(
                "lower_failed",
                "jobsctl add was not run",
                lower={"code": "unknown", "message": str(exc)},
            ) from None
        job = result.json.get("job") if isinstance(result.json, dict) else None
        job_id = job.get("id") if isinstance(job, dict) else None
        if not result.ok or not isinstance(job_id, str) or not JOB_RE.fullmatch(job_id):
            raise CliError(
                "lower_failed", "jobsctl add did not take the job", lower=_lower_error(result)
            )
        rounds.record_job(row.id, job_id=job_id)
        return Submitted("jobsctl", job_id)

    def _detach(self, rounds: Rounds, row: RoundRow) -> Submitted:
        try:
            fd = self.state.open_private_log(JOBS_DIR, f"{row.id}.log")
        except OSError:
            raise CliError("internal", "cannot open the job's output file") from None
        try:
            self.gw.start_detached(
                job_argv(row.id, self.config),
                cwd=self.state.path,
                out_fd=fd,
                env=job_env(self.environ, list(self.cfg.jobs.pass_env)),
            )
        except GatewayError as exc:
            raise CliError("internal", f"the job could not be started: {exc}") from None
        finally:
            os.close(fd)
        rounds.record_job(row.id, detached_at=rounds.lease.store.clock.now().timestamp())
        return Submitted("detached")
