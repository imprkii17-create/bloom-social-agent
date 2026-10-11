"""Loading and saving the post queue (posts/<id>/post.yaml)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml
from pydantic import ValidationError

from .models import Post, Status


@dataclass
class LoadError:
    path: Path
    message: str


def load_post(path: Path) -> Post:
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    post = Post.model_validate(data)
    post._folder = path.parent
    if post.id != path.parent.name:
        raise ValueError(f"id {post.id!r} must match its folder name {path.parent.name!r}")
    return post


def load_queue(posts_dir: Path) -> tuple[list[Post], list[LoadError]]:
    posts: list[Post] = []
    errors: list[LoadError] = []
    if not posts_dir.is_dir():
        return posts, errors
    for path in sorted(posts_dir.glob("*/post.yaml")):
        try:
            posts.append(load_post(path))
        except (ValidationError, ValueError, yaml.YAMLError, OSError) as exc:
            errors.append(LoadError(path, _short_error(exc)))
    posts.sort(key=lambda p: (p.scheduled_at, p.id))
    return posts, errors


def save_post(post: Post) -> None:
    path = post.folder / "post.yaml"
    tmp = path.with_suffix(".yaml.tmp")
    tmp.write_text(post.to_yaml(), encoding="utf-8")
    tmp.replace(path)  # atomic on the same filesystem


def due_posts(posts: list[Post], now: datetime, tz: ZoneInfo) -> list[Post]:
    """Approved posts whose time has come. `now` must be timezone-aware."""
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    now_utc = now.astimezone(UTC)
    return [p for p in posts if p.status == Status.APPROVED and p.scheduled_utc(tz) <= now_utc]


def _short_error(exc: Exception) -> str:
    if isinstance(exc, ValidationError):
        parts = []
        for err in exc.errors():
            loc = ".".join(str(x) for x in err["loc"]) or "post"
            parts.append(f"{loc}: {err['msg']}")
        return "; ".join(parts)
    return str(exc)
