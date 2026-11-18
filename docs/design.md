# review-respond: design

Status: designed (interface 0.1.0-design). The normative rules are in [`interface.md`](interface.md); this document records decisions, the live verification table, modules, the data model and the test strategy, and links to interface headings instead of restating them. Section references (section N) point to `../awkit-docs/architecture.md` unless marked "design guide" (`../awkit-docs/design-guide.md`).

## Role and layer

Respond to review comments on your own pull requests, with fixes and replies you confirm. Layer 3 (workflow). It works standalone and has no upward awareness. It is a skill-wrapper tool (section 5.5, design guide section 11); its default slot is `respond-to-review`, bundled as `review-respond:respond-to-review` in `plugin/`.

## Uses

| Tool | Binding | Why |
|---|---|---|
| forgectl | A | reads (open PRs, comments, PR facts, access, CI, identity); never writes |
| agent-spawn | A | finds, forks and launches respond agents, reads the frozen artifact, tears forks down |
| pr-comment | A | posts replies and resolves threads (the only writer to the host besides the push) |
| wtctl | A | round and clean throwaway checkouts, `extract`, `locate`, `remove` |
| git | A | hardened commit and fast-forward push, in the clean throwaway only |
| jobsctl | A, optional | queue `respond`; without it a detached `_job` under an internal cap |
| gatectl | A, optional | gate for the automatic modes |

pr-watch is not used (decision D-2). review-respond does not call claude-session directly: agent-spawn passes the session settings through, and `validate` reads `claude-session kinds --json` only for the sandbox probe.

## Decisions

