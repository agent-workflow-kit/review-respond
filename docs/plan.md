# review-respond: implementation plan

This plan implements [`interface.md`](interface.md) and [`design.md`](design.md). Each stage becomes GitHub issues; an issue is expanded into bite-sized test-first steps when it is picked up. Nothing here restates the interface: stages name the headings they implement.

## Conventions for every stage

- **Gateway boundary.** Every subprocess call (forgectl, agent-spawn, pr-comment, wtctl, jobsctl, git) goes through `src/review_respond/gateway.py`. Time is injectable: the gateway's `Clock` covers the waits and timeouts of its process handling, and code that needs the current time takes a `now=` argument (duration parsing does), so a test never patches the clock; the system clock is read only as the default of those two. Tests substitute fakes at that boundary and never patch the standard library.
- **Fakes and pinned schemas.** Each sibling has a fake binary in `tests/fakebin/` (copied per repo, never shared) and a copy of its schemas pinned to the minimum version in the design. A fake fails the test when the real sibling would refuse the call (an unknown flag, an argument that is not the documented form, a missing required flag).
- **Process and live tests.** Unit and process tests (jobs, leases, crash recovery between each pair of confirm steps, concurrent commands, a killed `_job`) run in CI. Tests marked `live` (a real headless fork round, the hardened-git recipe against real git, a push from a shallow clean throwaway to a real forge, `extract` limits, throwaway pruning, the sandbox probe) are deselected by default and never run in CI.
- **Golden files.** Every `--json` output and every hook payload has a golden file validated against its schema; every refusal reason, error code and state has at least one test that produces it.
- **Spike regressions.** Each verified behavior in the design's live-verification table becomes a test: a process test against the fakes where the fake can model it, otherwise a `live` test. Unverified items keep their conservative default and a tracking issue.
- **Safety tests.** Adversarial inputs are fixtures from the first stage: hostile comment text (fence breakouts, control and invisible characters), forged agent ids and labels, planted config and hooks in the round throwaway, symlinks, hard links and special paths in the artifact, a binary or hidden change in the diff, a moved head, a deleted comment and a resolved thread at each confirm step.
- **`make verify`** (ruff, pyright, pytest, markdown lint) passes before every commit. CI runs the same target on Linux only, plus the schema checks.

## Module map

| Module | Stage | Responsibility |
|---|---|---|
| `cli.py`, `config.py` | 0 | commands, global flags, exit codes, error envelope, config load and validation |
| `gateway.py`, `store.py` | 0 | the call boundary; SQLite, migrations, compare-and-set, leases |
| `audit.py`, `hooks.py` | 0, 5 | audit and log files; hook running, outbox delivery |
| `detect.py` | 1 | listing, comment reads, eligibility, the handled set, settle |
| `rounds.py` | 2 | round states, due checks, lineage, job submission and finalizing, cleanup |
| `job.py`, `prompt.py`, `artifact.py` | 2 | the hidden `_job` command, fenced prompt and sanitizer, artifact validation |
| `pending.py`, `diffstore.py` | 3 | the pending record, stored diff and flags, edits, expiry |
| `hgit.py` | 4 | the hardened-git module |
| `confirm.py` | 4 | the confirm order, push recovery and replies |
| `cron.py`, `validate.py` | 5 | the managed crontab block, offline validation |
| `plugin/` | 6 | the bundled plugin: skill and reference marker |

## Stage 0: Scaffold, config, store, gateway

**Goal**: A runnable CLI skeleton with config, state and the call boundary, and every sibling fake.
**Success Criteria**: `--version`, `validate` (config and dependency checks against the fakes), global flags and the error envelope work; config errors name the key; migrations apply and a newer schema makes writes exit 12; leases and compare-and-set are tested; `audit.jsonl` and `log.jsonl` append with the documented fields.
**Tests**: config golden cases (every key, per-repo overrides, environment overrides, drop-ins); migration tests; lease takeover only for a verified-gone holder; log and audit schema validation; a fake that rejects an undocumented flag.
**Status**: Not Started

## Stage 1: Detection and eligibility

