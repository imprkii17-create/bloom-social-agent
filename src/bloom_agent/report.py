"""Weekly performance report from Instagram Insights, written to reports/<year>-W<week>.md."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from .config import Settings
from .instagram import InstagramClient
from .models import Post, PostType
from .queue import load_queue
from .tokens import days_left

log = logging.getLogger(__name__)

FEED_METRICS = ["reach", "views", "likes", "comments", "saved", "shares", "profile_visits", "follows"]
REEL_METRICS = ["reach", "views", "likes", "comments", "saved", "shares", "ig_reels_avg_watch_time"]
STORY_METRICS = ["reach", "views", "replies", "shares", "profile_visits", "follows"]
ACCOUNT_METRICS = ["reach", "views", "profile_views", "website_clicks", "accounts_engaged", "total_interactions"]

LABELS = {
    "reach": "Reach",
    "views": "Views",
    "likes": "Likes",
    "comments": "Comments",
    "saved": "Saves",
    "shares": "Shares",
    "profile_visits": "Profile visits",
    "follows": "Follows",
    "replies": "Replies",
    "ig_reels_avg_watch_time": "Avg watch (ms)",
    "profile_views": "Profile views",
    "website_clicks": "Link clicks",
    "accounts_engaged": "Accounts engaged",
    "total_interactions": "Interactions",
}


@dataclass
class PostStats:
    post: Post
    metrics: dict[str, int]

    @property
    def score(self) -> int:
        """Rough 'did it move people' score: actions that lead toward an application weigh most."""
        m = self.metrics
        return (
            m.get("profile_visits", 0) * 3 + m.get("follows", 0) * 5 + m.get("shares", 0) * 3
            + m.get("saved", 0) * 2 + m.get("comments", 0) * 2 + m.get("likes", 0)
        )


def metrics_for(post: Post) -> list[str]:
    if post.type == PostType.REEL:
        return REEL_METRICS
    if post.type == PostType.STORY:
        return STORY_METRICS
    return FEED_METRICS


def collect(settings: Settings, ig: InstagramClient, days: int, now: datetime | None = None) -> tuple[
    list[PostStats], dict[str, int], int | None
]:
    now = now or datetime.now(UTC)
    since = now - timedelta(days=days)
    posts, _ = load_queue(settings.posts_dir)
    stats: list[PostStats] = []
    for post in posts:
        res = post.result.instagram
        if res is None:
            continue
        try:
            published = datetime.fromisoformat(res.published_at)
        except ValueError:
            continue
        if published.tzinfo is None:
            published = published.replace(tzinfo=settings.timezone)
        if published < since:
            continue
        stats.append(PostStats(post, ig.media_insights(res.media_id, metrics_for(post))))
    account = ig.account_insights(ACCOUNT_METRICS, int(since.timestamp()), int(now.timestamp()))
    followers: int | None = None
    try:
        raw = ig.me().get("followers_count")
        followers = int(raw) if raw is not None else None
    except Exception as exc:  # the report must not fail because one number is missing
        log.info("followers_count unavailable: %s", exc)
    return stats, account, followers


def render(
    stats: list[PostStats], account: dict[str, int], followers: int | None, *, days: int,
    token_days: int | None, now: datetime | None = None, ai_summary: str | None = None,
) -> str:
    now = now or datetime.now(UTC)
    year, week, _ = now.isocalendar()
    lines = [f"# Bloom Models — weekly report {year}-W{week:02d}", ""]
    lines.append(f"Period: last {days} days, generated {now.strftime('%Y-%m-%d %H:%M UTC')}.")
    lines.append("")
    if token_days is not None and token_days <= 14:
        lines += [f"> ⚠️ The Instagram token expires in {token_days} days. Check the *Refresh token* workflow.", ""]

    lines += ["## Account", ""]
    if followers is not None:
        lines.append(f"- **Followers:** {followers}")
    for key in ACCOUNT_METRICS:
        if key in account:
            lines.append(f"- **{LABELS[key]}:** {account[key]}")
    if followers is None and not account:
        lines.append("_No account data yet. Some metrics need at least 100 followers._")
    lines.append("")

    lines += ["## Posts", ""]
    if not stats:
        lines += ["_Nothing was published in this period._", ""]
    else:
        cols = ["reach", "views", "likes", "comments", "saved", "shares", "profile_visits", "follows"]
        lines.append("| Post | Type | " + " | ".join(LABELS[c] for c in cols) + " |")
        lines.append("|---|---|" + "|".join("---:" for _ in cols) + "|")
        for s in sorted(stats, key=lambda x: x.score, reverse=True):
            link = s.post.result.instagram.permalink if s.post.result.instagram else None
            name = f"[{s.post.id}]({link})" if link else s.post.id
            vals = [str(s.metrics[c]) if c in s.metrics else "–" for c in cols]
            lines.append(f"| {name} | {s.post.type.value} | " + " | ".join(vals) + " |")
        lines.append("")
        best = max(stats, key=lambda x: x.score)
        lines.append(f"**Best this week:** `{best.post.id}` — make more posts like this one.")
        lines.append("")
    if ai_summary:
        lines += ["## Claude's suggestions for next week", "", ai_summary.strip(), ""]
    return "\n".join(lines)


def build_report(settings: Settings, ig: InstagramClient, days: int = 7) -> tuple[str, str]:
    """Returns (relative path, markdown)."""
    now = datetime.now(UTC)
    stats, account, followers = collect(settings, ig, days, now)
    ai = None
    if settings.anthropic_api_key and stats:
        from .review import summarise_week  # optional dependency

        ai = summarise_week(settings, render(stats, account, followers, days=days, token_days=None, now=now))
    md = render(stats, account, followers, days=days, token_days=days_left(settings.repo_root), now=now, ai_summary=ai)
    year, week, _ = now.isocalendar()
    rel = f"reports/{year}-W{week:02d}.md"
    path = settings.repo_root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(md, encoding="utf-8")
    return rel, md
