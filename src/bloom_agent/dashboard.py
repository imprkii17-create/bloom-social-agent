"""One snapshot of everything the agent knows, for the web dashboard and the Telegram bot.

`build_snapshot` reads the repository (post queue, SEO, Instagram and marketing state, funnel numbers) and, when
allowed, checks a few things live (is the website up, current follower count). It never needs secrets to work:
whatever isn't configured is simply reported as such. The dashboard page adds the GitHub parts (workflow runs, open
alerts and pull requests) itself, live, from GitHub's public API.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import yaml

from .config import Settings
from .models import Post, Status
from .queue import load_queue
from .seo.sitecheck import PAUSED_MARKERS, Fetcher, HttpFetcher
from .tokens import STATUS_FILE

log = logging.getLogger(__name__)

SNAPSHOT_VERSION = 1
IG_STATE = "state/instagram-latest.json"
UPCOMING_DAYS = 14


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="minutes")


def _media_url(settings: Settings, post: Post) -> str | None:
    """Public address of the post's first image (the dashboard shows it as a thumbnail)."""
    if not settings.github_repository or not post.media:
        return None
    first = post.media[0]
    if first.lower().endswith((".mp4", ".mov")):
        first = post.cover or ""
        if not first:
            return None
    rel = f"posts/{post.id}/{first}"
    return f"https://raw.githubusercontent.com/{settings.github_repository}/main/{rel}"


def _caption_preview(post: Post, limit: int = 160) -> str:
    try:
        text = " ".join(post.caption().split())
    except (OSError, ValueError):
        return ""
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def post_card(settings: Settings, post: Post) -> dict[str, Any]:
    tz = settings.timezone
    ig = post.result.instagram
    return {
        "id": post.id,
        "status": post.status.value,
        "type": post.type.value,
        "scheduled_at": post.scheduled_at,
        "when": _iso(post.scheduled_dt(tz)),
        "caption": _caption_preview(post),
        "image": _media_url(settings, post),
        "permalink": ig.permalink if ig else None,
        "published_at": ig.published_at if ig else None,
        "error": post.result.last_error if post.status != Status.PUBLISHED else None,
    }


def queue_section(settings: Settings, now: datetime) -> dict[str, Any]:
    posts, errors = load_queue(settings.posts_dir)
    tz = settings.timezone
    counts: dict[str, int] = {}
    for p in posts:
        counts[p.status.value] = counts.get(p.status.value, 0) + 1
    horizon = now + timedelta(days=UPCOMING_DAYS)
    upcoming = sorted(
        (p for p in posts if p.status in (Status.DRAFT, Status.APPROVED) and now - timedelta(hours=3)
         <= p.scheduled_dt(tz) <= horizon),
        key=lambda p: p.scheduled_dt(tz),
    )
    recent = sorted(
        (p for p in posts if p.status in (Status.PUBLISHED, Status.FAILED, Status.MISSED)),
        key=lambda p: p.scheduled_dt(tz),
        reverse=True,
    )[:6]
    nxt = next((p for p in upcoming if p.status == Status.APPROVED and p.scheduled_dt(tz) >= now), None)
    return {
        "counts": counts,
        "upcoming": [post_card(settings, p) for p in upcoming],
        "recent": [post_card(settings, p) for p in recent],
        "next": post_card(settings, nxt) if nxt else None,
        "unreadable": [e.path.parent.name for e in errors],
        "drafts_next_7_days": [
            p.id for p in upcoming if p.status == Status.DRAFT and p.scheduled_dt(tz) <= now + timedelta(days=7)
        ],
    }


def site_section(settings: Settings, fetcher: Fetcher | None, now: datetime) -> dict[str, Any]:
    from .site.config import LANGS, load_site_config

    try:
        cfg = load_site_config(settings.repo_root)
    except (OSError, ValueError) as exc:
        return {"checked": False, "note": f"site/src/site.yaml can't be read: {exc}"}
    urls = [cfg.url(lang) for lang in LANGS]
    if fetcher is None:
        return {"checked": False, "base_url": cfg.base_url, "pages": [{"url": u} for u in urls]}
    pages = []
    for url in urls:
        r = fetcher.get(url)
        text = (r.text or "").lower()
        paused = any(m in text for m in PAUSED_MARKERS)
        ok = r.status == 200 and not paused
        note = "paused by Netlify" if paused else (r.error or ("" if ok else f"HTTP {r.status}"))
        pages.append({"url": url, "status": r.status, "ms": int(r.elapsed * 1000), "ok": ok, "note": note})
    return {
        "checked": True,
        "checked_at": _iso(now),
        "base_url": cfg.base_url,
        "ok": all(p["ok"] for p in pages),
        "pages": pages,
    }


