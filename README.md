# review-respond

Respond to review comments on your own pull requests, with fixes and replies you confirm.

## Status

Pre-design: interface not yet specified; see docs/design.md.

## Layer

L3 (workflow)

## What it does

- Detects new review comments on PRs you authored (through a pr-watch query watch it owns); only comments from collaborators with write access or configured reviewers trigger a respond.
- Resumes the PR's agent as a fork (first round) or its own earlier fork, with the eligible comments fenced in a prompt file; never sends untrusted comment text to the original implement session.
- The agent commits locally (it cannot push) and reports replies and commits, which become a pending confirmation.
- `confirm <id>` verifies the worktree HEAD and repo config, pushes fast-forward only to the PR head ref (audited), then posts the replies via pr-comment.
- Optional auto-push and auto-reply behind an enforced gatectl policy and built-in hard rules (never push a diff that touches an off-limits path, never push unless CI on the previous PR head is green); respond rounds per PR are capped. Off-limits paths are a configurable glob list whose default covers security-sensitive code, CI and build pipeline definitions, agent configuration and instruction files, and review-ownership files (the same default list and format as review-watch's).

## Indicative commands

Indicative, finalized in `docs/interface.md`.

```text
review-respond check
review-respond respond <pr> [--skill ...] [--mode ...]
review-respond list
review-respond pending
review-respond confirm <id>  (alias: approve)
review-respond reject <id>
```

## Dependencies

- `pr-watch` (query watch and events); binding: TBD (A/B/C), decided in this tool's design session
- `forgectl` (PR facts, collaborator checks, CI status); binding: TBD (A/B/C), decided in this tool's design session
- `agent-spawn` (finds, resumes, forks or launches respond agents); binding: TBD (A/B/C), decided in this tool's design session
- `pr-comment` (posts replies); binding: TBD (A/B/C), decided in this tool's design session
- `git` (audited fast-forward push on confirm); binding: TBD (A/B/C), decided in this tool's design session
- `jobsctl` (optional; without it the job command runs directly under an internal cap); binding: TBD (A/B/C), decided in this tool's design session
- `gatectl` (optional; auto-push / auto-reply decisions); binding: TBD (A/B/C), decided in this tool's design session

## Install

```sh
uv tool install git+https://github.com/agent-workflow-kit/review-respond
```

## Documentation

- System architecture: [`../awkit-docs/architecture.md`](../awkit-docs/architecture.md) ([published](https://github.com/agent-workflow-kit/awkit-docs/blob/main/architecture.md))
- Tool design guide: [`../awkit-docs/design-guide.md`](../awkit-docs/design-guide.md) ([published](https://github.com/agent-workflow-kit/awkit-docs/blob/main/design-guide.md))
- This tool's design: [`docs/design.md`](docs/design.md)
- This tool's interface: [`docs/interface.md`](docs/interface.md)

## License

MIT — see LICENSE.
