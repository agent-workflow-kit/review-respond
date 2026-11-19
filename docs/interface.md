# review-respond: interface

Version 0.1.0-design. This document is the single normative source for review-respond's rules. `docs/design.md`, the README and the architecture link to its headings and never restate its tables. Sibling tools are named in the Dependencies section; their own `docs/interface.md` files are normative for them.

## Overview

review-respond answers review comments on pull requests and merge requests that you opened. For each of your open PRs it reads the comments through forgectl, decides which are eligible to start work, and runs one round at a time: it creates a throwaway checkout at the PR head, forks the PR's agent session into it, and gives the fork the eligible comments as fenced data. The fork edits files in the throwaway and reports an artifact (replies, the files it changed or deleted, a proposed commit message). It cannot stage, commit, push or reach the network.

The result is a pending confirmation. `confirm` shows you the diff, then copies only the listed files into a second, clean throwaway, commits there with your identity through hardened git, pushes fast-forward only to the PR head ref, and posts the replies through pr-comment. Every pending confirmation needs a human `confirm`; there is no automatic mode in 0.x.

A PR whose head lives in a fork gets no round of any kind in 0.x. Review bodies (forgectl `reviews`) are not read in 0.x; inline and general comments are.

review-respond works standalone: forgectl, agent-spawn, pr-comment, wtctl and git are required; jobsctl is optional.

## Dependencies

| Tool | Binding | Used for | Minimum |
|---|---|---|---|
| forgectl | A | `prs`, `show`, `comments`, `access`, `whoami`, `compare` | 0.55.2 |
| agent-spawn | A | `list`, `show`, `resume --fork`, `launch`, `wait`, `stop`, `teardown` | 0.30.0 |
| pr-comment | A | `reply`, `resolve` | the first release with `reply` and `resolve` (pinned when it exists; until then `validate` reports `tool_too_old` and a `confirm` with replies exits 12 `tool_too_old`) |
| wtctl | A | `create --throwaway`, `extract`, `locate`, `remove`, `list`, `label add`, `show` | 0.31.0 |
| claude-session | A, `validate` only | `kinds --kind respond --json` (the sandbox probe) | 0.45.2 |
| git | A | hardened commit, diff and push (see Hardened git) | 2.34 (the `live` suite runs on the floor before 1.0) |
| jobsctl | A, optional | the `respond` queue (`add`, `show`, `cancel`, `resolve`) | 0.32.1 |

review-respond does not use pr-watch. Any other program (the harness, cron) may run `review-respond check` as an early trigger; `check` is idempotent.

`validate` checks each dependency's `--version` against the Minimum column and a missing or too-old dependency fails with a message naming its install command. A command that needs a dependency exits 12 with `error.code: tool_missing` or `tool_too_old` before doing anything.

## Global flags

| Flag | Meaning |
|---|---|
| `--json` | machine output; every command that prints data supports it (schemas in `schemas/cli/`) |
| `--config PATH` | config file (see Config) |
| `--dry-run` | on `check`, `respond`, `confirm`, `reject`, `edit`, `drop-reply`, `stop`, `prune`: show what would happen and change nothing |
| `--yes` | skip a confirmation; accepted only with a human actor (a TTY, or `--actor human:<user>`). `--yes`, `--force` and `--allow-sensitive` with an automatic actor exit 2 `human_only` |
| `--actor A` | `human:<user>` or `auto:<name>`; the default is `human:<user>` on a TTY and `auto:unknown` otherwise. Every action review-respond takes on its own (a job, `check`, a sweep) records the actor `auto:review-respond`; an `--actor auto:<name>` given by a caller is recorded as given |
| `--decision-id ID` | repeatable; recorded in audit and passed through to pr-comment by `confirm` |
| `--result-file PATH` | write the `--json` output there atomically, whatever the exit code |
| `--verbose`, `--quiet`, `--version` | as in the design guide |

Global flags go after the subcommand. Free text (a note, a reply, a commit message) is always taken from a file or stdin (`--note-file`, `--body-file`, `--message-file`, `-`), never from argv. Flag durations (`--older-than`, `--since`) use the config grammar with `h` and `d`; `--since` also takes an RFC 3339 time with an offset.

## Exit codes

| Code | Meaning here |
|---|---|
| 0 | success (including a no-op; `check` and `prune` also report `already_running`) |
| 1 | internal error |
| 2 | usage error |
| 10 | not found (PR, round or pending id) |
| 11 | vetoed by a `pre-round` hook (`respond`) or a `pre-push` hook (`confirm`) |
| 12 | failed for a reportable reason (a dependency failed, a push failed, the outcome of a step is unknown) |
| 13 | needs a human (`confirm` without a human actor, a `pre-push` or `pre-round` hook escalated) |
| 20 | refused; `reason` says why |
| 21 | ambiguous selector (an id prefix matches several PRs, rounds or pending records) |
| 22 | `nothing_to_respond`: `respond <pr>` found no eligible unhandled comment |

### Refusal reasons (exit 20)

`live` (an agent working on the PR is live), `busy` (another command holds the PR's lease), `pending_exists` (the PR has a pending confirmation), `round_active` (a round of the PR is queued or running), `round_limit`, `remote_moved`, `diff_changed` (`details.cause`: `expect_mismatch`, `recomputed` or `path_state`), `sensitive_paths`, `expired`, `not_pending`, `ineligible` (the PR is not yours, is closed or merged, is a fork PR, or its head is the repository's default branch; `details.reason` says which), `pr_closed`. A refusal carries `reason` and `details` in the error envelope. `--yes` never overrides a refusal. A human `--force` overrides only `round_limit`; `live` is never overridden.

### Error envelope

With `--json`, errors print `{"error": {"code", "message", "reason"?, "details"?}}` on stdout and the message on stderr. `lower_error` (`{code, message, reason?, details?}`) carries a sibling's error object when a sibling failed.

### Error codes

| Exit | `error.code` | Notes |
|---|---|---|
| 1 | `internal`, `lease_lost`, `lease_timeout` | |
| 2 | `usage`, `config_invalid`, `human_only` | `human_only`: `--yes`, `--force` or `--allow-sensitive` with an automatic actor |
| 10 | `not_found` | PR, round or pending id |
| 11 | `vetoed` | a `pre-round` hook (`respond`) or a `pre-push` hook (`confirm`) |
| 12 | `tool_missing`, `tool_too_old`, `lower_failed` (with `lower_error`), `all_hosts_failed`, `push_failed`, `reply_failed`, `outcome_unknown`, `identity_missing`, `throwaway_missing`, `state_unsafe`, `version_skew` | |
| 13 | `confirmation_required`, `escalated` | `confirm` without a human actor; a `pre-push` or `pre-round` escalation |
| 20 | `refused` | `reason` is one of the refusal reasons above |
| 21 | `ambiguous` | an id prefix matching several records |
| 22 | `nothing_to_respond` | `respond` found no eligible unhandled comment |

## Commands

### `check`

The one periodic pass. In order, under the `pass` lease (Concurrency; a second caller exits 0 with `already_running: true`):

1. Resolve your account id per host (`forgectl whoami`, cached).
2. List your open PRs per configured host and update the stored PR state (Detection).
3. Read the comments of each listed non-fork PR, least recently read first within the budget; update the comment records and compute the eligible unhandled comments (Eligibility, Handled set).
4. Start a round for each PR that is due (Settle and round start), oldest due first, up to the job cap; the `pre-round` hooks run here.
5. Housekeeping: finalize rounds whose job died (Round states), resolve held jobs, expire `pending` records, close `pending` records and stop rounds of closed PRs (Reject, expiry and closed PRs), compare the head of every PR with a `pending` record that has files (Confirm order, step 2), tear down forks marked for teardown (Fork lineage).
6. Deliver queued hook events.

`check --pr <pr>` limits the pass to one PR; it takes the same `pass` lease (the early trigger is dropped while a pass runs; the next pass covers it) and changes no host-wide state. `check` never waits for a round. It takes the `pr:` lease of each PR step without waiting; a PR whose lease is held is skipped and reported with `lease_held`. Exit 0, or 12 `all_hosts_failed` when no host could be read; a vetoed, escalated or failed PR is a per-PR result (`prs[]` with `state`, `started_round`, `lease_held`, `errors`), never the exit code. `check` never exits 11 or 13.

### `respond <pr> [--force] [--include-existing] [--note-file F] [--skill S] [--mode headless|interactive]`

Starts a round for one PR now, ignoring the settle delay but not the other rules. `--force` ignores the round limit (a human actor only). `--include-existing` applies to that round only: it also answers the `seen` comments you choose (they become `delivered`; the rest stay `seen`). `--note-file` adds your note to the prompt as trusted text in its own block (at most 8 KiB, else exit 2). On a PR in `waiting` with block `escalated`, `failed`, `hook_vetoed` or `hook_escalated` a human `respond` clears the block and starts a round; `round_limit` needs `--force`. Refusals: `live`, `busy`, `pending_exists`, `round_active`, `round_limit`, `ineligible`. Exit 22 when no eligible comment exists, 11 or 13 from a `pre-round` hook, 0 with the round object once the round is created.

### `list [--state S] [--repo ID] [--waiting]`

