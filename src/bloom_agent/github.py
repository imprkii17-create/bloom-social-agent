"""Small GitHub REST helpers: open/update an alert issue, and rotate an Actions secret."""

from __future__ import annotations

import base64
import logging
from typing import Any

import httpx
from nacl import encoding, public

log = logging.getLogger(__name__)
API = "https://api.github.com"


class GitHubError(RuntimeError):
    pass


def _client(token: str, transport: httpx.BaseTransport | None = None) -> httpx.Client:
    return httpx.Client(
        base_url=API,
        timeout=30,
        transport=transport,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "bloom-agent/1.0",
        },
    )


def _check(resp: httpx.Response, what: str) -> Any:
    if resp.status_code >= 400:
        raise GitHubError(f"GitHub {what} failed: HTTP {resp.status_code} {resp.text[:300]}")
    return resp.json() if resp.content else None


def open_or_update_issue(
    repo: str, token: str, title: str, body: str, *, label: str = "bloom-agent",
    transport: httpx.BaseTransport | None = None,
) -> str:
    """Opens an issue (GitHub emails/notifies the owner). If an open one with the same title exists, comments on it."""
    return report_issue(repo, token, title, body, label=label, transport=transport)[0]


def report_issue(
    repo: str, token: str, title: str, body: str, *, label: str = "bloom-agent",
    transport: httpx.BaseTransport | None = None,
) -> tuple[str, bool]:
    """Like open_or_update_issue, and also says whether anything new was written (False = same alert as before)."""
    with _client(token, transport) as gh:
        existing = _check(
            gh.get(f"/repos/{repo}/issues", params={"state": "open", "labels": label, "per_page": 50}),
            "list issues",
        )
        for issue in existing or []:
            if issue.get("title") == title and "pull_request" not in issue:
                if _latest_text(gh, repo, issue) != body:  # the same alert every 15 minutes would be spam
                    _check(gh.post(f"/repos/{repo}/issues/{issue['number']}/comments", json={"body": body}), "comment")
                    return str(issue["html_url"]), True
                return str(issue["html_url"]), False
        created = _check(
            gh.post(f"/repos/{repo}/issues", json={"title": title, "body": body, "labels": [label]}),
            "create issue",
        )
        return str(created["html_url"]), True


def _latest_text(gh: httpx.Client, repo: str, issue: dict[str, Any]) -> str:
    if not issue.get("comments"):
        return str(issue.get("body") or "")
    last_page = max(1, -(-int(issue["comments"]) // 100))
    comments = _check(
        gh.get(f"/repos/{repo}/issues/{issue['number']}/comments", params={"per_page": 100, "page": last_page}),
        "list comments",
    )
    return str(comments[-1].get("body") or "") if comments else str(issue.get("body") or "")


def encrypt_secret(public_key_b64: str, value: str) -> str:
    key = public.PublicKey(public_key_b64.encode("utf-8"), encoding.Base64Encoder)
    sealed = public.SealedBox(key).encrypt(value.encode("utf-8"))
    return base64.b64encode(sealed).decode("utf-8")


def set_actions_secret(
    repo: str, admin_token: str, name: str, value: str, *, transport: httpx.BaseTransport | None = None
) -> None:
    with _client(admin_token, transport) as gh:
        key = _check(gh.get(f"/repos/{repo}/actions/secrets/public-key"), "read secrets public key")
        payload = {"encrypted_value": encrypt_secret(key["key"], value), "key_id": key["key_id"]}
        resp = gh.put(f"/repos/{repo}/actions/secrets/{name}", json=payload)
        if resp.status_code not in (201, 204):
            raise GitHubError(f"GitHub update secret failed: HTTP {resp.status_code} {resp.text[:300]}")


class PullRequestsNotAllowed(GitHubError):
    """The repository doesn't let GitHub Actions open pull requests (a checkbox in Settings → Actions)."""


def create_or_update_pr(
    repo: str, token: str, *, head: str, base: str, title: str, body: str,
    transport: httpx.BaseTransport | None = None,
) -> str:
    """Opens a pull request from `head`, or updates the title/body of the open one. Returns its URL."""
    owner = repo.split("/", 1)[0]
    with _client(token, transport) as gh:
        existing = _check(
            gh.get(f"/repos/{repo}/pulls", params={"head": f"{owner}:{head}", "state": "open"}), "list pull requests"
        )
        if existing:
            number = existing[0]["number"]
            _check(gh.patch(f"/repos/{repo}/pulls/{number}", json={"title": title, "body": body}), "update pull request")
            return str(existing[0]["html_url"])
        resp = gh.post(
            f"/repos/{repo}/pulls",
            json={"title": title, "body": body, "head": head, "base": base, "maintainer_can_modify": True},
        )
        if resp.status_code == 403:
            raise PullRequestsNotAllowed(
                "GitHub Actions may not open pull requests in this repository. Turn on Settings → Actions → General → "
                "'Allow GitHub Actions to create and approve pull requests'."
            )
        return str(_check(resp, "create pull request")["html_url"])


def compare_url(repo: str, base: str, head: str) -> str:
    return f"https://github.com/{repo}/compare/{base}...{head}?expand=1"