**Goal**: `check` lists your PRs, reads comments, applies eligibility and the handled-set rules and reports which PRs are due, without starting anything.
**Success Criteria**: per-host listing within the request budget, least recently read first; eligibility reasons recorded; edited, deleted and resolved comments handled as specified; first-sight rule per comment; settle clock from local first-seen times; `list` and `show` print the PR state.
**Tests**: fixtures from the digest of real forgectl output (ids, `is_bot: null`, `access` null, outdated threads); an edit that restores old text; a deleted comment; a host failure for one host; budget exhaustion.
**Status**: Not Started

## Stage 2: Rounds, job and artifact

**Goal**: A due PR gets a round: throwaway, prompt, forked session, validated artifact, recorded outcome.
**Success Criteria**: the round state table and the exit-code mapping of the run outcomes hold; a queued round whose job never ran is reconciled; fork lineage (base agent only changes on a confirmed round); cleanup of throwaways and forks; with and without jobsctl; `stop`; the prompt fence, the sanitizer and prompt limits; strict artifact validation (marker, ids, paths, sizes, `report.implicit`).
**Tests**: process tests with fake agent-spawn and wtctl for every outcome code; a killed `_job`; label forgery and unrecorded agent ids; a prompt with a fence breakout; each artifact failure; the `live` fork round (spike regression).
**Status**: Not Started

## Stage 3: Pending confirmations and stored diff

**Goal**: A validated round becomes a pending confirmation with a diff the user can read.
**Success Criteria**: clean checkout, extraction and hardened `add` and `diff --cached` produce the stored diff, its hash and the flags list; the record, `pending`, `show`, `edit`, `drop-reply`, `reject` (with `--redo`) and expiry follow the state tables; the content hash covers diff, message and replies.
**Tests**: golden diffs including binary, renames, case-folding and hidden-change fixtures; `extract` refusals (atomic) and its known gaps; edit by an automatic actor refused; expiry and closed-PR sweeps.
**Status**: Not Started

## Stage 4: Hardened git and confirm

**Goal**: `confirm` commits and pushes exactly what was reviewed, then posts replies.
**Success Criteria**: the hardened-git module with the two documented corrections; the confirm order with its intent states and re-entry from every state; push guards (head repo, default branch, exact ref match); `remote_moved` detection; the commit message reduction; replies with per-comment dedupe keys and each skip rule; thread resolution when enabled.
**Tests**: every vector of the recipe against real git (`live`); a kill between each pair of steps; two racing confirms; a head that moves; a deleted comment and a resolved thread; a push that lands after a timeout; the three rejection texts.
**Status**: Not Started

## Stage 5: Hooks, cron, validate, maintenance

**Goal**: The operational surface.
**Success Criteria**: the five hooks with payload schemas and outbox delivery; the managed crontab block with a golden test (the harness reproduces it); `validate` probes the sandbox and dependency versions; `export`, `prune` (never touching a live round or a pending confirmation).
**Tests**: hook veto and escalation outcomes; outbox redelivery after a crash; cron block replacement (idempotent, an end-less block refused); prune never removes a protected throwaway.
**Status**: Not Started

## Stage 6: Plugin, live suite, release readiness

**Goal**: A usable 0.x.
**Success Criteria**: the bundled `respond-to-review` skill and reference marker; a documented permission profile; the `live` suite passes on the maintainer's machines (Linux, WSL); a macOS checklist is written; README quick start verified end to end against real siblings.
**Tests**: the `live` suite; a manual end-to-end round on a real PR.
**Status**: Not Started

## Version roadmap

- **0.x** is stages 0 to 6. Any breaking change before 1.0 is a `feat` whose title names the break.
- **1.0** requires: the `live` suite green against released versions of forgectl, agent-spawn, pr-comment and wtctl without interface changes; a real round run end to end on the maintainer's machines; the macOS checklist done; a deliberate breaking-change commit declaring the interface stable.
- **1.x** (additive): the automatic modes with the narrowed rule set recorded in the design (behind an enforced gatectl policy, forking only the original agent, plain-text replies only, exact allowlists); fork PRs (needs a wtctl option to fetch a given ref and the head repository's clone URL from forgectl); review bodies as a comment source; interactive forks once verified; a `browse` TUI after a feature inventory of forgectl's; a per-round cost cap once agent-spawn reports cost; a recipe for wiring pr-watch events to `check`.
- **2.0**: nothing planned.
