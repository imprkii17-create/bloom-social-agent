"""Checks every post before anything is published. Errors block publishing; warnings are advice."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from PIL import Image

from .config import Settings
from .facts import Facts, check_text, load_facts
from .models import Platform, Post, PostType, Status
from .queue import LoadError

IMAGE_EXT = {".jpg", ".jpeg"}
CONVERTIBLE_EXT = {".png", ".webp"}
VIDEO_EXT = {".mp4", ".mov"}
MAX_CAPTION = 2200
MAX_FEED_POSTS_PER_DAY = 3  # morning, afternoon, evening
MAX_HASHTAGS = 5  # Instagram limit since Dec 2025
MAX_MENTIONS = 20
MAX_IMAGE_BYTES = 8 * 1024 * 1024
MAX_VIDEO_BYTES = 95 * 1024 * 1024  # stay under GitHub's 100 MB file limit
FEED_MIN_RATIO, FEED_MAX_RATIO = 4 / 5, 1.91

HASHTAG_RE = re.compile(r"(?<![\w&])#\w+", re.UNICODE)
MENTION_RE = re.compile(r"(?<![\w.])@[\w.]+", re.UNICODE)


@dataclass(frozen=True)
class Issue:
    level: str  # "error" | "warning"
    post_id: str
    message: str

    def __str__(self) -> str:
        icon = "✖" if self.level == "error" else "!"
        return f"{icon} [{self.post_id}] {self.message}"


def count_hashtags(caption: str) -> int:
    return len(HASHTAG_RE.findall(caption))


def find_banned(caption: str, banned: tuple[str, ...]) -> list[str]:
    hits = []
    lowered = caption.lower()
    for word in banned:
        pattern = r"(?<![\w])" + re.escape(word.lower()) + r"(?![\w])"
        if re.search(pattern, lowered):
            hits.append(word)
    return hits


def validate_post(
    post: Post, settings: Settings, now: datetime | None = None, facts: Facts | None = None
) -> list[Issue]:
    issues: list[Issue] = []

    def err(msg: str) -> None:
        issues.append(Issue("error", post.id, msg))

    def warn(msg: str) -> None:
        issues.append(Issue("warning", post.id, msg))

    if post.status in (Status.PUBLISHED, Status.SKIPPED):
        return issues

    # ---- media ------------------------------------------------------------------------------------
    paths = post.media_paths()
    for path in paths:
        if not path.is_file():
            err(f"media file not found: {path.name}")
    if any(not p.is_file() for p in paths):
        return issues

    exts = [p.suffix.lower() for p in paths]
    if post.type == PostType.IMAGE:
        if len(paths) != 1:
            err("an image post needs exactly 1 file (use type: carousel for more)")
        _check_images(post, paths, issues, feed=True)
    elif post.type == PostType.CAROUSEL:
        if not 2 <= len(paths) <= 10:
            err(f"a carousel needs 2–10 files, found {len(paths)}")
        _check_images(post, [p for p in paths if p.suffix.lower() not in VIDEO_EXT], issues, feed=True)
        _check_videos(post, [p for p in paths if p.suffix.lower() in VIDEO_EXT], issues)
    elif post.type == PostType.REEL:
        if len(paths) != 1 or exts[0] not in VIDEO_EXT:
            err("a reel needs exactly 1 video file (.mp4 or .mov)")
        _check_videos(post, [p for p in paths if p.suffix.lower() in VIDEO_EXT], issues)
        if post.cover and not (post.folder / post.cover).is_file():
            err(f"cover image not found: {post.cover}")
    elif post.type == PostType.STORY:
        if len(paths) != 1:
            err("a story needs exactly 1 file")
        elif exts[0] in VIDEO_EXT:
            _check_videos(post, paths, issues)
        else:
            _check_images(post, paths, issues, feed=False)

    # ---- caption ----------------------------------------------------------------------------------
    caption = ""
    if post.caption_file:
        cap_path = post.folder / post.caption_file
        if cap_path.is_file():
            caption = post.caption()
        elif post.type != PostType.STORY:  # stories have no caption, so a missing file is fine there
            err(f"caption file not found: {post.caption_file}")
    if post.type != PostType.STORY:
        if not caption:
            warn("caption is empty")
        if len(caption) > MAX_CAPTION:
            err(f"caption is {len(caption)} characters; Instagram allows {MAX_CAPTION}")
        tags = count_hashtags(caption)
        if tags > MAX_HASHTAGS:
            err(f"caption has {tags} hashtags; Instagram allows at most {MAX_HASHTAGS}")
        mentions = len(MENTION_RE.findall(caption))
        if mentions > MAX_MENTIONS:
            err(f"caption has {mentions} @mentions; the limit is {MAX_MENTIONS}")
        banned = find_banned(caption + " " + (post.alt_text or ""), settings.banned_words)
        if banned:
            err("caption/alt text contains words that risk account restrictions: " + ", ".join(sorted(set(banned))))
        if caption and "18" not in caption:
            warn("caption doesn't say 18+ (brand rule: every post is clearly for adults)")
        if facts is not None:
            for fact_issue in check_text(caption, facts):
                warn(fact_issue.message)
    elif caption:
        warn("stories don't show captions; the caption will be ignored")

    if post.alt_text and post.type != PostType.IMAGE:
        warn("alt text is only sent for single-image posts")
    if post.type == PostType.IMAGE and not post.alt_text:
        warn("no alt_text set (recommended for accessibility)")

    # ---- platforms --------------------------------------------------------------------------------
    if Platform.FACEBOOK in post.platforms and post.type in (PostType.REEL, PostType.STORY):
        warn("Facebook publishing supports image and carousel posts only; facebook will be skipped for this post")
    if len(set(post.platforms)) != len(post.platforms):
        err("platforms are listed twice")
    if not post.supported_platforms():
        err("no platform can publish this post (check platforms: and type:)")

    # ---- schedule ---------------------------------------------------------------------------------
    if post.status == Status.APPROVED and now is not None:
        late = (now - post.scheduled_dt(settings.timezone)).total_seconds() / 60
        if late > settings.max_lateness_minutes:
            warn(
                f"scheduled {int(late)} min ago; it will be marked 'missed' instead of posting late "
                "(change scheduled_at to post it)"
            )
    return issues


def _check_images(post: Post, paths: list[Path], issues: list[Issue], *, feed: bool) -> None:
    for path in paths:
        ext = path.suffix.lower()
        if ext in CONVERTIBLE_EXT:
            issues.append(Issue("error", post.id, f"{path.name}: Instagram only accepts JPEG. Run `bloom prepare`."))
            continue
        if ext not in IMAGE_EXT:
            issues.append(Issue("error", post.id, f"{path.name}: unsupported image type {ext}"))
            continue
        if path.stat().st_size > MAX_IMAGE_BYTES:
            issues.append(Issue("error", post.id, f"{path.name}: larger than 8 MB"))
        try:
            with Image.open(path) as im:
                w, h = im.size
                mode = im.mode
        except OSError:
            issues.append(Issue("error", post.id, f"{path.name}: not a readable image"))
            continue
        if mode not in ("RGB", "L"):
            issues.append(Issue("error", post.id, f"{path.name}: colour mode {mode}; run `bloom prepare`"))
        ratio = w / h
        if feed and not (FEED_MIN_RATIO - 0.01 <= ratio <= FEED_MAX_RATIO + 0.01):
            issues.append(
                Issue("error", post.id, f"{path.name}: aspect ratio {w}x{h} is outside Instagram's 4:5 to 1.91:1 range")
            )
        if not feed and abs(ratio - 9 / 16) > 0.02:
            issues.append(Issue("warning", post.id, f"{path.name}: stories look best at 9:16 (1080x1920)"))
        if w < 320:
            issues.append(Issue("warning", post.id, f"{path.name}: only {w}px wide; aim for 1080px"))


def _check_videos(post: Post, paths: list[Path], issues: list[Issue]) -> None:
    for path in paths:
        if path.suffix.lower() not in VIDEO_EXT:
            issues.append(Issue("error", post.id, f"{path.name}: videos must be .mp4 or .mov"))
        elif path.stat().st_size > MAX_VIDEO_BYTES:
            issues.append(Issue("error", post.id, f"{path.name}: video over 95 MB (GitHub's limit is 100 MB)"))


def validate_all(
    posts: list[Post], load_errors: list[LoadError], settings: Settings, now: datetime | None = None
) -> list[Issue]:
    issues = [Issue("error", e.path.parent.name, f"post.yaml: {e.message}") for e in load_errors]
    facts = load_facts(settings.repo_root) if (settings.repo_root / "brand" / "facts.yaml").is_file() else None
    seen: dict[str, str] = {}
    days: dict[str, list[str]] = {}
    for post in posts:
        issues.extend(validate_post(post, settings, now, facts))
        if post.status in (Status.APPROVED, Status.DRAFT):
            if post.scheduled_at in seen:
                issues.append(Issue("warning", post.id, f"same time as {seen[post.scheduled_at]}"))
            seen.setdefault(post.scheduled_at, post.id)
            day = post.scheduled_at[:10]
            if post.type != PostType.STORY:
                same_day = days.setdefault(day, [])
                if len(same_day) >= MAX_FEED_POSTS_PER_DAY and post.status == Status.APPROVED:
                    issues.append(
                        Issue("warning", post.id, f"more than {MAX_FEED_POSTS_PER_DAY} feed posts on {day} "
                              f"(also {', '.join(same_day)})")
                    )
                same_day.append(post.id)
    return issues
