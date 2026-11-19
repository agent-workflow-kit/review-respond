# review-respond

Respond to review comments on your own pull requests, with fixes and replies you confirm.

## Status

Designed; not yet implemented. The interface is [`docs/interface.md`](docs/interface.md) (version 0.1.0-design) and the design is [`docs/design.md`](docs/design.md).

## Layer

L3 (workflow)

## What it does

- Finds your open PRs on the hosts you configure and reads their comments through forgectl. Only comments from reviewers you list or from accounts with write access start work; your own comments, bots and everyone else's are recorded and never shown to the agent. Review bodies are not read in 0.x (a review's inline comments are), and a PR whose head lives in a fork gets no round of any kind.
- Waits for a short quiet period, then runs one round per PR: it creates a throwaway checkout at the PR head and forks the PR's agent session into it, with the eligible comments as fenced data. The fork cannot stage, commit, push or reach the network.
- The fork edits files and reports replies, the files it changed or deleted and a proposed commit message. That becomes a pending confirmation with the diff computed in a clean checkout.
- `confirm` acts only on what you saw: it rebuilds the clean checkout, refuses if the diff changed, commits with your identity through hardened git, pushes fast-forward only to the PR head ref and then posts the replies through pr-comment.
- Every reply and push needs your `confirm`. Automatic replies and pushes are planned for a later version.

## Commands

```text
review-respond check
review-respond respond <pr> [--force] [--include-existing] [--note-file F]
review-respond list
review-respond show <pr | round | pending id>
review-respond pending
review-respond confirm <id>  (alias: approve)
review-respond reject <id> [--redo]
review-respond edit <id> [--reply COMMENT_ID --body-file F] [--message-file F]
review-respond drop-reply <id> <comment_id>
review-respond stop <round>
review-respond cron install|uninstall
review-respond export | prune | validate
```

All commands support `--json`. Free text comes from files or stdin. The full contract, exit codes (including 22 `nothing_to_respond`) and refusal reasons are in [`docs/interface.md`](docs/interface.md).

## Dependencies

- `forgectl` (reads; binding A): `uv tool install git+https://github.com/agent-workflow-kit/forgectl`
- `agent-spawn` (finds and forks agents; binding A): `uv tool install git+https://github.com/agent-workflow-kit/agent-spawn`
- `pr-comment` (posts replies; binding A): `uv tool install git+https://github.com/agent-workflow-kit/pr-comment`
- `wtctl` (throwaway checkouts and file extraction; binding A): `uv tool install git+https://github.com/agent-workflow-kit/wtctl`
- `git` 2.34 or newer (hardened commit and push; binding A)
- `claude-session` and Claude Code, through agent-spawn, with `bubblewrap` and `socat` on Linux and WSL (the respond kind needs the sandbox)
- `jobsctl` (optional, binding A): without it a detached job runs under an internal cap

A missing or too-old sibling stops the command with a message naming its install command. The minimum versions are in the interface's Dependencies table.

## Setup

review-respond keeps a throwaway checkout for every pending confirmation. Keep wtctl from removing them by setting these in wtctl's config: `prune.protect_labels` includes `app`, and `throwaway.max_age` is null or longer than review-respond's `pending_ttl`. `review-respond validate` does not check wtctl's configuration; a removed throwaway makes `confirm` exit 12 `throwaway_missing`, and `reject --redo` starts again.

## Install

```sh
uv tool install git+https://github.com/agent-workflow-kit/review-respond
```

## Configuration

`~/.config/review-respond/config.yaml` (plus `config.d/*.yaml`), documented key by key in `config.example.yaml`. Credentials for the push are named in config (`hosts.<host>.token_env` or `token_command`) and read from the environment. Environment overrides use `REVIEW_RESPOND_<KEY>` for top-level scalars.

## Hooks

`pre-round`, `pre-push`, `post-pending`, `post-attention`, `post-confirm`. Payloads carry ids, shas, counts and enums, never comment or diff text.

## Documentation

- System architecture: [`../awkit-docs/architecture.md`](../awkit-docs/architecture.md) ([published](https://github.com/agent-workflow-kit/awkit-docs/blob/main/architecture.md))
- Tool design guide: [`../awkit-docs/design-guide.md`](../awkit-docs/design-guide.md) ([published](https://github.com/agent-workflow-kit/awkit-docs/blob/main/design-guide.md))
- This tool's design: [`docs/design.md`](docs/design.md)
- This tool's interface: [`docs/interface.md`](docs/interface.md)

## License

MIT — see LICENSE.
