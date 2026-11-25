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
| agent-spawn | A | `list`, `show`, `resume --fork`, `launch`, `wait`, `stop`, `teardown`; `--no-implicit-done` on `resume --fork` and `launch` | 0.34.0 |
| pr-comment | A | `reply`, `resolve` | 0.33.0 |
| wtctl | A | `create --throwaway`, `extract`, `locate` (for `fetch_url`), `remove`, `list`, `label add`, `show` | 0.31.0 |
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
| `--dry-run` | on `check`, `respond`, `confirm`, `reject`, `edit`, `drop-reply`, `stop`, `prune`, `cron`: show what would happen and change nothing: reads only, no hook runs, no sibling write; `confirm --dry-run` runs steps 1 and 2 and prints the plan |
| `--yes` | skip a confirmation; accepted only with a human actor (a TTY, or `--actor human:<user>`). `--yes`, `--force`, `--allow-sensitive` and `--override-escalation` with an automatic actor exit 2 `human_only`. `confirm`, `edit` and `drop-reply` need a human actor (a TTY, or `--actor human:<user>`), else exit 13 `confirmation_required`; `reject` and `stop` do not |
| `--actor A` | `human:<user>` or `auto:<name>`; the default is `human:<user>` on a TTY and `auto:unknown` otherwise. Every action review-respond takes on its own (a job, `check`, a sweep) records the actor `auto:review-respond`; an `--actor auto:<name>` given by a caller is recorded as given |
| `--override-escalation` | human only; `respond` and `confirm`: every hook that escalates in that call counts as continuing (a veto still vetoes); recorded in audit. A plain rerun runs every hook again |
| `--decision-id ID` | repeatable; recorded in audit and passed through to pr-comment by `confirm` |
| `--result-file PATH` | write the `--json` output there atomically, whatever the exit code |
| `--verbose`, `--quiet`, `--version` | as in the design guide |

Global flags go after the subcommand. Free text (a note, a reply, a commit message) is always taken from a file or stdin (`--note-file`, `--body-file`, `--message-file`, `-`), never from argv. Flag durations use the config grammar; `--since` also takes an RFC 3339 time with an offset.

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

