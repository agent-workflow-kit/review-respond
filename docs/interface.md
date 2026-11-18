# review-respond: interface

Version 0.1.0-design. This document is the single normative source for review-respond's rules. `docs/design.md`, the README and the architecture link to its headings and never restate its tables. Sibling tools are named in the Dependencies section; their own `docs/interface.md` files are normative for them.

## Overview

review-respond answers review comments on pull requests and merge requests that you opened. For each of your open PRs it reads the comments through forgectl, decides which are eligible to start work, and runs one round at a time: it creates a throwaway checkout at the PR head, forks the PR's agent session into it, and gives the fork the eligible comments as fenced data. The fork edits files in the throwaway and reports an artifact (replies, the files it changed or deleted, a proposed commit message). It cannot stage, commit, push or reach the network.

The result is a pending confirmation. `confirm` shows you the diff, then copies only the listed files into a second, clean throwaway, commits there with your identity through hardened git, pushes fast-forward only to the PR head ref, and posts the replies through pr-comment. Opt-in automatic modes do the same behind an enforced gatectl policy and built-in hard rules.

review-respond works standalone: forgectl, agent-spawn, pr-comment, wtctl and git are required; jobsctl and gatectl are optional.

## Dependencies

| Tool | Binding | Used for | Minimum |
|---|---|---|---|
| forgectl | A | `prs`, `show`, `snapshot`, `comments`, `access`, `whoami`, `ci` | pinned in `docs/design.md` |
| agent-spawn | A | `list`, `show`, `resume --fork`, `launch`, `teardown`, `stop`, `wait` | pinned in `docs/design.md` |
| pr-comment | A | `reply`, `resolve` | pinned in `docs/design.md` |
| wtctl | A | `create --throwaway`, `extract`, `locate`, `remove`, `list`, `prune` | pinned in `docs/design.md` |
| git | A | hardened commit, diff and push (see Hardened git) | 2.34 |
| jobsctl | A, optional | the `respond` queue | pinned in `docs/design.md` |
| gatectl | A, optional | `check` for automatic modes, `outcome` | pinned in `docs/design.md` |

review-respond does not use pr-watch. Any other program (the harness, cron) may run `review-respond check` as an early trigger; `check` is idempotent.

`validate` checks each dependency's version against the pinned minimum and a missing or too-old dependency fails with a message naming its install command. A command that needs a dependency exits 12 with `error.code: tool_missing` or `tool_too_old` before doing anything.

## Global flags

| Flag | Meaning |
|---|---|
| `--json` | machine output; every command that prints data supports it (schemas in `schemas/cli/`) |
| `--config PATH` | config file (see Config) |
| `--dry-run` | on `check`, `respond`, `confirm`, `reject`, `stop`, `prune`: show what would happen and change nothing |
| `--yes` | skip a confirmation; accepted only with a human actor (a TTY, or `--actor human:<user>`); an automatic actor passing it exits 2 |
| `--actor A` | `human:<user>` or `auto:<name>`; the default is `human:<user>` on a TTY and `auto:unknown` otherwise |
| `--decision-id ID` | repeatable; recorded in audit |
| `--result-file PATH` | write the `--json` output there atomically, whatever the exit code |
| `--verbose`, `--quiet`, `--version` | as in the design guide |

Global flags go after the subcommand. Free text (a note, a reply, a commit message) is always taken from a file or stdin (`--note-file`, `--body-file`, `--message-file`, `-`), never from argv.

## Exit codes

| Code | Meaning here |
|---|---|
| 0 | success (including "already running" and a no-op) |
| 1 | internal error |
| 2 | usage error |
| 10 | not found (PR, round or pending id) |
| 11 | vetoed by a `pre-round` or `pre-push` hook |
| 12 | failed for a reportable reason (a dependency failed, a push failed, the outcome of a step is unknown) |
| 13 | needs a human (a pending confirmation was recorded, a round escalated, a hook escalated) |
| 20 | refused; `reason` says why |
| 21 | ambiguous selector (several PRs, rounds or pending records match) |
| 22 | `nothing_to_respond`: `respond <pr>` found no eligible comment to answer |

### Refusal reasons (exit 20)

`live` (an agent working on the PR is live), `pending_exists` (the PR has a pending confirmation), `round_limit`, `remote_moved`, `diff_changed`, `stale_head`, `sensitive_paths`, `expired`, `not_pending`, `ineligible` (the PR is not yours, is closed, is a draft where drafts are skipped, or is a fork PR for a fresh session), `pr_closed`, `held` (wtctl refused to remove a record another holder uses), `repo_config_changed`. A refusal carries `reason` and `details` in the error envelope. `--yes` never overrides a refusal. A human `--force` overrides only `round_limit` and `live` never.

### Error envelope

With `--json`, errors print `{"error": {"code", "message", "reason"?, "details"?}}` on stdout and the message on stderr. `lower_error` carries a sibling's error object when a sibling failed.

## Commands

### `check`

