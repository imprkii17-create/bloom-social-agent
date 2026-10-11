"""Publishes approved posts whose time has come. Designed to run every 15 minutes, any number of times.

Safety properties:
- A post is only ever published when status == approved and scheduled_at has passed (compared in UTC).
- Before asking Instagram to publish, the container id is saved *and pushed* to the repository (checkpoint).
  A later run that finds a saved container never creates a new post: it checks that container instead, and an
  Instagram container can only ever be published once. If the checkpoint can't be pushed, nothing is published.
- Progress is saved after every platform, so a platform that already succeeded is never posted to again.
- For a brand-new container we also look for an identical caption posted since the scheduled time.
- Posts more than MAX_LATENESS_MINUTES late are marked "missed" rather than posted at a random time.
- A failing post is retried on later runs, then marked "failed" after MAX_ATTEMPTS, and a human is notified.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import httpx

from .config import ConfigError, Settings
from .facebook import FacebookClient
from .graph import GraphError
from .instagram import ContainerError, ContainerProcessing, InstagramClient, Published
from .models import Platform, PlatformResult, Post, PostType, Status
from .queue import due_posts, load_post, load_queue, save_post
from .validate import CONVERTIBLE_EXT, validate_post

log = logging.getLogger(__name__)

VIDEO_EXT = (".mp4", ".mov")
DUPLICATE_WINDOW = timedelta(minutes=30)


@dataclass
class Outcome:
    post_id: str
    platform: str
    # published | dry-run | missed | failed | retrying | processing | waiting | blocked
    status: str
    detail: str = ""


@dataclass
class PublishReport:
    outcomes: list[Outcome] = field(default_factory=list)
    changed: bool = False

    @property
    def failures(self) -> list[Outcome]:
        return [o for o in self.outcomes if o.status in ("failed", "missed", "blocked")]

    def add(self, *args: str) -> None:
        self.outcomes.append(Outcome(*args))


class MediaUrlError(RuntimeError):
    pass


class CheckpointError(RuntimeError):
    """Saving progress to the repository failed, so publishing was not attempted."""


class PostChanged(RuntimeError):
    """The user edited or un-approved the post mid-run. Not a failure: the next run uses the new version."""


class MissedError(RuntimeError):
    """Too late to start a fresh upload."""


Checkpoint = Callable[[Post], None]


def media_url(settings: Settings, post: Post, name: str) -> str:
    if not settings.media_base_url:
        raise MediaUrlError(
            "MEDIA_BASE_URL is not set and this isn't running in GitHub Actions, so Instagram can't fetch the media."
        )
    rel = (post.folder / name).resolve().relative_to(settings.repo_root).as_posix()
    return f"{settings.media_base_url}/{rel}"


def check_url(url: str, *, expect_video: bool, http: httpx.Client) -> None:
    """Instagram fetches media itself; make sure the URL is public and the right type before asking it to."""
    try:
        resp = http.head(url, follow_redirects=True, timeout=20)
    except httpx.HTTPError as exc:
        raise MediaUrlError(f"media URL not reachable ({type(exc).__name__}): {url}") from None
    if resp.status_code != 200:
        raise MediaUrlError(
            f"media URL returned HTTP {resp.status_code}: {url} — is the repository public and the file committed?"
        )
    ctype = resp.headers.get("content-type", "").split(";")[0].strip().lower()
    if expect_video and not (ctype.startswith("video/") or ctype == "application/octet-stream"):
        raise MediaUrlError(f"media URL is served as {ctype or 'unknown'}, not a video: {url}")
    if not expect_video and ctype not in ("image/jpeg", "image/jpg", "application/octet-stream"):
        raise MediaUrlError(f"media URL is served as {ctype or 'unknown'}, not JPEG: {url}")


def _iso(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat(timespec="seconds")


def _from_iso(text: str | None) -> datetime | None:
    if not text:
        return None
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


class Publisher:
    def __init__(
        self,
        settings: Settings,
        *,
        instagram: Callable[[], InstagramClient] | None = None,
        facebook: Callable[[], FacebookClient] | None = None,
        http: httpx.Client | None = None,
        check_urls: bool = True,
        checkpoint: Checkpoint | None = None,
    ) -> None:
        self.s = settings
        self._ig_factory = instagram
        self._fb_factory = facebook
        self._ig: InstagramClient | None = None
        self._fb: FacebookClient | None = None
        self._http = http or httpx.Client()
        self._check_urls = check_urls
        self._checkpoint = checkpoint

    # ---- lazy clients ------------------------------------------------------------------------------

    @property
    def ig(self) -> InstagramClient:
        if self._ig is None:
            if self._ig_factory is None:
                raise ConfigError("Instagram client not configured")
            self._ig = self._ig_factory()
        return self._ig

    @property
    def fb(self) -> FacebookClient:
        if self._fb is None:
            if self._fb_factory is None:
                raise ConfigError("Facebook is not configured (FB_PAGE_ID / FB_PAGE_ACCESS_TOKEN).")
            self._fb = self._fb_factory()
        return self._fb

    # ---- main loop ---------------------------------------------------------------------------------

    def run(self, now: datetime, *, dry_run: bool = False) -> PublishReport:
        report = PublishReport()
        posts, load_errors = load_queue(self.s.posts_dir)
        for err in load_errors:
            report.add(err.path.parent.name, "all", "blocked", f"post.yaml can't be read: {err.message}")

        tz = self.s.timezone
        now_utc = now.astimezone(UTC)
        quota_left: int | None = None
        for post in due_posts(posts, now, tz):
            # The user re-approved a post that had failed: give it a fresh set of attempts.
            if post.result.attempts >= self.s.max_attempts and not dry_run:
                post.result.attempts = 0
                post.result.last_error = None
                save_post(post)
                report.changed = True

            late_min = (now_utc - post.scheduled_utc(tz)).total_seconds() / 60
            untouched = (
                len(post.remaining_platforms()) == len(post.supported_platforms())
                and not post.result.ig_pending_container
            )
            if late_min > self.s.max_lateness_minutes and untouched:
                report.add(post.id, "all", "missed", f"{int(late_min)} min late; not posted")
                if not dry_run:
                    post.status = Status.MISSED
                    post.result.last_error = f"Missed: scheduled {post.scheduled_at}, checked {int(late_min)} min late"
                    save_post(post)
                    report.changed = True
                continue

            unconverted = [m for m in post.media if m.lower().endswith(tuple(CONVERTIBLE_EXT))]
            if unconverted:
                report.add(post.id, "all", "waiting", f"waiting for CI to convert {', '.join(unconverted)} to JPEG")
                continue

            problems = [i for i in validate_post(post, self.s) if i.level == "error"]
            if problems:
                detail = "; ".join(i.message for i in problems)
                report.add(post.id, "all", "blocked", detail)
                if not dry_run:
                    post.status = Status.FAILED
                    post.result.last_error = f"Validation failed: {detail}"
                    save_post(post)
                    report.changed = True
                continue

            if dry_run:
                for platform in post.remaining_platforms():
                    report.add(post.id, platform.value, "dry-run", f"would publish {post.type.value}")
                continue

            if Platform.INSTAGRAM in post.remaining_platforms() and quota_left is None:
                try:
                    quota = self.ig.publishing_quota()
                except ConfigError:
                    quota = None
                quota_left = (quota[1] - quota[0]) if quota else 1_000
            self._publish_post(post, now_utc, report, quota_left)
            if quota_left is not None and post.result.instagram is not None:
                quota_left -= 1
        return report

    def _publish_post(self, post: Post, now_utc: datetime, report: PublishReport, quota_left: int | None) -> None:
        stamp = _iso(now_utc)
        try:
            for platform in post.remaining_platforms():
                if platform == Platform.INSTAGRAM:
                    if quota_left is not None and quota_left <= 0:
                        report.add(post.id, "instagram", "retrying", "Instagram 24h publishing limit reached")
                        return  # not an attempt; try again next run
                    published = self._publish_instagram(post, now_utc)
                    post.result.instagram = PlatformResult(
                        media_id=published.media_id, permalink=published.permalink, published_at=stamp
                    )
                    post.result.ig_pending_container = None
                    post.result.ig_pending_fingerprint = None
                    post.result.ig_attempt_started_at = None
                else:
                    published = self._publish_facebook(post, now_utc)
                    post.result.facebook = PlatformResult(
                        media_id=published.media_id, permalink=published.permalink, published_at=stamp
                    )
                save_post(post)  # persist after each platform
                report.changed = True
                report.add(post.id, platform.value, "published", published.permalink or published.media_id)
        except PostChanged as exc:
            report.add(post.id, "instagram", "skipped", str(exc))
            return  # the reloaded file on disk is the user's version; leave it alone
        except MissedError as exc:
            post.status = Status.MISSED
            post.result.last_error = f"Missed: {exc}"
            save_post(post)
            report.changed = True
            report.add(post.id, "instagram", "missed", str(exc))
            return
        except ContainerProcessing as exc:
            # Not a failure: the video is still being processed. The container id is saved; check again next run.
            save_post(post)
            report.changed = True
            report.add(post.id, "instagram", "processing", str(exc))
            return
        except (GraphError, ContainerError, MediaUrlError, CheckpointError, ConfigError, OSError, KeyError) as exc:
            post.result.attempts += 1
            post.result.last_error = str(exc)
            post.result.last_attempt_at = stamp
            final = (
                post.result.attempts >= self.s.max_attempts
                or isinstance(exc, ConfigError)
                or (isinstance(exc, GraphError) and exc.is_auth_error)
            )
            if final:
                post.status = Status.FAILED
            save_post(post)
            report.changed = True
            report.add(post.id, "-", "failed" if final else "retrying", str(exc))
            return

        if not post.remaining_platforms():
            post.status = Status.PUBLISHED
            post.result.last_error = None
            save_post(post)
            report.changed = True

    # ---- instagram ---------------------------------------------------------------------------------

    def _url(self, post: Post, name: str) -> str:
        url = media_url(self.s, post, name)
        if self._check_urls:
            check_url(url, expect_video=name.lower().endswith(VIDEO_EXT), http=self._http)
        return url

    def _publish_instagram(self, post: Post, now_utc: datetime) -> Published:
        ig = self.ig
        story = post.type == PostType.STORY
        fingerprint = post.fingerprint()
        res = post.result

        # 1) A container saved by an earlier run always comes first: it may already be live.
        pending = res.ig_pending_container
        if pending:
            started = _from_iso(res.ig_attempt_started_at)
            status, _ = ig.container_status(pending)
            if status == "PUBLISHED":
                # Published earlier but never recorded. Record it; never post again, even if the post changed since.
                return ig.recover_published(story=story, since=started)
            if status not in ("FINISHED", "IN_PROGRESS", "ERROR", "EXPIRED"):
                raise ContainerError(f"Instagram gave an unexpected status for container {pending}: {status!r}")
            if status in ("FINISHED", "IN_PROGRESS") and res.ig_pending_fingerprint == fingerprint:
                self._ensure_still_wanted(post, fingerprint)
                ig.wait_until_ready(pending, timeout=self._timeout(post), interval=self._interval(post))
                return ig.publish(pending, story=story, since=started)
            reason = "the post was edited" if res.ig_pending_fingerprint != fingerprint else f"it is {status}"
            log.warning("Not using saved container %s because %s; making a new one", pending, reason)
            res.ig_pending_container = None
            res.ig_pending_fingerprint = None

        # 2) About to upload fresh: don't start a brand-new post hours late.
        late_min = (now_utc - post.scheduled_utc(self.s.timezone)).total_seconds() / 60
        if late_min > self.s.max_lateness_minutes:
            raise MissedError(f"{int(late_min)} min late; not posted")

        # 3) Only relevant if an earlier attempt may have posted without us recording it (e.g. a local run).
        caption = "" if story else post.caption()
        previous = _from_iso(res.ig_attempt_started_at)
        if caption and (previous is not None or res.attempts > 0):
            since = (previous or post.scheduled_utc(self.s.timezone)) - DUPLICATE_WINDOW
            dup = ig.find_duplicate(caption, since=since)
            if dup:
                log.warning("Identical caption already on Instagram (%s); recording instead of re-posting", dup.get("id"))
                return Published(media_id=str(dup["id"]), permalink=dup.get("permalink"))

        container = self._create_container(post, caption)
        res.ig_pending_container = container
        res.ig_pending_fingerprint = fingerprint
        res.ig_attempt_started_at = _iso(now_utc)
        save_post(post)
        self._save_checkpoint(post)  # pushed to the repo before we publish; raises if that fails

        # 4) The checkpoint pulled in the latest repository state: honour edits made while we were working.
        self._ensure_still_wanted(post, fingerprint)
        ig.wait_until_ready(container, timeout=self._timeout(post), interval=self._interval(post))
        return ig.publish(container, story=story, since=now_utc)

    @staticmethod
    def _ensure_still_wanted(post: Post, fingerprint: str) -> None:
        """Re-read the file (a checkpoint may have pulled in edits) right before the irreversible step."""
        latest = load_post(post.folder / "post.yaml")
        if latest.status != Status.APPROVED or latest.fingerprint() != fingerprint:
            raise PostChanged("The post was changed or un-approved while publishing; nothing was posted.")

    def _save_checkpoint(self, post: Post) -> None:
        if self._checkpoint is None:
            return
        try:
            self._checkpoint(post)
        except Exception as exc:
            raise CheckpointError(f"Could not save progress to the repository, so nothing was published: {exc}") from exc

    def _create_container(self, post: Post, caption: str) -> str:
        ig = self.ig
        names = post.media
        if post.type == PostType.IMAGE:
            return ig.image_container(self._url(post, names[0]), caption=caption, alt_text=post.alt_text)
        if post.type == PostType.REEL:
            cover = self._url(post, post.cover) if post.cover else None
            return ig.video_container(
                self._url(post, names[0]), media_type="REELS", caption=caption,
                cover_url=cover, share_to_feed=post.share_to_feed,
            )
        if post.type == PostType.STORY:
            if names[0].lower().endswith(VIDEO_EXT):
                return ig.video_container(self._url(post, names[0]), media_type="STORIES")
            return ig.story_image_container(self._url(post, names[0]))
        # carousel: children first; each must be FINISHED before the parent can be made
        children = []
        for name in names:
            if name.lower().endswith(VIDEO_EXT):
                child = ig.video_container(self._url(post, name), media_type="VIDEO", carousel_item=True)
                ig.wait_until_ready(child, timeout=300, interval=10)
            else:
                child = ig.image_container(self._url(post, name), carousel_item=True)
                ig.wait_until_ready(child, timeout=60, interval=3)
            children.append(child)
        return ig.carousel_container(children, caption)

    @staticmethod
    def _timeout(post: Post) -> float:
        return 300.0 if any(m.lower().endswith(VIDEO_EXT) for m in post.media) else 90.0

    @staticmethod
    def _interval(post: Post) -> float:
        return 15.0 if any(m.lower().endswith(VIDEO_EXT) for m in post.media) else 3.0

    # ---- facebook ----------------------------------------------------------------------------------

    def _publish_facebook(self, post: Post, now_utc: datetime) -> Published:
        caption = post.caption()
        images = [m for m in post.media if not m.lower().endswith(VIDEO_EXT)]
        if not images:
            raise ContainerError("Facebook: this post has no images to publish")
        fb = self.fb
        # If an earlier attempt ended with an unclear error, check whether it actually went out.
        previous = _from_iso(post.result.fb_attempt_started_at)
        if previous is not None:
            found = fb.find_recent(caption, since=previous - timedelta(minutes=2))
            if found:
                post.result.fb_attempt_started_at = None
                return found
        urls = [self._url(post, m) for m in images]
        post.result.fb_attempt_started_at = _iso(now_utc)
        save_post(post)
        published = fb.publish_photo(urls[0], caption) if len(urls) == 1 else fb.publish_album(urls, caption)
        post.result.fb_attempt_started_at = None
        return published
