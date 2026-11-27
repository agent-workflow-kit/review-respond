"""Command-line entry point for review-respond: parser, global flags, dispatch, exit codes."""

from __future__ import annotations

import argparse
import os
import re
import sys
import traceback
from collections.abc import Callable, Sequence
from datetime import timedelta
from pathlib import Path
from typing import Any, NoReturn

from . import __version__
from .actor import check_decision_ids, check_human_only, default_actor, parse_actor
from .audit import AuditLog, JsonlFile, LogWriter, open_jsonl_files
from .check import handle as handle_check
from .config import ConfigError, LoadedConfig, load_config
from .durations import parse_duration, parse_since
from .errors import CliError, error_envelope
from .job import handle as handle_job
from .output import PROG, Context, Outcome, emit, sanitize_line, warn
from .respond import handle as handle_respond
from .stop import handle as handle_stop
from .store import Store, open_state_dir, state_dir_path
from .validate import handle as handle_validate
from .views import handle_list, handle_show

DESCRIPTION = (
    "Respond to review comments on your own pull requests, with fixes and replies you confirm."
)
USAGE_ERROR = 2

Handler = Callable[[Context, argparse.Namespace], Outcome]

# Commands implemented so far, by canonical name. Each later issue registers its handler here
# (one line each); a command with no handler exits 1 `internal` with a clear message.
HANDLERS: dict[str, Handler] = {
    "_job": handle_job,
    "check": handle_check,
    "list": handle_list,
    "respond": handle_respond,
    "show": handle_show,
    "stop": handle_stop,
    "validate": handle_validate,
}

PR_STATES = ("idle", "settling", "queued", "running", "pending", "waiting", "closed")
_SHA256_RE = re.compile(r"[0-9a-f]{64}")
# Flags only some commands accept (interface: Global flags).
_DRY_RUN_COMMANDS = frozenset(
    {"check", "respond", "confirm", "reject", "edit", "drop-reply", "stop", "prune", "cron"}
)
_OVERRIDE_COMMANDS = frozenset({"respond", "confirm"})
_ALIASES = {"approve": "confirm"}
# Commands that never write state.db: they still work on a schema newer than this binary knows
# (interface: Upgrades). `cron` edits the crontab only.
# `show` runs the finalizer for a running round, which writes round state, yet it is read-only here
# so it has no audit/log writers: the finalizer path opens them lazily (views._finalize).
READ_ONLY_COMMANDS = frozenset({"list", "show", "pending", "export", "validate", "cron"})


class _Parser(argparse.ArgumentParser):
    """Usage errors raise ``CliError`` (exit 2, envelope) instead of exiting from argparse."""

    def error(self, message: str) -> NoReturn:
        raise CliError("usage", message)


def _sha256_arg(text: str) -> str:
    if not _SHA256_RE.fullmatch(text):
        raise CliError("usage", "--expect must be a 64-character lowercase hex sha256")
    return text


def _duration_arg(flag: str) -> Callable[[str], timedelta]:
    return lambda text: parse_duration(text, flag=flag)


def _global_flags(command: str) -> _Parser:
    flags = _Parser(add_help=False, allow_abbrev=False)
    flags.add_argument("--json", action="store_true", help="emit machine-readable JSON output")
    flags.add_argument("--config", metavar="PATH", help="config file")
    if command in _DRY_RUN_COMMANDS:
        flags.add_argument(
            "--dry-run", action="store_true", help="show what would happen and change nothing"
        )
    flags.add_argument("--yes", action="store_true", help="skip a confirmation (human actor only)")
    flags.add_argument("--actor", metavar="A", help="human:<user> or auto:<name>")
    if command in _OVERRIDE_COMMANDS:
        flags.add_argument(
            "--override-escalation",
            action="store_true",
            help="count every escalating hook as continuing (human actor only)",
        )
    flags.add_argument(
        "--decision-id", action="append", metavar="ID", help="audit decision id (repeatable)"
    )
    flags.add_argument(
        "--result-file", metavar="PATH", help="write the JSON output there atomically"
    )
    noise = flags.add_mutually_exclusive_group()
    noise.add_argument("--verbose", action="store_true", help="more diagnostics on stderr")
    noise.add_argument("--quiet", action="store_true", help="no output on success")
    flags.add_argument("--version", action="version", version=f"{PROG} {__version__}")
    return flags