The one periodic pass. In order, under the instance lock (a second caller exits 0 with `already_running`):

1. Resolve your account id per host (`forgectl whoami`, cached).
2. List your open PRs per configured host (Detection). Update the stored PR state.
3. For each PR read the comments, update the comment records and compute the eligible unhandled comments (Eligibility, Handled set).
4. Start a round for each PR that is due (Settle and round start), up to the job cap; never for a PR that is waiting.
5. Finalize finished, dead or lost jobs (Round states), resolve held jobs, expire pending confirmations, cancel pending confirmations of closed PRs and tear down finished forks.
6. Deliver queued hook events.

`check --pr <pr>` limits the pass to one PR. `check` never waits for a round. Exit 0, or 12 when no host could be read, with a per-PR result in `--json` (`prs[]` with `state`, `started_round`, `errors`).

### `respond <pr> [--force] [--include-existing] [--note-file F] [--skill S] [--mode headless|interactive]`

Starts a round for one PR now, ignoring the settle delay but not the other rules. `--force` ignores the round limit (a human actor only). `--include-existing` also answers eligible comments that existed before review-respond first saw the PR. `--note-file` adds your note to the prompt as trusted text in its own block. Refusals: `live`, `pending_exists`, `round_limit`, `ineligible`. Exit 22 when no eligible comment exists, 13 when a hook escalates, 0 with the round object once the job is submitted or started.

### `list [--state S] [--repo ID] [--waiting]`

PRs and their state (Objects: PR state): `idle`, `settling`, `queued`, `running`, `pending`, `escalated`, `limit` (round limit reached), `closed`. Each row carries the counts of unhandled eligible, ineligible and edited comments, the round count, the base agent and the pending id when one exists.

### `show <pr | round id | pending id>`

One object in full. A pending confirmation shows the stored diff, the commit message and each reply, sanitized and marked agent-written, the off-limits paths touched and the gate decision when there is one. Exit 10 when nothing matches, 21 when a PR reference is ambiguous.

### `pending`

Lists pending confirmations: `id`, `tool`, `action`, `target`, `created`, `summary`, `expires` and the state.

### `confirm <id>` (alias `approve`) `[--allow-sensitive] [--expect-diff SHA]`

Performs the pending confirmation (Confirm order). Without a TTY and without `--yes --actor human:<user>` it exits 13 and does nothing. `--expect-diff` binds the call to the diff hash that `show` printed; a different hash refuses with `diff_changed`. `--allow-sensitive` is human only and allows a diff that touches off-limits paths after you saw it.

### `reject <id> [--redo]`

Drops the pending confirmation and removes the round throwaway. The comments stay handled. With `--redo` the round's comments are un-handled so that the next `check` starts a fresh round (it counts toward the round limit). Idempotent.

### `edit <id> [--reply COMMENT_ID --body-file F] [--message-file F]`

Replaces one reply or the commit message. The diff cannot be edited. An edited record is marked `edited`.

### `drop-reply <id> <comment_id>`

Removes one reply from the pending confirmation.

### `stop <round id>`

Ends a queued or running round: cancels its job, then stops the agent through agent-spawn, parks nothing and records the round `canceled`. A finished round exits 0 as a no-op. The round's comments stay unhandled.

### `cron install|uninstall [--dry-run]`

Writes or removes one managed crontab block (Files). Entries have an explicit `PATH` and an optional env-file prefix.

### `export [--since T]`

Prints `log.jsonl` and `audit.jsonl` records as JSON lines.

### `prune [--older-than D] [--dry-run]`

Deletes finished round and pending records, comment records of closed PRs and their stored files older than the retention age (default 30 days). Never touches a live round or a pending confirmation; removes leaked throwaways that carry the label `app=review-respond` and belong to no live record (found with `wtctl list --label`).

### `validate [--json]`

Offline checks: config parse, dependency versions and installation, the sandbox probe through `claude-session kinds --json`, the cron PATH, and the credential variables named in config (presence only). Never prompts and never sends anything. Exit 0, 12 on a failed check.

## Detection and eligibility

### Detection

`check` lists your open PRs per host with `forgectl prs --author me --state open --updated-since <cursor>` (the cursor is the stored timestamp of the previous successful listing, with an overlap window of 10 minutes), plus every PR that has a live record, a pending confirmation or unhandled eligible comments. For each such PR it reads the comments with `forgectl comments` and, when a decision needs facts, `forgectl show` (head ref and sha, head repo, fork flag, base, state, draft). A per-host request budget (`limits.requests_per_host`, default 120 per pass) bounds a pass; PRs not reached wait for the next pass. A failed listing for one host does not stop the other hosts.

A forgectl read that reports `incomplete` never marks anything as deleted. `not_found` for a PR means unknown, never closed: a PR counts as closed only when forgectl reports `state` `closed` or `merged`.

### Eligibility

A comment is eligible when all of these hold:

1. Its author id is not your own account id on that host. Opt-in `respond_to_own_comments_prefix` (default off; for example `/awkit`) admits your own comments that start with the prefix. A comment that carries pr-comment's marker on content from your own account is never eligible (pr-comment's Marker detection rule).
2. `author.is_bot` is exactly `false` (`null` counts as a bot), and the author is not in `bot_authors`.
3. The author id is in `reviewers` (entries `<host>:<id>`), or `forgectl access <repo> <login>` reports `write_access: true`. A `null` or `false` answer is not eligible. Answers are cached per (repo, login) for `access_cache_ttl` (default 1 hour).
4. The PR is yours, open and not from a repository the tool cannot reach. A comment on a closed or merged PR is never eligible. A fresh session needs an opted-in, non-fork PR (Fork lineage).
5. The thread is not resolved and the comment is not deleted.

An ineligible comment is recorded with its reason (`own`, `bot`, `no_access`, `unknown_access`, `resolved`, `closed`) and never reaches a prompt. `show` lists them. With `post-attention` reason `ineligible_comment` enabled (`attention.ineligible_comment`, default off) the first ineligible comment of a PR fires one hook event.

### Handled set

review-respond stores, per comment id (the full forgectl id, for example `gh:rc:<node id>`), the body sha256, the last `updated_at`, the state (`seen`, `delivered`, `handled`) and the round. Ids of different kinds never collide. A comment is **handled** when a round produced a valid artifact and the pending confirmation was recorded in one transaction, or when its thread was resolved or it was deleted.

- A new id on a known PR is unhandled.
- A handled comment whose body hash changed is unhandled again and marked `edited: true` in the next prompt, only while its thread is unresolved, the author is still eligible and the round limit allows. A changed `updated_at` with an equal hash does nothing.
- A comment missing from a complete read is `deleted`: it drops out of prompts and is never retried. A pending reply to it is skipped at `confirm` with a reason.
- A thread resolved by someone else removes its comments from prompts; a pending reply to it is skipped at `confirm` with a reason. review-respond never un-resolves a thread.
- First sight of a PR records every existing comment as `seen`, not answered. `respond <pr> --include-existing` runs a round on them.

### Settle and round start

A PR is due when it has eligible unhandled comments, no live agent (the original, the base fork or a pending fork), no queued or running round, no pending confirmation, fewer than `max_rounds` counted rounds, and its newest eligible unhandled comment is at least `settle` old (default 5 minutes). A round's prompt holds every eligible unhandled comment at the moment its job starts. A PR with a pending confirmation is `pending` and waits. At the limit the PR becomes `limit`, `post-attention` reason `round_limit` fires once, and only `respond --force` continues. A round counts toward the limit once its session was launched.

## Rounds

### Round states

`queued`, `running`, `pending`, `escalated`, `failed`, `canceled`, `expired`, `closed`. Every transition is a compare-and-set on the round's `rev`. A lease (`holder`, `lease_expires_at`) is renewed from a heartbeat every 60 seconds and expires after 5 minutes; a takeover of an expired lease is allowed only after the previous holder's process is verified gone by PID and start time.

- `queued` to `running`: the job started (or the internal start under the cap).
- `running` to `pending`: the session ended `done`, the artifact validated and the pending record was written in one transaction together with marking the comments handled.
- `running` to `escalated`: the session reported needs-input or blocked (agent-spawn outcome exit 13).
- `running` to `failed`: any other end, with `failure.reason`: `artifact_missing`, `artifact_invalid`, `skill_not_loaded`, `implicit_done`, `empty_result`, `agent_failed` (outcomes 14, 15, 17, 18), `timeout` (16), `launch_failed` (12), `refused` (20), `job_lost`, `diff_failed`.
- `running` or `queued` to `canceled`: `stop`, or the PR closed.
- `pending` to `expired` or `closed`: Reject, expiry and closed PRs.

`check`, `show` and `confirm` finalize a round whose job died: they read the recorded agent id with `agent-spawn show --json`, and the frozen artifact when the run is final, and either record the pending confirmation or mark the round `failed` with `job_lost`. A failed round is retried once automatically by the next `check` in a fresh throwaway (it counts toward the limit); after that `post-attention` reason `failed` fires and only a human `respond` continues. An `escalated` round is never retried: `post-attention` reason `escalated` fires, its comments stay unhandled, and `respond <pr>` (optionally with `--note-file`) starts a new round.

### Fork lineage

review-respond keeps a **base agent** per PR. At first it is the original agent, found with `agent-spawn list --label pr=<pr> --json`: its own earlier fork first (`owner=review-respond`), then the agent whose `owner` label equals `prefer_owner` (default `ticket-work`), else the most recent. A label is not authenticated, so review-respond verifies the match against its own records before trusting it (an agent whose worktree or branch does not match the PR head ref is skipped). Several candidates that tie exit 21 on a manual `respond`.

