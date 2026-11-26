# review-respond: design

Status: designed (interface 0.1.0-design). The normative rules are in [`interface.md`](interface.md); this document records decisions, the live verification table, modules, the data model and the test strategy, and links to interface headings instead of restating them. Section references (section N) point to `../awkit-docs/architecture.md` unless marked "design guide" (`../awkit-docs/design-guide.md`).

## Role and layer

Respond to review comments on your own pull requests, with fixes and replies you confirm. Layer 3 (workflow). It works standalone and has no upward awareness. It is a skill-wrapper tool (section 5.5, design guide section 11); its default slot is `respond-to-review`, bundled as `review-respond:respond-to-review` in `plugin/`.

## Uses

| Tool | Binding | Why |
|---|---|---|
| forgectl | A | reads (open PRs, comments, PR facts, access, identity, `compare` to decide an unclear push); never writes |
| agent-spawn | A | finds, forks and launches respond agents, waits for the run, reads the frozen artifact, tears forks down |
| pr-comment | A | posts replies and resolves threads (the only writer to the host besides the push) |
| wtctl | A | round and clean throwaway checkouts, `extract`, `locate`, `remove`, `list`, `show`, `label add` |
| git | A | hardened commit and fast-forward push, in the clean throwaway only |
| jobsctl | A, optional | queue `respond`; without it a detached `_job` under an internal cap |

pr-watch is not used (decision D-2). gatectl is not used in 0.x (decision D-12). review-respond does not call claude-session directly: agent-spawn passes the session settings through, and `validate` reads `claude-session kinds --json` only for the sandbox probe. Minimum versions of every sibling are in the interface Dependencies table, not here.

## Decisions

