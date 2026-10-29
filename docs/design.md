# review-respond: design

Status: Seeded from awkit architecture v1.4; not yet designed.

Section references (§N) point to `../awkit-docs/architecture.md` unless marked "design guide" (`../awkit-docs/design-guide.md`).

## Role and layer

Respond to review comments on your own pull requests, with fixes and replies you confirm.

Layer 3 (workflow). Must work fully standalone; no upward awareness.

Skill-wrapper tool (§5.5, design guide §11). Default slot: `respond-to-review` (bundled as `review-respond:respond-to-review` in `plugin/`).

## Responsibilities

From §5.3 (`review-respond`), verbatim:

Respond to review comments on your PRs.

- Commands: `check`, `respond <pr> [--skill ...] [--mode ...]`, `list`, `pending`, `confirm <id>` (alias `approve`), `reject <id>`.
- Uses: pr-watch, forgectl, agent-spawn, pr-comment, `git`; optionally jobsctl (without it, runs its job command directly under an internal cap); optionally gatectl (auto-push / auto-reply decisions).
- Notes:
  - "Your PRs" are PRs authored by the user. Only comments from repo collaborators with write access or from configured reviewers trigger a respond; other comments notify only. Ignores the user's own comments by default (opt-in: `respond_to_own_comments_prefix: "/awkit"`), comments carrying the `<!-- pr-comment:v1 -->` marker on content from the user's own account, and configured bot authors. All comment text is untrusted and fenced.
  - `check` ensures that the query watch `pr-watch watch add --mine --open --owner review-respond` exists, reads events since its own cursor, keeps a per-PR comment watermark, and resolves held respond jobs.
  - Finds the agent with `agent-spawn list --label pr=<pr> --json`. If several agents match, it prefers its own respond agent (`owner=review-respond`: an earlier fork or a fresh session), then the one whose `owner` label equals config `prefer_owner` (default `ticket-work`), else the most recent. It does not enqueue while any agent working in that worktree (the original or its respond fork) is live.
  - **Forked respond sessions.** The first respond round on an agent it does not own resumes it with `--fork` (section 9.4): Claude Code forks the conversation (`claude --resume <id> --fork-session`), the fork becomes a new respond agent labeled `pr=<pr>` and `owner=review-respond` in the same worktree, and the original implement session never receives untrusted comment text. Later rounds resume the fork without `--fork`. The fork is tainted (section 8.5). Forks are torn down when the PR is merged or closed (they never own the worktree, so teardown does not remove it).
  - One job per PR (queue `respond`, `--unique-key respond:<pr>`). The job runs review-respond's internal job command, which runs `agent-spawn resume <agent_id> [--fork --label pr=<pr> --label owner=review-respond] --kind respond --prompt-file <fenced "new review comments" file> --permission-profile <bundled file> --env-allowlist-only <vars> --wait`, then records the pending confirmation and marks the delivered comment ids handled. Comments are **delivered** once the resumed session reached `working`. The prompt file contains only comments whose author passes the trigger eligibility check above; other comments are never shown to the session.
  - The respond kind profile denies `git push` and `gh`/`glab` writes (public default; section 13.1). The agent commits locally and reports an artifact `{"replies": [{"comment_id", "body"}], "commits": ["<sha>", ...]}`, which becomes a pending confirmation. `confirm <id>` requires the worktree HEAD to equal the last element of `commits` and the agent's repo config to be unchanged (`repo_config_changed` false in `agent-spawn show --json`, else exit 20, `reason: repo_config_changed`), pushes fast-forward only to the PR head ref (section 13.2 rule 10; audited), then posts the replies via pr-comment. If the remote head moved, the confirmation stays pending with `reason: remote_moved` (exit 20).
  - **Auto-push and auto-reply** (opt-in) are automatic actions: they require an enforced allowing gate (section 12.3) and these built-in hard rules: never push a diff that touches the off-limits paths (the same default list and format as review-watch's, configured in each tool), and never push unless CI on the previous PR head is green. Otherwise the pending confirmation stays for the user.
  - Fresh sessions (no original agent) only for opted-in PRs (`repos.<id>.respond_fresh: true` or a PR label), never for PRs from forks. They use `--worktree new --branch <head-ref>` and label `owner=review-respond`, and are torn down when the PR is merged or closed.
  - Respond rounds per PR are capped (`max_rounds`, default 3), then it escalates.
  - Hooks (indicative): `post-pending`.

## Uses

- `pr-watch` (query watch and events); binding: TBD (A/B/C), decided in this tool's design session
- `forgectl` (PR facts, collaborator checks, CI status); binding: TBD (A/B/C), decided in this tool's design session
- `agent-spawn` (finds, resumes, forks or launches respond agents); binding: TBD (A/B/C), decided in this tool's design session
- `pr-comment` (posts replies); binding: TBD (A/B/C), decided in this tool's design session
- `git` (audited fast-forward push on confirm); binding: TBD (A/B/C), decided in this tool's design session
- `jobsctl` (optional; without it the job command runs directly under an internal cap); binding: TBD (A/B/C), decided in this tool's design session
- `gatectl` (optional; auto-push / auto-reply decisions); binding: TBD (A/B/C), decided in this tool's design session

## Key flow: §10.3 Review response (flow C)

Verbatim from the architecture:

1. pr-watch detects a new comment, review comment or review on your PR (through the query watch owned by review-respond).
2. A harness rule (or cron running `review-respond check`) runs `review-respond respond <pr>`. Only comments from collaborators with write access or configured reviewers trigger it; your own comments, comments carrying the awkit marker and configured bot comments are ignored; the round cap applies.
3. It finds the agent with `agent-spawn list --label pr=... --json` (preferring its own respond fork, then `prefer_owner`, else the most recent) and checks that no agent in that worktree is live. It enqueues one job per PR (queue `respond`, `--unique-key respond:<pr>`; without jobsctl it runs the job command directly) whose job command runs `agent-spawn resume <agent_id> --kind respond --prompt-file <fenced "new review comments" file> --permission-profile <bundled file> --env-allowlist-only <vars> --wait`, adding `--fork --label pr=<pr> --label owner=review-respond` on the first round so the comments go to a fork, never to the original implement session (section 5.3). The prompt file holds only comments from eligible authors. The session runs in the original agent's mode and worktree, and the comments count as delivered once it reaches `working`. If no agent exists and the PR is opted in (and not from a fork), it launches a fresh respond-to-review session with `--worktree new --branch <head-ref> --label owner=review-respond`.
4. The agent commits fixes locally (it cannot push) and reports replies and commits. The job command records a pending confirmation (`post-pending`) and marks the delivered comment ids handled. You run `review-respond confirm <id>`, which checks that the worktree HEAD equals the last reported commit and that the repo config is unchanged since launch, pushes fast-forward only to the PR head ref (refusing with `remote_moved` if the remote head changed), and posts the replies via pr-comment.

## Relevant cross-cutting sections

- §3.2, §3.3: layering rules and edge bindings
- §5.5: capability slots: `respond-to-review`, kind `respond`
- §8.5: resume, fork and taint
- §9.3, §9.4: post-exit policies; resume, attach and fork
- §10.3: review response flow (flow C)
- §12: autonomy and gates: public defaults, gates (§12.3), pending confirmations (§12.4)
- §13: security: respond profile (§13.1), trust tiers (§13.2 rule 7), repository-controlled config and pushes (§13.2 rule 10)
- §14: audit (the audited push on confirm) and the `<!-- pr-comment:v1 -->` marker
- design guide §4.4, §11, §12: confirmations; skill-wrapper pattern; safety

## Constraints (decided)

From §20 (`review-respond`):

a round is one successful resume or launch delivering at least one new comment (delivered = the resumed session reached `working`); the comment watermark is stored per PR in review-respond; before enqueueing it checks whether the agent is live and does not enqueue while live; comment ids are marked handled only after delivery. The respond permission profile is generated per call from the bundled allowlist profile plus the repo's configured test and build commands. The first round on a non-owned agent always uses `--fork`.

Packaging: The bundled plugin is shipped inside the package and located via `importlib.resources.files('review_respond') / 'plugin'`.

## Open topics

From §20 (`review-respond`):

detection details, reply drafting, pending-confirmation format, auto-push/auto-reply policy input.

## Decisions

| Decision | Rationale | Date |
|---|---|---|

## Questions for the design session

1. How exactly are new comments detected (events, cursor, watermark interplay, edited and deleted comments)?
2. How are replies drafted and attached to the right comments and threads?
3. What is the pending-confirmation format (fields beyond the required set, display)?
4. What input drives the auto-push / auto-reply policy (gate payload contents)?