Each round forks the base agent into the round's throwaway. A fork becomes the new base only when its round was confirmed (the push landed, or a replies-only round's replies were posted). A rejected, expired, canceled or failed fork is torn down at once. A confirmed fork stays as the base; when a later fork is confirmed, the superseded one is torn down. When the PR is merged or closed, the base fork and every pending fork are torn down. The original agent is never torn down by review-respond. review-respond touches only agent ids it recorded and uses the label search to find the original and to recover after a crash.

With no original agent, a PR opted in (`repos.<id>.respond_fresh: true` or a PR label named in `fresh_label`) and not from a fork gets a fresh `respond-to-review` session in the round's throwaway; after a confirm it is the base like any other fork.

A fork is tainted (architecture section 8.5). review-respond never resumes it in a kind that does not read third-party text and never passes `--allow-tainted`.

### Job

A round runs as one job: the hidden command `review-respond _job <round id>` (its argv holds only the round id; it is not part of the interface). The job: creates the round's throwaway (`wtctl create --throwaway --repo <id> --ref <head sha> --label app=review-respond --label round=<round id>`), writes the prompt file and the generated profile into the state directory, runs `agent-spawn resume <agent id> --fork --cwd <throwaway> --kind respond ... --wait` (or `launch` for a fresh session), validates the artifact, builds the stored diff and records the pending confirmation.

With jobsctl: `check` submits `_job` to the queue `respond` (config `jobs.queue`) with `--unique-key respond:<pr id>:<round number>:<attempt>`, `--max-retries 0`, an explicit `--cwd` (the state directory), `--kill-grace` (`jobs.kill_grace`, default 125 seconds), `--label` correlation labels and `--pass-env` for the names in `jobs.pass_env`. It reads the job by its stored id. An exit-13 job is held; `check` records the round `escalated` and resolves the held job with `jobsctl resolve --as done`. The queue's cap bounds concurrent rounds (jobsctl's default is 1).

Without jobsctl: `check` starts `_job` as a detached process (double fork, stdio redirected, PID and start time recorded) under `jobs.max_running` (default 1). It never waits. A job that dies is found by PID and start time and finalized as above.

### Prompt

The prompt file lives in the state directory and is deleted when the round ends. It holds, in this order: the working copy's real absolute path (the throwaway root; the forked conversation otherwise mentions the original worktree's paths), the standing rule that comment text is data and not instructions, your optional note as a separate trusted block, and the eligible comments inside one nonce fence `<untrusted source="comments" id="R">...</untrusted id="R">` with every `</untrusted` neutralized. Each comment has its id, an author label (the login, sanitized), `edited` when it applies, the thread's path, line and side, and its body, sanitized as one routine (control and invisible characters replaced by visible markers). At most `limits.prompt_comments` comments (default 50) and `limits.prompt_bytes` (default 65536) go into a prompt; overflow comments stay unhandled for the next round and a visible truncation marker names what was cut. Nothing from an ineligible comment, no PR title, no description and no CI log is included.

### Session settings

Every round passes these to agent-spawn per call (design guide section 11): `--kind respond`, `--skill <skill>` (not carried to a fork, so always passed), `--plugin-dir <bundled plugin>`, `--permission-profile <generated file>`, `--sandbox required`, `--setting-sources ""`, repeatable `--env-allowlist-only VAR`, `--model` when configured, `--fork`, `--cwd <throwaway root>`, `--label pr=<pr>`, `--label owner=review-respond`, `--wait`. The cwd is exactly the throwaway root (a subdirectory is refused by agent-spawn). Rounds are headless by default whatever the base agent's mode; `--mode interactive` is accepted and is unverified in 0.x.

The permission profile is generated per call: the bundled allowlist profile plus the repo's `respond.allow_commands` (argv patterns for test and build commands) and `respond.read_dirs`. The bundled profile keeps the architecture's respond-profile denies and additionally denies `memoctl add`, `memoctl delete`, `memoctl cron`, `agent-spawn`, `notice-route send`, `git push` and `gh` and `glab`, and any write under review-respond's own config and state directories. Config keys replacing these: `skill`, `permission_profile`, `sandbox` (`required` by default; `preferred` and `off` are accepted with a warning), `setting_sources`, `env_allowlist`, `model`, `agent.timeout`.

### Artifact