| ID | Decision | Rationale | Interface |
|---|---|---|---|
| D-1 | Skill-wrapper tool with one default slot `respond-to-review`, kind `respond`, always a fork, headless by default | architecture section 5.3; spike: the verified path | [Session settings](interface.md#session-settings) |
| D-2 | Detection uses forgectl only; pr-watch is dropped from Uses | pr-watch is design-only, its events carry no text or edits, and it reports review-respond's own replies; forgectl is needed for text anyway. Every pass reads every open PR, least recently attempted first, so a failing PR cannot starve the others, because `updated_at` does not change on every edit, deletion or resolution | [Detection](interface.md#detection) |
| D-3 | Eligibility: not own, not a bot (`null` counts as a bot), `reviewers` or `write_access: true`, open own PR that is not a fork | comments carry no `author_association`, so `access` is the only write-access source; a `null` author and a minimized comment are ineligible, and the access cache is keyed by author id | [Eligibility](interface.md#eligibility) |
| D-4 | Handled set keyed by the full comment id with a body hash; edits re-open, deletions drop, resolved threads leave prompts | `updated_at` alone is noisy on review comments; the settle clock is the local time a body hash was first seen, so host clock skew does not matter; a comment first stored before installation is only recorded (`seen`) and a later one is answered, decided per comment | [Handled set](interface.md#handled-set) |
| D-5 | A settle delay, one round per PR, none while a pending confirmation exists, a round limit | reviewers post in bursts; a second round on a stale checkout would fork stale work | [Settle and round start](interface.md#settle-and-round-start) |
| D-6 | Handled means validated: comments are marked handled in the transaction that records the pending confirmation; `working` only starts the round | the run reaches `working` before the prompt is read, and several failures exit 0 | [Artifact](interface.md#artifact) |
| D-7 | The diff you review is computed in a clean checkout at record time and recomputed at confirm (`diff_changed`) | no trusted process may run git in an agent-written throwaway | [Stored diff](interface.md#stored-diff) |
| D-8 | Confirm order: push first, then replies, with an intent state per step | a crash must never repeat a push or lose a reply; each record state allows a fixed set of commands; `pushing` and `pushed` are the intent states and never expire; per-reply statuses are the reply intents; a push that may have landed is recognized by any commit sha the record tried, and nothing is rebuilt | [Confirm order](interface.md#confirm-order) |
| D-9 | Fork lineage: each round forks the last confirmed fork (or the original); a rejected or failed fork is never a base | a fork remembers edits that exist only in an old throwaway; the switch happens in the transaction that records the landed push, and escalated forks are torn down too | [Fork lineage](interface.md#fork-lineage) |
| D-10 | review-respond owns a hardened-git module; wtctl runs no commit or push | wtctl's git wrapper is internal | [Hardened git](interface.md#hardened-git) |
| D-11 | Five deviations from the hardened recipe: the allowed protocol is the URL's scheme for `push` and `ls-remote` only, the shallow file is the clean checkout's own, pathspecs are literal by environment, `core.sshCommand` is one argument, and review-respond's own diff flags | verified against git 2.43; the literal recipe breaks pushes | [Hardened git](interface.md#hardened-git) |
| D-12 | Automatic modes are a 1.x item | gatectl is not implemented, and an automatic reply publishes agent text that may carry the forked conversation's secrets with no human look | none in 0.x |
| D-13 | One job per round through jobsctl (`--max-retries 0`) or a detached `_job` | the same pattern as the other L3 tools; the job records the agent id before it waits, and every fork carries a `round=` label, so a crashed job can be finalized; a failed round is not retried automatically and waits for a human `respond`; the job environment is built from scratch in both placements | [Job](interface.md#job) |
| D-14 | Replies post after the push through pr-comment with a per-comment dedupe key and no head check | its own push moves the head; the key hashes the comment id, which can hold characters the forge key grammar refuses | [Replies](interface.md#replies) |
| D-15 | The agent is told the throwaway's real path; the artifact echoes a marker from the plugin | the forked conversation names the original paths; a missing skill exits 0, so a wrong or missing marker is an invalid artifact | [Prompt](interface.md#prompt) |
| D-16 | No TUI in 0.x | a forgectl-style feature inventory is needed first | [TUI](interface.md#tui) |
| D-17 | Config under XDG as usual (not a home-only tool) | a session cannot reach its config directory (sandbox and profile denies) | [Config](interface.md#config) |
| D-18 | Round and PR states: terminal `confirmed` and `rejected`; a stored PR block stops escalated, failed, limited and vetoed PRs; a hook escalation creates no round | a PR that waits for a human must not start a round every pass; a queued round whose job never ran is reconciled against the job by `check`; a human `stop` blocks the PR until a human `respond` | [Round states](interface.md#round-states) |
| D-19 | One lease design: `pass`, `pr:` and `round:` leases with one takeover rule; only `check` and `prune` report `already_running` | a contended `confirm` that exits 0 would look done; a holder that is verified gone (absent, zombie, reused PID or other boot) is taken over at once; without `/proc` only an absent PID counts as gone, and the state directory is used from one PID namespace | [Concurrency](interface.md#concurrency) |
| D-20 | Fork PRs get no round in 0.x; every push also needs the head repository to equal the base repository and a head ref that is not the default branch | the push URL and the head fetch reach the base repository, not the fork; a push would create or advance a branch there; the default branch is read from the remote at confirm (`ls-remote --symref`), since wtctl stores it only after a mirror refresh | [Eligibility](interface.md#eligibility) |
| D-21 | The reviewed diff is a full-text, full-index, rename-free diff; binary paths need the same explicit allow as off-limits paths | a NUL byte or a `-diff` attribute hides content from a normal diff; rename detection hides the deleted side of an off-limits path; an oversized diff, very long lines and known token prefixes need the same allow | [Stored diff](interface.md#stored-diff) |
| D-22 | No file mode is carried from the round | a mode set after the reviewed diff would land unreviewed | [Stored diff](interface.md#stored-diff) |
| D-23 | The commit message is sanitized by a closed procedure and always ends with review-respond's trailer paragraph | git and the forge read trailers and closing keywords from agent text otherwise; lines with references, URLs and CI directives are dropped whole | [Commit](interface.md#commit) |
| D-24 | A pending reply to a deleted comment or a resolved thread is skipped; one to an edited comment is skipped and the comment re-opens | the reviewer's current text is what gets answered, and a deleted comment may have been retracted | [Replies](interface.md#replies) |
| D-25 | A confirmation binds one content hash over the diff, the message and the replies; editing needs a human | the human approves the text that is posted, not only the diff | [Record](interface.md#record) |

## Resolved open topics

The seed's four questions: comment detection (events and watermarks are replaced by a stored handled set, D-2 and D-4), reply drafting and thread attachment (the agent proposes replies, you edit them, pr-comment attaches them to the comment's thread, D-14), the pending-confirmation format ([Record](interface.md#record)), and the gate input (not needed in 0.x: automatic modes are deferred, D-12).

## Live verification

| ID | Behavior relied on | Source | Status |
|---|---|---|---|
| V-1 | `agent-spawn resume --fork --cwd <throwaway> --kind respond ...` works as written; without `--kind respond` the fork is refused (`third_party_run`) and a subdirectory cwd is refused (`third_party_cwd`) | headless spike, haiku | verified |
| V-2 | The sandbox blocks `git add`, `git commit`, push and network; writes outside the throwaway vanish silently; `git status` lists placeholder files | spike | verified |
| V-3 | `first_working_at` is set at SessionStart before the prompt is submitted and is null for a refused launch | spike | verified |
| V-4 | A misspelled skill, a malformed artifact and a `done` report without an artifact all exit 0 | spike | verified |
| V-5 | A fork carries the original conversation; a second round forks the first fork with no extra flags | spike | verified |
| V-6 | `wtctl remove` of a throwaway exits 0 after the run ended and 20 `in_use` during it; `teardown` is idempotent | spike | verified |
| V-7 | Interactive forks | needs termctl | unverified |
| V-8 | `extract` refusals are atomic and exit 12; file modes are not carried; `--delete-from` and `--dest` are not checked against the source or to throwaways | spike on git 2.43 | verified (review-respond checks both itself; see Artifact) |
| V-9 | Hardened-git corrections; push moves and races give three different texts | spike | verified on git 2.43; 2.34 to be run by the `live` suite before 1.0 |
| V-10 | Planted config in the round throwaway does not affect the clean throwaway | spike | verified |
| V-11 | forgectl `comments` shape, ids, no `author_association`, no edit flag, `access` costs and nulls | read-only spike on GitHub | verified |
| V-12 | Deleted comments, GitLab behavior, the pr-comment marker in real data | no data found or no host | unverified |
| V-13 | A push to a real forge from a shallow clean throwaway | local bare remote only | unverified (live test) |
| V-14 | pr-comment `reply` and `resolve` | pr-comment 0.33.0 has `reply` and `resolve` (interface at 0.33.1) | unverified (live test) |
| V-15 | `agent-spawn resume --fork` without `--wait` returns the fork id at run start; `wait` returns the outcome | agent-spawn interface | unverified (live test) |
| V-16 | `--no-promote` keeps an exit-13 respond round headless | agent-spawn interface | unverified (needs a blocked report) |
| V-17 | A `--porcelain` push to a real forge reports `!` lines for moved and rejected refs; `ls-remote` decides | git documentation | unverified (live test) |
| V-18 | forgectl `compare` reports `ahead` from a pushed commit to a later head | forgectl interface | unverified (live test) |
| V-19 | A host advertises its default branch as the symref of HEAD to `ls-remote --symref` | git documentation; spike B ran against a local bare remote | unverified (live test) |

Every verified item becomes a `live` or process regression test (Test strategy); every unverified item has a conservative default.

## Modules

| Module | Responsibility |
|---|---|
| `cli.py` | argparse, global flags, exit codes, the error envelope |
| `errors.py` | exit codes, error codes, `CliError` and the error envelope |
| `actor.py` | actor default and grammar, decision ids, the `human_only` rule |
| `durations.py` | the duration grammar and RFC 3339 times for flag values |
| `output.py` | the per-run `Context`, `Outcome`, result printing and the atomic `--result-file` |
| `textin.py` | free text read only from a file or stdin |
| `config.py` | config load and validation, per-repo overrides |
| `gateway.py` | every subprocess and HTTP call (forgectl, agent-spawn, pr-comment, wtctl, jobsctl, git); the `Clock` of its process waits and timeouts (duration parsing takes an injectable `now=` instead) |
| `store.py` | SQLite access, migrations, compare-and-set, the three lease kinds (no separate module) |
| `detect.py` | listing, comment reads, the eligibility rules, the handled set |
| `rounds.py` | round states, due checks, lineage, job submission, the queued reconcile, finalizing and cleanup |
| `job.py` | the hidden `_job` command: throwaway, prompt, fork, artifact, stored diff |
| `prompt.py` | fenced prompt building and the one sanitizer |
| `artifact.py` | strict artifact validation |
| `pending.py` | the pending record, edits, expiry |
| `confirm.py` | the confirm order, push recovery, replies |
| `hgit.py` | the hardened-git module |
| `hooks.py` | hook running and the outbox |
| `audit.py` | `audit.jsonl` and `log.jsonl` |
| `cron.py` | the managed crontab block |
| `plugin/` | the bundled plugin (skill, reference marker) |

## Data model

Tables in `state.db` (additive migrations): `meta` (including `installed_at`, the start of the first command that read comments), `prs` (PR key, repo, head, base agent, `last_comments_read_at`, `last_comments_attempt_at`, `last_show_attempt_at` (rotates the `show` of unlisted open PRs), `read_failures`, `comment_count` (the budget estimate), host state, `block`), `comments` (PR, comment id, thread id, author id and login, `created_at`, `host_updated_at`, body hash, `body_file` and `body_bytes` (the bounded stored text), `first_seen_at`, `body_seen_at` (the settle timestamp), state, round, `delivered_sha256`, `delivered_from`, `handled_sha256`, `handled_reason`, `edited`, `eligible`, eligibility reason, `thread_resolved`, `outdated`, `path`, `line`, `reply_to`, `rev`; keyed by PR and comment id; a PR's rows are deleted before the PR and its stored files), `rounds` (id, PR, number, state, `rev`, `trigger`, `forced`, `include_existing`, `cancel_requested_at`, `cancel_reason`, `counted`, job id or detached start time, agent id, throwaway id, failure, escalation), `pendings` (id, round, state, `rev`, `content_sha256`, `commit_shas`, `commit_sha`, `moved_head`, `redone_at`, flags, expiry), `replies` (pending, comment id, status, status reason, dedupe key), `forks` (agent id, PR, role, state: base, round, teardown_pending, torn_down), `access_cache`, `outbox`, `leases` (one row per `pass`, `pr:` and `round:` key). Text lives in files named by hash under a per-record directory of the state directory.

## Concurrency

One lease design for every command, with one takeover rule: see [Concurrency](interface.md#concurrency) (decision D-19). Round and pending transitions are compare-and-set on `rev`; `check`, `show` and `confirm` finalize idempotently. Throwaways and forks are released by one idempotent cleanup rule (see [Round states](interface.md#round-states); it may move to a Cleanup heading).

## Error handling

A sibling's failure maps to exit 12 with the sibling's error in `lower_error`; a refusal maps to exit 20 with its reason (the exit and error code tables are in [Exit codes](interface.md#exit-codes)); an unknown outcome (a push or a post that may have landed) is recorded as an intent state and resolved by re-running the same command. A failed round is not retried automatically: it sets a block and waits for a human `respond`. A dead sibling never loses a comment: comments are handled only in the transaction that records a validated result.

## Test strategy

All subprocess and HTTP calls go through `gateway.py`; fake binaries for each sibling live in `tests/fakebin/` with pinned copies of the siblings' schemas. Golden files cover every `--json` output and every hook payload. Process tests cover the job, the leases, the confirm intent states and the crash recovery (a kill between each pair of steps 4 to 9 of the confirm order, a kill of `_job` between `resume` returning and the agent id record, SIGTERM of `_job` during `wait` with and without a cancel flag, and a child `wait` exiting 143). A queued round with no job id, with a jobsctl job ended `failed` (`missing_env`) and with a canceled job; a stopped running round followed by a `check` that starts nothing; every cell of the record state-by-command table; `reject --redo` after expiry, twice; the three-line `ls-remote` case (`refs/heads/a/refs/heads/feat`, `refs/heads/feat`, `refs/heads/zz/refs/heads/feat`) and a late first push after a second confirm; a remote with no HEAD symref refuses at confirm; a diff with a `ghp_` line and one over `limits.diff_bytes` need `--allow-sensitive`. A glob-named file (`a*.py`) must stage literally. `live` tests (deselected by default, never in CI) cover a real headless fork round, the hardened-git recipe against real git (every vector in architecture section 13.2 rule 10), a push from a shallow clean throwaway to a real forge, each verified spike behavior above, a NUL-byte change and a `-diff` attribute that show in the stored diff, and a rename out of an off-limits path that is refused. `make verify` runs ruff, pyright, pytest and markdown lint.

## Dependencies justified

`pyyaml` (config), `pydantic` (validation), and the standard library for everything else (`sqlite3`, `argparse`, `subprocess`, `json`). Test-only: `pytest`, and `jsonschema` (with its dependency `referencing`), which validates every golden `--json` output, hook payload and scripted sibling answer against `schemas/` and the pinned sibling copies in `tests/pinned/`; the schemas use `$ref` across files and `if`/`then` conditions that a hand-written check would not cover. They are never imported by the package.

## Non-goals for 0.x

A TUI, interactive forks, fork PRs, automatic replies and pushes (a 1.x item), automatic retry of a failed round, review bodies (inline and general comments only), a dollar or per-round cost cap, macOS verification, GitLab write-access verification, and a recipe for wiring pr-watch events to `check` (the harness owns it).

## Deviations from the design session

Review of the first design changed these decisions: fork PRs get no round in 0.x; the automatic modes (`auto_reply`, `auto_push`) and the gatectl dependency moved to 1.x; no file mode is carried from the round; fresh sessions are opted in only by `repos.<id>.respond_fresh`, not by a PR label (anyone with triage rights can set a label); detection has no `updated_since` cursor and reads every open PR each pass; comments that existed before the first pass are recorded without being answered and later ones are answered; and review-respond's dedupe keys, base switch and label rules were tightened. A second review added: no automatic retry of a failed round, a reply to a deleted comment is skipped, and the default branch is read from the remote.