def build_parser() -> argparse.ArgumentParser:
    parser = _Parser(prog=PROG, description=DESCRIPTION, allow_abbrev=False)
    parser.add_argument("--version", action="version", version=f"{PROG} {__version__}")
    # Only the hidden `_job` argv puts --config before the subcommand (interface: Job).
    parser.add_argument("--config", dest="config_top", metavar="PATH", help=argparse.SUPPRESS)
    sub = parser.add_subparsers(dest="command", metavar="<command>", parser_class=_Parser)

    def add(
        name: str, help_text: str | None, *, aliases: Sequence[str] = ()
    ) -> argparse.ArgumentParser:
        # No `help` keyword for the hidden command keeps it out of the command list.
        extra: dict[str, Any] = {} if help_text is None else {"help": help_text}
        return sub.add_parser(
            name,
            aliases=list(aliases),
            description=help_text,
            parents=[_global_flags(name)],
            allow_abbrev=False,
            **extra,
        )

    p = add("check", "run the periodic pass: detect, read comments, start due rounds")
    p.add_argument("--pr", metavar="PR", help="limit the pass to one pull request")

    p = add("respond", "start a round for one pull request now")
    p.add_argument("pr", metavar="PR")
    p.add_argument("--force", action="store_true", help="ignore the round limit (human actor only)")
    p.add_argument("--include-existing", action="store_true")
    p.add_argument("--note-file", metavar="F", help="your note for the agent (file or -)")
    p.add_argument("--skill", metavar="S")
    p.add_argument("--mode", choices=("headless", "interactive"))

    p = add("list", "list pull requests and their state")
    p.add_argument("--state", choices=PR_STATES)
    p.add_argument("--repo", metavar="ID")
    p.add_argument("--waiting", action="store_true", help="same as --state waiting")

    p = add("show", "show one pull request, round or pending confirmation")
    p.add_argument("ref", metavar="<pr | round id | pending id>")

    add("pending", "list pending confirmations")

    p = add("confirm", "perform a pending confirmation", aliases=["approve"])
    p.add_argument("id", metavar="ID")
    p.add_argument("--allow-sensitive", action="store_true", help="human actor only")
    p.add_argument("--expect", metavar="SHA", type=_sha256_arg, help="content_sha256 you saw")

    p = add("reject", "end a pending confirmation")
    p.add_argument("id", metavar="ID")
    p.add_argument("--redo", action="store_true")

    p = add("edit", "edit a reply or the commit message of a pending confirmation")
    p.add_argument("id", metavar="ID")
    p.add_argument("--reply", metavar="COMMENT_ID")
    p.add_argument("--body-file", metavar="F", help="new reply text (file or -)")
    p.add_argument("--message-file", metavar="F", help="new commit message (file or -)")

    p = add("drop-reply", "remove one reply from a pending confirmation")
    p.add_argument("id", metavar="ID")
    p.add_argument("comment_id", metavar="COMMENT_ID")

    p = add("stop", "stop a queued or running round")
    p.add_argument("round_id", metavar="ROUND_ID")

    p = add("cron", "install or remove the managed crontab block")
    p.add_argument("action", choices=("install", "uninstall"))

    p = add("export", "print log and audit records as JSON lines")
    p.add_argument("--since", metavar="T", type=lambda t: parse_since(t, flag="--since"))

    p = add("prune", "remove finished records and old files")
    p.add_argument("--older-than", metavar="D", type=_duration_arg("--older-than"))

    add("validate", "offline checks of config, dependencies and credentials")

    p = add("_job", None)  # hidden: not part of the interface
    p.add_argument("round_id", metavar="ROUND_ID")
    return parser


def _prescan(argv: Sequence[str]) -> tuple[bool, Path | None]:
    """Find ``--json`` and ``--result-file`` even when the full parse fails."""
    scan = _Parser(add_help=False, allow_abbrev=False)
    scan.add_argument("--json", action="store_true")
    scan.add_argument("--result-file")
    try:
        found, _ = scan.parse_known_args(list(argv))
    except CliError:
        return "--json" in argv, None
    path = Path(found.result_file) if found.result_file else None
    return bool(found.json), path


def _check_flags(args: argparse.Namespace) -> Path | None:
    """The usage checks on the config and result-file flags; returns the config path."""
    config: str | None = args.config
    if args.command == "_job":
        if config is None:
            config = args.config_top  # a --config after the subcommand wins
    elif args.config_top is not None:
        raise CliError("usage", "--config goes after the subcommand")
    if config == "":
        raise CliError("usage", "--config needs a path")
    if args.result_file == "":
        raise CliError("usage", "--result-file needs a path")
    return Path(config) if config is not None else None


def _open_store(command: str, *, dry_run: bool) -> Store | None:
    """The state check, made before anything else is read (interface: Files and state).

    `validate` never opens the state. `--dry-run` only reads: a missing state directory is not
    created (no store), an existing one is verified, and `state.db` is used as it is, never
    created or migrated. Every other command creates the directory and migrates; the read-only
    commands (`READ_ONLY_COMMANDS`) tolerate a schema newer than this binary.
    """
    if command == "validate":
        return None
    if dry_run:
        path = state_dir_path()
        if not os.path.lexists(path):
            return None
        return Store.open_read_only(open_state_dir(path, create=False), close_state=True)
    return Store.open(open_state_dir(), write=command not in READ_ONLY_COMMANDS, close_state=True)


def _open_logs(command: str, store: Store | None) -> tuple[JsonlFile, JsonlFile] | None:
    """The checked audit.jsonl and log.jsonl, made right after the state check.

    Not for `validate` or `--dry-run` (no store), nor for the read-only commands: they never
    write any state, so a config warning in `list` is printed and not logged.
    """
    if store is None or store.source is None or command in READ_ONLY_COMMANDS:
        return None
    return open_jsonl_files(store.source)