PRs and their state (Settle and round start, PR state): `idle`, `settling`, `queued`, `running`, `pending`, `waiting`, `closed`. `waiting` rows carry `waiting_reason`, one of `escalated`, `failed`, `round_limit`, `hook_vetoed`, `hook_escalated`, `no_agent`, `agent_live`, `capacity`. `--waiting` equals `--state waiting`. Each row carries the counts of unhandled eligible, `seen`, ineligible and edited comments, the round count, the base agent and the pending id when one exists.

### `show <pr | round id | pending id>`

One object in full. A pending confirmation shows the stored diff, the commit message and each reply, sanitized and marked agent-written, the off-limits paths and the binary paths touched, the count of non-ASCII characters in added lines, and the comments that were delivered and got no reply. A round row prints `finalize` (`done`, `busy`, or null) when `show` ran the finalizer. A stored file whose sha256 differs from the record exits 12 `state_unsafe`. Exit 10 when nothing matches, 21 when an id prefix matches several objects. `show` dispatches by prefix (Objects, Values).

### `pending`

Lists pending confirmations: `id`, `tool`, `action`, `target`, `pr`, `created`, `summary`, `expires`, `state`, `diff_sha256`, `files_changed` and `reply_count`.

### `confirm <id>` (alias `approve`) `[--allow-sensitive] [--expect-diff SHA]`

Performs the pending confirmation (Confirm order). Without a TTY and without `--yes --actor human:<user>` it exits 13 and does nothing. `--expect-diff` binds the call to the diff hash that `show` printed; a different hash refuses with `diff_changed`. `--allow-sensitive` is human only and allows a diff that touches off-limits paths or binary paths after you saw it. Re-running `confirm` resumes a record in `pushing`, `pushed` or `posting` (Record).

### `reject <id> [--redo]`

Only a `pending` record (see the state table in Record). Removes the round throwaway, marks the round's fork for teardown, marks the record `rejected` and the round `rejected`. With `--redo` the round's comments become unhandled again so that the next `check` starts a fresh round (it counts toward the round limit, also after a `remote_moved` refusal). Rejecting a `rejected` record is a no-op.

### `edit <id> [--reply COMMENT_ID --body-file F] [--message-file F]`

Replaces one reply or the commit message, only in the states the Record table allows. The diff cannot be edited. An edited record is marked `edited`.

### `drop-reply <id> <comment_id>`

Removes one reply from the pending confirmation, only in the states the Record table allows.

### `stop <round id>`

Ends a queued or running round through agent-spawn; the closed-PR sweep uses the same rule with actor `auto:review-respond` and `cancel_reason: pr_closed`.

```text
stop(round):
  take lease pr:<pr>
  if round is terminal: exit 0 (no-op, changed false)
  if round is pending: exit 20 pending_exists (hint: reject)
  CAS set cancel_requested_at, cancel_reason (stopped or pr_closed)
  if queued and a jobsctl job id is stored: jobsctl cancel <job>; a refusal "live" means it started: continue as running
     a queued round whose job was canceled -> canceled
  if running and agent_id is known: agent-spawn stop <agent_id> --actor <actor>
  (a running job without an agent yet sees the flag before it launches the agent)
  exit 0 with the round (state as stored, cancel_requested_at set)
```

The round ends `canceled` when its job or a finalizer maps the stopped outcome (Run outcomes). A running round is never stopped with `jobsctl cancel --force`. Without jobsctl no signal is needed: the detached job reads the flag. The round's comments stay unhandled.

### `cron install|uninstall [--dry-run]`

Writes or removes one managed crontab block (Files and state). Entries have an explicit `PATH` and an optional env-file prefix.

### `export [--since T]`

Prints `log.jsonl` and `audit.jsonl` records as JSON lines.

### `prune [--older-than D] [--dry-run]`

Takes the `pass` lease. Removes finished round and pending records (terminal states only), comment records of closed PRs and their stored files older than the retention age (default 30 days). It removes throwaways labeled `app=review-respond` that are older than one day and whose `round=` or `pending=` label names no non-terminal record (found with `wtctl list --label`). It never touches a record in `pending`, `pushing`, `pushed` or `posting`, or a live round.

### `validate`

