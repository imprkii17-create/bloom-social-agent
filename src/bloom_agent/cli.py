"""Command line: `bloom <command>` (or `python -m bloom_agent <command>`)."""

from __future__ import annotations

import argparse
import logging
import os
import subprocess
import sys
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from . import __version__
from .config import ConfigError, Settings, load_settings
from .facebook import FacebookClient
from .graph import GraphClient, GraphError
from .instagram import InstagramClient
from .models import SCHEDULE_FORMAT, Platform, Post, PostType, Status
from .prepare import prepare_all
from .publisher import Publisher, PublishReport
from .queue import load_post, load_queue, save_post
from .validate import validate_all

log = logging.getLogger("bloom")


# ---- helpers ----------------------------------------------------------------------------------------

def summary(markdown: str) -> None:
    """Print, and add to the GitHub Actions job summary when running there."""
    print(markdown)
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(markdown + "\n")


def notify(settings: Settings, title: str, body: str) -> None:
    if not (settings.github_token and settings.github_repository):
        return
    from .github import GitHubError, open_or_update_issue

    try:
        url = open_or_update_issue(settings.github_repository, settings.github_token, title, body)
        print(f"Alert issue: {url}")
    except GitHubError as exc:
        log.error("Could not open alert issue: %s", exc)


def ig_client(settings: Settings) -> InstagramClient:
    user_id, token = settings.require_instagram()
    return InstagramClient(GraphClient(settings.ig_base, token), user_id)


def fb_client(settings: Settings) -> FacebookClient:
    page_id, token = settings.require_facebook()
    return FacebookClient(GraphClient(settings.fb_base, token), page_id)


def parse_now(settings: Settings, value: str | None) -> datetime:
    if value:
        return datetime.strptime(value, SCHEDULE_FORMAT).replace(tzinfo=settings.timezone)
    return datetime.now(settings.timezone)


# ---- commands ---------------------------------------------------------------------------------------

def cmd_validate(settings: Settings, args: argparse.Namespace) -> int:
    posts, errors = load_queue(settings.posts_dir)
    issues = validate_all(posts, errors, settings, now=datetime.now(settings.timezone))
    n_err = sum(1 for i in issues if i.level == "error")
    n_warn = len(issues) - n_err
    lines = [f"### Validation: {len(posts)} posts, {n_err} errors, {n_warn} warnings", ""]
    lines += [f"- {i}" for i in issues] or ["- All good ✅"]
    summary("\n".join(lines) + "\n")
    if n_err or (args.strict and n_warn):
        return 1
    return 0


def cmd_prepare(settings: Settings, args: argparse.Namespace) -> int:
    posts, _ = load_queue(settings.posts_dir)
    result = prepare_all(posts)
    if not result.converted and not result.resized:
        print("Nothing to prepare.")
    for line in result.converted:
        print(f"converted {line}")
    for line in result.resized:
        print(f"resized   {line}")
    return 0


def cmd_publish(settings: Settings, args: argparse.Namespace) -> int:
    now = parse_now(settings, args.now)
    publisher = Publisher(
        settings,
        instagram=lambda: ig_client(settings),
        facebook=lambda: fb_client(settings),
        check_urls=not args.no_url_check,
        checkpoint=_git_checkpoint(settings) if os.environ.get("GITHUB_ACTIONS") == "true" else None,
    )
    try:
        report = publisher.run(now, dry_run=args.dry_run)
    except ConfigError as exc:
        summary(f"### ✖ Configuration problem\n\n{exc}\n")
        notify(settings, "Bloom agent: configuration problem", str(exc))
        return 1
    _publish_summary(report, now, args.dry_run)
    if report.failures and not args.dry_run:
        body = "\n".join(f"- `{o.post_id}` ({o.platform}): **{o.status}** — {o.detail}" for o in report.failures)
        body += "\n\nFix the post (or the setting) and set `status: approved` again to retry. "
        body += "See the workflow run for details."
        notify(settings, "Bloom agent: a post needs attention", body)
        return 1
    return 0


def _git_checkpoint(settings: Settings) -> Callable[[Post], None]:
    """Commit and push a post's state before publishing it (see publisher.py for why)."""
    script = settings.repo_root / "scripts" / "commit-back.sh"

    def checkpoint(post: Post) -> None:
        subprocess.run(
            [str(script), f"bloom-agent: publishing {post.id}", "posts"],
            cwd=settings.repo_root, check=True, timeout=180,
        )

    return checkpoint


def _publish_summary(report: PublishReport, now: datetime, dry_run: bool) -> None:
    title = "Dry run" if dry_run else "Publish run"
    lines = [f"### {title} — {now.strftime('%Y-%m-%d %H:%M %Z')}", ""]
    if not report.outcomes:
        lines.append("Nothing due right now.")
    else:
        lines += ["| Post | Platform | Result | Details |", "|---|---|---|---|"]
        for o in report.outcomes:
            lines.append(f"| {o.post_id} | {o.platform} | {o.status} | {o.detail.replace('|', '/')} |")
    summary("\n".join(lines) + "\n")