def _build_context(
    args: argparse.Namespace,
    config_path: Path | None,
    store: Store | None,
    files: tuple[JsonlFile, JsonlFile] | None = None,
) -> Context:
    explicit_actor = parse_actor(args.actor) if args.actor is not None else None
    loaded: LoadedConfig | None = None
    config_error: ConfigError | None = None
    try:
        loaded = load_config(config_path)
    except ConfigError as exc:
        config_error = exc
    actor_source = "default"  # audit inputs.actor_source: flag, tty or default
    if explicit_actor is not None:
        actor = explicit_actor
        actor_source = "flag"
    elif args.command == "_job":
        actor = "auto:review-respond"  # a job acts on its own (interface: Global flags)
    else:
        actor = default_actor(user=loaded.config.me.name if loaded is not None else None)
        if not actor.startswith("auto:"):
            actor_source = "tty"
    check_human_only(
        actor,
        {
            "yes": args.yes,
            "force": getattr(args, "force", False),
            "allow_sensitive": getattr(args, "allow_sensitive", False),
            "override_escalation": getattr(args, "override_escalation", False),
        },
    )
    # `validate` loads the config itself and reports a bad one as its failed `config` check.
    if config_error is not None and args.command != "validate":
        raise config_error.to_cli_error()
    log = LogWriter(files[1]) if files is not None else None
    audit: AuditLog | None = None
    if files is not None:
        audit = AuditLog(
            files[0],
            actor=actor,
            decision_ids=check_decision_ids(args.decision_id or ()),
            config_sha256=loaded.config.sha256() if loaded is not None else None,
            actor_source=actor_source,
            override_escalation=getattr(args, "override_escalation", False),
        )
    if loaded is not None:
        for item in loaded.warnings:
            if log is not None:
                # the log is a record, not a notice: --quiet does not silence it
                log.warning(item.code, key=item.key)
            # --quiet silences notices, never the weakened-sandbox warning (a safety setting).
            if args.quiet and item.code != "sandbox_weakened":
                continue
            warn(f"{PROG}: warning: {item.code}: {sanitize_line(item.message)}\n")
    return Context(
        json=args.json,
        config=config_path,
        dry_run=getattr(args, "dry_run", False),
        yes=args.yes,
        actor=actor,
        override_escalation=getattr(args, "override_escalation", False),
        decision_ids=tuple(check_decision_ids(args.decision_id or ())),
        result_file=Path(args.result_file) if args.result_file is not None else None,
        verbose=args.verbose,
        quiet=args.quiet,
        settings=loaded.config if loaded is not None else None,
        store=store,
        audit=audit,
        log=log,
    )


def _not_implemented(command: str) -> Handler:
    def handler(_ctx: Context, _args: argparse.Namespace) -> Outcome:
        raise CliError("internal", f"{command} is not implemented yet")

    return handler


def _report(error: CliError, ctx: Context) -> int:
    label = f"{error.code} ({error.reason})" if error.reason else error.code
    warn(f"{PROG}: error: {label}: {sanitize_line(error.message)}\n")
    try:
        return emit(error_envelope(error), "", error.exit_code, ctx)
    except Exception:  # emit does not raise; this keeps the exit code whatever happens
        return error.exit_code


def main(argv: list[str] | None = None) -> int:
    args_list = list(sys.argv[1:] if argv is None else argv)
    pre_json, pre_result = _prescan(args_list)
    fallback = Context(json=pre_json, result_file=pre_result)
    parser = build_parser()
    ctx = fallback
    opened: Store | None = None
    files: tuple[JsonlFile, JsonlFile] | None = None
    try:
        args = parser.parse_args(args_list)
        if args.command is None:
            if not pre_json:
                parser.print_help(sys.stderr)
            raise CliError("usage", "a command is required")
        raw: str = args.command
        command = _ALIASES.get(raw, raw)
        args.command = command
        config_path = _check_flags(args)
        # The state check comes before the config is read (a refusal there must not depend on it).
        opened = _open_store(command, dry_run=getattr(args, "dry_run", False))
        files = _open_logs(command, opened)
        ctx = _build_context(args, config_path, opened, files)
        handler = HANDLERS.get(command) or _not_implemented(command)
        outcome = handler(ctx, args)
        return emit(outcome.payload, outcome.human, outcome.exit_code, ctx)
    except CliError as error:
        return _report(error, ctx)
    except KeyboardInterrupt:
        return _report(CliError("internal", "interrupted"), ctx)
    except Exception as exc:
        if ctx.verbose:
            lines = traceback.format_exc().splitlines()
            warn("".join(sanitize_line(line) + "\n" for line in lines))
        internal = CliError("internal", f"unexpected {type(exc).__name__}")
        return _report(internal, ctx)
    finally:
        for file in files or ():
            file.close()
        if opened is not None:
            opened.close()
