"""Builders for schema-valid sibling documents and the scenarios the safety tests share.

Every builder returns a plain dict that validates against the pinned schema (a test checks it), so a
fake can answer with it. Hosts and names are the reserved example domain, never real ones.
"""

from __future__ import annotations

from typing import Any

HOST = "git.example.test"
REPO = f"{HOST}/org/repo"
PR = f"{REPO}#7"
HEAD_A = "a" * 40
HEAD_B = "b" * 40
TS = "2026-01-01T12:00:00Z"
TS_LATER = "2026-01-01T13:00:00Z"
ME = {"login": "me-user", "id": "acct-me", "is_bot": False}
REVIEWER = {"login": "reviewer-one", "id": "acct-rev", "is_bot": False}


def forgectl_show(
    *, pr: str = PR, head_sha: str = HEAD_A, head_ref: str = "feat/x", state: str = "open"
) -> dict[str, Any]:
    repo, _, number = pr.partition("#")
    return {
        "pr": pr,
        "repo": repo,
        "repo_id": "R_kgDOexample",
        "pr_id": "PR_kwDOexample",
        "number": int(number),
        "url": f"https://{repo.split('/', 1)[0]}/{repo.split('/', 1)[1]}/pull/{number}",
        "backend": "github",
        "title": "A change",
        "body": "Body",
        "author": ME,
        "state": state,
        "draft": False,
        "created_at": TS,
        "updated_at": TS,
        "closed_at": None if state == "open" else TS_LATER,
        "merged_at": TS_LATER if state == "merged" else None,
        "merged_by": ME["login"] if state == "merged" else None,
        "labels": [],
        "author_association": "OWNER",
        "head": {"sha": head_sha, "ref": head_ref, "repo": repo, "is_fork": False},
        "base": {"ref": "main", "sha": "c" * 40, "is_default": True},
        "merged_head_sha": head_sha if state == "merged" else None,
        "merge_commit_sha": "d" * 40 if state == "merged" else None,
        "head_fetch_ref": f"refs/pull/{number}/head",
        "mergeable": True,
        "blocked_by": [],
        "auto_merge": {"enabled": False, "by": None},
        "merge_queue": False,
        "merge_methods_allowed": ["squash"],
        "additions": 1,
        "deletions": 0,
        "changed_files": 1,
        "review_decision": "none",
        "approvals": 0,
        "approvals_at_head": 0,
        "approvals_left": None,
    }


def comment(
    comment_id: str,
    body: str,
    *,
    author: dict[str, Any] | None = None,
    created_at: str = TS,
    updated_at: str | None = None,
    reply_to: str | None = None,
    minimized: bool = False,
) -> dict[str, Any]:
    return {
        "id": comment_id,
        "author": author or REVIEWER,
        "body": body,
        "created_at": created_at,
        "updated_at": updated_at or created_at,
        "minimized": minimized,
        "reply_to": reply_to,
        "review_id": None,
    }


def thread(
    thread_id: str,
    comments: list[dict[str, Any]],
    *,
    resolved: bool = False,
    resolvable: bool = True,
    outdated: bool | None = False,
    path: str | None = "src/app.py",
    line: int | None = 3,
    commit_sha: str | None = HEAD_A,
) -> dict[str, Any]:
    return {
        "id": thread_id,
        "resolvable": resolvable,
        "resolved": resolved and resolvable,
        "outdated": outdated,
        "path": path,
        "line": line,
        "side": "right" if path else None,
        "start_line": None,
        "commit_sha": commit_sha,
        "comments": comments,
    }


def forgectl_comments(
    threads: list[dict[str, Any]],
    *,
    pr: str = PR,
    head_sha: str = HEAD_A,
    complete: bool = True,
) -> dict[str, Any]:
    return {"pr": pr, "head_sha": head_sha, "threads": threads, "complete": complete}


def forgectl_whoami(*, login: str = "me-user") -> dict[str, Any]:
    return {
        "hosts": [
            {
                "host": HOST,
                "backend": "github",
                "login": login,
                "id": "acct-me",
                "me": login,
                "me_matches_login": True,
                "token_source": "token_env",
                "scopes": None,
            }
        ],
        "errors": [],
    }


def error_envelope(code: str, message: str, **extra: Any) -> dict[str, Any]:
    return {"error": {"code": code, "message": message, **extra}}


# -- scenarios ------------------------------------------------------------------------------------


def scenario_moved_head() -> tuple[dict[str, Any], dict[str, Any]]:
    """The PR before and after a push by someone else: same PR, new head sha."""
    return forgectl_show(head_sha=HEAD_A), forgectl_show(head_sha=HEAD_B)


def scenario_comment_lifecycle(
    body: str = "Please rename this.",
) -> dict[str, dict[str, Any]]:
    """One thread seen as it is created, edited, deleted and resolved (forgectl `comments`)."""
    cid, tid = "gh:rc:c1", "gh:rt:t1"
    return {
        "fresh": forgectl_comments([thread(tid, [comment(cid, body)])]),
        "edited": forgectl_comments(
            [thread(tid, [comment(cid, body + " (edited)", updated_at=TS_LATER)])]
        ),
        "edit_restored": forgectl_comments(
            [thread(tid, [comment(cid, body, updated_at=TS_LATER)])]
        ),
        "deleted_comment": forgectl_comments([]),
        "deleted_reply": forgectl_comments([thread(tid, [comment(cid, body)])], head_sha=HEAD_A),
        "resolved_thread": forgectl_comments([thread(tid, [comment(cid, body)], resolved=True)]),
        "incomplete_listing": forgectl_comments([], complete=False),
        "outdated_thread": forgectl_comments(
            [thread(tid, [comment(cid, body)], outdated=True, line=None)]
        ),
    }