The agent files one JSON artifact with the Write tool in `$CSESSION_DIR/artifacts/` and reports `done` with `--artifact`. Exact shape (`schemas/artifact.json`): `skill_marker` (the string in the plugin's reference file), `replies` (list of `{comment_id, body}`), `changed_files` and `deleted_files` (lists of relative paths) and `commit_message`. Unknown keys are refused.

review-respond reads the frozen artifact from `agent-spawn show --json` (`.runs[-1].outcome.report.artifact.path`), only once the run is final, and validates strictly: the schema; the marker equals the plugin's; each reply's `comment_id` is one of the round's delivered comments, once; each reply body is at most `limits.reply_bytes` (default 8192); each path is relative, normalized, has no `.git` component compared case-folded, and the lists have at most 1000 entries without duplicates and without overlap; `commit_message` is at most 4 KiB; `report.implicit` is false; the run's tool list shows no unexpected tool (a difference is a warning on the round, and a hard failure for an automatic mode). A `done` report with no artifact, a malformed artifact and a missing skill (the marker is absent) are each a `failed` round. An artifact with no replies and no changed or deleted files is `failed` with `empty_result`. A copy of the validated artifact is stored in the state directory.

The artifact's file lists are never trusted for what they say about the working copy: the stored diff comes from extraction in a clean checkout, not from the round throwaway.

## Pending confirmations

### Record

A pending confirmation has the id `rp-` plus 8 base32 characters and holds: `pr`, `repo`, `round`, `head_sha` and `head_ref` at round time, `base_ref`, the fork agent id, the round throwaway id, `changed_files`, `deleted_files`, `commit_message`, the replies (`comment_id`, `thread_id`, `body`, `dedupe_key`, `status`), `diff_sha256`, `diff_stats` (files, additions, deletions), `sensitive_paths` (the off-limits paths touched), `gate` (the decision object when one was made), `edited`, `state`, `created` and `expires` (default 7 days, `pending_ttl`). Free text lives in stored files and appears in `show --json` only inside fields marked untrusted in the schema.

States: `pending`, `pushing`, `pushed`, `posting`, `done`, `rejected`, `expired`, `closed`. `pushing`, `pushed` and `posting` are the intent states of Confirm order.

### Stored diff

When the job ends, review-respond creates a clean throwaway, extracts the listed files into it, computes the diff with hardened git (`add` of the literal paths, `diff --cached` with the injected flags), stores it with its sha256 and removes the clean throwaway again. `pending` and `show` print the stored diff. If extraction or the diff fails, the round is `failed` with `diff_failed` and nothing is recorded. The round throwaway stays, untouched by any trusted process, until `confirm`, `reject`, expiry or the PR ending. It carries the labels `app=review-respond`, `round=<id>` and `pending=<id>`, and wtctl's `prune.protect_labels` must list `app` (`validate` checks that wtctl's `throwaway.max_age` cannot remove it; see Residual risks).

## Confirm order

`confirm <id>` runs these steps. Each step that acts first writes its intent to the record, and each is idempotent.

1. **Preconditions.** The record is `pending` (else `not_pending`); it is not expired; the PR is open (else `pr_closed`); an actor is human, or an enforced allowing gate applies (Automatic modes); `--expect-diff`, when given, equals `diff_sha256`. Re-run the `pre-push` hooks (an escalating hook that already escalated this record is skipped in this call).
2. **Current head.** Read the PR with `forgectl show` (and `ci` for the automatic mode). If the head sha differs from the recorded one: exit 20, `remote_moved`, the record stays `pending`.
3. **Clean checkout.** `wtctl create --throwaway --source remote --repo <id> --ref <head sha> --label app=review-respond --label pending=<id>`; the `--depth` is wtctl's default, so the push stays a fast-forward from shallow history. Extract the listed files and deletions with `wtctl extract <round id> --files-from F --delete-from F --dest <clean path>`. Refuse when the agent's processes are still alive (`in_use`, exit 20 `live`).
4. **Diff check.** Compute the diff again in the clean checkout. A different sha256 refuses with `diff_changed`. Compute the off-limits paths: any touched path refuses with `sensitive_paths` unless `--allow-sensitive` was given by a human (never for an automatic action).
5. **Commit.** Restore the executable bit (Commit), stage exactly the listed paths with literal pathspecs and commit with your identity (Commit).
6. **Push.** Record `pushing` with the commit sha and the recorded head, then push fast-forward only (Push). On success record `pushed`. A replies-only record skips steps 3 to 6 and goes to 7.
7. **Replies.** Record `posting`, then post each remaining reply with `pr-comment reply` (Replies), updating each reply's `status`. When all are posted or skipped, run the optional thread resolves, record `done`, fire `post-confirm` and remove both throwaways.

### Commit

The identity is `git.user_name` and `git.user_email`; when unset they are read from your global git config file directly (parsed, no includes, no git process); with neither, `confirm` refuses with `error.code: identity_missing`. The message is the agent's `commit_message` after sanitizing: trailer lines (`Signed-off-by`, `Co-authored-by` and any `Key: value` last paragraph) and lines starting with `#` are removed, the subject is cut at 72 characters, the body at 4 KiB, and control and invisible characters are replaced. A trailer `Assisted-by: review-respond (confirmed by <me.name>)` is appended (config `git.trailer`, `null` turns it off; automatic pushes always append `Pushed automatically under policy <name>` and the decision id). The message goes to git through `-F <file>`.

Modes: only the executable bit is restored, for a listed file that already has it in the head or that is executable in the round throwaway (read with a no-follow `lstat` there), through `update-index --chmod=+x`. No other mode is copied.

### Push

