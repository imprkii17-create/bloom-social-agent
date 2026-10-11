"""Instagram search SEO: Instagram search reads the Name field, username, bio, captions, alt text and hashtags, and
Google indexes public posts of professional accounts. These checks make sure every post and the profile can be found
by the words applicants actually type."""

from __future__ import annotations

import itertools
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import yaml

from ..facts import Facts, check_text
from ..models import Post, PostType, Status
from ..validate import HASHTAG_RE
from .findings import FindingList
from .onpage import _contains_keyword

AREA = "Instagram"
NAME_MAX = 64
BIO_MAX = 150
VISIBLE_CAPTION = 125  # characters shown before "… more"
MIN_ALT = 40
GAP_DAYS = 3


@dataclass
class InstagramSeoConfig:
    name_field: str = ""
    keywords: list[str] = field(default_factory=list)
    hashtag_pool: list[str] = field(default_factory=list)
    site_host: str = "bloommodels.hu"


def load_ig_seo(repo_root: Path) -> InstagramSeoConfig:
    path = repo_root / "brand" / "seo.yaml"
    if not path.is_file():
        return InstagramSeoConfig()
    data = (yaml.safe_load(path.read_text(encoding="utf-8")) or {}).get("instagram") or {}
    kw = data.get("keywords") or {}
    keywords = [str(k) for lang in ("hu", "en") for k in kw.get(lang) or []]
    return InstagramSeoConfig(
        name_field=str(data.get("name_field", "")),
        keywords=keywords,
        hashtag_pool=[str(h).lower() for h in data.get("hashtag_pool") or []],
    )


def profile_findings(profile: dict[str, Any], cfg: InstagramSeoConfig, facts: Facts | None = None) -> FindingList:
    f = FindingList()
    name = str(profile.get("name") or "")
    bio = str(profile.get("biography") or "")
    website = str(profile.get("website") or "")
    if profile.get("account_type") and str(profile["account_type"]).upper() not in ("BUSINESS", "MEDIA_CREATOR", "CREATOR"):
        f.add(
            AREA,
            "critical",
            "ig-not-pro",
            "The account isn't a professional account",
            fix="Professional accounts get insights and are indexed by Google. Settings → Account type.",
        )
    if not name:
        f.add(AREA, "info", "ig-name-unknown", "Couldn't read the profile Name field")
    else:
        if len(name) > NAME_MAX:
            f.add(AREA, "warning", "ig-name-long", f"Name field is {len(name)} characters (max {NAME_MAX})")
        if not _contains_keyword(name, cfg.keywords):
            f.add(
                AREA,
                "warning",
                "ig-name-keyword",
                "The Name field has no search keyword",
                name,
                f"Instagram search weighs the Name field heavily. Suggested: “{cfg.name_field}”.",
            )
        else:
            f.ok(AREA, "ig-name", "Name field contains a search keyword")
    if bio:
        if len(bio) > BIO_MAX:
            f.add(AREA, "warning", "ig-bio-long", f"Bio is {len(bio)} characters (max {BIO_MAX})")
        if not _contains_keyword(bio, cfg.keywords):
            f.add(
                AREA, "info", "ig-bio-keyword", "The bio has no search keyword", fix="Work in 'online modell' or 'Budapest'."
            )
        if "18" not in bio:
            f.add(AREA, "warning", "ig-bio-18", "The bio doesn't say 18+", fix="Add '18+' so it's clear who it's for.")
        if facts is not None:
            for issue in check_text(bio, facts):
                f.add(AREA, "warning", "ig-bio-facts", "Bio disagrees with brand/facts.yaml", issue.message)
    known = profile.get("biography") is not None or profile.get("website") is not None
    if known and cfg.site_host not in website:
        f.add(
            AREA,
            "warning",
            "ig-website",
            "The profile link doesn't point to the website",
            website or "(no link)",
            f"Set the profile website to https://{cfg.site_host}/ — it's the only clickable link.",
        )
    return f


def post_seo_issues(post: Post, caption: str, cfg: InstagramSeoConfig) -> list[str]:
    """Soft suggestions for one post (never blocks publishing)."""
    issues: list[str] = []
    if post.type == PostType.STORY:
        return issues
    first = caption.strip()[:VISIBLE_CAPTION]
    if cfg.keywords and not _contains_keyword(first, cfg.keywords):
        issues.append(
            f"The first {VISIBLE_CAPTION} characters (what shows before '… more') have no search keyword "
            f"({', '.join(cfg.keywords[:3])}…)."
        )
    if post.type != PostType.REEL:
        alt = (post.alt_text or "").strip()
        if len(alt) < MIN_ALT:
            issues.append(
                "Alt text is missing or very short; describe the image in a full sentence (Instagram and Google read it)."
            )
    tags = [t.lower() for t in HASHTAG_RE.findall(caption)]
    if len(tags) < 3:
        issues.append(f"Only {len(tags)} hashtag(s); 3–5 specific ones help Instagram categorise the post.")
    if cfg.hashtag_pool and tags and not set(tags) & set(cfg.hashtag_pool):
        issues.append("None of the hashtags are from the hashtag pool in brand/seo.yaml.")
    return issues


def queue_findings(
    posts: list[Post], captions: dict[str, str], cfg: InstagramSeoConfig, now: datetime, tz: ZoneInfo
) -> FindingList:
    """Cadence, format mix and per-post SEO for what's coming up."""
    now = now.astimezone(UTC)
    f = FindingList()
    upcoming = sorted(
        (p for p in posts if p.status in (Status.APPROVED, Status.DRAFT) and p.scheduled_utc(tz) >= now),
        key=lambda p: p.scheduled_at,
    )
    approved = [p for p in upcoming if p.status == Status.APPROVED]
    week = now + timedelta(days=7)
    if not [p for p in approved if p.scheduled_utc(tz) <= week]:
        f.add(
            AREA,
            "warning",
            "ig-nothing-approved",
            "Nothing approved for the next 7 days",
            f"{len([p for p in upcoming if p.status == Status.DRAFT])} draft(s) waiting",
            "Approve drafts (or merge the agent's weekly pull request) so the account stays active.",
        )
    times = [p.scheduled_utc(tz) for p in upcoming if p.type != PostType.STORY]
    for a, b in itertools.pairwise(times):
        if (b - a).days > GAP_DAYS:
            f.add(
                AREA,
                "info",
                "ig-gap",
                f"{(b - a).days}-day gap between posts ({a:%b %d} → {b:%b %d})",
                fix="Consistency matters more than volume; fill gaps with a Story or a simple post.",
            )
            break
    if upcoming and not any(p.type == PostType.REEL for p in upcoming):
        f.add(
            AREA,
            "info",
            "ig-no-reels",
            "No Reels planned",
            fix="Reels are what Instagram shows to people who don't follow you yet. Aim for 1–2 a week.",
        )
    grouped: dict[str, list[str]] = {}
    for p in upcoming[:15]:
        for issue in post_seo_issues(p, captions.get(p.id, ""), cfg):
            grouped.setdefault(issue, []).append(p.id)
    for issue, ids in grouped.items():
        f.add(AREA, "info", "ig-post-seo", f"{len(ids)} upcoming post(s): {issue}", ", ".join(ids))
    return f
