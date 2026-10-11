"""Instagram API with Instagram Login: container → wait until FINISHED → media_publish."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from .graph import GraphClient, GraphError

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Published:
    media_id: str
    permalink: str | None


class ContainerError(RuntimeError):
    """Instagram could not use this container (ERROR/EXPIRED) or refused it."""


class ContainerProcessing(ContainerError):
    """Still processing (usually video). Keep the container and check again on the next run."""


class ContainerAlreadyPublished(ContainerError):
    """The container was published (perhaps by an earlier run whose answer was lost)."""


class InstagramClient:
    def __init__(
        self,
        graph: GraphClient,
        ig_user_id: str,
        *,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.g = graph
        self.ig_id = ig_user_id
        self._sleep = sleep
        self._clock = clock

    # ---- account ----------------------------------------------------------------------------------

    def me(self) -> dict[str, Any]:
        return self.g.get("me", {"fields": "user_id,username,account_type,followers_count,media_count"})

    def publishing_quota(self) -> tuple[int, int] | None:
        """Returns (used, total) for the rolling 24h window, or None if Meta doesn't say."""
        try:
            data = self.g.get(f"{self.ig_id}/content_publishing_limit", {"fields": "quota_usage,config"})
        except GraphError as exc:
            log.warning("Could not read publishing limit: %s", exc)
            return None
        rows = data.get("data") or []
        if not rows:
            return None
        row = rows[0]
        used = int(row.get("quota_usage", 0))
        total = int((row.get("config") or {}).get("quota_total", 100))
        return used, total

    # ---- containers ---------------------------------------------------------------------------------

    def image_container(
        self, image_url: str, *, caption: str | None = None, alt_text: str | None = None, carousel_item: bool = False
    ) -> str:
        data: dict[str, Any] = {"image_url": image_url}
        if carousel_item:
            data["is_carousel_item"] = True
        else:
            data["caption"] = caption or None
        if alt_text:
            data["alt_text"] = alt_text
        return self._create(data)

    def video_container(
        self,
        video_url: str,
        *,
        media_type: str,
        caption: str | None = None,
        cover_url: str | None = None,
        share_to_feed: bool | None = None,
        carousel_item: bool = False,
    ) -> str:
        data: dict[str, Any] = {"video_url": video_url}
        if carousel_item:
            data["is_carousel_item"] = True
            data["media_type"] = "VIDEO"
        else:
            data["media_type"] = media_type
            if media_type != "STORIES":
                data["caption"] = caption or None
        if cover_url:
            data["cover_url"] = cover_url
        if share_to_feed is not None and media_type == "REELS":
            data["share_to_feed"] = share_to_feed
        return self._create(data)

    def story_image_container(self, image_url: str) -> str:
        return self._create({"image_url": image_url, "media_type": "STORIES"})

    def carousel_container(self, children: list[str], caption: str | None) -> str:
        if not 2 <= len(children) <= 10:
            raise ContainerError("A carousel needs between 2 and 10 items.")
        return self._create({"media_type": "CAROUSEL", "children": children, "caption": caption or None})

    def _create(self, data: dict[str, Any]) -> str:
        # Creating a container is safe to retry: an unpublished container is harmless and expires in 24h.
        resp = self.g.post(f"{self.ig_id}/media", data, retry_network=True)
        container_id = resp.get("id")
        if not container_id:
            raise ContainerError(f"Meta did not return a container id: {resp}")
        return str(container_id)

    def container_status(self, container_id: str) -> tuple[str | None, str | None]:
        info = self.g.get(container_id, {"fields": "status_code,status"})
        return info.get("status_code"), info.get("status")

    def wait_until_ready(self, container_id: str, *, timeout: float = 300.0, interval: float = 5.0) -> None:
        """Returns when FINISHED. Raises ContainerAlreadyPublished / ContainerProcessing / ContainerError."""
        deadline = self._clock() + timeout
        while True:
            code, detail = self.container_status(container_id)
            if code == "FINISHED":
                return
            if code == "PUBLISHED":
                raise ContainerAlreadyPublished(f"container {container_id} is already published")
            if code in ("ERROR", "EXPIRED"):
                raise ContainerError(f"Instagram could not process the media ({code}): {detail or 'no details'}")
            if self._clock() >= deadline:
                raise ContainerProcessing(f"Media still processing after {int(timeout)}s; will check again next run.")
            self._sleep(interval)

    # ---- publish ------------------------------------------------------------------------------------

    def publish(self, container_id: str, *, story: bool = False, since: datetime | None = None) -> Published:
        try:
            resp = self.g.post(f"{self.ig_id}/media_publish", {"creation_id": container_id}, retry_network=False)
            media_id = str(resp["id"])
        except (GraphError, KeyError) as exc:
            # The request may have succeeded even though we didn't get the answer. Ask the container.
            try:
                status, _ = self.container_status(container_id)
            except GraphError:
                status = None
            if status != "PUBLISHED":
                raise
            log.warning("media_publish reported %s but the container is PUBLISHED; recovering media id", exc)
            return self.recover_published(story=story, since=since)
        return Published(media_id=media_id, permalink=self.permalink(media_id))

    def recover_published(self, *, story: bool, since: datetime | None) -> Published:
        """Find the post we just made when Meta published it but we never got the id back."""
        rows = self.recent_stories() if story else self.recent_media(10)
        cutoff = (since - timedelta(minutes=2)) if since else None
        for row in rows:  # newest first
            ts = _parse_ts(row.get("timestamp"))
            if cutoff is None or (ts is not None and ts >= cutoff):
                media_id = str(row["id"])
                return Published(media_id=media_id, permalink=row.get("permalink") or self.permalink(media_id))
        raise ContainerError("Instagram says it was published, but the new post could not be found; check the profile.")

    def permalink(self, media_id: str) -> str | None:
        try:
            return self.g.get(media_id, {"fields": "permalink"}).get("permalink")
        except GraphError:
            return None

    def recent_media(self, limit: int = 15) -> list[dict[str, Any]]:
        data = self.g.get(
            f"{self.ig_id}/media",
            {"fields": "id,caption,media_type,media_product_type,permalink,timestamp", "limit": limit},
        )
        rows = data.get("data") or []
        return [r for r in rows if isinstance(r, dict)]

    def recent_stories(self) -> list[dict[str, Any]]:
        data = self.g.get(f"{self.ig_id}/stories", {"fields": "id,permalink,timestamp"})
        rows = data.get("data") or []
        return [r for r in rows if isinstance(r, dict)]

    def find_duplicate(self, caption: str, since: datetime, limit: int = 15) -> dict[str, Any] | None:
        """Guards against double-posting if a previous run published but crashed before saving state.

        Only posts made since `since` count, so a caption that is deliberately reused weeks later still goes out.
        """
        needle = _norm(caption)
        if not needle:
            return None
        for row in self.recent_media(limit):
            ts = _parse_ts(row.get("timestamp"))
            if ts is None or ts < since:
                continue
            if _norm(row.get("caption") or "") == needle:
                return row
        return None

    # ---- insights -----------------------------------------------------------------------------------

    def media_insights(self, media_id: str, metrics: list[str]) -> dict[str, int]:
        """Asks for each metric separately so one unsupported metric doesn't sink the rest."""
        out: dict[str, int] = {}
        for metric in metrics:
            try:
                data = self.g.get(f"{media_id}/insights", {"metric": metric})
            except GraphError as exc:
                log.info("metric %s unavailable for %s: %s", metric, media_id, exc)
                continue
            value = _first_value(data)
            if value is not None:
                out[metric] = value
        return out

    def account_insights(self, metrics: list[str], since: int, until: int) -> dict[str, int]:
        out: dict[str, int] = {}
        for metric in metrics:
            try:
                data = self.g.get(
                    f"{self.ig_id}/insights",
                    {"metric": metric, "period": "day", "metric_type": "total_value", "since": since, "until": until},
                )
            except GraphError as exc:
                log.info("account metric %s unavailable: %s", metric, exc)
                continue
            value = _first_value(data)
            if value is not None:
                out[metric] = value
        return out


def _parse_ts(value: Any) -> datetime | None:
    """Instagram timestamps look like 2026-10-19T16:31:05+0000."""
    if not value:
        return None
    try:
        return datetime.strptime(str(value), "%Y-%m-%dT%H:%M:%S%z")
    except ValueError:
        try:
            return datetime.fromisoformat(str(value))
        except ValueError:
            return None


def _norm(text: str) -> str:
    return " ".join(text.split()).strip().lower()


def _first_value(payload: dict[str, Any]) -> int | None:
    rows = payload.get("data") or []
    if not rows:
        return None
    row = rows[0]
    total = row.get("total_value")
    if isinstance(total, dict) and "value" in total:
        return int(total["value"])
    values = row.get("values") or []
    if values and isinstance(values[0], dict) and "value" in values[0]:
        v = values[0]["value"]
        return int(v) if isinstance(v, int | float) else None
    return None