# -- listing, identity and access (forgectl prs, whoami, access) ----------------------------------

HOST_B = "other.example.test"


def pr_ref(number: int, *, host: str = HOST, repo: str = "org/repo") -> str:
    return f"{host}/{repo}#{number}"


def pr_item(
    number: int,
    *,
    host: str = HOST,
    repo: str = "org/repo",
    state: str = "open",
    head_sha: str = HEAD_A,
    head_ref: str = "feat/x",
    is_fork: bool | None = False,
    head_repo: str | None = ...,  # type: ignore[assignment]
    created_at: str = TS,
    updated_at: str = TS,
    author: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """One `forgectl prs` item. `head_repo` defaults to the PR's repo (not a fork). The author is
    you: the account the tests configure for the host (`acct-me`, `acct-me-b` on `HOST_B`)."""
    full = f"{host}/{repo}"
    mine = author or (ME if host != HOST_B else {**ME, "id": "acct-me-b"})
    return {
        "pr": f"{full}#{number}",
        "pr_id": f"PR_kwDOexample{number}",
        "repo": full,
        "number": number,
        "url": f"https://{host}/{repo}/pull/{number}",
        "title": f"Change {number}",
        "author": mine,
        "state": state,
        "draft": False,
        "labels": [],
        "head": {
            "sha": head_sha,
            "ref": head_ref,
            "repo": full if head_repo is ... else head_repo,
            "is_fork": is_fork,
        },
        "base": {"ref": "main"},
        "created_at": created_at,
        "updated_at": updated_at,
        "repo_id": "R_kgDOexample",
    }


def forgectl_prs(
    items: list[dict[str, Any]],
    *,
    complete: bool = True,
    errors: list[dict[str, Any]] | None = None,
    host: str = HOST,
) -> dict[str, Any]:
    return {
        "items": items,
        "complete": complete,
        "errors": errors or [],
        "hosts_queried": [host],
        "rate_limit": {"host": host, "remaining": None, "reset_at": None},
    }


def host_error(
    host: str, code: str = "upstream_error", message: str = "host said no"
) -> dict[str, Any]:
    return {"host": host, "code": code, "message": message}


def forgectl_whoami_hosts(
    *hosts: str,
    ids: dict[str, str | None] | None = None,
    token_source: dict[str, str] | None = None,
    errors: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """`forgectl whoami` for several hosts; `token_source` maps a host to `none` and so on."""
    entries = []
    for host in hosts:
        account = (ids or {}).get(host, f"acct-me-{host.split('.')[0]}")
        entries.append(
            {
                "host": host,
                "backend": "github",
                "login": "me-user",
                "id": account,
                "me": "me-user",
                "me_matches_login": True,
                "token_source": (token_source or {}).get(host, "token_env"),
                "scopes": None,
            }
        )
    return {"hosts": entries, "errors": errors or []}


def forgectl_access(
    *, write_access: bool | None, user: str = "reviewer-one", repo: str = REPO
) -> dict[str, Any]:
    """`forgectl access`; `write_access: None` is the unknown answer (consumers treat it as no)."""
    known = write_access is not None
    return {
        "repo": repo,
        "user": user,
        "write_access": write_access,
        "level": ("write" if write_access else "read") if known else None,
        "source": "collaborator_permission",
        "reason": None if known else "no_permission_to_read",
    }


def real_shape_comments(pr: str = PR) -> dict[str, Any]:
    """`forgectl comments` as a real GitHub host returned it (spike V-11), with the values that
    break naive code: `is_bot: null`, a null author login and id, an outdated thread with no line,
    an unresolvable thread, a comment minimized by a moderator and a reply chain."""
    null_author = {"login": None, "id": None, "is_bot": None}
    unknown_bot = {"login": "some-app", "id": "app-1", "is_bot": None}
    return forgectl_comments(
        [
            thread(
                "gh:rt:PRRT_kwDOabc-1",
                [
                    comment("gh:rc:PRRC_kwDOabc-1", "Please rename `foo`.", author=REVIEWER),
                    comment(
                        "gh:rc:PRRC_kwDOabc-2", "Done?", author=ME, reply_to="gh:rc:PRRC_kwDOabc-1"
                    ),
                ],
            ),
            thread(
                "gh:rt:PRRT_kwDOabc-2",
                [comment("gh:rc:PRRC_kwDOabc-3", "stale remark", author=unknown_bot)],
                outdated=True,
                line=None,
                commit_sha=HEAD_B,
            ),
            thread(
                "gh:ic:IC_kwDOabc-4",
                [comment("gh:ic:IC_kwDOabc-4", "ghost wrote this", author=null_author)],
                resolvable=False,
                path=None,
                line=None,
                commit_sha=None,
            ),
            thread(
                "gh:rt:PRRT_kwDOabc-5",
                [comment("gh:rc:PRRC_kwDOabc-5", "spam", minimized=True)],
                resolved=True,
            ),
        ],
        pr=pr,
    )
