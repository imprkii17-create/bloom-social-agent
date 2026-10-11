"""Facebook Page publishing (Pages API). Optional: only used when a post lists `facebook` in platforms.

Supported here: single image and multi-image posts. Reels/stories to Facebook are skipped with a clear note,
because they need a different, multi-step upload flow that isn't worth the risk until the page exists.
"""

from __future__ import annotations

from datetime import datetime

from .graph import GraphClient, GraphError
from .instagram import Published


class FacebookClient:
    def __init__(self, graph: GraphClient, page_id: str) -> None:
        self.g = graph
        self.page_id = page_id

    def publish_photo(self, image_url: str, message: str) -> Published:
        resp = self.g.post(f"{self.page_id}/photos", {"url": image_url, "caption": message, "published": True})
        post_id = str(resp.get("post_id") or resp["id"])
        return Published(media_id=post_id, permalink=f"https://www.facebook.com/{post_id}")

    def publish_album(self, image_urls: list[str], message: str) -> Published:
        # Upload each photo unpublished, then attach them to a single feed post.
        media_ids = []
        for url in image_urls:
            resp = self.g.post(f"{self.page_id}/photos", {"url": url, "published": False})
            media_ids.append(str(resp["id"]))
        data: dict[str, str] = {"message": message}
        for i, mid in enumerate(media_ids):
            data[f"attached_media[{i}]"] = f'{{"media_fbid":"{mid}"}}'
        resp = self.g.post(f"{self.page_id}/feed", data)
        post_id = str(resp["id"])
        return Published(media_id=post_id, permalink=f"https://www.facebook.com/{post_id}")

    def find_recent(self, message: str, since: datetime) -> Published | None:
        """Look for a page post with this exact text made after `since` (used after an unclear failure)."""
        try:
            data = self.g.get(f"{self.page_id}/posts", {"fields": "id,message,created_time", "limit": 10})
        except GraphError:
            return None
        target = " ".join(message.split())
        for row in data.get("data") or []:
            created = row.get("created_time")
            try:
                when = datetime.strptime(str(created), "%Y-%m-%dT%H:%M:%S%z")
            except ValueError:
                continue
            if when >= since and " ".join(str(row.get("message") or "").split()) == target:
                post_id = str(row["id"])
                return Published(media_id=post_id, permalink=f"https://www.facebook.com/{post_id}")
        return None