| ID | Decision | Rationale | Interface |
|---|---|---|---|
| D-1 | Skill-wrapper tool with one default slot `respond-to-review`, kind `respond`, always a fork, headless by default | architecture section 5.3; spike: the verified path | [Session settings](interface.md#session-settings) |
| D-2 | Detection uses forgectl only; pr-watch is dropped from Uses | pr-watch is design-only, its events carry no text or edits, and it reports review-respond's own replies; forgectl is needed for text anyway | [Detection](interface.md#detection) |
| D-3 | Eligibility: not own, not a bot (`null` counts as a bot), `reviewers` or `write_access: true`, open own PR | comments carry no `author_association`, so `access` is the only write-access source | [Eligibility](interface.md#eligibility) |
| D-4 | Handled set keyed by the full comment id with a body hash; edits re-open, deletions drop, resolved threads leave prompts | `updated_at` alone is noisy on review comments | [Handled set](interface.md#handled-set) |
| D-5 | A settle delay, one round per PR, none while a pending confirmation exists, a round limit | reviewers post in bursts; a second round on a stale checkout would fork stale work | [Settle and round start](interface.md#settle-and-round-start) |
| D-6 | Handled means validated: comments are marked handled in the transaction that records the pending confirmation; `working` only starts the round | the run reaches `working` before the prompt is read, and several failures exit 0 | [Artifact](interface.md#artifact) |
| D-7 | The diff you review is computed in a clean checkout at record time and recomputed at confirm (`diff_changed`) | no trusted process may run git in an agent-written throwaway | [Stored diff](interface.md#stored-diff) |
| D-8 | Confirm order: push first, then replies, with an intent state per step | a crash must never repeat a push or lose a reply | [Confirm order](interface.md#confirm-order) |
| D-9 | Fork lineage: each round forks the last confirmed fork (or the original); a rejected or failed fork is never a base | a fork remembers edits that exist only in an old throwaway | [Fork lineage](interface.md#fork-lineage) |
| D-10 | review-respond owns a hardened-git module; wtctl runs no commit or push | wtctl's git wrapper is internal | [Hardened git](interface.md#hardened-git) |
| D-11 | Two corrections to the hardened recipe (protocol scheme, shallow file) | verified against git 2.43; the literal recipe breaks pushes | [Hardened git](interface.md#hardened-git) |
| D-12 | Automatic modes: separate `auto_reply` and `auto_push`, one gate call per round with a closed input, hard rules before and after | architecture section 12.3 | [Automatic modes](interface.md#automatic-modes) |
| D-13 | One job per round attempt through jobsctl (`--max-retries 0`) or a detached `_job` | the same pattern as the other L3 tools | [Job](interface.md#job) |
| D-14 | Replies post after the push through pr-comment with a per-comment dedupe key and no head check | its own push moves the head | [Replies](interface.md#replies) |
| D-15 | The agent is told the throwaway's real path; the artifact echoes a marker from the plugin | the forked conversation names the original paths; a missing skill exits 0 | [Prompt](interface.md#prompt) |
| D-16 | No TUI in 0.x | a forgectl-style feature inventory is needed first | [TUI](interface.md#tui) |
| D-17 | Config under XDG as usual (not a home-only tool) | a session cannot reach its config directory (sandbox and profile denies) | [Config](interface.md#config) |

## Resolved open topics

The seed's four questions: comment detection (events, cursors and watermarks are replaced by a stored handled set, D-2 and D-4), reply drafting and thread attachment (the agent proposes replies, you edit them, pr-comment attaches them to the comment's thread, D-14), the pending-confirmation format ([Record](interface.md#record)), and the gate input ([Automatic modes](interface.md#automatic-modes)).

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
| V-8 | `extract` refusals are atomic and exit 12; the executable bit is not carried; `--delete-from` and `--dest` are not checked against the source or to throwaways | spike on git 2.43 | verified (review-respond checks both itself) |
| V-9 | Hardened-git corrections; push moves and races give three different texts | spike | verified on git 2.43; git 2.34 unverified |
| V-10 | Planted config in the round throwaway does not affect the clean throwaway | spike | verified |
| V-11 | forgectl `comments` shape, ids, no `author_association`, no edit flag, `access` costs and nulls | read-only spike on GitHub | verified |
| V-12 | Deleted comments, `changes_requested` reviews, GitLab behavior, the pr-comment marker in real data | no data found or no host | unverified |
| V-13 | A push to a real forge from a shallow clean throwaway | local bare remote only | unverified (live test) |
| V-14 | pr-comment `reply` and `resolve` | not implemented on main when designed | unverified (a minimum version is pinned) |

Every verified item becomes a `live` or process regression test (Test strategy); every unverified item has a conservative default.

## Modules

| Module | Responsibility |
|---|---|
| `cli.py` | argparse, global flags, exit codes, the error envelope |
| `config.py` | config load and validation, per-repo overrides |
| `gateway.py` | every subprocess and HTTP call (forgectl, agent-spawn, pr-comment, wtctl, jobsctl, gatectl, git) |
| `store.py` | SQLite access, migrations, compare-and-set, leases |
| `detect.py` | listing, comment reads, the eligibility rules, the handled set |
| `rounds.py` | round states, due checks, lineage, job submission and finalizing |
| `job.py` | the hidden `_job` command: throwaway, prompt, fork, artifact, stored diff |
| `prompt.py` | fenced prompt building and the one sanitizer |
| `artifact.py` | strict artifact validation |
| `pending.py` | the pending record, edits, expiry |
| `confirm.py` | the confirm order, recovery |
| `hgit.py` | the hardened-git module |
| `gate.py` | hard rules and the gatectl call |
| `hooks.py` | hook running and the outbox |
| `audit.py` | `audit.jsonl` and `log.jsonl` |
| `cron.py` | the managed crontab block |
| `plugin/` | the bundled plugin (skill, reference marker) |

## Data model

Tables in `state.db` (additive migrations): `meta`, `prs` (PR key, repo, head, base agent, last read, state), `comments` (PR, comment id, author id, body hash, `updated_at`, state, round, eligibility reason), `rounds` (id, PR, number, attempt, state, `rev`, lease, job id, agent id, throwaway id, failure), `pendings` (id, round, state, `rev`, intent fields, expiry), `replies` (pending, comment id, status, dedupe key), `forks` (agent id, PR, role, state), `access_cache`, `outbox`, `leases`. Text lives in files named by hash under the state directory.

## Concurrency

One instance per state directory (a lock; a second caller exits 0 "already running"). Round and pending transitions are compare-and-set on `rev`. Leases carry the holder's PID, start time and boot id plus a heartbeat and an expiry; a takeover needs the holder verified gone. `check`, `show` and `confirm` finalize idempotently. Two racing `confirm` calls are serialized by the lock and, for the push, by the host's ref lock.

## Error handling

A sibling's failure maps to exit 12 with the sibling's error in `lower_error`; a refusal maps to exit 20 with its reason; an unknown outcome (a push or a post that may have landed) is recorded as an intent state and resolved by re-running the same command. A dead sibling never loses a comment: comments are handled only in the transaction that records a validated result.

## Test strategy

All subprocess and HTTP calls go through `gateway.py`; fake binaries for each sibling live in `tests/fakebin/` with pinned copies of the siblings' schemas. Golden files cover every `--json` output and every hook payload. Process tests cover the job, the lease, the confirm intent states and the crash recovery (a kill between each pair of steps). `live` tests (deselected by default, never in CI) cover a real headless fork round, the hardened-git recipe against real git (every vector in architecture section 13.2 rule 10), a push from a shallow clean throwaway to a real forge, and each verified spike behavior above. `make verify` runs ruff, pyright, pytest and markdown lint.

## Dependencies justified

`pyyaml` (config), `pydantic` (validation), and the standard library for everything else (`sqlite3`, `argparse`, `subprocess`, `json`).

## Non-goals for 0.x

A TUI, interactive forks, a dollar or per-round cost cap, macOS verification, GitLab write-access verification, and a recipe for wiring pr-watch events to `check` (the harness owns it).