`live` (an agent working on the PR is live), `busy` (another command holds the PR's lease), `pending_exists` (the PR has a pending confirmation), `round_active` (a round of the PR is queued or running), `round_limit`, `remote_moved`, `diff_changed` (`details.cause`: `expect_mismatch`, `recomputed` or `path_state`), `sensitive_paths` (`details.causes` lists the flag causes), `expired`, `not_pending`, `ineligible` (`details.reason`: `not_yours`, `fork_pr` or `default_branch`), `pr_closed` (the PR is closed or merged). A refusal carries `reason` and `details` in the error envelope. `--yes` never overrides a refusal. A human `--force` overrides only `round_limit`; `live` is never overridden.

### Error envelope

With `--json`, errors print `{"error": {"code", "message", "reason"?, "details"?, "lower_error"?}}` on stdout and the message on stderr. `confirm` and `check` print their result object (`schemas/cli/confirm.json`, `schemas/cli/check.json`) also on exit 12, with the envelope's inner object in an added `error` member; every other command prints the envelope. `lower_error` (`{code, message, reason?, details?}`) carries a sibling's error object when a sibling failed.

### Error codes

| Exit | `error.code` | Notes |
|---|---|---|
| 1 | `internal`, `lease_lost`, `lease_timeout` | |
| 2 | `usage`, `config_invalid`, `human_only` | `human_only`: `--yes`, `--force`, `--allow-sensitive` or `--override-escalation` with an automatic actor |
| 10 | `not_found` | PR, round or pending id |
| 11 | `vetoed` | a `pre-round` hook (`respond`) or a `pre-push` hook (`confirm`) |
| 12 | `tool_missing`, `tool_too_old`, `lower_failed` (with `lower_error`), `all_hosts_failed`, `push_failed`, `reply_failed`, `outcome_unknown`, `identity_missing`, `throwaway_missing`, `state_unsafe`, `version_skew` | |
| 13 | `confirmation_required`, `escalated` | `confirm`, `edit` or `drop-reply` without a human actor, or a `confirm` the human declined; a `pre-push` or `pre-round` escalation |
| 20 | `refused` | `reason` is one of the refusal reasons above |
| 21 | `ambiguous` | an id prefix matching several records |
| 22 | `nothing_to_respond` | `respond` found no eligible unhandled comment |

## Commands

### `check`

The one periodic pass. In order, under the `pass` lease (Concurrency; a second caller exits 0 with `already_running: true`):

1. Resolve your account id per host (`forgectl whoami`, cached).
2. List your open PRs per configured host and update the stored PR state (Detection).
3. Read the comments of each listed non-fork PR, least recently attempted first within the budget; update the comment records and compute the eligible unhandled comments (Eligibility, Handled set).
4. Start a round for each PR that is due (Settle and round start), oldest due first, while capacity remains (Settle and round start); the `pre-round` hooks run here.
5. Housekeeping: reconcile queued rounds and finalize running rounds whose job is gone (Round states), resolve held jobs of escalated rounds, end `pending` records that expired or whose PR closed and stop rounds of closed PRs (Reject, expiry and closed PRs), run the cleanup (Cleanup), compare the head of every PR with a `pending` record that has a diff (one `post-attention` `remote_moved` per new head, nothing else), tear down forks marked for teardown (Fork lineage).
6. Deliver queued hook events.

`check --pr <pr>` limits the pass to one PR, read the same way (`forgectl show`, then `read_comments`); a PR that is not yours is reported with the error `refused` (`not_yours`) in `prs[]`. It takes the same `pass` lease (the early trigger is dropped while a pass runs; the next pass covers it) and changes no host-wide state other than `installed_at` when none is stored. `check` never waits for a round. It takes the `pr:` lease of each PR step without waiting; a PR whose lease is held is skipped and reported with `lease_held`. Exit 0, or 12 `all_hosts_failed` when no host could be read; a vetoed, escalated or failed PR is a per-PR result (`prs[]` with `state`, `started_round`, `lease_held` and `errors`, a list of `error.code` values such as `vetoed`, `escalated`, `refused` and `lower_failed`; for example `waiting` with `hook_vetoed`), never the exit code. `check` never exits 11 or 13.

### `respond <pr> [--force] [--include-existing] [--note-file F] [--skill S] [--mode headless|interactive] [--override-escalation]`

Starts a round for one PR now, ignoring the settle delay but not the other rules. It first runs `forgectl show <pr>` (the author's account id must be yours, else exit 20 `ineligible`, `details.reason: not_yours`) and one comment read (Detection, `read_comments`). `--force` ignores the round limit (a human actor only). `--include-existing` applies to that round only: its job also selects every eligible `seen` comment (they become `delivered`; a failed round returns them to `seen`). `--note-file` adds your note to the prompt as trusted text in its own block (at most 8 KiB, else exit 2). On a PR in `waiting` with block `escalated`, `failed`, `stopped`, `no_agent`, `hook_vetoed` or `hook_escalated` a human `respond` clears the block and starts a round (it is not bound by `jobs.max_running`); `round_limit` needs `--force`. Refusals: `live`, `busy`, `pending_exists`, `round_active`, `round_limit`, `ineligible`. Exit 22 when no eligible comment exists, 11 or 13 from a `pre-round` hook, 0 with the round object once the round is created.

### `list [--state S] [--repo ID] [--waiting]`

PRs and their state (Settle and round start, PR state): `idle`, `settling`, `queued`, `running`, `pending`, `waiting`, `closed`. `waiting` rows carry `waiting_reason`, one of `escalated`, `failed`, `stopped`, `round_limit`, `hook_vetoed`, `hook_escalated`, `no_agent`, `agent_live`, `capacity`. `--waiting` equals `--state waiting`. Each row carries the counts of unhandled eligible, `seen`, ineligible and edited comments, the round count, the base agent and the pending id when one exists.

### `show <pr | round id | pending id>`

One object in full. A pending confirmation shows the stored diff, the commit message and each reply, sanitized and marked agent-written, `content_sha256`, the flags (Stored diff), the count of non-ASCII characters in added lines, in the message and in each reply, and the comments that were delivered and got no reply. A round row prints `finalize` (`done`, `busy`, or null) when `show` ran the finalizer, and, for an escalated round, the agent's question (`escalation.reason`, `escalation.summary`, marked agent-written and untrusted). A stored file whose sha256 differs from the record exits 12 `state_unsafe`. Exit 10 when nothing matches, 21 when an id prefix matches several objects. `show` dispatches by prefix (Objects, Values).

### `pending`

Lists records in `pending`, `pushing` and `pushed`: `id`, `tool`, `action`, `target`, `pr`, `created`, `summary`, `expires`, `state`, `diff_sha256`, `content_sha256`, `files_changed` and `reply_count`. It does not re-check stored files; `show <id>` shows any record, terminal ones too.

### `confirm <id>` (alias `approve`) `[--allow-sensitive] [--expect SHA] [--override-escalation]`

Performs the pending confirmation (Confirm order). Without a TTY and without `--yes --actor human:<user>` it exits 13 and does nothing. `--expect` binds the call to the `content_sha256` that `show` or `pending` printed; a different value refuses with `diff_changed` (`details.cause: expect_mismatch`). `--allow-sensitive` is human only and allows a record whose `flags` are not empty after you saw them. Re-running `confirm` resumes a record in `pushing` or `pushed` (Record).

### `reject <id> [--redo]`

Ends a `pending` record (`rejected`); with `--redo` the round's comments become unhandled again so that the next `check` starts a fresh round (it counts toward the round limit, also after a `remote_moved` refusal). `--redo` also works once on a `rejected` or `expired` record. See the Record tables.

### `edit <id> [--reply COMMENT_ID --body-file F] [--message-file F]`

Replaces one reply or the commit message of a `pending` record. The new text passes the artifact's checks (a reply non-empty and at most `limits.reply_bytes`; the message at most 4 KiB, then the commit-message procedure). A `COMMENT_ID` that has no reply in the record exits 10 `not_found`; `edit` cannot add a reply. The diff cannot be edited. An edited record is marked `edited` and has a new `content_sha256`. Needs a human actor.

### `drop-reply <id> <comment_id>`

Removes one reply from the pending confirmation, only in the states the Record tables allow. Removing the last open reply of a `pushed` record finishes it as Confirm order step 9 does, without the thread resolve, so a reply that can never post (a deleted PR, a revoked token) can always be ended. Needs a human actor.

### `stop <round id>`

Ends a queued or running round through agent-spawn; the closed-PR sweep uses the same rule with actor `auto:review-respond` and `cancel_reason: pr_closed`.

```text
stop(round):
  take lease pr:<pr>
  if round is terminal: exit 0 (no-op, changed false)
  if round is pending: exit 20 pending_exists (hint: reject)
  CAS set cancel_requested_at, cancel_reason (stopped or pr_closed)
  if queued: jobsctl job id stored: `jobsctl cancel <job>` (a refusal `live` means it started: continue as running);
     canceled, or no job submitted yet, or the detached process is gone: -> canceled (cancel_reason)
  if running and agent_id is known: agent-spawn stop <agent_id> --grace <G> --actor <actor>
  (a running job without an agent yet sees the flag before it launches the agent)
  exit 0 with the round (state as stored, cancel_requested_at set)
```

The round ends `canceled` when its job, a finalizer or the queued reconcile maps the stopped outcome (Run outcomes). A `stop` with `cancel_reason: stopped` sets block `stopped` when the round ends; a human `respond` clears it. A running round is never stopped with `jobsctl cancel --force`. Without jobsctl no signal is needed: the detached job reads the flag.

### `cron install|uninstall [--dry-run]`

Writes or removes one managed crontab block (Files and state). Entries have an explicit `PATH` and an optional env-file prefix.

### `export [--since T]`

Prints `log.jsonl` and `audit.jsonl` records as JSON lines.

### `prune [--older-than D] [--dry-run]`

Takes the `pass` lease. Removes finished round and pending records (terminal states only), comment records of closed PRs and their stored files older than the retention age (default 30 days). It also deletes, at the same age, `prs` rows of closed PRs, `forks` rows in `torn_down`, delivered outbox rows, expired `access_cache` rows and lease rows whose holder is gone; it never touches `log.jsonl` or `audit.jsonl`. It prints `{already_running, dry_run, results}`. It removes throwaways labeled `app=review-respond` that are older than one day and whose `round=` and `pending=` labels name only terminal records of this state database (found with `wtctl list --label`); a throwaway naming an id this database does not know is skipped with the warning `unknown_throwaway`. It never touches a record in `pending`, `pushing` or `pushed`, or a live round.

### `validate`

Offline checks: `config` (`validate` loads the config itself and reports an invalid config as its failed `config` check, exit 12, not exit 2), `dependencies` (each `--version` against the Minimum column), `sandbox` (the probe through `claude-session kinds --kind respond --json`), `cron_path` (skipped, `ok: true` and `skipped: true`, when no managed block is installed; otherwise every directory of the block's `PATH` exists and holds the resolved binaries of review-respond, forgectl, agent-spawn, pr-comment, wtctl, git and, when used, jobsctl) and `credentials` (each `hosts.<host>.token_env` variable is set, and each `token_command`'s first element resolves to an executable; it is not run). It does not inspect wtctl's configuration: wtctl has no command that prints it, so the README tells you to set wtctl's `prune.protect_labels` to include `app` and to leave `throwaway.max_age` null or longer than `pending_ttl`. Never prompts and never sends anything. Exit 0, 12 on a failed check.

## Detection and eligibility

### Detection

Per host H in `hosts_to_check` (default: the hosts that `forgectl whoami` lists with a credential), every `check` pass does this:

```text
listing = forgectl prs --host H --author me --state open --limit 1000 --json     (cost 1 per 100 items)
host error -> host failed, nothing on H changes state this pass
upsert every listed PR (state open, head, is_fork, created_at)
if listing.complete: a stored open PR of H that is not listed is read with forgectl show (budget) to learn closed or merged
work = listed non-fork PRs, ordered by last_comments_attempt_at ascending (never attempted first)
for pr in work while budget(H) allows:      # estimate: 1 + (stored comments of pr) / 100, plus 2 per uncached access lookup
   last_comments_attempt_at = now
   read_comments(pr)
read_comments(pr):         # the one per-PR read: check, check --pr, respond and _job
   forgectl comments <pr>; failed: nothing changes; complete: false: nothing is marked deleted
   update comment records (Handled set), eligibility, last_comments_read_at = now on success
```

There is no cursor and no `updated_at` filter: every pass reads every open PR, least recently attempted first, so a failing PR cannot starve the others, because a host's `updated_at` does not change on every edit, deletion or resolution. A per-host request budget (`limits.requests_per_host`, default 120 per pass; the cost of a read is an estimate, since forgectl reports no request count) bounds a pass. PRs not reached are attempted first in the next pass. A failed listing for one host does not stop the other hosts. When a decision needs facts, `forgectl show` supplies the head ref and sha, head repo, fork flag, base, state and draft flag. The start of the first command that reads comments is stored as `installed_at` (Handled set).

A forgectl read with `complete: false` never marks anything as deleted. `not_found` for a PR means unknown, never closed: a PR counts as closed only when forgectl reports `state` `closed` or `merged`. Review bodies (forgectl `reviews`) are not read in 0.x; a review's inline comments are.

### Eligibility

A comment is eligible when all of these hold:

1. Its author id is not your own account id on that host. A comment that carries pr-comment's marker on content from your own account is `own` whatever the prefix. Opt-in `respond_to_own_comments_prefix` (default off; for example `/awkit`) admits your other own comments that start with the prefix.
2. Its author id is not null (else `unknown_author`), `author.is_bot` is exactly `false` (`null` counts as a bot), and the author is not in `bot_authors`.
3. The author id is in `reviewers` (entries `<host>:<id>`, split at the last `:`; a per-repo `reviewers` list replaces the global one), or `forgectl access <repo> <login>` reports `write_access: true`. A `null` or `false` answer is not eligible. Answers are cached per (repo, author id) for `access_cache_ttl` (default 1 hour); a cached answer is used only when the comment's login equals the stored login (a renamed or re-registered login is a miss).
4. The PR is yours, open and not a fork (`head.is_fork` is false and `head.repo` equals the PR's repo; a null `is_fork` counts as a fork). A closed, merged or fork PR has no eligible comment; review-respond does not read the comments of a fork PR.
5. The thread is not resolved, the comment is not deleted and it is not minimized. Draft comments are not skipped.

An ineligible comment is recorded with its reason (`own`, `bot`, `unknown_author`, `no_access` (`write_access: false`), `unknown_access` (null, a failed lookup or the budget ran out), `resolved`, `minimized`) and never reaches a prompt. `show` lists them. With `post-attention` reason `ineligible_comment` enabled (`attention.ineligible_comment`, default off) the first ineligible comment of a PR fires one hook event.

### Handled set

review-respond stores, per comment id (the full forgectl id, for example `gh:rc:<node id>`), the body sha256, the last `updated_at`, the state, the round, `delivered_sha256`, `handled_sha256`, `delivered_from`, `first_seen_at`, and an eligibility flag with its reason (`eligible`, `reason`), re-evaluated on every read; an ineligible comment never leaves `seen` or `unhandled`. Ids of different kinds never collide. The states are `seen`, `unhandled`, `delivered`, `handled` and `deleted`; "unhandled" in prose means the state `unhandled`. A comment is **handled** when a round produced a valid artifact and the pending confirmation was recorded in one transaction, or when its thread was resolved.

| From | Event | To |
|---|---|---|
| (none) | first stored, host `created_at` before `installed_at` | `seen` |
| (none) | first stored, otherwise | `unhandled` |
| `seen` | the job of an `--include-existing` round selects it | `delivered` (`delivered_from: seen`, `round`, `delivered_sha256`) |
| `unhandled` | a job selects it into a prompt | `delivered` (`delivered_from: unhandled`, `round`, `delivered_sha256`) |
| `delivered` | its round becomes `pending` | `handled` (`handled_sha256` equals `delivered_sha256`) |
| `delivered` | its round ends `failed`, `canceled` or `escalated` | its `delivered_from` state |
| `handled` | its record ends `rejected` without `--redo`, `expired`, `closed` or `done` | `handled` (no change) |
| `handled` (by round R, `handled_reason` null) | `reject --redo` of R's record (`pending`, `rejected` or `expired`) | `unhandled` |
| `handled` | its current body hash differs from `handled_sha256`, its thread is unresolved, its author is still eligible and no record of the PR is open | `unhandled`, `edited: true` |
| `seen`, `unhandled`, `handled` | its thread is resolved | `handled` (`handled_reason: resolved`) |
| any but `deleted` | absent from a read with `complete: true` | `deleted` (terminal) |

Notes. A comment cut from a prompt by the budget is never `delivered`. A `delivered` comment that is edited, resolved or deleted during its round keeps `delivered`; the round's end moves it, and the next read applies the other rows. `installed_at` is the local time at the start of the first command that read comments (any `check`, or `respond`); a comment first stored with a host `created_at` before it is `seen` (installing review-respond never answers old comments), any later comment is `unhandled`; `list` shows the `seen` count. A host clock that is off by seconds can classify a comment posted in those seconds either way; that is accepted. A thread that someone un-resolves after review-respond recorded it `resolved` stays `handled`; review-respond answers new and edited comments only, and never un-resolves a thread. A changed `updated_at` with an equal hash does nothing. A deleted comment drops out of prompts and is never retried; a pending reply to it is skipped (Replies). A thread resolved by someone else removes its comments from prompts and a pending reply to it is skipped. **Settle timestamp:** `settle_ts(c)` is the local time review-respond first stored the comment's current body hash (an edit restarts it), so host clock skew does not matter. A comment edited while its round is running or its record is open keeps `delivered` or `handled`; the edit is acted on after the record ends (the `handled` to `unhandled` row), and `confirm` skips its pending reply (Replies).

### Settle and round start

A PR is **due** when this holds:

```text
due(pr):
  pr is open and not a fork
  and eligible_unhandled(pr) is not empty
  and no round of pr is queued or running
  and no record of pr is pending, pushing or pushed
  and pr.block is null
  and counted(pr) < max_rounds        # else set block round_limit and fire post-attention round_limit once
  and resolve_base(pr) is an agent or a fresh-session opt-in (repos.<id>.respond_fresh)   # else set block no_agent
  and not agent_busy(pr)
  and (now - max(settle_ts(c) for c in eligible_unhandled(pr)) >= settle
       or now - min(settle_ts(c) for c in eligible_unhandled(pr)) >= 1h)      # the second bound is a constant: edits cannot hold a PR in settling
```

`agent_busy(pr)`: the base agent or the PR's base fork has status `launching`, `live` or `tearing_down`, or a promotion pending.

`check` starts due rounds, oldest due first, while the number of `queued` and `running` rounds over all PRs is below `jobs.max_running` (default 1), in both placements (with jobsctl the queue's own cap applies as well). A due PR over that number is `waiting` with `capacity`. A human `respond` is not bound by `jobs.max_running`. A round's prompt holds the eligible unhandled comments at the moment its job starts.

**Counting.** A round is **counted** when its run started: agent-spawn reported an outcome other than `not-started` (exit 0 after a run, 13 from the session phase, 14 to 18), or `agent-spawn show` gives its run a non-null `first_working_at`. Every counted round counts, including a stopped one that started and the round after `reject --redo`. The PR's limit is reached when the number of counted rounds is at least `max_rounds`. At the limit, `post-attention` reason `round_limit` fires once per block episode; only `respond --force` or a higher `max_rounds` continues.

**Stored PR block** (`prs.block`, null or `{reason, set_at, comment_set_sha256}`) keeps escalated, failed, stopped, limited, agentless and vetoed PRs from re-firing. `comment_set_sha256` is the sha256 of the sorted eligible unhandled comment ids joined by `\n`.

| Block reason | Set when | Cleared when |
|---|---|---|
| `escalated` | a round ended `escalated` (the agent asked for input, or a lower hook escalated its launch) | a human `respond` |
| `failed` | a round ended `failed`, or the third consecutive `read_failed` cancel | a human `respond` |
| `stopped` | a round ended `canceled` with `cancel_reason: stopped` | a human `respond` |
| `round_limit` | a due check finds the limit reached | a human `respond --force`, or `max_rounds` raised above the count |
| `no_agent` | the due rule finds no agent and no fresh-session opt-in | `check` finds an agent again (`resolve_base`), or a human `respond` |
| `hook_vetoed` | a `pre-round` hook vetoed | a human `respond`, or the set of eligible unhandled comment ids differs from `comment_set_sha256` (a new or edited comment) |
| `hook_escalated` | a `pre-round` hook escalated | a human `respond`; the hook runs again unless `--override-escalation` is given |

A failed round sets block `failed`; a human `respond` starts a new round. There is no automatic retry.

**PR state** (derived, the first matching row wins; `list` and `show` print it):

```text
if host state is closed or merged                  -> closed
elif a round of the PR is queued                   -> queued
elif a round of the PR is running                  -> running
elif a record is pending, pushing or pushed        -> pending
elif block is set                                  -> waiting (waiting_reason = block.reason)
elif no eligible unhandled comment                 -> idle
elif agent_busy(pr)                                -> waiting (waiting_reason = agent_live)
elif neither settle bound has passed                  -> settling
else                                               -> waiting (waiting_reason = capacity)
```

`pre-round` hooks run after the due rule and before the round row is created (Hooks). A veto creates no round, sets block `hook_vetoed`, leaves the PR `waiting` (`hook_vetoed`) with `vetoed` in its `errors` (exit 0) and makes `respond` exit 11. An escalation creates no round, sets block `hook_escalated`, fires `post-attention` reason `escalated` with `details.cause: hook`, leaves `check` at exit 0 and makes `respond` exit 13.

## Rounds

### Round states

A round has the number `number` (1 for a PR's first round, incremented for every new round of the PR, comment-triggered or `respond`). Every transition is a compare-and-set on the round's `rev` (Concurrency).

| From | Event | To | Same transaction |
|---|---|---|---|
| (none) | the due rule holds (or a human `respond`), the `pre-round` hooks continued | `queued` | round row (`number`, `base_agent_id`, `trigger`, `forced`, `include_existing`); then the job is submitted and its job id (jobsctl) or the detached start time stored |
| `queued` | `_job` finds `cancel_requested_at` set | `canceled` (the stored `cancel_reason`) | not counted |
| `queued` | `_job` reads the PR closed or merged | `canceled` (`pr_closed`) | not counted |
| `queued` | `_job`'s PR or comment read failed | `canceled` (`read_failed`) | not counted; no block, so the next due `check` starts a new round, until the third consecutive `read_failed` of the PR sets block `failed` (`prs.read_failures` counts them; a round of the PR that reaches `running`, or a human `respond`, resets it) and queues `post-attention` `failed` once (`details.round_id` only) |
| `queued` | `_job` selects no comment (or the PR is a fork or not yours, defensive) | `canceled` (`nothing_to_respond`) | not counted |
| `queued` | `_job` re-read the PR and the comments and selected at least one comment | `running` | `head_sha`; each selected comment `delivered` with `delivered_sha256` and `delivered_from` |
| `queued` | `stop` or the closed-PR sweep: the jobsctl job was canceled, or no job was submitted yet, or (detached) the job process is gone | `canceled` (`stopped` or `pr_closed`) | not counted |
| `queued` | queued reconcile (below): the job is terminal or gone and the round is still `queued` | `failed` (`job_lost`), or `canceled` (stored `cancel_reason`) when `cancel_requested_at` is set | not counted |
| `running` | the run ended `done`, the artifact validated, the stored diff was built | `pending` | pending record; delivered comments `handled`; `post-pending` queued |
| `running` | agent-spawn exit 13 (Run outcomes) | `escalated` | delivered comments back to `delivered_from`; block `escalated`; the report's reason and summary stored; `post-attention` `escalated` queued |
| `running` | `cancel_requested_at` is set and the run ended stopped, or the job saw the flag before it launched the agent | `canceled` (stored `cancel_reason`) | delivered comments back to `delivered_from`; block `stopped` when `cancel_reason` is `stopped` |
| `running` | any other end (Run outcomes, including `job_lost` and `job_terminated`) | `failed` | delivered comments back to `delivered_from`; block `failed`; `post-attention` `failed` (or `job_lost`) queued |
| `pending` | its record reached `done` | `confirmed` | (the record's transaction) |
| `pending` | its record became `rejected`, `expired` or `closed` | `rejected`, `expired` or `closed` | (the record's transaction) |

Terminal states: `confirmed`, `rejected`, `expired`, `closed`, `escalated`, `failed`, `canceled`; they are immutable. A round stays `pending` while its record is `pending`, `pushing` or `pushed`. Cleanup of throwaways and forks for every terminal round: Cleanup. An `escalated` round is never retried: `post-attention` reason `escalated` fires; `show <round>` prints the agent's question (`escalation.reason`, `escalation.summary`, untrusted, each at most 4 KiB); `respond <pr> --note-file F` starts a new round from the PR's base, so the note must carry the answer in full.

`check` housekeeping reconciles queued rounds under the PR's `pr:` lease:

```text
reconcile_queued(r):                       # r.state == queued
  if a live process holds the round: lease: skip      # _job is running or about to run
  if no job id and no PID is stored:       # check crashed between the round row and the submission
     if now - r.created < 2 minutes: skip
     submit the job again (jobsctl: the same --unique-key makes add idempotent; detached: start it again)
  elif jobsctl:
     j = jobsctl show <r.job_id> --json
     j.status in (queued, starting, running, held): nothing      # a held job keeps the round queued; stop cancels it
     j is terminal (done, failed, canceled) and r is still queued: -> failed (job_lost), or canceled when cancel_requested_at is set
  else (detached):
     the start time is over 2 minutes ago, no process holds the round: lease, and r is still queued: -> failed (job_lost), or canceled as above
```

A jobsctl job that ends without running `_job` (`missing_env`, `spawn_failed`, `lost`, a `pre-start` veto, a human `jobsctl cancel`) therefore ends the round `failed` (`job_lost`) at the next `check`, sets block `failed` and fires `post-attention` `job_lost` once. A job held by jobsctl (`manual`, or `escalated` by a jobsctl `pre-start` hook) keeps the round `queued`; `stop` or `jobsctl release` or `cancel` continues.

`check`, `show` and `confirm` run the finalizer for a `running` round whose `round:` holder is verified gone:

```text
finalize(r):
  if r.agent_id is null:
    cands = agent-spawn list --label round=<r.id> --label owner=review-respond --json
    accept c only if (fork: c.forked_from == r.base_agent_id) or (fresh: c.run_kind == respond and c.cwd == realpath(round throwaway))
    exactly one accepted: record it as r.agent_id
    none: -> failed (job_lost)
    several: record none, mark none for teardown (teardown acts only on the forks table), -> failed (job_lost), warning ambiguous_fork with the candidate ids
  a = agent-spawn show r.agent_id --json
  if a.status in (launching, live, tearing_down) or promotion pending: leave r running (try again next pass)
  else finish_round(r, outcome of a.runs[-1])
```

### Run outcomes

One table maps every exit of `resume`, `launch` and `wait`, with the result's `phase`, `outcome` and `end_reason`, into the round. The `_job` column is the job's exit code; it only feeds jobsctl, the round row is the truth (0 pending recorded or canceled, 12 failed, 13 escalated, 1 internal).

| Exit (command) | Condition | Round state | `failure.reason` | Counted | `_job` exit |
|---|---|---|---|---|---|
| 0 (`resume`, `launch`) | the run started | stays `running`, agent id and forks row recorded | | per Counting | (continues) |
| 0 (`wait`) | outcome `done`, artifact valid, diff built | `pending` | | yes | 0 |
| 0 (`wait`) | outcome `done`, validation or diff failed | `failed` | `artifact_missing`, `artifact_invalid`, `implicit_done`, `empty_result`, `diff_failed` | yes | 12 |
| 10 (`resume`) | base agent not found, torn down or `session_missing` | `failed` | `agent_gone` (the base is reset) | no | 12 |
| 10 (`wait`) | the fork's own id is unknown | `failed` | `agent_lost` (the base is kept) | per Counting | 12 |
| 11 | a claude-session `pre-launch` hook vetoed | `failed` | `vetoed` | no | 12 |
| 12 | launch failed, `not-started`, `no_run`, `tool_missing`, with `cancel_requested_at` set | `canceled` | | no | 0 |
| 12 | the same, any other case | `failed` | `launch_failed` | no | 12 |
| 13 | session phase (report `blocked` or `needs-input`) | `escalated` | | yes | 13 |
| 13 | launch phase (envelope `code: escalated`, a lower hook) | `escalated` (`details.cause: hook`) | | no | 13 |
| 14 | `exited-unreported`, after agent-spawn's single automatic report resume (the resumed run's outcome maps by its own row) or when none ran | `failed` | `unreported` | yes | 12 |
| 15 | `killed` with end reason `stopped` or `forwarded_*` and `cancel_requested_at` set | `canceled` | | yes | 0 |
| 15 | `killed`, any other case | `failed` | `agent_failed` | yes | 12 |
| 16 | `timeout` | `failed` | `timeout` | yes | 12 |
| 17 | `lost` | `failed` | `agent_lost` | yes | 12 |
| 18 | `failed` | `failed` | `agent_failed` | yes | 12 |
| 20 | refused (`live`, `tearing_down`, `third_party_cwd`, `third_party_run`, `tainted`) | `failed` | `refused` | no | 12 |
| 1, 2, 21, 22 | internal, usage, ambiguous (22 cannot occur: no wait timeout is passed) | `failed` | `internal` | no | 1 |
| 130, 143 (`wait`) | the child got the process group's signal | re-read `agent-spawn show` and map its last run; never an outcome by itself | | | |
| (SIGTERM of `_job`) | without `cancel_requested_at` | `failed` | `job_terminated` | per Counting | 12 |
| wtctl `create --throwaway` | any non-zero exit (the sibling error in `lower_error`) | `failed` | `throwaway_failed` | no | 12 |
| (none) | job gone, no agent found (finalizer or queued reconcile) | `failed` | `job_lost` | no | |

The closed `failure.reason` values are `throwaway_failed`, `launch_failed`, `vetoed`, `refused`, `agent_gone`, `unreported`, `agent_failed`, `agent_lost`, `timeout`, `job_terminated`, `job_lost`, `artifact_missing`, `artifact_invalid`, `implicit_done`, `empty_result`, `diff_failed` and `internal`. An invalid artifact carries a closed `failure.detail` (Artifact).

### Fork lineage

review-respond keeps a **base agent** per PR (`prs.base_agent_id`). It acts (resume, stop, teardown) only on agent ids in its own `forks` records: label searches only find candidates, never select its own forks and never cause a teardown. The original agent is never torn down, stopped or labeled by review-respond.

```text
resolve_base(pr):
  if pr.base_agent_id:
     a = agent-spawn show <pr.base_agent_id> --json
     exit 10 or status torn_down: pr.base_agent_id = null (the confirmed context is lost; warning base_lost)
     else return a
  if the PR reference is too long for a label (Labels): cands = []
  else cands = agent-spawn list --label pr=<pr> --json
  keep c only if c.agent_id is not in forks, c.repo == pr.repo, c.worktree is not null,
     wtctl show <c.worktree.record_id> --json has branch == pr.head_ref, and c.run_kind is not third-party-reading
  preferred = [c for c in cands if label owner=<prefer_owner>]
  return newest(preferred) or newest(cands) or FRESH if opted in (repos.<id>.respond_fresh) or NONE
```

A label is not authenticated, so the match is verified against the worktree and branch as above; among equal candidates the most recent wins. Each round forks the base agent into the round's throwaway. The lineage updates are transactional:

```text
lineage updates (forks table states: base, round, teardown_pending, torn_down):
  agent id recorded (_job): round fork row role round
  record pushed (with a diff) or done (without): pr.base_agent_id = round fork; previous base fork (if ours) -> teardown_pending   [same transaction]
  terminal rounds and closed PRs: Cleanup
  check: for each teardown_pending: agent-spawn teardown <id> --actor auto:review-respond; exit 0 or 10 -> torn_down;
         20 live -> agent-spawn stop first, retry next pass; after 3 failed passes post-attention teardown_refused once
```

With no original agent, a PR in a repo opted in with `repos.<id>.respond_fresh: true` gets a fresh `respond-to-review` session in the round's throwaway; after a confirm it is the base like any other fork. A fresh session knows the PR only from the comments and the files. A fork PR gets no round of any kind in 0.x (Eligibility).

### Cleanup

Throwaway removal and fork teardown marks are not listed per transition. One idempotent rule runs in the same command after the transaction of a terminal transition and again in every `check` until it succeeds. A throwaway is removed only when everything it belongs to is terminal, so a `pushing` record never loses its throwaways, and only under its PR's `pr:` lease: a command that ends a record or round already holds it, and `check` takes it without waiting and skips the PR's throwaways when it is held (a running `confirm` keeps its clean checkout).

```text
cleanup (after a terminal transition, and in every check):
  for each throwaway labeled app=review-respond (a round= id this database does not know: skip, warning unknown_throwaway):
     its round (round=) is terminal and its record (pending=), if any, is terminal -> wtctl remove (exit 20 in_use: retried next check)
     a clean checkout (role=clean) whose record is pending or pushed (not pushing) -> wtctl remove
  for each terminal round: its round fork -> teardown_pending, unless the fork is the PR's base
  for a closed or merged PR with no record in pushing or pushed: the base fork -> teardown_pending
```

A fork is tainted (architecture section 8.5). review-respond never resumes it in a kind that does not read third-party text and never passes `--allow-tainted`.

### Job

A round runs as one job: the hidden command `review-respond _job <round id>` (not part of the interface; its argv is `<absolute review-respond> [--config <absolute path>] _job <round id>`).

```text
_job(round_id):
  take lease round:<round_id> without waiting; held by a live holder: exit 0
  r = load round; if r.state != queued: exit 0
  if r.cancel_requested_at: -> canceled (r.cancel_reason); exit 0
  pr = forgectl show <pr>; failed: -> canceled (read_failed); exit 0
  if pr closed or merged: -> canceled (pr_closed); exit 0
  if pr is a fork or not yours, or its head ref fails git check-ref-format --branch: -> canceled (nothing_to_respond); exit 0        # defensive
  read_comments(pr) (Detection; under pr:); failed: -> canceled (read_failed); exit 0
  comments = select(eligible and unhandled, or seen when r.include_existing; prompt budget)
  if comments empty: -> canceled (nothing_to_respond); exit 0
  CAS queued -> running (head_sha = pr.head.sha, delivered ids, hashes, delivered_from)
  tw = wtctl create --throwaway --repo <id> --ref <head_sha> --label app=review-respond --label role=round --label round=<round_id> --json
       any non-zero exit -> failed (throwaway_failed)
  record tw id; write prompt file and generated profile
  if r.cancel_requested_at: -> canceled (r.cancel_reason)
  res = agent-spawn resume <base> --fork ... --json     (or launch ...; Session settings; no --wait)
       exit 0: CAS record agent_id = res.agent.agent_id and the forks row (role round)
       other exit: finish_round(r, res); exit
  if r.cancel_requested_at: agent-spawn stop <agent_id> --grace <G> --actor auto:review-respond
  res = agent-spawn wait <agent_id> --json; exit 130 or 143: res = agent-spawn show <agent_id> --json
  finish_round(r, res)
finish_round(r, res):            # the same function the finalizer calls
  map with Run outcomes
  outcome done: a = agent-spawn show <agent_id> --json; a live (launching, live, tearing_down): leave r running for the finalizer; exit 0
     validate the artifact (Artifact), build the stored diff (Stored diff), then one transaction: pending record,
     comments handled, round pending, post-pending queued; then wtctl label add <tw> pending=<pending id>
  otherwise one transaction: the round's state, comments back to delivered_from, the block, post-attention queued
on SIGTERM: if agent_id known: agent-spawn stop <agent_id> --grace <G> --actor auto:review-respond;
  wait for the wait call to return (at most jobs.kill_grace minus 15 s); finish_round
  (stopped with cancel_requested_at -> canceled (r.cancel_reason); without it -> failed (job_terminated))
G = jobs.kill_grace minus 50 s, at least 5 s (75 s with the default), so agent-spawn's stop (grace plus 30 s) ends inside the job's budget
```

The job records the agent id before it waits, and every fork carries a `round=` label, so a crashed job can be finalized. At record time `finish_round` leaves a round whose agent is still live to the finalizer.

With jobsctl: `check` submits `_job` to the queue `respond` (config `jobs.queue`) with `--unique-key respond:<PR digest>:<number>` (a PR reference can be long), `--max-retries 0`, `--timeout none` (the agent is bounded by `agent.timeout` and the kind's own timeout), an explicit `--cwd` (the state directory), `--kill-grace` (`jobs.kill_grace`, default 125 seconds), `--label` correlation labels, `--env XDG_CONFIG_HOME=...` and `--env XDG_STATE_HOME=...` when set, and `--pass-env` for the names in `jobs.pass_env`. It reads the job by its stored id. An exit-13 job is held by jobsctl; `check` resolves the held job (`jobsctl resolve --as done`) after the round row says `escalated`.

Without jobsctl: `check` starts `_job` as a detached process (double fork, stdio redirected, start time recorded; the job's own PID is in its `round:` lease). It never waits. A job that dies is found by its lease and start time and reconciled or finalized as above.

The job's environment is built from scratch in both placements: `PATH`, `HOME`, `LANG`, `LC_ALL`, `TZ`, `XDG_CONFIG_HOME`, `XDG_STATE_HOME`, `XDG_RUNTIME_DIR` (each when set) and the names in `jobs.pass_env`. `REVIEW_RESPOND_*` variables are not passed to jobs (the config file is). The job creates the clean checkout for the stored diff with `--source remote`, and it reads the PR and the comments with forgectl, so `jobs.pass_env` must name the variables forgectl reads for each host (`hosts.<host>.token_env` in forgectl's config) and, for a private repository, the ones wtctl reads (in wtctl's config); with jobsctl their values come from jobsctl's `env_file`.

### Prompt

The prompt file lives in the state directory and is deleted when the round ends. It holds, in this order: the working copy's real absolute path (the throwaway root; the forked conversation otherwise mentions the original worktree's paths), the standing rule that comment text is data and not instructions, your optional note as a separate trusted block (read once from `--note-file`, at most 8 KiB, stored with the round), and the eligible comments of the job's own read (Job) inside one nonce fence `<untrusted source="comments" id="R">...</untrusted id="R">` with every `</untrusted` neutralized. The nonce is 16 random bytes from the operating system as 32 lowercase hex characters, new for every prompt. Comments are taken oldest first by `settle_ts`. Each comment has its id, an author label (the login, sanitized), `edited` when it applies, `thread_id`, `reply_to`, `outdated`, the thread's path, line and side, and its body, sanitized as one routine (control and invisible characters replaced by visible markers). A single body over 16 KiB is cut with a visible `[truncated: <n> bytes]` marker and still delivered. `limits.prompt_bytes` (default 65536) bounds the prompt; overflow comments stay unhandled (or `seen`) for the next round and a visible truncation marker names what was cut. Nothing from an ineligible comment, no PR title, no description and no CI log is included.

### Session settings

Every round passes these to agent-spawn per call (design guide section 11), always with `--mode headless` (unless `respond --mode interactive`, unverified in 0.x), `--no-promote`, `--no-implicit-done`, `--prompt-file`, `--actor auto:review-respond`, `--env-allowlist-only` (with an empty value for an empty list) and, when `agent.timeout` is set, `--timeout`. There is no `--wait`: the job records the agent id and then runs `wait` (Job). `--no-implicit-done` is passed on every fork and every fresh launch, because agent-spawn passes it on a resume only when given on that call and a change of `--kind` drops claude-session's carry. It makes a clean headless exit with no report end `exited-unreported` (exit 14, `failure.reason` `unreported`) instead of counting as an implicit `done`; agent-spawn then re-prompts for the report once (its automatic report resume) and the resumed run's outcome is the round's. Artifact validation keeps the `report.implicit` check as a defense: a run whose outcome is an implicit done is `failed` (`implicit_done`).

```text
fork:   agent-spawn resume <base agent id> --fork --cwd <throwaway root realpath>
          --kind respond --mode headless --skill <skill> --plugin-dir <bundled plugin dir>
          --prompt-file <prompt file> --permission-profile <generated profile file>
          --sandbox <sandbox> --setting-sources "<setting_sources, comma-joined, empty for none>"
          --env-allowlist-only <VAR> ... | --env-allowlist-only ''
          [--model <model>] [--timeout <agent.timeout>] --no-promote --no-implicit-done
          --label pr=<pr> --label owner=review-respond --label repo=<repo id> --label round=<round id>
          --actor auto:review-respond --json
fresh:  agent-spawn launch --kind respond --mode headless --worktree none --cwd <throwaway root realpath> --repo <repo id>
          (the same flags from --skill to --json)
```

The cwd is exactly the throwaway root (a subdirectory is refused by agent-spawn). `--skill` is not carried to a fork, so it is always passed. Labels that cannot hold the PR reference or the repo id (over 256 characters) are omitted (Labels).

The permission profile is generated per call: the bundled allowlist profile plus the repo's `respond.allow_commands` (argv patterns for test and build commands) and `respond.read_dirs`. The bundled profile keeps the architecture's respond-profile denies and additionally denies `memoctl add`, `memoctl delete`, `memoctl cron`, `agent-spawn`, `notice-route send`, `git push` and `gh` and `glab`, and any read or write under review-respond's own config and state directories. Config keys replacing these: `skill`, `permission_profile`, `sandbox` (`required` by default; `preferred` and `off` are accepted with a warning), `setting_sources`, `env_allowlist` (names passed with `--env-allowlist-only`), `model`, `agent.timeout`.

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
  path-state checks -> artifact_invalid (detail path_state)
```

`empty_result` is decided after the stored diff: a round with no replies and an empty diff fails `empty_result`. A missing skill exits 0 in agent-spawn, so a wrong or missing marker is an invalid artifact. Paths are refused, never rewritten. **Path-state checks** run before every extract, after the agent is not live: each `changed_files` path is, by a no-follow `lstat` chain from the round throwaway root, a regular file with one link; each `deleted_files` path is absent (ENOENT); otherwise the round fails `artifact_invalid` (detail `path_state`) at record time, or `confirm` exits 20 `diff_changed` (`details.cause: path_state`). `--dest` is always the clean throwaway path from review-respond's own record. A copy of the validated artifact is stored in the state directory.

The artifact's file lists are never trusted for what they say about the working copy: the stored diff and the touched paths come from extraction in a clean checkout, not from the round throwaway or the lists.

## Pending confirmations

### Record

A pending confirmation has the id `rp-` plus 8 base32 characters and holds: `pr`, `repo`, `round`, `head_sha` and `head_ref` at round time, `base_ref`, the fork agent id, the round throwaway id, `changed_files`, `deleted_files`, `commit_message`, the replies, `diff_sha256` (null when the record has no diff), `diff_file_sha256`, `message_sha256`, `content_sha256`, `diff_stats` (files, additions, deletions), `flags` (Stored diff), `no_reply_comment_ids` (comments that were delivered and got no reply), `removed_line_count` (lines the commit-message procedure dropped), `commit_shas` (every sha a push intent tried, newest last), `commit_sha` (the landed sha, or null), `moved_head` (the last head that fired `remote_moved`), `clean_throwaway_id`, `push_started_at`, `redone_at`, `edited`, `state`, `created` and `expires` (default 7 days, `pending_ttl`). `content_sha256` is the sha256 of the UTF-8 text made of the line `diff:<diff_sha256 or none>`, the line `message:<message_sha256>` and one line `reply:<comment id>:<sha256>` per reply in record order, joined by `\n`; `edit` and `drop-reply` change it. Each reply holds `comment_id`, `thread_id`, `body`, `dedupe_key`, `status`, `status_reason`, `threaded` and `sha256` (of its stored body file). Free text lives in stored files and appears in `show --json` only inside fields marked untrusted in the schema.

States: `pending`, `pushing`, `pushed`, `done`, `rejected`, `expired`, `closed`. `pushing` and `pushed` are the intent states of Confirm order; `pushed` means the push landed, or there was nothing to push; the per-reply status is the intent of each reply. One function, `end_record(record, to)`, ends a `pending` record for `reject`, expiry and a closed PR, whether `check` or `confirm` finds the cause. Each state allows a fixed set of commands:

| State | `confirm` | `reject` | `reject --redo` | `edit` | `drop-reply` | Expiry (`now >= expires`) | PR closed or merged |
|---|---|---|---|---|---|---|---|
| `pending` | full order | `end_record(rejected)` | `end_record(rejected)`, then redo | yes (human) | yes (human) | `end_record(expired)` (`check`, or `confirm` step 1) | `end_record(closed)` (`check`, or `confirm` step 2) |
| `pushing` | `resume_pushing` (Push) | 20 `not_pending` | 20 `not_pending` | 20 `not_pending` | 20 `not_pending` | never; `post-attention` `confirm_incomplete` once | never; `resume_pushing` decides |
| `pushed` | resume at Replies | 20 `not_pending` | 20 `not_pending` | 20 `not_pending` | yes (human), for replies with status `pending`, `failed` or `unknown`; the last one finishes the record | as `pushing` | never; replies are allowed on closed PRs |
| `done` | 20 `not_pending` | 20 `not_pending` | 20 `not_pending` | 20 `not_pending` | 20 `not_pending` | | |
| `rejected` | 20 `not_pending` | 0 (no-op) | redo once; again 0 (no-op) | 20 `not_pending` | 20 `not_pending` | | |
| `expired` | 20 `expired` | 0 (no-op) | redo once; again 0 (no-op) | 20 `expired` | 20 `expired` | | |
| `closed` | 20 `pr_closed` | 0 (no-op) | 20 `pr_closed` | 20 `pr_closed` | 20 `pr_closed` | | |

Transitions:

| From | Trigger | To | Same transaction |
|---|---|---|---|
| (none) | the round's validated artifact and stored diff were recorded | `pending` | round `pending`; delivered comments `handled`; `post-pending` queued |
| `pending` | `confirm` step 7 starts, the record has a diff | `pushing` | the new commit sha appended to `commit_shas`; `push_started_at` |
| `pending` | `confirm` step 8 starts, the record has no diff | `pushed` | |
| `pushing` | the push landed (Push) | `pushed` | `commit_sha` (the landed one); lineage switch (Fork lineage) |
| `pending` | `confirm` step 2 finds a head that satisfies `landed` for a sha in `commit_shas` | `pushed` | `commit_sha` (the matching one); lineage switch (Fork lineage) |
| `pushing` | refused, the ref is missing, the remote moved, or nothing landed and the clean checkout is gone (Push) | `pending` | |
| `pushed` | every reply is `posted`, `skipped` or `dropped` (`confirm` step 9, or `drop-reply` of the last open reply) | `done` | round `confirmed`; lineage switch for a record without a diff; `post-confirm` queued |
| `pending` | `end_record(to)`, `to` in `rejected`, `expired`, `closed` | `to` | round `to`; `post-attention` `pending_expired` (expired) or `pr_closed` (closed) queued |
| `rejected`, `expired` | `reject --redo` | (unchanged) | the round's comments that are still `handled` by this round and not `handled_reason: resolved` become `unhandled`; `redone_at` set (a second `--redo` is a no-op) |

`end_record` and the round's terminal transition run in one transaction; throwaways and forks are then handled by Cleanup. A resume (`pushing`, `pushed`) needs a human actor like a fresh confirm; it re-runs no `pre-push` hook and no expiry check, and only the arm of `resume_pushing` that pushes repeats the live reads of Confirm order step 2 (`--expect` still applies).

### Stored diff

When the job ends, review-respond builds the stored diff in a clean checkout, then removes that checkout. One recipe serves record time and confirm time:

```text
clean = wtctl create --throwaway --source remote --repo <id> --ref <head_sha>
          --label app=review-respond --label role=clean --label round=<round id> [--label pending=<pending id>] --json
record clean.id immediately, in its own transaction (record time: on the round; confirm time: clean_throwaway_id on the record)
confirm time: a live fork (agent-spawn show <fork id> status in launching, live, tearing_down) -> exit 20 live
path-state checks of the artifact lists in the round throwaway (Artifact)
wtctl extract <round throwaway id> --files-from F --delete-from F --dest <clean.path from the record>    # F are files review-respond wrote
removal: record time, right after the diff is stored or failed; confirm time, Cleanup
```

```text
stored_diff(clean):
  add --pathspec-from-file=<F> --pathspec-file-nul            # literal pathspecs (Hardened git)
  if diff --cached --quiet: no file changes: the record has no diff (diff_sha256 null) and its changed_files and deleted_files are cleared
  text   = diff --cached --text --full-index --no-renames
  status = diff --cached --name-status -z --no-renames         # touched = every path listed (adds, modifies, deletes)
  numstat= diff --cached --numstat -z --no-renames             # binary = paths with "-" counts
  flags = review_flags(touched, numstat, text, message, replies)
  diff_sha256 = sha256(text); diff_stats from numstat
```

`flags` is a list of `{where, cause, line}` of at most 100 entries, followed by one `{where: null, cause: truncated}` entry when more were found (the refusal rule stays: the list is not empty). Causes: `off_limits` (a touched path matches `off_limits`), `binary` (numstat `-` counts), `oversized` (the stored diff is over `limits.diff_bytes`, default 262144; `where` null), `long_line` (an added line over 2000 bytes, a constant) and `secret_pattern` (an added line, a commit message line or a reply line matching a closed list: `-----BEGIN` followed by a space, `ghp_`, `gho_`, `ghs_`, `ghu_`, `github_pat_`, `glpat-`, `AKIA` followed by 16 of `[0-9A-Z]`, `xox[abp]-`) and `truncated`. `where` is the path, `commit_message` or the reply's comment id. The tripwire is not a guarantee. `confirm` refuses with `sensitive_paths` (`details.causes` lists the causes) when `flags` is not empty, unless a human passes `--allow-sensitive`. `show` prints the flags, the diff through the one sanitizer, and the count of non-ASCII characters in added lines, in the message and in each reply. The sha256 of every stored text file is kept in the record and re-checked by `show` and `confirm`; a differing file exits 12 `state_unsafe`. If extraction or the diff fails, the round is `failed` with `diff_failed` and nothing is recorded. The round throwaway stays, untouched by any trusted process, until its round and record are terminal (Cleanup). It carries the labels `app=review-respond`, `role=round`, `round=<id>` and `pending=<id>`. The README setup lists the wtctl settings that keep it (`prune.protect_labels` includes `app`).

## Confirm order

`confirm <id>` runs these steps. Each step that acts first writes its intent to the record, and each is idempotent. Steps 4 to 7 are skipped when the record has no diff (`diff_sha256` null); steps 1 to 3 and 8 to 9 always run.

1. **Preconditions.** State `pending` (other states: the Record tables). Not expired (else `end_record(expired)` and the command exits 20 `expired`). A human actor (a TTY, or `--yes --actor human:<user>`), else exit 13 `confirmation_required`. `--expect`, when given, equals `content_sha256` (else 20 `diff_changed`, `details.cause: expect_mismatch`). Each stored file's sha256 equals the record's (else 12 `state_unsafe`). Lease `pr:` taken. On a TTY without `--yes`, `confirm` prints, from the files whose hashes it just checked, the stored diff through the one sanitizer, the cleaned commit message, every reply (marked agent-written), the flags, the comments that got no reply and `content_sha256`, then asks once; any answer other than `y` or `yes` exits 13 `confirmation_required` and changes nothing.
2. **Live reads.** `forgectl show <pr>`: closed or merged makes the record `closed` (`end_record(closed)`) and exits 20 `pr_closed`; a fork PR (or a head repo other than the base repo) exits 20 `ineligible` (`details.reason: fork_pr`); for a record with a diff, the default branch is read from `ls-remote --symref <url> HEAD` (the line `ref: refs/heads/<name>` then a tab and `HEAD`): a head ref equal to it (compared with case folded), or no such line, exits 20 `ineligible` (`details.reason: default_branch`), a failed `ls-remote` exits 12 `push_failed` with the record unchanged, and a head ref other than the record's `head_ref` exits 20 `remote_moved`, and a head sha other than `head_sha` first checks a late push: when `commit_shas` is not empty and `landed(record, head)` (Push) holds, the record becomes `pushed` (`commit_sha` the matching sha, lineage switch) and `confirm` continues at Replies, otherwise it exits 20 `remote_moved` (the record stays `pending`; `post-attention` `remote_moved` fires when the observed head differs from `moved_head`, which it then stores). `forgectl comments <pr>` is read once for Replies (not when no reply is open). A failed `forgectl show` or `forgectl comments` exits 12 `lower_failed` with the record unchanged.
3. **Hooks.** `pre-push` hooks run (for every record: `files_changed: 0` and `diff_sha256: null` for a record without a diff): a veto exits 11; an escalation exits 13 (the record stays `pending`) unless `--override-escalation` was given.
4. **Clean checkout.** First remove a clean checkout already named by `clean_throwaway_id` on a `pending` record (a crash leak), then run the recipe in Stored diff. The round throwaway is looked up with `wtctl show`; a missing one exits 12 `throwaway_missing` and the record stays `pending` (`reject --redo` starts again). This is the only place `throwaway_missing` is produced.
5. **Diff check.** Compute the stored diff again. A different sha256 refuses with `diff_changed` (`details.cause: recomputed`). Any `flags` refuse with `sensitive_paths` unless a human passed `--allow-sensitive`.
6. **Commit.** Stage exactly the listed paths with literal pathspecs and commit with your identity (Commit); the sha is appended to `commit_shas` by the push intent.
7. **Push.** Record `pushing`, then push fast-forward only (Push). On success record `pushed` and update the lineage (Fork lineage).
8. **Replies.** Record `pushed` if the record has no diff, then post each reply in record order (Replies).
9. **Finish.** When every reply is `posted`, `skipped` or `dropped`: the optional thread resolve (Replies), then one transaction: record `done`, round `confirmed`, the lineage switch for a record without a diff, `post-confirm` queued. Cleanup then removes the throwaways and marks the replaced base fork for teardown.

### Commit

The identity is `git.user_name` and `git.user_email`; when unset they are read from your global git config file directly (parsed, no includes, no git process); with neither, `confirm` refuses with `error.code: identity_missing`. The message goes through a closed procedure and then to git through `-F <file>`:

```text
sanitize_message(m):
  replace control and invisible characters with visible markers (the one sanitizer)
  drop every line that (case-insensitive):
     starts, after spaces, with '#'
     contains '#' followed by a letter or a digit     # #9, owner/repo#9, group/sub#9, PROJ-1 #done
     contains '://'                                   # URL references
     contains GH- followed by a digit
     contains '[' ... ']' holding the word skip or ci, or contains NO_CI     # CI-skip directives
  drop lines matching ^\s*[A-Za-z][A-Za-z0-9-]*\s*:\s inside the last paragraph (repeat until the last paragraph has none)
  strip leading fixup!, squash! and amend! from the subject until none is left
  subject: first line, cut at 72 characters; empty -> "Address review comments"
  body: at most 4 KiB
  append a blank line and the trailer paragraph: git.trailer with <me.name> filled
  record removed_line_count; show prints the cleaned message, the removed lines and the count
```

`git.trailer` is a non-empty string (default `Assisted-by: review-respond (confirmed by <me.name>)`), so the last paragraph is always review-respond's. File modes are never copied from the round: an existing file keeps its mode and a new file is 0644 (wtctl's extraction rule).

### Push

The URL is `fetch_url` from `wtctl locate --json`, checked to be unchanged by `insteadOf` rewriting (`ls-remote --get-url`, a local command). The refspec is `HEAD:refs/heads/<head ref>`, always the full ref name. The push is `push --porcelain --no-verify`, never forced and never to a remote name. Credentials per host come from `hosts.<host>.token_env` or `token_command`, and go only as one URL-scoped `http.<scheme>://<host>/.extraHeader` through `GIT_CONFIG_COUNT`, `GIT_CONFIG_KEY_n` and `GIT_CONFIG_VALUE_n` in the hardened environment (never in argv); an ssh URL uses `SSH_AUTH_SOCK` with `BatchMode`.

```text
remote_head(url, ref):
  out = ls-remote <url> refs/heads/<ref>
  the sha of the one line whose refname column is exactly refs/heads/<ref>; no such line: absent   # other lines are ignored
landed(record, remote):
  remote in record.commit_shas, or
  forgectl compare <pr> --no-diff --from <s> --to <remote> is ahead or identical for some s in commit_shas
push(record):            # step 6 made a new commit in the clean checkout
  CAS pending -> pushing (append commit sha to commit_shas, push_started_at)
  remote = remote_head(url, head_ref)
  if remote is absent or remote != head_sha: -> pending, exit 20 remote_moved (details.ref_missing when absent)
  out = push --porcelain --no-verify <url> HEAD:refs/heads/<head_ref>
  ok: -> pushed (commit_sha = this sha, lineage switch); continue at Replies
  else: remote = remote_head again
     landed(record, remote): -> pushed (commit_sha = remote's match)
     remote == head_sha: -> pending, exit 12 push_failed (host reason), post-attention push_failed
     otherwise: -> pending, exit 20 remote_moved
  a timeout or a kill leaves the record pushing; the command exits 12 outcome_unknown
resume_pushing(record):
  remote = remote_head(url, head_ref)
  landed(record, remote): -> pushed, continue at Replies
  remote == head_sha:     # nothing landed yet
     the clean checkout exists and its HEAD (rev-parse) is the newest of commit_shas:
        first the live reads of Confirm order step 2 (closed or merged: pr_closed; a fork or the default branch: ineligible; another head ref: remote_moved):
        a refusal -> pending (the next check closes a closed PR), a failed read: exit 12, record stays pushing
        then push it (as push, without the CAS)
     else: -> pending, exit 20 remote_moved (details.cause: retry)   # the next confirm runs the full order
  remote is absent: -> pending, exit 20 remote_moved (details.ref_missing)
  else: -> pending, exit 20 remote_moved
```

`resume_pushing` never rebuilds a commit: it recognizes any commit sha this record ever tried, so a late-landing first push is recognized, and when nothing landed and the clean checkout is gone the record returns to `pending` and the next `confirm` makes a new commit from a new clean checkout. Every network git call (`ls-remote`, `push`) has a wall-clock limit of 120 seconds (a constant, not a config key); a timed-out `ls-remote` exits 12 `push_failed` with the record unchanged.

### Replies

Each reply is posted with `pr-comment reply <pr> --to <target> --body-file F --dedupe-key respond:<pending id>:<first 20 hex of sha256(comment id)> --yes --actor <actor> [--decision-id D]...`; the footer and the marker come from pr-comment. The key hashes the comment id because comment ids can hold characters or lengths the forge key grammar (`^[A-Za-z0-9._:-]{1,128}$`) refuses. No `--expected-head` is passed, because the push moves the head. `confirm` reads `forgectl comments` once per call, only when at least one reply has status `pending`, `failed` or `unknown`, and applies the skip rule, in this order (a resolved thread first, then absence, then the hash):

```text
read = forgectl comments <pr>            # once per confirm call; failed: exit 12 lower_failed, statuses unchanged
for each reply r in record order with status pending (fresh confirm), or pending, failed or unknown (resume):
  t = the thread r.thread_id in read; c = the comment r.comment_id in read
  if t is present and t.resolved:          skipped (thread_resolved)       # general comments have resolved false
  elif c is absent and read.complete:      skipped (comment_deleted)
  elif c is absent:                        leave pending                  # the read was incomplete
  elif sha256(c.body) != delivered_sha256: skipped (comment_edited)       # the comment re-opens after the record ends
  else: pr-comment reply <pr> --to <c.id> --body-file F --dedupe-key <key> --yes --actor <actor> [--decision-id D]...; map the exit (table)
after the loop: any unknown -> exit 12 outcome_unknown; else any pending -> exit 12 lower_failed (incomplete read);
  else any failed -> exit 12 reply_failed, post-attention reply_failed once; the record stays pushed
```

`threaded` from pr-comment's result is stored per reply (a reply to a general comment is a new general comment, `threaded: false`). Reply status is one of `pending`, `posted`, `skipped`, `dropped`, `failed`, `unknown`, with `status_reason` one of `comment_deleted`, `thread_resolved`, `comment_edited`, `vetoed`, `escalated`, `host_error` or null. pr-comment's exit maps to the status:

| pr-comment exit | Status | Effect |
|---|---|---|
| 0 (also `already_posted: true`) | `posted` | |
| 10 `not_found`, `details.what: to` | `skipped` (`comment_deleted`) | |
| 11 | `failed` (`vetoed`) | |
| 13 | `failed` (`escalated`) | |
| 12 with `retry: same_command` (`outcome_unknown`, network, rate limit) | `unknown` | stop posting; the command exits 12 `outcome_unknown`; a later `confirm` retries with the same key |
| 12 other, 20, 2 | `failed` (`host_error`) | continue with the next reply |

A later `confirm` retries `failed` and `unknown` replies with the same key; a reply that keeps failing (for example pr-comment's `retry: new_key`, which the same key cannot complete) is ended with `drop-reply`. With `resolve_after_confirm: true`, after every reply is `posted`, `skipped` or `dropped`, `confirm` runs one `pr-comment resolve <pr> --thread <id>... --yes --actor <actor>` for the resolvable threads that got a posted reply. Any failure is a warning (`resolve_failed`) and never changes the record or the exit code; a resume that reaches Finish again runs it again (resolving a resolved thread is a no-op). A general comment has no resolvable thread and is skipped. A comment edited after delivery re-opens after the record ends (Handled set).

### Crash recovery

Re-running `confirm` resumes per the Record tables; a push is never repeated while an earlier attempt may have landed (Push).

## Reject, expiry and closed PRs

`reject`, expiry and a closed or merged PR end a `pending` record through one function (`end_record`, Record), whether `check` or `confirm` finds the cause; queued and running rounds of a closed PR get the `stop` order with `cancel_reason: pr_closed`, and `post-attention` `pr_closed` fires once per ended record or round. Without `--redo` the comments stay handled. Expiry (`pending_ttl`) is `reject` without `--redo` with `post-attention` reason `pending_expired`. Records in `pushing` and `pushed` never expire and are never closed: `confirm` resumes them (a missing ref returns a `pushing` record to `pending`, which the next `check` closes), and `drop-reply` ends the open replies of a `pushed` record that cannot post; `post-attention` `confirm_incomplete` fires once when such a record is older than `pending_ttl`. Throwaways and forks: Cleanup (`teardown_refused` is a `post-attention` reason when agent-spawn keeps refusing; the next `check` retries).

## Off-limits paths

The list (`off_limits`) uses the format, the default list and the path-matching rules of review-watch's interface (section Path matching): one segment matcher with case folding, `**` for zero or more whole segments, git-quoted paths failing closed. Touched paths are every path of the clean checkout's `--name-status --no-renames` listing (deletions and both sides of a rename included), never the artifact lists. A touched path makes `confirm` refuse with `sensitive_paths` unless a human passes `--allow-sensitive` after the diff was shown; binary paths and the other flags need the same allow (Stored diff).

## Hooks

Hooks are configured in `hooks.<name>` lists as the design guide's section 8 describes. A payload goes to stdin and holds `tool`, `hook`, `object_type`, `object_id`, `repo` and `labels` (the object's labels, at most 32: for a round, its fork's labels; for a PR or a pending record, `pr=<pr>` and `repo=<id>` plus `round=` and `pending=` where they apply), plus the fields below, and never any comment, reply, diff or message text. Environment: `REVIEW_RESPOND_HOOK`, `REVIEW_RESPOND_ID`, `REVIEW_RESPOND_OBJECT_TYPE`. Schemas: `schemas/hooks/`.

| Hook | Object | Fires | Can veto | Payload fields |
|---|---|---|---|---|
| `pre-round` | `pr` (`object_id` the PR) | once per round start, after the due rule, before the round is created | veto (no round; the PR waits, `hook_vetoed`, until a new or edited eligible comment or a human `respond`) or escalate (no round; the PR waits, `hook_escalated`; a human reruns with `--override-escalation`) | `pr`, `round_number`, `head_sha`, `comment_count`, `trigger` (`check`, `respond`), `forced`, `reads_third_party` |
| `pre-push` | `pending` (the pending id) | in `confirm`, after the live reads and before the commit and push, for every record | veto or escalate (a human reruns with `--override-escalation`) | `pending_id`, `pr`, `head_sha`, `diff_sha256`, `files_changed`, `additions`, `deletions`, `sensitive` (a boolean: `flags` is not empty), `actor_class` (`human` only in 0.x), `reads_third_party`, `tainted` |
| `post-pending` | `pending` | when a pending confirmation was recorded | no | `pending_id`, `action` (`push_and_reply`, `reply`), `event` (always `recorded`), `files_changed`, `files_deleted`, `additions`, `deletions`, `reply_count`, `summary` (a fixed phrase with counts), `expires`, `dedupe_key`, `reads_third_party`, `tainted` |
| `post-attention` | per the details table | once per cause, with a closed `reason` | no | `reason`, `object_type` (`round`, `pending`, `pr`), `details` (below), `dedupe_key`, `reads_third_party`, `tainted` |
| `post-confirm` | `pending` | after a confirm reached `done` | no | `pending_id`, `pr`, `commit_sha` (or null), `pushed`, `replies_posted`, `replies_skipped`, `dedupe_key`, `reads_third_party`, `tainted` (always true) |

`post-attention` reasons: `escalated`, `failed`, `round_limit`, `pending_expired`, `confirm_incomplete`, `remote_moved`, `push_failed`, `reply_failed`, `job_lost`, `ineligible_comment`, `pr_closed`, `teardown_refused`, `no_agent`. `job_lost` fires (instead of `failed`) when a round fails with `job_lost`; `pr_closed` fires once per record or round the closed-PR sweep ends. `remote_moved` fires when the observed head differs from the record's `moved_head`, which it then stores. The `details` keys per reason are a closed set:

| Reason | Object | `details` keys |
|---|---|---|
| `escalated` | `round` (cause `agent`) or `pr` (cause `hook`) | `round_id` (only for `cause: agent`), `cause` (`agent`, `hook`) |
| `failed`, `job_lost` | `round` | `round_id`, `failure_reason` (`failed` only) |
| `round_limit`, `ineligible_comment`, `no_agent` | `pr` | `rounds` (`round_limit`), `comment_count` (`ineligible_comment`), `pr` (`no_agent`) |
| `pending_expired`, `confirm_incomplete` | `pending` | `pending_id` |
| `remote_moved` | `pending` | `pending_id`, `head_sha` |
| `push_failed`, `reply_failed` | `pending` | `pending_id`, `comment_count` (failed replies) |
| `pr_closed` | `pending` or `round` | `pending_id` or `round_id` |
| `teardown_refused` | `pr` | `agent_id` |

The post hooks are delivered at least once through an outbox, in order per object, with an `event_id` (`ev-` plus 16 characters of `[a-z2-7]`) and a `dedupe_key` in notice-route's plain grammar: `rp-<id>.recorded`, `rp-<id>.done`, or `<object id or PR digest>.<reason>.<episode>`, where `<episode>` is the number of earlier `post-attention` events with the same object and reason (0 for the first), stored with the outbox row in the transaction that queues it. Each `post-attention` event is queued in the transaction that creates its cause (a block set, a record ended, a new head seen), so a cause fires once and a redelivery reuses its key. They cannot veto. `pre-` hook outcomes follow the design guide (exit 0 continues, 13 escalates, other non-zero vetoes and the command exits 11). A `decision_id` on a hook's stdout is recorded in the round or pending record and in audit. Deviation from the design guide's `post-pending` wording: the record's `summary` is a fixed phrase and never agent text, so a notification can use it.

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
| `jobs.queue`, `.kill_grace`, `.max_running`, `.pass_env` | `respond`, `125s`, `1`, `[]` | job placement; `max_running` bounds queued plus running rounds in both placements; `pass_env` names the variables passed to the job (with jobsctl their values come from jobsctl's `env_file`) |
| `git.user_name`, `git.user_email`, `git.trailer` | from your git config, from your git config, the default trailer | commit identity; the trailer is a non-empty string |
| `limits.requests_per_host`, `.prompt_bytes`, `.reply_bytes`, `.diff_bytes` | `120`, `65536`, `8192`, `262144` | bounds; a stored diff over `diff_bytes` is flagged `oversized` |
| `attention.ineligible_comment` | `false` | fire a hook for ineligible comments |
| `retention.records` | `30d` | prune age |
| `cron.env_file` | `null` | file sourced before `check` in the cron line |
| `hooks.<name>` | none | hook command lists |
| `repos.<id>.*` | none | per-repo overrides, exactly these keys: `respond_fresh`, `respond.allow_commands`, `respond.read_dirs`, `settle`, `max_rounds`, `reviewers` |

Every duration (config keys, `--older-than`, `--since`) is an integer and a unit: `ms`, `s`, `m`, `h` or `d`. Invalid config fails fast with the key's name. Every command except `validate` loads the config first and exits 2 `config_invalid` (the message names the file or `REVIEW_RESPOND_*` variable and the key, never the value) when it is invalid.

Loading: with `--config PATH` that file replaces `config.yaml` (it must exist) and the drop-ins are read from the `config.d` beside it. Each file and drop-in is at most 256 KiB (1 MiB together, 256 drop-ins) and must be a regular file owned by you and not writable by group or others, because hooks run from this config; YAML anchors, aliases, tags, duplicate keys, non-string keys and the merge key are refused, and `yes`, `no`, `on` and `off` are strings. A key may be `null` only where `schemas/config.json` allows it. A `repos.<id>` key left out inherits the global value. Narrower than the schema: durations are at most `3650d`, `pending_ttl`, `agent.timeout`, `jobs.kill_grace` and hook timeouts are not `0`, a hook timeout is at most 600 seconds, `async` is for post hooks only, `jobs.pass_env` cannot name `REVIEW_RESPOND_*`, and free text has no control characters (a hook `cmd` may hold a newline or tab). `sandbox: preferred` or `off` prints a warning. A `REVIEW_RESPOND_*` variable that is not in the list above is ignored with a warning.

## Environment variables

`REVIEW_RESPOND_<KEY>` sets only these keys (nested keys joined with `__`): `settle`, `max_rounds`, `access_cache_ttl`, `prefer_owner`, `retention.records` (`REVIEW_RESPOND_RETENTION__RECORDS`) and `limits.requests_per_host`. An override applies to the command that reads it; jobs never see `REVIEW_RESPOND_*` (Job). Every other key is config-file only, including every safety key (`off_limits`, `sandbox`, `permission_profile`, `setting_sources`, `env_allowlist`, `skill`, `hosts.*`, `git.*`, `jobs.*`, `hooks`, `reviewers`, `bot_authors`, `respond_to_own_comments_prefix`, `resolve_after_confirm`, `repos`). Credentials are named in config and read from the environment. Hook variables are listed under Hooks.

## Labels

review-respond puts these labels on what it creates: on forks, `pr=<host/path#N>`, `owner=review-respond`, `repo=<id>` and `round=<round id>`; on throwaways, `app=review-respond`, `role=round` or `role=clean`, `round=<round id>` (the round throwaway) and `pending=<pending id>` (added to the round throwaway with `wtctl label add` when the record is created, given at create time to the clean one); on jobs, the correlation labels `pr`, `round` and `owner`. It gives no meaning to any other label. It reads the `pr` and `owner` labels of agents only to find the original agent and verifies the match.

A label value is at most 256 characters; a PR reference or repo id longer than that is not used as a label (warning `label_skipped`), and such a PR has no label lookup of the original agent (a fresh session if the repo opted in, else block `no_agent`).

## Files and state

State under `~/.local/state/review-respond/` (`XDG_STATE_HOME` respected): `state.db` (private SQLite, WAL, versioned additive migrations), `log.jsonl` (public, hash-only, no comment text), `audit.jsonl` (public), and directories for prompt files, generated profiles, stored artifacts, stored diffs and reply drafts, each deleted when its record ends or by `prune`. The state directory and its subdirectories are created 0700 and every file 0600 (explicit modes, not the umask). Every command refuses to run (exit 12 `state_unsafe`) when the state directory is not owned by you or is accessible to group or others. Stored text files (prompts, profiles, artifact copies, stored diffs and reply drafts) live in a directory named by the id of the one round or pending record that owns them and are named by the sha256 of their content, so no file is shared between records, and a file is deleted only with its owner. The managed crontab block written by `cron install` has the first line `# review-respond managed v1 every=5m`, a `PATH=` line, then `XDG_CONFIG_HOME=` and `XDG_STATE_HOME=` lines when those variables are set at install (the same refusal rules as `PATH`, as review-watch's block), then the line `*/5 * * * * . '<env_file>' && '<absolute review-respond>' check --quiet` (the env-file prefix only with `cron.env_file`) and the last line `# review-respond managed end`.

## Concurrency

One lease table has three key kinds: `pass`, `pr:<PR digest>` and `round:<round id>`, one row format and one takeover rule. A lease row holds the holder's PID, process start time, boot id, `acquired_at`, `renewed_at` and `expires_at`. The holder renews it every 60 seconds from a heartbeat while it holds it; it expires 5 minutes after the last renewal. Another process takes a lease over when its holder is verified gone (PID absent, or its start time or the boot id differs), whether or not it expired. An expired lease whose holder is alive is never taken over; it is logged (`lease`, `stalled`). Every state write is a compare-and-set on the row's `rev` in a transaction that also checks the writer still holds its lease; a lost lease aborts the command with exit 1 `lease_lost`. No lease is held across a jobsctl queue wait; the `round:` lease is held by `_job` for its whole life (it is the job's liveness).

| Command | Leases | When contended |
|---|---|---|
| `check`, `prune` | `pass` for the run; `check` also takes `pr:` per PR step without waiting | a second caller exits 0 with `already_running: true` (also `check --pr`); a held `pr:` lease skips that PR (`lease_held` in its result) |
| `respond`, `stop` | `pr:` | wait up to 30 s, then exit 20 `busy` |
| `confirm`, `reject`, `edit`, `drop-reply` | `pr:` of the record's PR, held for the whole command | wait up to 30 s, then exit 20 `busy` |
| `_job` | `round:` for its life, taken without waiting (a live holder: exit 0); `pr:` for each short transaction | `pr:` waits up to 60 s, then exit 1 `lease_timeout` (the round stays as stored, for the finalizer or the queued reconcile) |
| finalizer in `check`, `show`, `confirm` | `round:` only by takeover | a live holder: skip; `show` prints the stored row with `finalize: busy` |
| `list`, `show`, `pending`, `export`, `validate` | none | |

The closed-PR sweep calls the `stop` function inside the `pr:` lease `check` already holds for that PR. Only `check` and `prune` report `already_running`; a contended `confirm` exits 20 `busy`, never 0.

## Audit and log

`audit.jsonl` records every external action: each push and each reply post, as an `attempted` record before the call and an outcome record after it sharing an `attempt_id` (22 URL-safe characters, 128 random bits), with `ts`, `actor`, `decision_id`, `target` (PR), `action`, `inputs` (head sha, diff sha256, commit sha, the pending id, `config_sha256` of the merged config after defaults, and `override_escalation: true` when given) and `result`. The `action` is one of `push`, `reply`, `resolve`, `confirm`, `reject`, `edit`, `drop_reply`, `stop`, `teardown`. Human decisions (`confirm`, `reject`, `edit`, `drop_reply`, `stop`) are single outcome records. If the first append fails nothing is sent. `log.jsonl` holds events (`check_pass`, `pr_state`, `round_state`, `pending_state`, `hook_result`, `finalize`, `host_failed`, `lease`, `warning`) with ids and hashes only. Schemas: `schemas/audit.json`, `schemas/log.json`.

## Hardened git

review-respond runs git itself, only in the clean throwaway that no agent wrote, and always through one module that implements architecture section 13.2 rule 10: an environment built from scratch, the full override list, the injected flags and rejected options, a fixed allowlist of subcommands (`rev-parse`, `config`, `ls-remote`, `add`, `diff`, `commit`, `push`, and `check-ref-format`, which reads no repository), filter-driver and extra-header blanking from `config --list -z`, explicit `--git-dir` and `--work-tree`. Deviations from the recipe's literal text, all found against real git and recorded in `docs/design.md`:

1. `GIT_ALLOW_PROTOCOL` is `none` for every command except `push` and `ls-remote`, which get the scheme of the URL in use (`file` for a plain path); a push over `none` fails.
2. `GIT_SHALLOW_FILE` is the clean throwaway's own shallow file, not `/dev/null` (which breaks a push from a shallow clone).
3. `GIT_LITERAL_PATHSPECS=1` is set in the environment and paths go through `--pathspec-from-file` with `--pathspec-file-nul`, since a leading `--literal-pathspecs` option is refused by the recipe's option rule.
4. `core.sshCommand` is one argv element, `core.sshCommand=ssh -o BatchMode=yes`.
5. review-respond's own injected diff flags `--text --full-index --no-renames`: the module inserts them with the other injected flags, callers may not pass them, and the option-abbreviation rule compares against the table of exact options (so `--text` is not an abbreviation of `--textconv`); a `live` test covers both.

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
- The secret tripwire knows only a closed list of token prefixes and PEM headers; an encoded secret in the diff, the message or a reply is caught only by your reading of it.
- A stored file edited by a same-user process is detected by its hash; the state database itself is not authenticated.

## Platform notes

Linux and WSL are supported. macOS is checked by a manual checklist (the sandbox is Seatbelt; `lsof` for `in_use`). Interactive forks need tmux and are unverified in 0.x.

## TUI

None in 0.x. A `browse` command (`--focus`, `--result-file`) is a 1.x item and follows a feature inventory of forgectl's TUI. Automatic modes (`auto_reply`, `auto_push`), fork PRs and review bodies are 1.x items too.

## Objects

- **PR state** (`list`, `show`): `pr`, `repo`, `state`, `waiting_reason`, `block` (`reason`, `set_at`), `head_sha`, `head_ref`, `base_ref`, `is_fork`, `base_agent`, `rounds`, `unhandled`, `seen`, `ineligible`, `edited`, `pending_id`, `updated`.
- **Round** (`respond`, `show`, `list --json`): `id`, `pr`, `number`, `state`, `head_sha`, `agent_id`, `base_agent_id`, `job_id`, `comment_ids`, `delivered` (comment ids), `counted`, `cancel_requested_at`, `cancel_reason` (`stopped`, `pr_closed`, `nothing_to_respond`, `read_failed`), `note_sha256`, `include_existing`, `escalation` (`{reason, summary}` or null, untrusted), `failure` (`reason`, `detail`), `started`, `ended`, `warnings`.
- **Pending confirmation**: Pending confirmations, Record.
- **Comment record** (`show --json`): `id`, `author_id` (null when the host gave none), `eligible`, `reason`, `state`, `delivered_sha256`, `delivered_from`, `handled_reason`, `first_seen_at`, `edited`, `thread_id`, `path`, `line`, `body` (untrusted, sanitized).

Every `--json` output is described in `schemas/cli/<command>.json`; fields that carry comment, reply, diff or message text are marked `x-untrusted`.

### Values

This subsection states what the schemas use so that they follow the interface.

- **PR reference**: forgectl's `pr` (`host/path#N`); `show` and `respond` also accept an https PR URL.
- **PR digest**: the first 20 hex characters of the sha256 of the PR reference; used in the job's unique key, the `pr:` lease key and hook dedupe keys.
- **Ids**: a round is `rd-` plus 8 characters of `[a-z2-7]`, a pending record `rp-` plus 8, a hook event `ev-` plus 16. A unique prefix of at least 4 characters after the prefix is accepted (several matches exit 21). Agent, job and throwaway ids are the siblings' own; comment and thread ids are forgectl's and opaque. `show` dispatches by prefix: `rp-` is a pending record, `rd-` a round, anything else a PR reference.
- **`pending` rows**: `id`, `tool` (`review-respond`), `action` (`push_and_reply`, `reply`), `target` and `pr` (the PR reference), `created`, `summary`, `expires`, `state`, `diff_sha256`, `content_sha256`, `files_changed`, `reply_count`.
- **Output envelopes**: `list` prints `{prs: [...]}`, `pending` prints `{pending: [...]}`, `respond` prints `{dry_run, round, comment_ids}`, `show` adds `object_type` (`pr`, `round`, `pending`), `check` prints `{already_running, dry_run, hosts: [{host, ok, code, warnings}], prs: [{pr, state, started_round, lease_held, errors}]}` (`errors` is a list of `error.code` values; an optional `error` member holds the envelope's object on exit 12), `stop` prints `{dry_run, round, changed, cancel_requested}` and `prune` prints `{already_running, dry_run, older_than, results: [{object_type, id, result}], warnings}` with `object_type` one of `round`, `pending`, `comments`, `throwaway` and `result` one of `deleted`, `would_delete`, `skipped`.
- **Stored text files** live in a directory named by their owning record's id and are named by the sha256 of their content (Files and state).
- **Config defaults** the schemas rely on: `me.accounts` maps host to id (default none), `agent.timeout` is null (the kind's own timeout applies), `permission_profile` is null (the bundled profile), `model` is null, `skill` is `respond-to-review`, `setting_sources` is empty.