def cmd_status(settings: Settings, args: argparse.Namespace) -> int:
    posts, errors = load_queue(settings.posts_dir)
    lines = ["| When | Post | Type | Status | Platforms | Result |", "|---|---|---|---|---|---|"]
    for p in posts:
        done = []
        if p.result.instagram:
            done.append(f"[IG]({p.result.instagram.permalink})" if p.result.instagram.permalink else "IG ✓")
        if p.result.facebook:
            done.append("FB ✓")
        if p.result.last_error and p.status != Status.PUBLISHED:
            done.append(f"⚠ {p.result.last_error[:60]}")
        plats = ", ".join(x.value for x in p.platforms)
        lines.append(f"| {p.scheduled_at} | {p.id} | {p.type.value} | {p.status.value} | {plats} | {' '.join(done)} |")
    for e in errors:
        lines.append(f"| – | {e.path.parent.name} | – | **unreadable** | – | {e.message[:80]} |")
    summary("\n".join(lines) + "\n")
    return 0


def cmd_check(settings: Settings, args: argparse.Namespace) -> int:
    """Setup check: is the token valid, which account is it, how much quota is left."""
    from .tokens import days_left

    try:
        ig = ig_client(settings)
        me = ig.me()
        quota = ig.publishing_quota()
    except (ConfigError, GraphError) as exc:
        summary(f"### ✖ Instagram check failed\n\n{exc}\n")
        return 1
    lines = [
        "### ✅ Instagram connected",
        "",
        f"- Account: @{me.get('username')} ({me.get('account_type', 'unknown type')})",
        f"- Followers: {me.get('followers_count', '–')}, posts: {me.get('media_count', '–')}",
        f"- Publishing quota (24h): {f'{quota[0]}/{quota[1]} used' if quota else 'unknown'}",
        f"- Media will be served from: {settings.media_base_url or '⚠ not set (only set inside GitHub Actions)'}",
    ]
    left = days_left(settings.repo_root)
    if left is not None:
        lines.append(f"- Token expires in about {left} days (auto-refreshed weekly)")
    if settings.fb_page_id:
        lines.append(f"- Facebook page configured: {settings.fb_page_id}")
    summary("\n".join(lines) + "\n")
    return 0


def cmd_refresh_token(settings: Settings, args: argparse.Namespace) -> int:
    from .github import GitHubError, set_actions_secret
    from .tokens import refresh_instagram_token, write_status

    try:
        _, token = settings.require_instagram()
        new = refresh_instagram_token(token)
    except (ConfigError, GraphError) as exc:
        summary(f"### ✖ Token refresh failed\n\n{exc}\n")
        notify(settings, "Bloom agent: Instagram token refresh failed",
               f"{exc}\n\nIf the token has expired, generate a new one (SETUP.md step 4) and update IG_ACCESS_TOKEN.")
        return 1
    if not (settings.github_admin_token and settings.github_repository):
        msg = ("The token could be renewed, but the agent can't save it: the GH_ADMIN_TOKEN secret is missing or "
               "expired (SETUP.md step 6). The current token keeps working until it expires, then posting stops.")
        summary(f"### ⚠ Token refreshed but not saved\n\n{msg}\n")
        notify(settings, "Bloom agent: Instagram token can't be saved", msg)
        return 1
    try:
        set_actions_secret(settings.github_repository, settings.github_admin_token, "IG_ACCESS_TOKEN", new.access_token)
    except GitHubError as exc:
        summary(f"### ✖ Could not save the new token\n\n{exc}\n")
        notify(settings, "Bloom agent: could not save refreshed token", str(exc))
        return 1
    write_status(settings.repo_root, new.expires_at)
    summary(f"### ✅ Instagram token refreshed\n\nValid until {new.expires_at:%Y-%m-%d}.\n")
    return 0


def cmd_report(settings: Settings, args: argparse.Namespace) -> int:
    from .report import build_report

    try:
        rel, md = build_report(settings, ig_client(settings), days=args.days)
    except (ConfigError, GraphError) as exc:
        summary(f"### ✖ Report failed\n\n{exc}\n")
        return 1
    summary(md)
    print(f"\nSaved {rel}")
    return 0


def cmd_review(settings: Settings, args: argparse.Namespace) -> int:
    from .review import review_post

    if not settings.anthropic_api_key:
        print("ANTHROPIC_API_KEY is not set; skipping Claude review.")
        return 0
    posts, _ = load_queue(settings.posts_dir)
    targets = [p for p in posts if (p.id in args.ids) or (not args.ids and p.status == Status.DRAFT)]
    if not targets:
        print("No posts to review.")
        return 0
    worst = 0
    for post in targets:
        review = review_post(settings, post)
        (post.folder / "review.md").write_text(review.to_markdown(post.id, settings.claude_model), encoding="utf-8")
        summary(f"- `{post.id}`: {'✅ ok' if review.verdict == 'ok' else '✏️ needs changes'}"
                + (f" — {review.issues[0]}" if review.issues else ""))
        worst = max(worst, 0 if review.verdict == "ok" else 1)
    return worst if args.strict else 0


