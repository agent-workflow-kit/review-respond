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
        "merged_at": None,
        "merged_by": None,
        "labels": [],
        "author_association": "OWNER",
        "head": {"sha": head_sha, "ref": head_ref, "repo": repo, "is_fork": False},
        "base": {"ref": "main", "sha": "c" * 40, "is_default": True},
        "merged_head_sha": None,
        "merge_commit_sha": None,
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