The URL is `fetch_url` from `wtctl locate --json`, checked to be unchanged by `insteadOf` rewriting (`ls-remote --get-url`, a local command). The refspec is `HEAD:refs/heads/<head ref>`, always the full ref name. The push is `push --no-verify`, never forced and never to a remote name. Credentials per host come from `hosts.<host>.token_env` or `token_command`, and go only as one URL-scoped `http.extraHeader` over https; an ssh URL uses `SSH_AUTH_SOCK` with `BatchMode`. After a rejected ref, `ls-remote` is run again and compared with the recorded head: a different head is `remote_moved` (exit 20, the record returns to `pending`, nothing was pushed); the same head is exit 12 `push_failed` with the host's reason, and the record returns to `pending`. A timeout after the push started leaves the record `pushing`: re-running `confirm` runs `ls-remote` first and treats a remote head equal to the new commit as `pushed`.

### Replies

Each reply is posted with `pr-comment reply <pr> --to <comment_id> --body-file F --dedupe-key respond:<pending id>:<comment id> --yes --actor <actor>`; the footer and the marker come from pr-comment. No `--expected-head` is passed, because the push moves the head. A reply whose comment was deleted (`not_found` with `details.what: to`), whose thread was resolved, or that you dropped is skipped with a status reason. A post whose outcome is unknown is retried by re-running `confirm` with the same key. With `resolve_after_confirm: true`, the threads that got a reply are resolved through `pr-comment resolve` after all replies are done; a general comment cannot be resolved and is skipped.

### Crash recovery

The intent states make `confirm` re-runnable: `pushing` re-checks the remote head (Push), `pushed` skips straight to replies, `posting` posts only the replies whose status is not `posted` or `skipped`. A push is never repeated and never forced.

## Reject, expiry and closed PRs

- `reject` removes the round throwaway (`wtctl remove`), tears down the round's fork and marks the record `rejected`. Without `--redo` the comments stay handled.
- Expiry (`pending_ttl`) is `reject` without `--redo`, fired by `check`, with `post-attention` reason `pending_expired`.
- A closed or merged PR: the next `check` marks pending confirmations `closed`, marks queued or running rounds `canceled`, removes the throwaways and tears down the base fork and every pending fork (`teardown_refused` is a `post-attention` reason when agent-spawn refuses; the next `check` retries).
- A throwaway removal that `wtctl` refuses (`in_use`, exit 20) is retried by the next `check`.

## Automatic modes

Both are off by default, set per repo (`repos.<id>.auto_reply`, `repos.<id>.auto_push`) and need gatectl.

- `auto_reply` posts the replies without a push or a diff (the round must have no changed or deleted files).
- `auto_push` commits, pushes and then posts the replies.

At the end of a job, for a round that qualifies, review-respond runs the hard rules, then one `gatectl check <gate.policy> --input - --actor auto:review-respond` call per round with a closed declared-field input (`schemas/gate-input.json`): `repo`, `pr`, `round`, `head_sha`, `diff_sha256`, `files_changed`, `files_deleted`, `additions`, `deletions`, `reply_count`, `paths`, one boolean per hard rule, `author_kinds` of the triggering comments, `edited_count`, `ci_state`, `mentions_person`, `sandbox_active`, `tools_unexpected`, `implicit`. No free text. The hook timeout is at least 30 seconds above `--timeout` (`gate.timeout`, at most 600).

Hard rules, checked before the gate and again right before the push: no off-limits path; `ci.state` is `passing` on the previous head; the PR is open, not a draft and its head equals the round's head; every triggering comment's author is in `reviewers` or has `write_access: true`; no edited comment among them; the artifact validated with `report.implicit` false and no unexpected tools; every changed path matches the repo's `auto_push.paths` allowlist (empty means none; same matcher as off-limits); the round was not re-run after an escalation.

An automatic action happens only on `"enforced": true`, `"decision": "allow"` and a `decision_id`. Anything else leaves the pending confirmation for you. The decision id goes into the audit record, the commit trailer and the reply footer; automatic replies use `--actor auto:<policy>`. When you later confirm, reject or edit a gated pending, review-respond files `gatectl outcome <id> agree|disagree --actor human:<user>` (an edited pending files `disagree`); automatic actions file no outcome.

## Off-limits paths

The list (`off_limits`) uses the format, the default list and the path-matching rules of review-watch's interface (section Path matching): one segment matcher with case folding, `**` for zero or more whole segments, git-quoted paths failing closed, renames tested on both paths. It applies to a manual `confirm` (refused unless `--allow-sensitive` after the diff was shown) and to every automatic push.

## Hooks

Hooks are configured in `hooks.<name>` lists as the design guide's section 8 describes. A payload goes to stdin and holds `tool`, `hook`, `object_type`, `object_id`, `repo` and `labels`, plus the fields below, and never any comment, reply, diff or message text. Environment: `REVIEW_RESPOND_HOOK`, `REVIEW_RESPOND_ID`, `REVIEW_RESPOND_OBJECT_TYPE`. Schemas: `schemas/hooks/`.