def instagram_section(settings: Settings, ig_factory: Callable[[], Any] | None, now: datetime) -> dict[str, Any]:
    out: dict[str, Any] = {"configured": bool(settings.ig_user_id and settings.ig_access_token)}
    week = _read_json(settings.repo_root / IG_STATE)
    if isinstance(week, dict):
        out["week"] = week
    if ig_factory is not None:
        try:
            me = ig_factory().me()
            out["live"] = {
                "username": me.get("username"),
                "followers": me.get("followers_count"),
                "media_count": me.get("media_count"),
                "fetched_at": _iso(now),
            }
        except Exception as exc:  # the dashboard must never fail because Instagram didn't answer
            out["live_error"] = str(exc)[:200]
    token = _read_json(settings.repo_root / STATUS_FILE)
    if isinstance(token, dict) and token.get("expires_at"):
        try:
            expires = datetime.fromisoformat(str(token["expires_at"]))
            out["token"] = {"expires_at": token["expires_at"], "days_left": (expires - now.astimezone(UTC)).days}
        except ValueError:
            pass
    return out


def seo_section(settings: Settings) -> dict[str, Any]:
    latest = _read_json(settings.repo_root / "state" / "seo-latest.json")
    out: dict[str, Any] = {}
    if isinstance(latest, dict):
        findings = latest.get("findings") or []
        order = {"critical": 0, "warning": 1, "info": 2}
        findings = sorted(findings, key=lambda f: order.get(str(f.get("severity")), 3))
        out = {
            "created": latest.get("created"),
            "scores": latest.get("scores") or {},
            "findings": [
                {k: f.get(k) for k in ("area", "severity", "title", "page", "fix")} for f in findings[:10]
            ],
            "counts": {s: sum(1 for f in findings if f.get("severity") == s) for s in ("critical", "warning", "info")},
            "search": latest.get("search"),
            "speed": [
                {
                    "url": s.get("url"),
                    "strategy": s.get("strategy"),
                    "score": s.get("score"),
                    "lcp_ms": (s.get("lab") or {}).get("lcp_ms"),
                    "cls": (s.get("lab") or {}).get("cls"),
                    "error": s.get("error") or "",
                }
                for s in (latest.get("speed") or []) if isinstance(s, dict)
            ],
        }
    history_path = settings.repo_root / "state" / "seo-history.jsonl"
    if history_path.is_file():
        rows = []
        for line in history_path.read_text(encoding="utf-8").splitlines()[-26:]:
            try:
                rows.append(json.loads(line))
            except ValueError:
                continue
        out["history"] = rows
    return out


def marketing_section(settings: Settings) -> dict[str, Any]:
    latest = _read_json(settings.repo_root / "state" / "marketing-latest.json")
    if not isinstance(latest, dict):
        return {}
    plan = latest.get("plan") or {}
    week = latest.get("week")
    return {
        "week": week,
        "mode": latest.get("mode"),
        "summary": plan.get("summary"),
        "bottleneck": plan.get("bottleneck"),
        "bottleneck_reason": plan.get("bottleneck_reason"),
        "wins": plan.get("wins") or [],
        "problems": plan.get("problems") or [],
        "decisions": plan.get("decisions_for_you") or [],
        "reels": plan.get("reel_ideas") or [],
        "stories": plan.get("story_ideas") or [],
        "experiments": plan.get("experiments") or [],
        "posts_pr": latest.get("posts_pr"),
        "site_pr": latest.get("site_pr"),
        "spend_usd": latest.get("spend_usd"),
        "brief": f"reports/marketing/{week}.md" if week else None,
    }


def funnel_section(settings: Settings) -> list[dict[str, Any]]:
    path = settings.repo_root / "marketing" / "funnel.yaml"
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return []
    weeks = data.get("weeks") or [] if isinstance(data, dict) else []
    return [w for w in weeks if isinstance(w, dict)][-8:]


def week_label(d: datetime) -> str:
    year, week, _ = d.isocalendar()
    return f"{year}-W{week:02d}"