def cmd_new(settings: Settings, args: argparse.Namespace) -> int:
    datetime.strptime(args.at, SCHEDULE_FORMAT)
    folder = settings.posts_dir / args.id
    if folder.exists():
        print(f"{folder} already exists", file=sys.stderr)
        return 1
    folder.mkdir(parents=True)
    media = args.media or (["video.mp4"] if args.type == "reel" else ["image.jpg"])
    post = Post(
        id=args.id, status=Status.DRAFT, type=PostType(args.type), scheduled_at=args.at, media=media,
        platforms=[Platform(p) for p in args.platforms.split(",")],
        caption_file=None if args.type == "story" else "caption.txt",
    )
    post._folder = folder
    save_post(post)
    if args.type != "story":
        (folder / "caption.txt").write_text("Write the caption here.\n\n#bloommodels\n", encoding="utf-8")
    print(f"Created {folder}. Add {', '.join(media)} and edit the caption, then set status: approved.")
    return 0


def cmd_approve(settings: Settings, args: argparse.Namespace) -> int:
    rc = 0
    for post_id in args.ids:
        path = settings.posts_dir / post_id / "post.yaml"
        if not path.is_file():
            print(f"No such post: {post_id}", file=sys.stderr)
            rc = 1
            continue
        post = load_post(path)
        if post.status == Status.PUBLISHED:
            print(f"{post_id} is already published")
            continue
        post.status = Status.APPROVED
        post.result.attempts = 0
        post.result.last_error = None
        save_post(post)
        print(f"approved {post_id} for {post.scheduled_at}")
    return rc


# ---- entry point ------------------------------------------------------------------------------------

def cmd_merge_state(settings: Settings, args: argparse.Namespace) -> int:
    """Internal, used by scripts/commit-back.sh: merge the agent's post.yaml into the person's newer one."""
    from .statemerge import merge_post_yaml

    target = Path(args.target)
    merged = merge_post_yaml(Path(args.ours).read_text(encoding="utf-8"), target.read_text(encoding="utf-8"), target.parent)
    target.write_text(merged, encoding="utf-8")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="bloom", description="Bloom Models social media agent")
    p.add_argument("--version", action="version", version=f"bloom-agent {__version__}")
    p.add_argument("-v", "--verbose", action="store_true")
    p.add_argument("--root", type=Path, default=None, help="repository root (default: current directory)")
    sub = p.add_subparsers(dest="command", required=True)

    s = sub.add_parser("validate", help="check every post")
    s.add_argument("--strict", action="store_true", help="fail on warnings too")
    s.set_defaults(func=cmd_validate)

    s = sub.add_parser("prepare", help="convert images to Instagram-ready JPEG")
    s.set_defaults(func=cmd_prepare)

    s = sub.add_parser("publish", help="publish approved posts that are due")
    s.add_argument("--dry-run", action="store_true", help="show what would happen; change nothing")
    s.add_argument("--now", help=f"pretend it is this time ({SCHEDULE_FORMAT}), for testing")
    s.add_argument("--no-url-check", action="store_true", help=argparse.SUPPRESS)
    s.set_defaults(func=cmd_publish)

    s = sub.add_parser("status", help="show the queue")
    s.set_defaults(func=cmd_status)

    s = sub.add_parser("check", help="verify the Instagram connection")
    s.set_defaults(func=cmd_check)

    s = sub.add_parser("refresh-token", help="refresh the Instagram token and store it as a GitHub secret")
    s.set_defaults(func=cmd_refresh_token)

    s = sub.add_parser("report", help="write the weekly insights report")
    s.add_argument("--days", type=int, default=7)
    s.set_defaults(func=cmd_report)

    s = sub.add_parser("review", help="ask Claude to review draft posts (needs ANTHROPIC_API_KEY)")
    s.add_argument("ids", nargs="*")
    s.add_argument("--strict", action="store_true")
    s.set_defaults(func=cmd_review)

    s = sub.add_parser("new", help="create a new post folder")
    s.add_argument("id")
    s.add_argument("--at", required=True, help=f"publish time, {SCHEDULE_FORMAT} (local time)")
    s.add_argument("--type", choices=[t.value for t in PostType], default="image")
    s.add_argument("--platforms", default="instagram")
    s.add_argument("--media", nargs="*")
    s.set_defaults(func=cmd_new)

    s = sub.add_parser("merge-state", help=argparse.SUPPRESS)
    s.add_argument("ours")
    s.add_argument("target")
    s.set_defaults(func=cmd_merge_state)

    s = sub.add_parser("approve", help="mark posts as approved")
    s.add_argument("ids", nargs="+")
    s.set_defaults(func=cmd_approve)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, format="%(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)  # httpx logs full URLs at INFO, which would include tokens
    settings = load_settings(args.root)
    return int(args.func(settings, args))


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