| Hook | Fires | Can veto | Payload fields |
|---|---|---|---|
| `pre-round` | once per round, after the eligibility, settle and cap checks, before the job is submitted | veto (the round is not started; re-asked on the next new comment) or escalate (the round is created held, continued by `respond`) | `pr`, `round`, `head_sha`, `comment_count`, `trigger` (`check`, `respond`), `forced`, `reads_third_party` |
| `pre-push` | in `confirm`, after the preconditions and before the commit and push | veto or escalate | `pending_id`, `pr`, `head_sha`, `diff_sha256`, `files_changed`, `additions`, `deletions`, `sensitive`, `actor_class`, `reads_third_party`, `tainted` |
| `post-pending` | when a pending confirmation was recorded | no | `pending_id`, `action`, `event`, `summary` (a fixed phrase with counts), `expires`, `dedupe_key`, `reads_third_party`, `tainted` |
| `post-attention` | once per cause, with a closed `reason` | no | `reason`, `object_type` (`round`, `pending`, `pr`), `details` (ids, enums, counts), `dedupe_key`, `reads_third_party`, `tainted` |
| `post-confirm` | after a confirm reached `done` | no | `pending_id`, `pr`, `commit_sha` (or null), `pushed`, `replies_posted`, `replies_skipped`, `dedupe_key` |

`post-attention` reasons: `escalated`, `failed`, `round_limit`, `pending_expired`, `remote_moved`, `push_failed`, `reply_failed`, `job_lost`, `ineligible_comment`, `pr_closed`, `teardown_refused`. The post hooks are delivered at least once through an outbox, in order per object, with an `event_id` and a `dedupe_key`; they cannot veto. `pre-` hook outcomes follow the design guide (exit 0 continues, 13 escalates, other non-zero vetoes and the command exits 11). A `decision_id` on a hook's stdout is recorded in the round or pending record and in audit. Deviation from the design guide's `post-pending` wording: the record's `summary` is a fixed phrase and never agent text, so a notification can use it.

## Config

Location, drop-ins and precedence follow the design guide (`~/.config/review-respond/config.yaml`, `config.d/*.yaml`, `XDG_CONFIG_HOME`, flags over per-repo over environment over config over defaults). `schemas/config.json` is self-contained and `config.example.yaml` documents every key.

| Key | Default | Meaning |
|---|---|---|
| `me.name`, `me.accounts` | OS user, none | your name for audit and the trailer; optional per-host account ids (otherwise `forgectl whoami`) |
| `hosts.<host>.token_env`, `.token_command` | none | credentials for the push (a variable name or an argv list); never stored |
| `hosts_to_check` | all hosts forgectl knows | hosts whose PRs `check` lists |
| `reviewers` | `[]` | entries `<host>:<id>` whose comments are always eligible |
| `bot_authors` | `[]` | extra account ids treated as bots |
| `respond_to_own_comments_prefix` | `null` | opt-in prefix for your own comments |
| `settle` | `5m` | minimum age of the newest eligible comment |
| `max_rounds` | `3` | counted rounds per PR |
| `pending_ttl` | `7d` | pending confirmation lifetime |
| `access_cache_ttl` | `1h` | cache of `forgectl access` answers |
| `prefer_owner` | `ticket-work` | preferred owner label when several agents match |
| `fresh_label` | `null` | PR label that opts a PR into a fresh session |
| `resolve_after_confirm` | `false` | resolve replied threads after confirm |
| `off_limits` | review-watch's list | globs |
| `skill`, `permission_profile`, `sandbox`, `setting_sources`, `env_allowlist`, `model`, `agent.timeout` | see Session settings | per-call safety settings |
| `jobs.queue`, `.kill_grace`, `.max_running`, `.pass_env` | `respond`, `125s`, `1`, `[]` | job placement |
| `gate.policy`, `gate.timeout` | `respond-auto`, `540` | gate call |
| `git.user_name`, `git.user_email`, `git.trailer` | from your git config, from your git config, the default trailer | commit identity |
| `limits.requests_per_host`, `.prompt_comments`, `.prompt_bytes`, `.reply_bytes` | `120`, `50`, `65536`, `8192` | bounds |
| `attention.ineligible_comment` | `false` | fire a hook for ineligible comments |
| `retention.records` | `30d` | prune age |
| `hooks.<name>` | none | hook command lists |
| `repos.<id>.*` | none | per-repo overrides: `respond_fresh`, `auto_reply`, `auto_push` (`enabled`, `paths`), `respond.allow_commands`, `respond.read_dirs`, `settle`, `max_rounds`, `reviewers`, `clone_url` passthrough is wtctl's |

Durations accept the design guide's grammar plus `h` and `d` for the keys that name hours or days. Invalid config fails fast with the key's name.

## Environment variables

`REVIEW_RESPOND_<KEY>` sets a top-level scalar (nested keys joined with `__`; structured keys such as hooks, `reviewers` and `repos` cannot be set from the environment). Credentials are named in config and read from the environment. Hook variables are listed under Hooks.