def attention_items(snap: dict[str, Any], now: datetime) -> list[dict[str, str]]:
    """What needs a human, most urgent first. Alerts and pull requests are added live by the page."""
    items: list[dict[str, str]] = []
    site = snap.get("site") or {}
    if site.get("checked") and not site.get("ok"):
        bad = [p for p in site.get("pages", []) if not p.get("ok")]
        items.append({"level": "critical", "text": "The website isn't answering properly: "
                      + "; ".join(f"{p['url']} ({p.get('note') or p.get('status')})" for p in bad)})
    queue = snap.get("queue") or {}
    for card in queue.get("recent", []):
        try:
            recent = datetime.fromisoformat(card["when"]) >= now - timedelta(days=3)
        except (KeyError, ValueError):
            recent = False
        if card["status"] in ("failed", "missed") and recent:
            detail = f": {card['error']}" if card.get("error") else ""
            items.append({"level": "critical", "text": f"Post {card['id']} {card['status']}{detail}"})
    for name in queue.get("unreadable", []):
        items.append({"level": "critical", "text": f"posts/{name}/post.yaml can't be read"})
    token = (snap.get("instagram") or {}).get("token") or {}
    if isinstance(token.get("days_left"), int) and token["days_left"] <= 14:
        items.append({"level": "critical" if token["days_left"] <= 3 else "warning",
                      "text": f"The Instagram token expires in {token['days_left']} days (Refresh token workflow)."})
    drafts = queue.get("drafts_next_7_days") or []
    if drafts:
        items.append({"level": "warning", "text": f"{len(drafts)} draft post(s) in the next 7 days aren't approved: "
                      + ", ".join(drafts) + ". Approve them, or they won't go out."})
    if not (queue.get("upcoming") or []):
        items.append({"level": "warning", "text": "Nothing is scheduled for the next two weeks."})
    seo = snap.get("seo") or {}
    crit = (seo.get("counts") or {}).get("critical", 0)
    if crit:
        items.append({"level": "critical", "text": f"{crit} critical SEO problem(s) in the latest report."})
    funnel = snap.get("funnel") or []
    last_week = week_label(now - timedelta(days=7))
    decisions = [str(d) for d in (snap.get("marketing") or {}).get("decisions", [])]
    already_asked = any("funnel.yaml" in d for d in decisions)
    if not any(str(w.get("week")) == last_week for w in funnel) and now.weekday() <= 2 and not already_asked:
        items.append({"level": "info", "text": f"Log {last_week}'s applications in marketing/funnel.yaml."})
    for decision in (snap.get("marketing") or {}).get("decisions", [])[:5]:
        items.append({"level": "info", "text": str(decision)})
    return items


def build_snapshot(
    settings: Settings,
    *,
    now: datetime | None = None,
    fetcher: Fetcher | None = None,
    ig_factory: Callable[[], Any] | None = None,
) -> dict[str, Any]:
    now = (now or datetime.now(settings.timezone)).astimezone(settings.timezone)
    snap: dict[str, Any] = {
        "version": SNAPSHOT_VERSION,
        "generated_at": _iso(now),
        "timezone": str(settings.timezone),
        "repo": settings.github_repository,
        "dashboard_url": settings.dashboard_url,
        "features": {
            "claude": bool(settings.anthropic_api_key),
            "telegram": bool(settings.telegram_bot_token and settings.telegram_chat_id),
            "search_console": bool(settings.gsc_service_account_json),
            "pagespeed_key": bool(settings.pagespeed_api_key),
            "instagram": bool(settings.ig_user_id and settings.ig_access_token),
        },
    }
    sections: dict[str, Callable[[], Any]] = {
        "site": lambda: site_section(settings, fetcher, now),
        "queue": lambda: queue_section(settings, now),
        "instagram": lambda: instagram_section(settings, ig_factory, now),
        "seo": lambda: seo_section(settings),
        "marketing": lambda: marketing_section(settings),
        "funnel": lambda: funnel_section(settings),
    }
    for key, make in sections.items():
        try:
            snap[key] = make()
        except Exception as exc:  # one broken part must not take the whole dashboard down
            log.warning("dashboard section %s failed: %s", key, exc)
            snap[key] = {"error": str(exc)[:300]}
    snap["attention"] = attention_items(snap, now)
    return snap


def live_fetcher() -> Fetcher:
    return HttpFetcher()


def write_snapshot(snap: dict[str, Any], path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(snap, ensure_ascii=False, indent=1, default=str) + "\n", encoding="utf-8")
    return path