Offline checks: `config` (parse), `dependencies` (each `--version` against the Minimum column), `sandbox` (the probe through `claude-session kinds --kind respond --json`), `cron_path` (every directory of the managed block's `PATH` exists and holds the resolved binaries of review-respond, forgectl, agent-spawn, pr-comment, wtctl, git and, when used, jobsctl) and `credentials` (presence of the variables named in config). It does not inspect wtctl's configuration: wtctl has no command that prints it, so the README tells you to set wtctl's `prune.protect_labels` to include `app` and to leave `throwaway.max_age` null or longer than `pending_ttl`. Never prompts and never sends anything. Exit 0, 12 on a failed check.

## Detection and eligibility

### Detection

Per host H in `hosts_to_check` (default: the hosts that `forgectl whoami` lists with a credential), every `check` pass does this:

```text
listing = forgectl prs --host H --author me --state open --limit 1000 --json     (cost 1 per 100 items)
host error -> host failed, nothing on H changes state this pass
upsert every listed PR (state open, head, is_fork, created_at)
if listing.complete: a stored open PR of H that is not listed is read with forgectl show (budget) to learn closed or merged
work = listed non-fork PRs, ordered by last_comments_read_at ascending (never read first)
for pr in work while budget(H) allows (cost: 1 + pages of the previous read, plus 2 per uncached access lookup):
   forgectl comments <pr>; update comment records; eligibility; due checks
PRs not reached wait for the next pass (they come first then)
```

There is no cursor and no `updated_at` filter: every pass reads every open PR, least recently read first, because a host's `updated_at` does not change on every edit, deletion or resolution. A per-host request budget (`limits.requests_per_host`, default 120 per pass) bounds a pass. A failed listing for one host does not stop the other hosts. When a decision needs facts, `forgectl show` supplies the head ref and sha, head repo, fork flag, base, state and draft flag. The end of the first successful pass is stored as `installed_at` (Handled set).

A forgectl read that reports `incomplete` never marks anything as deleted. `not_found` for a PR means unknown, never closed: a PR counts as closed only when forgectl reports `state` `closed` or `merged`. Review bodies (forgectl `reviews`) are not read in 0.x; a review's inline comments are.

### Eligibility

A comment is eligible when all of these hold:

1. Its author id is not your own account id on that host. A comment that carries pr-comment's marker on content from your own account is `own` whatever the prefix. Opt-in `respond_to_own_comments_prefix` (default off; for example `/awkit`) admits your other own comments that start with the prefix.
2. Its author id is not null (else `unknown_author`), `author.is_bot` is exactly `false` (`null` counts as a bot), and the author is not in `bot_authors`.
3. The author id is in `reviewers` (entries `<host>:<id>`, split at the last `:`; a per-repo `reviewers` list replaces the global one), or `forgectl access <repo> <login>` reports `write_access: true`. A `null` or `false` answer is not eligible. Answers are cached per (repo, author id) for `access_cache_ttl` (default 1 hour); a cached answer is used only when the comment's login equals the stored login (a renamed or re-registered login is a miss).
4. The PR is yours, open and not a fork (`head.is_fork` is false and `head.repo` equals the PR's repo; a null `is_fork` counts as a fork). A comment on a closed or merged PR or on a fork PR is never eligible.
5. The thread is not resolved, the comment is not deleted and it is not minimized. Draft comments are not skipped.

An ineligible comment is recorded with its reason (`own`, `bot`, `unknown_author`, `no_access`, `unknown_access`, `resolved`, `minimized`, `closed`, `fork_pr`) and never reaches a prompt. `show` lists them. With `post-attention` reason `ineligible_comment` enabled (`attention.ineligible_comment`, default off) the first ineligible comment of a PR fires one hook event. Comments of fork PRs are not read in detail; they are recorded once for `show`.

### Handled set

review-respond stores, per comment id (the full forgectl id, for example `gh:rc:<node id>`), the body sha256, the last `updated_at`, the state, the round, `delivered_sha256`, `handled_sha256`, `first_seen_at`, and an eligibility flag with its reason (`eligible`, `reason`), re-evaluated on every read; an ineligible comment never leaves `seen` or `unhandled`. Ids of different kinds never collide. The states are `seen`, `unhandled`, `delivered`, `handled` and `deleted`; "unhandled" in prose means the state `unhandled`. A comment is **handled** when a round produced a valid artifact and the pending confirmation was recorded in one transaction, or when its thread was resolved.

| From | Event | To |
|---|---|---|
| (none) | first read of a PR created before `installed_at` | `seen` |
| (none) | a new id on a known PR, or on a PR first seen with `created_at` at or after `installed_at` | `unhandled` |
| `seen` | `respond --include-existing` selects it for that round | `delivered` |
| `unhandled` | a job selects it into a prompt | `delivered` (`round`, `delivered_sha256`) |
| `delivered` | its round becomes `pending` | `handled` (`handled_sha256` equals `delivered_sha256`) |
| `delivered` | its round ends `failed`, `canceled` or `escalated` | `unhandled` |
| `handled` | `reject --redo` of its round | `unhandled` |
| `handled` | its current body hash differs from `handled_sha256`, its thread is unresolved, its author is still eligible and no record of the PR is open | `unhandled`, `edited: true` |
| `seen`, `unhandled`, `handled` | its thread is resolved | `handled` (`handled_reason: resolved`) |
| any but `deleted` | absent from a read with `complete: true` | `deleted` (terminal) |

Notes. A comment cut from a prompt by the budget is never `delivered`; it stays `unhandled`. `installed_at` is the end of the first successful `check` pass: a PR created after it has its first comments answered, a PR created before it has them recorded as `seen` (installing review-respond never answers old comments), and `list` shows the `seen` count. A changed `updated_at` with an equal hash does nothing. A deleted comment drops out of prompts and is never retried; a pending reply to it is posted to its thread when the thread still exists and skipped otherwise (Replies). A thread resolved by someone else removes its comments from prompts and a pending reply to it is skipped; review-respond never un-resolves a thread. **Settle timestamp:** `settle_ts(c)` is the local time review-respond first stored the comment's current body hash (an edit restarts it), so host clock skew does not matter. A comment edited while its round is running or its record is open keeps `delivered` or `handled`; the edit is acted on after the record ends (the `handled` to `unhandled` row), and `confirm` skips its pending reply (Replies).

### Settle and round start

A PR is **due** when this holds:

```text
due(pr):
  pr is open and not a fork
  and eligible_unhandled(pr) is not empty
  and no round of pr is queued or running
  and no record of pr is pending, pushing, pushed or posting
  and pr.block is null
  and counted(pr) < max_rounds        # else set block round_limit and fire post-attention round_limit once
  and resolve_base(pr) is an agent or a fresh-session opt-in (repos.<id>.respond_fresh)   # else waiting no_agent, post-attention no_agent once per episode
  and that agent and the PR's base fork are not live (status launching, live or tearing_down, or promotion pending)
  and now - max(settle_ts(c) for c in eligible_unhandled(pr)) >= settle
```

`check` starts rounds for due PRs up to the job cap, oldest due first. A round's prompt holds the eligible unhandled comments at the moment its job starts. An attempt-2 retry is due under the same rule: a failed attempt 1 whose reason is retried sets no block.

**Counting.** A round attempt is **counted** when its run started: agent-spawn reported an outcome other than `not-started` (exit 0 after a run, 13 from the session phase, 14 to 18), or `agent-spawn show` gives its run a non-null `first_working_at`. Every counted attempt counts, including the automatic retry and the round after `reject --redo`. The PR's limit is reached when the number of counted attempts is at least `max_rounds`. At the limit, `post-attention` reason `round_limit` fires once per block episode; only `respond --force` or a higher `max_rounds` continues.

**Stored PR block** (`prs.block`, null or `{reason, set_at, hook, comment_set_sha256}`) keeps escalated, failed, limited and vetoed PRs from re-firing:

| Block reason | Set when | Cleared when |
|---|---|---|
| `escalated` | a round ended `escalated` (the agent asked for input, or a lower hook escalated its launch) | a human `respond` |
| `failed` | a round attempt 2 ended `failed`, or attempt 1 ended `failed` with a reason that is not retried | a human `respond` |
| `round_limit` | a due check finds the limit reached | a human `respond --force`, or `max_rounds` raised above the count |
| `hook_vetoed` | a `pre-round` hook vetoed | a human `respond`, or the set of eligible unhandled comment ids differs from `comment_set_sha256` (a new or re-opened comment) |
| `hook_escalated` | a `pre-round` hook escalated (`hook` holds the hook's name) | a human `respond`, which skips the hook named in `hook` for that call |

**PR state** (derived, the first matching row wins; `list` and `show` print it):

```text
if host state is closed or merged                  -> closed
elif a round of the PR is queued                   -> queued
elif a round of the PR is running                  -> running
elif a record is pending, pushing, pushed or posting -> pending
elif block is set                                  -> waiting (waiting_reason = block.reason)
elif no eligible unhandled comment                 -> idle
elif agent resolution gives no agent               -> waiting (waiting_reason = no_agent)
elif the original agent or the base fork is live   -> waiting (waiting_reason = agent_live)
elif the settle time has not passed                -> settling
else                                               -> waiting (waiting_reason = capacity)
```

`pre-round` hooks run after the due rule and before the round row is created (Hooks). A veto creates no round, sets block `hook_vetoed`, makes `check` report `vetoed` for that PR (exit 0) and makes `respond` exit 11. An escalation creates no round, sets block `hook_escalated`, fires `post-attention` reason `escalated` with `details.cause: hook`, leaves `check` at exit 0 and makes `respond` exit 13.

## Rounds

### Round states

A round has the number `number` (1 for a PR's first round, incremented for every new round of the PR, comment-triggered or `respond`) and an `attempt` (1, or 2 for the one automatic retry of a failed round, which keeps its `number`). Every transition is a compare-and-set on the round's `rev` (Concurrency).

| From | To | Trigger | Same transaction |
|---|---|---|---|
| (none) | `queued` | the due rule holds, the `pre-round` hooks continued, a job slot was requested (`check`, `respond`) | round row (number, attempt, base agent id, trigger, forced) |
| `queued` | `running` | the job started, re-read the PR (open, not a fork, head sha) and selected at least one comment (`_job`) | `head_sha`; each selected comment becomes `delivered` with `delivered_sha256` |
| `queued` | `canceled` | `stop`, PR closed, job canceled before it ran, or at job start no eligible unhandled comment remains (`cancel_reason` `stopped`, `pr_closed`, `nothing_to_respond`) | not counted |
| `running` | `pending` | the run ended `done`, the artifact validated, the stored diff was built | pending record created; delivered comments become `handled` |
| `running` | `escalated` | agent-spawn exit 13 (Run outcomes) | delivered comments back to `unhandled`; PR block `escalated`; fork marked for teardown |
| `running` | `failed` | any other end (Run outcomes) | delivered comments back to `unhandled`; fork marked for teardown; round throwaway removed |
| `running` | `canceled` | `cancel_requested_at` is set and the run ended stopped, or the job saw the flag before it launched the agent | delivered comments back to `unhandled`; fork marked for teardown |
| `pending` | `confirmed` | its pending record reached `done` (`confirm`) | |
| `pending` | `rejected` | its pending record became `rejected` (`reject`) | with `--redo`, its comments back to `unhandled` |
| `pending` | `expired` | its pending record became `expired` (`check`) | |
| `pending` | `closed` | its pending record became `closed` (`check`) | |

Terminal states: `confirmed`, `rejected`, `expired`, `closed`, `escalated`, `failed`, `canceled`; they are immutable. A round stays `pending` while its record is in `pending`, `pushing`, `pushed` or `posting`. A failed attempt 1 whose reason is retried gives the PR no block; the next due `check` starts attempt 2. A failed attempt 2, or a reason that is not retried, sets block `failed` and fires `post-attention` reason `failed` once. An `escalated` round is never retried: `post-attention` reason `escalated` fires and `respond <pr>` (optionally with `--note-file`) starts a new round.

`check`, `show` and `confirm` run the finalizer for a `running` round whose `round:` lease is expired and whose holder is verified gone:

```text
finalize(r):
  if r.agent_id is null:
    cands = agent-spawn list --label round=<r.id> --label owner=review-respond --json
    accept c only if (fork: c.forked_from == r.base_agent_id) or (fresh: c.run_kind == respond and c.cwd == realpath(round throwaway))
    exactly one accepted: record it as r.agent_id
    none: -> failed (job_lost); remove the throwaway
    several: record none, mark every accepted candidate for teardown, -> failed (job_lost)
  a = agent-spawn show r.agent_id --json
  if a.status in (launching, live, tearing_down) or promotion pending: leave r running (try again next pass)
  else finish_round(r, outcome of a.runs[-1])
```

### Run outcomes

One table maps every exit of `resume` or `launch` and of `wait`, with the result's `phase`, `outcome` and `end_reason`, into the round. The `_job` column is the job's exit code; it only feeds jobsctl, the round row is the truth (0 pending recorded or canceled, 12 failed, 13 escalated, 1 internal).

| agent-spawn exit | Condition | Round state | `failure.reason` | Counted | Automatic retry | `_job` exit |
|---|---|---|---|---|---|---|
| 0 | `resume` or `launch` without `--wait`: the run started | stays `running`, agent id recorded | | yes | | (continues) |
| 0 | `wait`: outcome `done`, artifact valid, diff built | `pending` | | yes | | 0 |
| 0 | `wait`: outcome `done`, validation or diff failed | `failed` | `artifact_missing`, `artifact_invalid`, `implicit_done`, `empty_result`, `diff_failed` | yes | yes | 12 |
| 10 | base agent not found, torn down or `session_missing` | `failed` | `agent_gone` (the base is reset) | no | yes | 12 |
| 11 | a claude-session `pre-launch` hook vetoed | `failed` | `vetoed` | no | no | 12 |
| 12 | launch failed, `not-started`, `no_run`, `tool_missing`; end reason `canceled` with `cancel_requested_at` set | `canceled` | | no | | 0 |
| 12 | the same, any other case | `failed` | `launch_failed` | no | yes | 12 |
| 13 | session phase (report `blocked` or `needs-input`) | `escalated` | | yes | no | 13 |
| 13 | launch phase (envelope `code: escalated`, a lower hook) | `escalated` (`details.cause: hook`) | | no | no | 13 |
| 14 | `exited-unreported` | `failed` | `unreported` | yes | yes | 12 |
| 15 | `killed` with end reason `stopped` or `forwarded_*` and `cancel_requested_at` set | `canceled` | | yes | | 0 |
| 15 | `killed`, any other case | `failed` | `agent_failed` | yes | yes | 12 |
| 16 | `timeout` | `failed` | `timeout` | yes | yes | 12 |
| 17 | `lost` | `failed` | `agent_lost` | yes | yes | 12 |
| 18 | `failed` | `failed` | `agent_failed` | yes | yes | 12 |
| 20 | refused (`live`, `tearing_down`, `third_party_cwd`, `third_party_run`, `tainted`) | `failed` | `refused` | no | yes | 12 |
| 1, 2, 21, 22 | internal, usage, ambiguous (22 cannot occur: no wait timeout is passed) | `failed` | `internal` | no | no | 1 |
| 130, 143 | the job was interrupted | the SIGTERM path (Job) | | | | |
| wtctl | `create --throwaway` exit 10, 12 or 20 | `failed` | `throwaway_failed` | no | yes | 12 |
| (none) | job gone, no agent found (finalizer) | `failed` | `job_lost` | no | yes | |

The closed `failure.reason` values are `throwaway_failed`, `launch_failed`, `vetoed`, `refused`, `agent_gone`, `unreported`, `agent_failed`, `agent_lost`, `timeout`, `job_terminated`, `job_lost`, `artifact_missing`, `artifact_invalid`, `implicit_done`, `empty_result`, `diff_failed` and `internal`. An invalid artifact carries a closed `failure.detail` (Artifact).

### Fork lineage

review-respond keeps a **base agent** per PR (`prs.base_agent_id`). It acts (resume, stop, teardown) only on agent ids in its own `forks` records: label searches only find candidates, never select its own forks and never cause a teardown. The original agent is never torn down, stopped or labeled by review-respond.

```text
resolve_base(pr):
  if pr.base_agent_id:
     a = agent-spawn show <pr.base_agent_id> --json
     exit 10 or status torn_down: pr.base_agent_id = null (the confirmed context is lost; warning base_lost)
     else return a
  if the PR reference is not label-safe (Labels): cands = []
  else cands = agent-spawn list --label pr=<pr> --json
  keep c only if c.agent_id is not in forks, c.repo == pr.repo, c.worktree is not null,
     wtctl show <c.worktree.record_id> --json has branch == pr.head_ref, and c.run_kind is not third-party-reading
  preferred = [c for c in cands if label owner=<prefer_owner>]
  return newest(preferred) or newest(cands) or FRESH if opted in (repos.<id>.respond_fresh) or NONE
```

A label is not authenticated, so the match is verified against the worktree and branch as above; among equal candidates the most recent wins. Each round forks the base agent into the round's throwaway. The lineage updates are transactional:

```text
lineage updates (forks table states: base, round, superseded, teardown_pending, torn_down):
  round created: round fork row role round
  record pushed (files) or done (replies only): pr.base_agent_id = round fork; previous base fork (if ours) -> teardown_pending   [same transaction]
  round failed, escalated, canceled, rejected, expired: round fork -> teardown_pending
  PR closed: base fork and every round fork -> teardown_pending
  check: for each teardown_pending: agent-spawn teardown <id> --actor auto:review-respond; exit 0 or 10 -> torn_down;
         20 live -> agent-spawn stop first, retry next pass; after 3 failed passes post-attention teardown_refused once
```

With no original agent, a PR in a repo opted in with `repos.<id>.respond_fresh: true` gets a fresh `respond-to-review` session in the round's throwaway; after a confirm it is the base like any other fork. A fresh session knows the PR only from the comments and the files. A fork PR gets no round of any kind in 0.x (Eligibility).

A fork is tainted (architecture section 8.5). review-respond never resumes it in a kind that does not read third-party text and never passes `--allow-tainted`.

### Job

A round runs as one job: the hidden command `review-respond _job <round id>` (not part of the interface; its argv is `<absolute review-respond> [--config <absolute path>] _job <round id>`).

```text
_job(round_id):
  take lease round:<round_id> (Concurrency); if held by a live holder: exit 0
  r = load round; if r.state != queued: exit 0
  if r.cancel_requested_at: -> canceled (stopped); exit 0
  pr = forgectl show <pr>                     # fresh read
  if pr closed or merged: -> canceled (pr_closed); exit 0
  if pr is a fork: -> canceled (nothing_to_respond); exit 0   # defensive
  comments = select(eligible unhandled, prompt budget)
  if comments empty: -> canceled (nothing_to_respond); exit 0   # not counted
  CAS queued -> running with head_sha = pr.head.sha, delivered ids and hashes
  tw = wtctl create --throwaway --repo <id> --ref <head_sha> --label app=review-respond --label role=round --label round=<round_id> --json
       failure -> failed (throwaway_failed)
  record tw id; write prompt file and generated profile
  if r.cancel_requested_at: remove tw; -> canceled (stopped)
  res = agent-spawn resume <base> --fork ... --json     (or launch ...; Session settings; no --wait)
       exit 0: record agent_id from res.agent.agent_id (CAS)
       other exit: map with Run outcomes; exit
  if r.cancel_requested_at: agent-spawn stop <agent_id> --actor auto:review-respond
  res = agent-spawn wait <agent_id> --json
  finish_round(r, res)       # the same function the finalizer calls
finish_round:
  map the exit with Run outcomes; on exit 0 read agent-spawn show <agent_id> --json, validate the artifact (Artifact),
  build the stored diff (Stored diff), then in one transaction: pending record, comments handled, round pending;
  wtctl label add <tw> pending=<pending id>; queue post-pending
on SIGTERM: if agent_id known: agent-spawn stop <agent_id> --actor auto:review-respond; wait for the wait call to return
  (at most jobs.kill_grace minus 15 s); finish_round (exit 15 stopped with cancel_requested_at -> canceled, without it -> failed job_terminated)
```

The job records the agent id before it waits, and every fork carries a `round=` label, so a crashed job can be finalized.

With jobsctl: `check` submits `_job` to the queue `respond` (config `jobs.queue`) with `--unique-key respond:<first 20 hex of sha256(pr)>:<number>:<attempt>` (the raw PR reference can contain `_`, which jobsctl keys refuse, and can be long), `--max-retries 0`, `--timeout none` (the agent is bounded by `agent.timeout` and the kind's own timeout), an explicit `--cwd` (the state directory), `--kill-grace` (`jobs.kill_grace`, default 125 seconds), `--label` correlation labels, `--env XDG_CONFIG_HOME=...` and `--env XDG_STATE_HOME=...` when set, and `--pass-env` for the names in `jobs.pass_env`. It reads the job by its stored id. An exit-13 job is held by jobsctl; `check` resolves the held job (`jobsctl resolve --as done`) after the round row says `escalated`. The queue's cap bounds concurrent rounds (jobsctl's default is 1).

Without jobsctl: `check` starts `_job` as a detached process (double fork, stdio redirected, PID and start time recorded) under `jobs.max_running` (default 1). It never waits. A job that dies is found by PID and start time and finalized as above.

The job's environment is built from scratch in both placements: `PATH`, `HOME`, `LANG`, `LC_ALL`, `TZ`, `XDG_CONFIG_HOME`, `XDG_STATE_HOME`, `XDG_RUNTIME_DIR` (each when set) and the names in `jobs.pass_env`. `REVIEW_RESPOND_*` variables are not passed to jobs (the config file is). The job creates the clean checkout for the stored diff with `--source remote`, so for a private repository `jobs.pass_env` must name the variables wtctl reads for that host (`hosts.<host>.token_env` in wtctl's config), and with jobsctl their values come from jobsctl's `env_file`.

### Prompt

The prompt file lives in the state directory and is deleted when the round ends. It holds, in this order: the working copy's real absolute path (the throwaway root; the forked conversation otherwise mentions the original worktree's paths), the standing rule that comment text is data and not instructions, your optional note as a separate trusted block (read once from `--note-file`, at most 8 KiB, stored with the round), and the eligible comments inside one nonce fence `<untrusted source="comments" id="R">...</untrusted id="R">` with every `</untrusted` neutralized. The nonce is 16 random bytes from the operating system as 32 lowercase hex characters, new for every prompt. Comments are taken oldest first by `settle_ts`. Each comment has its id, an author label (the login, sanitized), `edited` when it applies, `thread_id`, `reply_to`, `outdated`, the thread's path, line and side, and its body, sanitized as one routine (control and invisible characters replaced by visible markers). A single body over 16 KiB is cut with a visible `[truncated: <n> bytes]` marker and still delivered. `limits.prompt_bytes` (default 65536) bounds the prompt; overflow comments stay unhandled for the next round and a visible truncation marker names what was cut. Nothing from an ineligible comment, no PR title, no description and no CI log is included.

### Session settings

Every round passes these to agent-spawn per call (design guide section 11), always with `--mode headless` (unless `respond --mode interactive`, unverified in 0.x), `--no-promote`, `--prompt-file`, `--actor auto:review-respond`, `--env-allowlist-only` (with an empty value for an empty list) and, when `agent.timeout` is set, `--timeout`. There is no `--wait`: the job records the agent id and then runs `wait` (Job).

```text
fork:   agent-spawn resume <base agent id> --fork --cwd <throwaway root realpath>
          --kind respond --mode headless --skill <skill> --plugin-dir <bundled plugin dir>
          --prompt-file <prompt file> --permission-profile <generated profile file>
          --sandbox <sandbox> --setting-sources "<setting_sources, comma-joined, empty for none>"
          --env-allowlist-only <VAR> ... | --env-allowlist-only ''
          [--model <model>] [--timeout <agent.timeout>] --no-promote
          --label pr=<pr> --label owner=review-respond --label repo=<repo id> --label round=<round id>
          --actor auto:review-respond --json
fresh:  agent-spawn launch --kind respond --mode headless --worktree none --cwd <throwaway root realpath> --repo <repo id>
          (the same flags from --skill to --json)
```

The cwd is exactly the throwaway root (a subdirectory is refused by agent-spawn). `--skill` is not carried to a fork, so it is always passed. Labels that cannot hold the PR reference or the repo id are omitted (Labels).

The permission profile is generated per call: the bundled allowlist profile plus the repo's `respond.allow_commands` (argv patterns for test and build commands) and `respond.read_dirs`. The bundled profile keeps the architecture's respond-profile denies and additionally denies `memoctl add`, `memoctl delete`, `memoctl cron`, `agent-spawn`, `notice-route send`, `git push` and `gh` and `glab`, and any write under review-respond's own config and state directories. Config keys replacing these: `skill`, `permission_profile`, `sandbox` (`required` by default; `preferred` and `off` are accepted with a warning; a per-repo value may not loosen it), `setting_sources`, `env_allowlist` (names passed with `--env-allowlist-only`), `model`, `agent.timeout`.

### Artifact

The agent files one JSON artifact with the Write tool in `$CSESSION_DIR/artifacts/` and reports `done` with `--artifact`. Exact shape (`schemas/artifact.json`): `skill_marker` (the string in the plugin's reference file), `replies` (list of `{comment_id, body}`), `changed_files` and `deleted_files` (lists of relative paths) and `commit_message`. Unknown keys are refused.

review-respond reads the frozen artifact from `agent-spawn show --json` (`.runs[-1].outcome.report.artifact.path`), only once the run is final, and validates it with one function; a failure at a step is the round's `failure.reason`, and `artifact_invalid` carries the step as its `failure.detail`:

```text
validate(outcome):
  r = outcome.report
  r.implicit is true            -> implicit_done
  r.artifact is null            -> artifact_missing
  open r.artifact.path without following links; regular file; size == r.artifact.size <= 1 MiB; sha256 == r.artifact.sha256  else artifact_invalid (detail frozen_file)
  strict JSON (no duplicate keys, depth <= 8); any exception -> artifact_invalid (detail json)
  schema (schemas/artifact.json, unknown keys refused) -> artifact_invalid (detail schema)
  skill_marker != the bundled plugin's marker -> artifact_invalid (detail skill_marker)
  replies: comment_id in the round's delivered ids, each once; body non-empty, at most limits.reply_bytes UTF-8 bytes -> artifact_invalid (detail replies)
  each path in changed_files and deleted_files: UTF-8, at most 4096 bytes, each component at most 255 bytes, no NUL or control character,
     relative, no backslash, no ':', no empty, '.' or '..' component, no component that folds to .git (wtctl's alias rule),
     unique after NFC and case folding, no path a prefix directory of another, no path in both lists, at most 1000 per list -> artifact_invalid (detail paths)
  commit_message at most 4 KiB -> artifact_invalid (detail commit_message)
  no replies and both lists empty -> empty_result
  path-state checks -> artifact_invalid (detail path_state)
```

A missing skill exits 0 in agent-spawn, so a wrong or missing marker is an invalid artifact. Paths are refused, never rewritten. **Path-state checks** run before every extract, after the agent is not live: each `changed_files` path is, by a no-follow `lstat` chain from the round throwaway root, a regular file with one link; each `deleted_files` path is absent (ENOENT); otherwise the round fails `artifact_invalid` (detail `path_state`) at record time, or `confirm` exits 20 `diff_changed` (`details.cause: path_state`). `--dest` is always the clean throwaway path from review-respond's own record. A copy of the validated artifact is stored in the state directory.

The artifact's file lists are never trusted for what they say about the working copy: the stored diff and the touched paths come from extraction in a clean checkout, not from the round throwaway or the lists.

## Pending confirmations

### Record

A pending confirmation has the id `rp-` plus 8 base32 characters and holds: `pr`, `repo`, `round`, `head_sha` and `head_ref` at round time, `base_ref`, the fork agent id, the round throwaway id, `changed_files`, `deleted_files`, `commit_message`, the replies, `diff_sha256`, `diff_file_sha256`, `message_sha256`, `diff_stats` (files, additions, deletions), `sensitive_paths` (the off-limits paths touched), `binary_paths`, `no_reply_comment_ids` (comments that were delivered and got no reply), `removed_line_count` (lines the commit-message procedure dropped), `escalated_by` (the hook that escalated a confirm), `commit_sha`, `commit_date`, `clean_throwaway_id`, `push_started_at`, `edited`, `state`, `created` and `expires` (default 7 days, `pending_ttl`). Each reply holds `comment_id`, `thread_id`, `body`, `dedupe_key`, `status`, `status_reason`, `threaded` and `sha256` (of its stored body file). Free text lives in stored files and appears in `show --json` only inside fields marked untrusted in the schema.

States: `pending`, `pushing`, `pushed`, `posting`, `done`, `rejected`, `expired`, `closed`. `pushing`, `pushed` and `posting` are the intent states of Confirm order. Each state allows a fixed set of commands:

| State | `confirm` | `reject` | `edit` | `drop-reply` | Expiry (`check`) | Closed PR (`check`) |
|---|---|---|---|---|---|---|
| `pending` | full order (Confirm order) | to `rejected` | yes | yes | to `expired` after `pending_ttl` | to `closed` |
| `pushing` | resume from the push check (Push) | 20 `not_pending` | 20 `not_pending` | 20 `not_pending` | never; `post-attention` `confirm_incomplete` once after `pending_ttl` | never |
| `pushed` | resume at Replies | 20 `not_pending` | 20 `not_pending` | 20 `not_pending` | as `pushing` | never |
| `posting` | resume at Replies, only replies with status `pending`, `failed` or `unknown` | 20 `not_pending` | 20 `not_pending` | yes, for replies with status `pending` or `failed` | as `pushing` | never |
| `done` | 20 `not_pending` | 20 `not_pending` | 20 `not_pending` | 20 `not_pending` | | |
| `rejected` | 20 `not_pending` | 0 (no-op) | 20 `not_pending` | 20 `not_pending` | | |
| `expired` | 20 `expired` | 0 (no-op) | 20 `expired` | 20 `expired` | | |
| `closed` | 20 `pr_closed` | 0 (no-op) | 20 `pr_closed` | 20 `pr_closed` | | |

A resume (`pushing`, `pushed`, `posting`) needs a human actor like a fresh confirm; it re-runs no `pre-push` hook, no expiry check, no open-PR check (replies are allowed on closed and merged PRs) and no head check, and `--expect-diff` is ignored with a warning.

### Stored diff

When the job ends, review-respond builds the stored diff in a clean checkout, then removes that checkout. One recipe serves record time and confirm time:

```text
clean = wtctl create --throwaway --source remote --repo <id> --ref <head_sha>
          --label app=review-respond --label role=clean --label round=<round id> [--label pending=<pending id>] --json
record clean.id immediately (record time: on the round; confirm time: clean_throwaway_id on the record)
refuse when the round's agent is live: agent-spawn show <fork id> status in (launching, live, tearing_down) -> remove clean, exit 20 live
path-state checks of the artifact lists in the round throwaway (Artifact)
wtctl extract <round throwaway id> --files-from F --delete-from F --dest <clean.path from the record>    # F are files review-respond wrote
removal: record time, always right after the diff is stored or failed; confirm time, on every exit that leaves the record pending, and at pushed
```

```text
stored_diff(clean):
  add --pathspec-from-file=<F> --pathspec-file-nul            # literal pathspecs (Hardened git)
  if diff --cached --quiet: no file changes -> the record is replies-only (diff_sha256 null)
  text   = diff --cached --text --full-index --no-renames
  status = diff --cached --name-status -z --no-renames         # touched = every path listed (adds, modifies, deletes)
  numstat= diff --cached --numstat -z --no-renames             # binary = paths with "-" counts
  sensitive_paths = [p for p in touched if off_limits matches p]; binary_paths = binary
  diff_sha256 = sha256(text); diff_stats from numstat
```

`confirm` refuses with `sensitive_paths` when `sensitive_paths` or `binary_paths` is non-empty, unless a human passes `--allow-sensitive`. `show` prints both lists, the diff through the one sanitizer, and the count of non-ASCII characters in added lines. The sha256 of every stored text file is kept in the record and re-checked by `show`, `pending` and `confirm`; a differing file exits 12 `state_unsafe`. If extraction or the diff fails, the round is `failed` with `diff_failed` and nothing is recorded. The round throwaway stays, untouched by any trusted process, until `confirm`, `reject`, expiry or the PR ending. It carries the labels `app=review-respond`, `role=round`, `round=<id>` and `pending=<id>`. The README setup lists the wtctl settings that keep it (`prune.protect_labels` includes `app`).

## Confirm order

`confirm <id>` runs these steps. Each step that acts first writes its intent to the record, and each is idempotent. Steps 3 to 7 are skipped for a record with no changed or deleted files.

1. **Preconditions.** State `pending` (other states: the Record table). Not expired (else the record becomes `expired` and the command exits 20 `expired`). A human actor (a TTY, or `--yes --actor human:<user>`), else exit 13 `confirmation_required`. `--expect-diff`, when given, equals `diff_sha256` (else 20 `diff_changed`, `details.cause: expect_mismatch`; on a replies-only record `--expect-diff` exits 2). Each stored file's sha256 equals the record's (else 12 `state_unsafe`). Lease `pr:` taken.
2. **Live reads.** `forgectl show <pr>`: closed or merged makes the record `closed` and exits 20 `pr_closed`; a fork PR (or a head repo other than the base repo) exits 20 `ineligible` (`details.reason: fork_pr`); for a record with files, a head ref that is the repository's default branch (wtctl `locate` `default_branch`; unknown fails closed) exits 20 `ineligible` (`details.reason: default_branch`), and a head sha other than `head_sha` exits 20 `remote_moved` (the record stays `pending`, `post-attention` `remote_moved` once per head). `forgectl comments <pr>` marks each reply's skip reason (Replies).
3. **Hooks.** `pre-push` hooks run (also for a replies-only record, with `files_changed: 0`), skipping the hook named in the record's `escalated_by`: a veto exits 11, an escalation stores `escalated_by` and exits 13; the record stays `pending`.
4. **Clean checkout.** The recipe in Stored diff.
5. **Diff check.** Compute the stored diff again. A different sha256 refuses with `diff_changed` (`details.cause: recomputed`). Any `sensitive_paths` or `binary_paths` refuses with `sensitive_paths` unless a human passed `--allow-sensitive`.
6. **Commit.** Stage exactly the listed paths with literal pathspecs and commit with your identity, storing `commit_sha` and `commit_date` (Commit).
7. **Push.** Record `pushing`, then push fast-forward only (Push). On success record `pushed` and update the lineage (Fork lineage).
8. **Replies.** Record `posting`, then post each reply in record order (Replies).
9. **Finish.** When every reply is `posted`, `skipped` or `dropped`: the optional thread resolves, record `done`, round `confirmed`, the lineage update for a replies-only record, `post-confirm`, both throwaways removed, superseded forks marked for teardown.

### Commit

The identity is `git.user_name` and `git.user_email`; when unset they are read from your global git config file directly (parsed, no includes, no git process); with neither, `confirm` refuses with `error.code: identity_missing`. The message goes through a closed procedure and then to git through `-F <file>`:

```text
sanitize_message(m):
  replace control and invisible characters with visible markers (the one sanitizer)
  drop lines whose first non-space text matches (case-insensitive) ^#
  drop lines matching ^\s*[A-Za-z][A-Za-z0-9-]*\s*:\s  inside the last paragraph (repeat until the last paragraph has none)
  drop lines matching (case-insensitive) \b(close[sd]?|fix(e[sd])?|resolve[sd]?)\b\s*:?\s*([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)?#[0-9]+
  strip a leading fixup!, squash! or amend! from the subject
  subject: first line, cut at 72 characters; empty -> "Address review comments"
  body: at most 4 KiB
  append a blank line and the trailer paragraph: git.trailer with <me.name> filled
  record removed_line_count; show prints the cleaned message and the count
```

`git.trailer` is a non-empty string (default `Assisted-by: review-respond (confirmed by <me.name>)`), so the last paragraph is always review-respond's. File modes are never copied from the round: an existing file keeps its mode and a new file is 0644 (wtctl's extraction rule).

### Push

The URL is `fetch_url` from `wtctl locate --json`, checked to be unchanged by `insteadOf` rewriting (`ls-remote --get-url`, a local command). The refspec is `HEAD:refs/heads/<head ref>`, always the full ref name. The push is `push --porcelain --no-verify`, never forced and never to a remote name. Credentials per host come from `hosts.<host>.token_env` or `token_command`, and go only as one URL-scoped `http.<scheme>://<host>/.extraHeader` through `GIT_CONFIG_COUNT`, `GIT_CONFIG_KEY_n` and `GIT_CONFIG_VALUE_n` in the hardened environment (never in argv); an ssh URL uses `SSH_AUTH_SOCK` with `BatchMode`.

```text
push(record):
  # step 6 already stored commit_sha and commit_date (GIT_AUTHOR_DATE = GIT_COMMITTER_DATE = commit_date)
  CAS pending -> pushing with commit_sha, commit_date, clean_throwaway_id, push_started_at
  remote = ls-remote <url> refs/heads/<head_ref>
  if remote is absent or remote != head_sha: -> pending, remove clean, exit 20 remote_moved (details.ref_missing when absent)
  out = push --porcelain --no-verify <url> HEAD:refs/heads/<head_ref>
  if ok: CAS pushing -> pushed (+ lineage update); remove clean; continue at Replies
  else:  # a line starting with "!" or a non-zero exit
    remote = ls-remote again
    remote == commit_sha: -> pushed (as above)
    remote == head_sha:   -> pending, remove clean, exit 12 push_failed (host reason), post-attention push_failed
    otherwise:            -> pending, remove clean, exit 20 remote_moved
  a timeout or a kill leaves the record pushing (the clean checkout stays)
resume_pushing(record):
  remote = ls-remote <url> refs/heads/<head_ref>
  if remote == commit_sha: -> pushed, continue at Replies
  elif remote == head_sha:   # nothing landed
     if the clean checkout exists and its HEAD (rev-parse) == commit_sha: push it (same rules as push)
     else rebuild steps 4 to 6 with the stored commit_date; if the new sha != commit_sha store it (still pushing); push
  elif remote is absent: -> pending, exit 20 remote_moved (details.ref_missing)
  else:
     c = forgectl compare <pr> --no-diff --from <commit_sha> --to <remote>
     c.status in (ahead, identical): -> pushed (our commit landed and someone pushed on top)
     otherwise: -> pending, exit 20 remote_moved
```

The clean checkout is kept only while the record is `pushing`.

### Replies

Each reply is posted with `pr-comment reply <pr> --to <target> --body-file F --dedupe-key respond:<pending id>:<first 20 hex of sha256(comment id)> --yes --actor <actor> [--decision-id D]...`; the footer and the marker come from pr-comment. The key hashes the comment id because comment ids can hold characters or lengths the forge key grammar (`^[A-Za-z0-9._:-]{1,128}$`) refuses. No `--expected-head` is passed, because the push moves the head. `confirm` reads `forgectl comments` once per call and applies the skip rule:

```text
for each reply with status pending (fresh confirm) or pending, failed or unknown (resume):
  read = forgectl comments <pr> (once per confirm call)
  c = the comment in read; t = its thread (from the record's thread_id)
  if read.complete and c absent:
      if t present and t.id != c.id (an inline thread): target = t.id   # the reply joins the thread
      else: skipped (comment_deleted)
  elif t.resolved: skipped (thread_resolved)
  elif sha256(c.body) != delivered_sha256: skipped (comment_edited)  # the comment re-opens after the record ends
  else: target = c.id
  pr-comment reply ... --to <target> ...
```

The key does not change when the target falls back to the thread id, so a retry stays deduplicated. `threaded` from pr-comment's result is stored per reply (a reply to a general comment is a new general comment, `threaded: false`). Reply status is one of `pending`, `posted`, `skipped`, `dropped`, `failed`, `unknown`, with `status_reason` one of `comment_deleted`, `thread_resolved`, `comment_edited`, `vetoed`, `escalated`, `host_error` or null. pr-comment's exit maps to the status:

| pr-comment exit | Status | Effect |
|---|---|---|
| 0 (also `already_posted: true`) | `posted` | |
| 10 `not_found`, `details.what: to` | `skipped` (`comment_deleted`) | |
| 11 | `failed` (`vetoed`) | |
| 13 | `failed` (`escalated`) | |
| 12 with `retry: same_command` (`outcome_unknown`, network, rate limit) | `unknown` | stop posting; the command exits 12 `outcome_unknown`; a later `confirm` retries with the same key |
| 12 other, 20, 2 | `failed` (`host_error`) | continue with the next reply |

After the loop: any `unknown` exits 12 `outcome_unknown`; else any `failed` exits 12 `reply_failed` and fires `post-attention` `reply_failed` once; the record stays `posting`. A later `confirm` retries `failed` and `unknown` replies; `drop-reply` gives up on one. With `resolve_after_confirm: true`, the threads that got a reply are resolved through `pr-comment resolve` after all replies are done; a general comment cannot be resolved and is skipped. A comment edited after delivery re-opens after the record ends (Handled set).

### Crash recovery

Re-running `confirm` resumes per the Record state table; a push is never repeated while the first attempt may have landed (Push).

## Reject, expiry and closed PRs

- `reject` acts on a `pending` record: it removes the round throwaway (`wtctl remove`), marks the round's fork for teardown and marks the record and round `rejected`. Without `--redo` the comments stay handled.
- Expiry (`pending_ttl`) acts on a `pending` record: it is `reject` without `--redo`, fired by `check`, with `post-attention` reason `pending_expired`; the round becomes `expired`.
- A closed or merged PR: the next `check` marks `pending` records `closed`, gives queued or running rounds the `stop` order with `cancel_reason: pr_closed`, removes the throwaways and marks the base fork and every round fork for teardown (`teardown_refused` is a `post-attention` reason when agent-spawn keeps refusing; the next `check` retries).
- Records in `pushing`, `pushed` and `posting` never expire and are never closed; `post-attention` `confirm_incomplete` fires once when such a record is older than `pending_ttl`.
- A throwaway removal that wtctl refuses (`in_use`, exit 20) is retried by the next `check`.

## Off-limits paths

The list (`off_limits`) uses the format, the default list and the path-matching rules of review-watch's interface (section Path matching): one segment matcher with case folding, `**` for zero or more whole segments, git-quoted paths failing closed. Touched paths are every path of the clean checkout's `--name-status --no-renames` listing (deletions and both sides of a rename included), never the artifact lists. A touched path makes `confirm` refuse with `sensitive_paths` unless a human passes `--allow-sensitive` after the diff was shown; binary paths need the same allow.

## Hooks

Hooks are configured in `hooks.<name>` lists as the design guide's section 8 describes. A payload goes to stdin and holds `tool`, `hook`, `object_type`, `object_id`, `repo` and `labels` (the object's labels, at most 32), plus the fields below, and never any comment, reply, diff or message text. Environment: `REVIEW_RESPOND_HOOK`, `REVIEW_RESPOND_ID`, `REVIEW_RESPOND_OBJECT_TYPE`. Schemas: `schemas/hooks/`.

| Hook | Fires | Can veto | Payload fields |
|---|---|---|---|
| `pre-round` | once per round start, after the due rule, before the round is created (`object_type` is `pr`, `object_id` the PR) | veto (no round; the PR waits, `hook_vetoed`, until a new or re-opened eligible comment or a human `respond`) or escalate (no round; the PR waits, `hook_escalated`; a human `respond` continues and skips that hook) | `pr`, `round_number`, `attempt`, `head_sha`, `comment_count`, `trigger` (`check`, `respond`), `forced`, `reads_third_party` |
| `pre-push` | in `confirm`, after the preconditions and before the commit and push | veto or escalate | `pending_id`, `pr`, `head_sha`, `diff_sha256`, `files_changed`, `additions`, `deletions`, `sensitive` (a boolean: off-limits or binary paths touched), `actor_class` (`human` only in 0.x), `reads_third_party`, `tainted` |
| `post-pending` | when a pending confirmation was recorded | no | `pending_id`, `action` (`push_and_reply`, `reply`), `event` (always `recorded`), `files_changed`, `files_deleted`, `additions`, `deletions`, `reply_count`, `summary` (a fixed phrase with counts), `expires`, `dedupe_key`, `reads_third_party`, `tainted` |
| `post-attention` | once per cause, with a closed `reason` | no | `reason`, `object_type` (`round`, `pending`, `pr`), `details` (below), `dedupe_key`, `reads_third_party`, `tainted` |
| `post-confirm` | after a confirm reached `done` | no | `pending_id`, `pr`, `commit_sha` (or null), `pushed`, `replies_posted`, `replies_skipped`, `dedupe_key`, `reads_third_party`, `tainted` (always true) |

`post-attention` reasons: `escalated`, `failed`, `round_limit`, `pending_expired`, `confirm_incomplete`, `remote_moved`, `push_failed`, `reply_failed`, `job_lost`, `ineligible_comment`, `pr_closed`, `teardown_refused`, `no_agent`. The `details` keys per reason are a closed set:

| Reason | `details` keys |
|---|---|
| `escalated` | `round_id`, `cause` (`agent`, `hook`) |
| `failed` | `round_id`, `failure_reason`, `attempts` |
| `round_limit` | `rounds` |
| `pending_expired`, `confirm_incomplete` | `pending_id` |
| `remote_moved` | `pending_id`, `head_sha` |
| `push_failed`, `reply_failed` | `pending_id`, `comment_count` (failed replies) |
| `job_lost` | `round_id` |
| `ineligible_comment` | `comment_count` |
| `pr_closed` | `pending_id` or `round_id` |
| `teardown_refused` | `agent_id` |
| `no_agent` | `pr` |

The post hooks are delivered at least once through an outbox, in order per object, with an `event_id` (`ev-` plus 16 characters of `[a-z2-7]`) and a `dedupe_key` in notice-route's plain grammar: `rp-<id>.recorded`, `rp-<id>.done`, or `<object id or PR digest>.<reason>.<episode>`, where a PR's digest is the first 16 hex characters of the sha256 of the PR reference. They cannot veto. `pre-` hook outcomes follow the design guide (exit 0 continues, 13 escalates, other non-zero vetoes and the command exits 11). A `decision_id` on a hook's stdout is recorded in the round or pending record and in audit. Deviation from the design guide's `post-pending` wording: the record's `summary` is a fixed phrase and never agent text, so a notification can use it.

## Config

Location, drop-ins and precedence follow the design guide (`~/.config/review-respond/config.yaml`, `config.d/*.yaml`, `XDG_CONFIG_HOME`, flags over per-repo over environment over config over defaults). `schemas/config.json` is self-contained and `config.example.yaml` documents every key.

| Key | Default | Meaning |
|---|---|---|
| `me.name`, `me.accounts` | OS user, none | your name for audit and the trailer; per-host account ids (a map from host to id; otherwise `forgectl whoami`) |
| `hosts.<host>.token_env`, `.token_command` | none | credentials for the push (a variable name or an argv list); never stored |
| `hosts_to_check` | hosts that `forgectl whoami` lists with a credential | hosts whose PRs `check` lists |
| `reviewers` | `[]` | entries `<host>:<id>` whose comments are always eligible; `repos.<id>.reviewers` replaces this list |
| `bot_authors` | `[]` | entries `<host>:<id>` treated as bots |
| `respond_to_own_comments_prefix` | `null` | opt-in prefix for your own comments |
| `settle` | `5m` | minimum age of the newest eligible comment |
| `max_rounds` | `3` | counted rounds per PR |
| `pending_ttl` | `7d` | pending confirmation lifetime |
| `access_cache_ttl` | `1h` | cache of `forgectl access` answers |
| `prefer_owner` | `ticket-work` | preferred owner label when several agents match |
| `resolve_after_confirm` | `false` | resolve replied threads after confirm |
| `off_limits` | review-watch's list | globs |
| `skill`, `permission_profile`, `sandbox`, `setting_sources`, `env_allowlist`, `model`, `agent.timeout` | `respond-to-review`, bundled profile, `required`, empty, `[]`, none, none (the kind's own timeout applies) | per-call safety settings (Session settings) |
| `jobs.queue`, `.kill_grace`, `.max_running`, `.pass_env` | `respond`, `125s`, `1`, `[]` | job placement; `pass_env` names the variables passed to the job (with jobsctl their values come from jobsctl's `env_file`) |
| `git.user_name`, `git.user_email`, `git.trailer` | from your git config, from your git config, the default trailer | commit identity; the trailer is a non-empty string |
| `limits.requests_per_host`, `.prompt_bytes`, `.reply_bytes` | `120`, `65536`, `8192` | bounds |
| `attention.ineligible_comment` | `false` | fire a hook for ineligible comments |
| `retention.records` | `30d` | prune age |
| `cron.env_file` | `null` | file sourced before `check` in the cron line |
| `hooks.<name>` | none | hook command lists |
| `repos.<id>.*` | none | per-repo overrides, exactly these keys: `respond_fresh`, `respond.allow_commands`, `respond.read_dirs`, `settle`, `max_rounds`, `reviewers`; a per-repo value may not loosen `sandbox` |

Durations accept the design guide's grammar plus `h` and `d` for the keys that name hours or days. Invalid config fails fast with the key's name.

## Environment variables

`REVIEW_RESPOND_<KEY>` sets only these keys (nested keys joined with `__`): `settle`, `max_rounds`, `pending_ttl`, `access_cache_ttl`, `prefer_owner`, `model`, `agent.timeout` (`REVIEW_RESPOND_AGENT__TIMEOUT`), `retention.records` and `limits.requests_per_host`. Every safety key (`off_limits`, `sandbox`, `permission_profile`, `setting_sources`, `env_allowlist`, `skill`, `hosts.*`, `git.*`, `jobs.*`, `hooks`, `reviewers`, `bot_authors`, `respond_to_own_comments_prefix`, `resolve_after_confirm`, `repos`) is config-file only. Credentials are named in config and read from the environment. Hook variables are listed under Hooks.

## Labels

review-respond puts these labels on what it creates: on forks, `pr=<host/path#N>`, `owner=review-respond`, `repo=<id>` and `round=<round id>`; on throwaways, `app=review-respond`, `role=round` or `role=clean`, `round=<round id>` (the round throwaway) and `pending=<pending id>` (added to the round throwaway with `wtctl label add` when the record is created, given at create time to the clean one); on jobs, the correlation labels `pr`, `round` and `owner`. It gives no meaning to any other label. It reads the `pr` and `owner` labels of agents only to find the original agent and verifies the match.

A label value may hold `_` (an additive loosening of the family grammar: `[A-Za-z0-9_.:/#@+=,-]`). Until every sibling accepts it, a PR reference or repo id with a character outside the older grammar is **label-unsafe**: review-respond omits that label (warning `label_skipped`), cannot find the original agent by label (so such a PR has no original agent: a fresh session if the repo opted in, else `waiting` with `no_agent`), and still labels throwaways and forks with `round=`, `owner=` and `app=`.

## Files and state

State under `~/.local/state/review-respond/` (`XDG_STATE_HOME` respected): `state.db` (private SQLite, WAL, versioned additive migrations), `log.jsonl` (public, hash-only, no comment text), `audit.jsonl` (public), and directories for prompt files, generated profiles, stored artifacts, stored diffs and reply drafts, each deleted when its record ends or by `prune`. Stored text files are named by the sha256 of their content. The managed crontab block written by `cron install` has the first line `# review-respond managed v1 every=5m`, a `PATH=` line, the line `*/5 * * * * . '<env_file>' && '<absolute review-respond>' check --quiet` (the env-file prefix only with `cron.env_file`) and the last line `# review-respond managed end`.

## Concurrency

One lease table has three key kinds: `pass`, `pr:<pr digest>` and `round:<round id>`, one row format and one takeover rule. A lease row holds the holder's PID, process start time, boot id, `acquired_at`, `renewed_at` and `expires_at`. The holder renews it every 60 seconds from a heartbeat while it holds it; it expires 5 minutes after the last renewal. Another process takes it over only when it has expired and the holder is verified gone (PID absent, or its start time or the boot id differs). Every state write is a compare-and-set on the row's `rev` in a transaction that also checks the writer still holds its lease; a lost lease aborts the command with exit 1 `lease_lost`. No lease is held across a jobsctl queue wait; the `round:` lease is held by `_job` for its whole life (it is the job's liveness).

| Command | Leases | When contended |
|---|---|---|
| `check`, `prune` | `pass` for the run; `check` also takes `pr:` per PR step without waiting | a second caller exits 0 with `already_running: true` (also `check --pr`); a held `pr:` lease skips that PR (`lease_held` in its result) |
| `respond`, `stop` | `pr:` | wait up to 30 s, then exit 20 `busy` |
| `confirm`, `reject`, `edit`, `drop-reply` | `pr:` of the record's PR, held for the whole command | wait up to 30 s, then exit 20 `busy` |
| `_job` | `round:` for its life; `pr:` only for its short transactions | waits up to 60 s, else exit 1 `lease_timeout`; the round stays running for a finalizer |
| finalizer in `check`, `show`, `confirm` | `round:` only by takeover | a live holder: skip; `show` prints the stored row with `finalize: busy` |
| `list`, `show`, `pending`, `export`, `validate` | none | |

Only `check` and `prune` report `already_running`; a contended `confirm` exits 20 `busy`, never 0.

## Audit and log

`audit.jsonl` records every external action: each push and each reply post, as an `attempted` record before the call and an outcome record after it sharing an `attempt_id` (22 URL-safe characters, 128 random bits), with `ts`, `actor`, `decision_id`, `target` (PR), `action`, `inputs` (head sha, diff sha256, commit sha, the pending id, `config_sha256` of the merged config after defaults) and `result`. The `action` is one of `push`, `reply`, `resolve`, `confirm`, `reject`, `edit`, `drop_reply`, `stop`, `teardown`. Human decisions (`confirm`, `reject`, `edit`, `drop_reply`, `stop`) are single outcome records. If the first append fails nothing is sent. `log.jsonl` holds events (`check_pass`, `pr_state`, `round_state`, `pending_state`, `hook_result`, `finalize`, `host_failed`, `lease`, `warning`) with ids and hashes only. Schemas: `schemas/audit.json`, `schemas/log.json`.

## Hardened git

review-respond runs git itself, only in the clean throwaway that no agent wrote, and always through one module that implements architecture section 13.2 rule 10: an environment built from scratch, the full override list, the injected flags and rejected options, a fixed allowlist of subcommands (`rev-parse`, `config`, `ls-remote`, `add`, `diff`, `commit`, `push`), filter-driver and extra-header blanking from `config --list -z`, explicit `--git-dir` and `--work-tree`. Deviations from the recipe's literal text, all found against real git and recorded in `docs/design.md`:

1. `GIT_ALLOW_PROTOCOL` is `none` for every command except `push` and `ls-remote`, which get the scheme of the URL in use (`file` for a plain path); a push over `none` fails.
2. `GIT_SHALLOW_FILE` is the clean throwaway's own shallow file, not `/dev/null` (which breaks a push from a shallow clone).
3. `GIT_LITERAL_PATHSPECS=1` is set in the environment and paths go through `--pathspec-from-file` with `--pathspec-file-nul`, since a leading `--literal-pathspecs` option is refused by the recipe's option rule.
4. `core.sshCommand` is one argv element, `core.sshCommand=ssh -o BatchMode=yes`.
5. review-respond's own injected diff flags: `--text --full-index --no-renames` for the stored diff.

A `live` regression test of the recipe is required, on the git floor before 1.0.

## Upgrades

`state.db` migrations are additive. A newer schema than the binary knows makes read-only commands work and write commands exit 12 `version_skew`. Config keys added later have defaults. Hook payload keys are only added.

## Residual risks

- A prompt-injected fork can steer its own outputs (the edits, replies and message). They are untrusted and shown to you before anything is pushed or posted; nothing happens without your `confirm`.
- A fork carries its source conversation, which may contain secrets the original agent read. The sandbox has no network and the throwaway holds only the PR's files (architecture section 13.2, residual risk (i)).
- A human-only rule is advisory against a same-user process that claims a human actor.
- wtctl's throwaway `max_age` and an operator's own `prune` can remove a throwaway under a pending confirmation; `confirm` then refuses with `error.code: throwaway_missing` (exit 12) and `reject --redo` starts again.
- The edit test uses a body hash, so an edit that restores the earlier text is not seen.
- Access answers are cached and are by login; a collaborator removed within the cache lifetime stays eligible until it expires.
- The commit message and the diff are reviewed by you; a convincing change that you approve is yours.
- Between the last `ls-remote` and the push a branch deletion can still turn the push into a branch creation; the window is milliseconds and the host's ref lock covers every other race.
- A new file is committed without the executable bit.
- A PR on a host with a port has a non-plain id in notifications; notice-route stores its object id as null.
- A PR whose reference holds a character outside the older label grammar has no label lookup until the siblings accept it.
- A stored file edited by a same-user process is detected by its hash; the state database itself is not authenticated.

## Platform notes

Linux and WSL are supported. macOS is checked by a manual checklist (the sandbox is Seatbelt; `lsof` for `in_use`). Interactive forks need tmux and are unverified in 0.x.

## TUI

None in 0.x. A `browse` command (`--focus`, `--result-file`) is a 1.x item and follows a feature inventory of forgectl's TUI. Automatic modes (`auto_reply`, `auto_push`), fork PRs and review bodies are 1.x items too.

## Objects

- **PR state** (`list`, `show`): `pr`, `repo`, `state`, `waiting_reason`, `block` (`reason`, `set_at`, `hook`), `head_sha`, `head_ref`, `base_ref`, `is_fork`, `base_agent`, `rounds`, `unhandled`, `seen`, `ineligible`, `edited`, `pending_id`, `updated`.
- **Round** (`respond`, `show`, `list --json`): `id`, `pr`, `number`, `attempt`, `state`, `head_sha`, `agent_id`, `base_agent_id`, `job_id`, `comment_ids`, `delivered` (comment ids), `counted`, `cancel_requested_at`, `cancel_reason` (`stopped`, `pr_closed`, `nothing_to_respond`), `note_sha256`, `failure` (`reason`, `detail`), `started`, `ended`, `warnings`.
- **Pending confirmation**: Pending confirmations, Record.
- **Comment record** (`show --json`): `id`, `author_id` (null when the host gave none), `eligible`, `reason`, `state`, `delivered_sha256`, `handled_reason`, `first_seen_at`, `edited`, `thread_id`, `path`, `line`, `body` (untrusted, sanitized).

Every `--json` output is described in `schemas/cli/<command>.json`; fields that carry comment, reply, diff or message text are marked `x-untrusted`.

### Values

This subsection states what the schemas use so that they follow the interface.

- **PR reference**: forgectl's `pr` (`host/path#N`); `show` and `respond` also accept an https PR URL.
- **Ids**: a round is `rd-` plus 8 characters of `[a-z2-7]`, a pending record `rp-` plus 8, a hook event `ev-` plus 16. A unique prefix of at least 4 characters after the prefix is accepted (several matches exit 21). Agent, job and throwaway ids are the siblings' own; comment and thread ids are forgectl's and opaque. `show` dispatches by prefix: `rp-` is a pending record, `rd-` a round, anything else a PR reference.
- **`pending` rows**: `id`, `tool` (`review-respond`), `action` (`push_and_reply`, `reply`), `target` and `pr` (the PR reference), `created`, `summary`, `expires`, `state`, `diff_sha256`, `files_changed`, `reply_count`.
- **Output envelopes**: `list` prints `{prs: [...]}`, `pending` prints `{pending: [...]}`, `respond` prints `{dry_run, round, comment_ids}`, `show` adds `object_type` (`pr`, `round`, `pending`), `check` prints `{already_running, dry_run, hosts: [{host, ok, code, warnings}], prs: [{pr, state, started_round, lease_held, errors}]}`, `stop` prints `{round, changed, cancel_requested}` and `prune` prints `{results: [{object_type, id, result}]}` with `object_type` one of `round`, `pending`, `comments`, `throwaway` and `result` one of `deleted`, `would_delete`, `skipped`.
- **Stored text files** are named by the sha256 of their content.
- **Config defaults** the schemas rely on: `me.accounts` maps host to id (default none), `agent.timeout` is null (the kind's own timeout applies), `permission_profile` is null (the bundled profile), `model` is null, `skill` is `respond-to-review`, `setting_sources` is empty.
