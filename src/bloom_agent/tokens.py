"""Keeps the Instagram token alive. Long-lived tokens last 60 days; refreshing resets the clock.

Meta's rules: the token must be at least 24 hours old and not yet expired. We refresh weekly, store the new
token back into the IG_ACCESS_TOKEN GitHub secret, and record the expiry date (never the token) in the repo.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx

from .graph import GraphError

REFRESH_URL = "https://graph.instagram.com/refresh_access_token"
STATUS_FILE = "state/token-status.json"


@dataclass(frozen=True)
class RefreshedToken:
    access_token: str
    expires_at: datetime


def refresh_instagram_token(token: str, *, transport: httpx.BaseTransport | None = None) -> RefreshedToken:
    with httpx.Client(timeout=30, transport=transport) as http:
        try:
            resp = http.get(REFRESH_URL, params={"grant_type": "ig_refresh_token", "access_token": token})
        except httpx.HTTPError as exc:
            raise GraphError(f"Network error refreshing token: {type(exc).__name__}", transient=True) from None
    try:
        payload = resp.json()
    except ValueError:
        payload = {}
    if resp.status_code >= 400 or "access_token" not in payload:
        err = payload.get("error", {}) if isinstance(payload, dict) else {}
        message = str(err.get("message") or f"HTTP {resp.status_code}").replace(token, "***")
        raise GraphError(f"Token refresh failed: {message}", status=resp.status_code, code=err.get("code"))
    expires_in = int(payload.get("expires_in") or 60 * 24 * 3600)
    return RefreshedToken(
        access_token=str(payload["access_token"]),
        expires_at=datetime.now(UTC) + timedelta(seconds=expires_in),
    )


def write_status(repo_root: Path, expires_at: datetime) -> Path:
    path = repo_root / STATUS_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "refreshed_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "expires_at": expires_at.isoformat(timespec="seconds"),
    }
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return path


def days_left(repo_root: Path) -> int | None:
    path = repo_root / STATUS_FILE
    if not path.is_file():
        return None
    try:
        expires = datetime.fromisoformat(json.loads(path.read_text(encoding="utf-8"))["expires_at"])
    except (ValueError, KeyError, json.JSONDecodeError):
        return None
    return (expires - datetime.now(UTC)).days