## Labels

review-respond puts these labels on what it creates: on forks, `pr=<host/path#N>` and `owner=review-respond` (and `repo=<id>`); on throwaways, `app=review-respond`, `round=<id>` and `pending=<id>`; on jobs, the correlation labels `pr`, `round` and `owner`. It gives no meaning to any other label. It reads the `pr` and `owner` labels of agents only to find the original agent and verifies the match.

## Files and state

State under `~/.local/state/review-respond/` (`XDG_STATE_HOME` respected): `state.db` (private SQLite, WAL, versioned additive migrations), `log.jsonl` (public, hash-only, no comment text), `audit.jsonl` (public), and directories for prompt files, generated profiles, stored artifacts, stored diffs and reply drafts, each deleted when its record ends or by `prune`. The managed crontab block written by `cron install` has the first line `# review-respond managed v1 every=5m`, a `PATH=` line, the line `*/5 * * * * . '<env_file>' && '<absolute review-respond>' check --quiet` (the env-file prefix only with `cron.env_file`) and the last line `# review-respond managed end`.

## Audit and log

`audit.jsonl` records every external action: each push and each reply post, as an `attempted` record before the call and an outcome record after it sharing an `attempt_id`, with `ts`, `actor`, `decision_id`, `target` (PR), `action`, `inputs` (head sha, diff sha256, commit sha, the pending id, config hash) and `result`. Human decisions (`confirm`, `reject`, `edit`) and gate decisions are recorded too. If the first append fails nothing is sent. `log.jsonl` holds events (rounds, transitions, errors) with ids and hashes only. Schemas: `schemas/audit.json`, `schemas/log.json`.

## Hardened git

review-respond runs git itself, only in the clean throwaway that no agent wrote, and always through one module that implements architecture section 13.2 rule 10: an environment built from scratch, the full override list, the injected flags and rejected options, a fixed allowlist of subcommands (`rev-parse`, `config`, `ls-remote`, `add`, `diff`, `status`, `update-index`, `commit`, `push`), filter-driver and extra-header blanking from `config --list -z`, explicit `--git-dir` and `--work-tree`. Two deviations from the recipe's literal text, both found against real git and recorded in `docs/design.md`: `GIT_ALLOW_PROTOCOL` is set to the scheme of the URL in use (a push over `none` fails), and `GIT_SHALLOW_FILE` is not `/dev/null` (the throwaway keeps its own shallow file; `/dev/null` breaks a push from a shallow clone). Pathspecs are literal (`--literal-pathspecs`). A `live` regression test of the recipe is required.

## Upgrades

`state.db` migrations are additive. A newer schema than the binary knows makes read-only commands work and write commands exit 12 `version_skew`. Config keys added later have defaults. Hook payload keys are only added.

## Residual risks

- A prompt-injected fork can steer its own outputs (the edits, replies and message). They are untrusted, shown to you before anything is pushed and gated before anything automatic happens.
- A fork carries its source conversation, which may contain secrets the original agent read. The sandbox has no network and the throwaway holds only the PR's files (architecture section 13.2, residual risk (i)).
- A human-only rule is advisory against a same-user process that claims a human actor.
- `wtctl`'s throwaway `max_age` and an operator's own `prune` can remove a throwaway under a pending confirmation; `confirm` then refuses with `error.code: throwaway_missing` and `reject --redo` starts again.
- The edit test uses a body hash, so an edit that restores the earlier text is not seen.
- Access answers are cached; a collaborator removed within the cache lifetime stays eligible until it expires.
- The commit message and the diff are reviewed by you; a convincing change that you approve is yours.

## Platform notes

Linux and WSL are supported. macOS is checked by a manual checklist (the sandbox is Seatbelt; `lsof` for `in_use`). Interactive forks need tmux and are unverified in 0.x.

## TUI

None in 0.x. A `browse` command (`--focus`, `--result-file`) is a 1.x item and follows a feature inventory of forgectl's TUI.

## Objects

- **PR state** (`list`, `show`): `pr`, `repo`, `state`, `head_sha`, `head_ref`, `base_ref`, `is_fork`, `base_agent`, `rounds`, `unhandled`, `ineligible`, `edited`, `pending_id`, `updated`.
- **Round** (`respond`, `show`, `list --json`): `id`, `pr`, `number`, `attempt`, `state`, `head_sha`, `agent_id`, `job_id`, `comment_ids`, `failure` (`reason`, `detail`), `started`, `ended`, `warnings`.
- **Pending confirmation**: Pending confirmations, Record.
- **Comment record** (`show --json`): `id`, `author_id`, `eligible`, `reason`, `state`, `edited`, `thread_id`, `path`, `line`, `body` (untrusted, sanitized).

Every `--json` output is described in `schemas/cli/<command>.json`; fields that carry comment, reply, diff or message text are marked `x-untrusted`.
