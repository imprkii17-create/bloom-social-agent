"""The post format. One folder per post under posts/, described by post.yaml."""

from __future__ import annotations

import hashlib
from datetime import UTC, date, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator


class Status(StrEnum):
    DRAFT = "draft"  # being worked on; never published
    APPROVED = "approved"  # will be published at scheduled_at
    PUBLISHED = "published"  # done on every requested platform
    FAILED = "failed"  # gave up after max attempts; needs a human
    MISSED = "missed"  # approved too late / runner was down; not posted late on purpose
    SKIPPED = "skipped"  # intentionally not posted


class PostType(StrEnum):
    IMAGE = "image"
    CAROUSEL = "carousel"
    REEL = "reel"
    STORY = "story"


class Platform(StrEnum):
    INSTAGRAM = "instagram"
    FACEBOOK = "facebook"


class PlatformResult(BaseModel):
    model_config = ConfigDict(extra="allow")
    media_id: str
    permalink: str | None = None
    published_at: str


class PostResult(BaseModel):
    model_config = ConfigDict(extra="allow")
    instagram: PlatformResult | None = None
    facebook: PlatformResult | None = None
    attempts: int = 0
    last_error: str | None = None
    last_attempt_at: str | None = None
    # A container that was created but not yet published (e.g. a video still processing). Reused next run.
    ig_pending_container: str | None = None
    ig_pending_fingerprint: str | None = None  # hash of media + caption the container was made from
    ig_attempt_started_at: str | None = None  # UTC time we first tried; used to find our post after a lost answer
    fb_attempt_started_at: str | None = None  # set while a Facebook post is in flight; checked after unclear errors


SCHEDULE_FORMAT = "%Y-%m-%d %H:%M"


class Post(BaseModel):
    """A single scheduled post. Lives at posts/<id>/post.yaml."""

    model_config = ConfigDict(extra="forbid", use_enum_values=False)

    id: str
    status: Status = Status.DRAFT
    type: PostType = PostType.IMAGE
    platforms: list[Platform] = Field(default_factory=lambda: [Platform.INSTAGRAM])
    scheduled_at: str  # "YYYY-MM-DD HH:MM", in the configured local timezone
    media: list[str]
    caption_file: str | None = "caption.txt"
    alt_text: str | None = None
    cover: str | None = None  # optional reel cover image (relative path)
    share_to_feed: bool = True  # reels: also show in the main grid
    notes: str | None = None
    result: PostResult = Field(default_factory=PostResult)

    # Not serialised: where the post lives on disk.
    _folder: Path | None = None

    @field_validator("scheduled_at", mode="before")
    @classmethod
    def _check_schedule(cls, value: Any) -> str:
        """Accepts what people (and YAML) naturally write, and normalises to "YYYY-MM-DD HH:MM"."""
        if isinstance(value, datetime):
            return value.strftime(SCHEDULE_FORMAT)
        if isinstance(value, date):
            raise ValueError("scheduled_at needs a time too, e.g. 2026-10-19 18:30")
        text = str(value).strip()
        for fmt in (SCHEDULE_FORMAT, "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M", "%Y-%m-%dT%H:%M:%S", "%Y.%m.%d %H:%M"):
            try:
                return datetime.strptime(text, fmt).strftime(SCHEDULE_FORMAT)
            except ValueError:
                continue
        raise ValueError(f"scheduled_at must look like 2026-10-19 18:30 (got {text!r})")

    @field_validator("media")
    @classmethod
    def _check_media(cls, value: list[str]) -> list[str]:
        if not value:
            raise ValueError("media must list at least one file")
        for name in value:
            if name.startswith("/") or ".." in Path(name).parts:
                raise ValueError(f"media path must be relative to the post folder: {name!r}")
        return value

    # ---- convenience -------------------------------------------------------------------------------

    @property
    def folder(self) -> Path:
        if self._folder is None:
            raise RuntimeError("post was not loaded from disk")
        return self._folder

    def scheduled_dt(self, tz: ZoneInfo) -> datetime:
        return datetime.strptime(self.scheduled_at, SCHEDULE_FORMAT).replace(tzinfo=tz)

    def scheduled_utc(self, tz: ZoneInfo) -> datetime:
        """Always compare in UTC: aware datetimes sharing one tzinfo compare by wall clock, which breaks at DST."""
        return self.scheduled_dt(tz).astimezone(UTC)

    def fingerprint(self) -> str:
        """Changes whenever the media files, caption or alt text change."""
        h = hashlib.sha256()
        h.update(self.type.value.encode())
        for path in self.media_paths():
            h.update(path.name.encode())
            h.update(path.read_bytes() if path.is_file() else b"missing")
        h.update(self.caption().encode())
        h.update((self.alt_text or "").encode())
        return h.hexdigest()[:16]

    def caption(self) -> str:
        if not self.caption_file:
            return ""
        path = self.folder / self.caption_file
        if not path.is_file() and self.type == PostType.STORY:
            return ""
        return extract_caption(path.read_text(encoding="utf-8"))

    def media_paths(self) -> list[Path]:
        return [self.folder / m for m in self.media]

    def supported_platforms(self) -> list[Platform]:
        """Facebook publishing is limited to image/carousel posts; other types go to Instagram only."""
        return [
            p for p in self.platforms
            if not (p == Platform.FACEBOOK and self.type in (PostType.REEL, PostType.STORY))
        ]

    def remaining_platforms(self) -> list[Platform]:
        done = {p for p in Platform if getattr(self.result, p.value) is not None}
        return [p for p in self.supported_platforms() if p not in done]

    def to_yaml(self) -> str:
        data: dict[str, Any] = self.model_dump(mode="json", exclude_none=True)
        if self.type != PostType.REEL:
            data.pop("share_to_feed", None)  # only meaningful for reels
        result = data.get("result") or {}
        if result in ({}, {"attempts": 0}):
            data.pop("result", None)
        return yaml.safe_dump(data, sort_keys=False, allow_unicode=True, width=100)


CAPTION_START = "----- CAPTION (copy from here) -----"
CAPTION_END = "----- END CAPTION -----"


def extract_caption(text: str) -> str:
    """Accept either a plain caption file or the copy-paste files we generate (with START/END markers)."""
    if CAPTION_START in text:
        text = text.split(CAPTION_START, 1)[1]
        text = text.split(CAPTION_END, 1)[0]
    return text.strip()
