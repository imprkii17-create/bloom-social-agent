from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from bloom_agent.config import Settings
from bloom_agent.facebook import FacebookClient
from bloom_agent.graph import GraphClient
from bloom_agent.instagram import InstagramClient
from bloom_agent.models import Status
from bloom_agent.publisher import Publisher
from bloom_agent.queue import load_post

from .conftest import IG_BASE, IG_ID, MEDIA_BASE, TOKEN


def at(settings: Settings, text: str) -> datetime:
    return datetime.strptime(text, "%Y-%m-%d %H:%M").replace(tzinfo=settings.timezone)


def mock_media_ok(router: respx.MockRouter) -> None:
    router.head(url__startswith=MEDIA_BASE).mock(
        side_effect=lambda req: httpx.Response(
            200, headers={"content-type": "video/mp4" if req.url.path.endswith(".mp4") else "image/jpeg"}
        )
    )


def mock_ig_happy(router: respx.MockRouter, *, recent: list[dict[str, Any]] | None = None) -> dict[str, respx.Route]:
    routes = {
        "quota": router.get(f"{IG_BASE}/{IG_ID}/content_publishing_limit").mock(
            return_value=httpx.Response(200, json={"data": [{"quota_usage": 2, "config": {"quota_total": 100}}]})
        ),
        "recent": router.get(f"{IG_BASE}/{IG_ID}/media").mock(
            return_value=httpx.Response(200, json={"data": recent or []})
        ),
        "create": router.post(f"{IG_BASE}/{IG_ID}/media").mock(return_value=httpx.Response(200, json={"id": "C1"})),
        "status": router.get(f"{IG_BASE}/C1").mock(return_value=httpx.Response(200, json={"status_code": "FINISHED"})),
        "publish": router.post(f"{IG_BASE}/{IG_ID}/media_publish").mock(
            return_value=httpx.Response(200, json={"id": "M1"})
        ),
        "permalink": router.get(f"{IG_BASE}/M1").mock(
            return_value=httpx.Response(200, json={"permalink": "https://www.instagram.com/p/abc/"})
        ),
    }
    return routes


def publisher(settings: Settings, ig_factory: Callable[[], InstagramClient], **kw: Any) -> Publisher:
    return Publisher(settings, instagram=ig_factory, **kw)


@respx.mock
def test_publishes_due_image_post(settings: Settings, make_post: Callable[..., Path], ig_factory: Any) -> None:
    folder = make_post()
    mock_media_ok(respx.mock)
    routes = mock_ig_happy(respx.mock)

    report = publisher(settings, ig_factory).run(at(settings, "2026-10-19 18:31"))

    assert [o.status for o in report.outcomes] == ["published"]
    post = load_post(folder / "post.yaml")
    assert post.status == Status.PUBLISHED
    assert post.result.instagram is not None
    assert post.result.instagram.media_id == "M1"
    assert post.result.instagram.permalink == "https://www.instagram.com/p/abc/"
    assert post.result.ig_pending_container is None

    sent = dict(httpx.QueryParams(routes["create"].calls[0].request.content.decode()))
    assert sent["image_url"] == f"{MEDIA_BASE}/posts/2026-10-19-mon/image.jpg"
    assert sent["caption"].startswith("Rugalmas beosztás")
    assert "END CAPTION" not in sent["caption"] and "Alt text" not in sent["caption"]
    assert sent["alt_text"] == "A smiling woman on a green sofa"
    assert sent["access_token"] == TOKEN


@respx.mock
def test_not_due_and_drafts_are_untouched(settings: Settings, make_post: Callable[..., Path], ig_factory: Any) -> None:
    make_post("a-later", scheduled_at="2026-10-20 18:30")
    make_post("b-draft", status="draft", scheduled_at="2026-10-19 10:00")
    report = publisher(settings, ig_factory).run(at(settings, "2026-10-19 18:31"))
    assert report.outcomes == []
    assert not respx.mock.calls  # no network at all


@respx.mock
def test_late_post_is_marked_missed_not_posted(settings: Settings, make_post: Callable[..., Path], ig_factory: Any) -> None:
    folder = make_post(scheduled_at="2026-10-19 09:00")
    report = publisher(settings, ig_factory).run(at(settings, "2026-10-19 18:00"))
    assert report.outcomes[0].status == "missed"
    assert load_post(folder / "post.yaml").status == Status.MISSED
    assert not respx.mock.calls


@respx.mock
def test_dry_run_changes_nothing(settings: Settings, make_post: Callable[..., Path], ig_factory: Any) -> None:
    folder = make_post()
    before = (folder / "post.yaml").read_text()
    report = publisher(settings, ig_factory).run(at(settings, "2026-10-19 18:31"), dry_run=True)
    assert [o.status for o in report.outcomes] == ["dry-run"]
    assert (folder / "post.yaml").read_text() == before
    assert not respx.mock.calls


@respx.mock
def test_transient_failure_retries_then_gives_up(
    settings: Settings, make_post: Callable[..., Path], ig_factory: Any
) -> None:
    folder = make_post()
    mock_media_ok(respx.mock)
    routes = mock_ig_happy(respx.mock)
    routes["create"].mock(return_value=httpx.Response(400, json={"error": {"message": "Media URL broken", "code": 9004}}))

    pub = publisher(settings, ig_factory)
    for expected_attempts in (1, 2):
        report = pub.run(at(settings, "2026-10-19 18:31"))
        post = load_post(folder / "post.yaml")
        assert report.outcomes[-1].status == "retrying"
        assert post.status == Status.APPROVED
        assert post.result.attempts == expected_attempts
    report = pub.run(at(settings, "2026-10-19 19:01"))
    post = load_post(folder / "post.yaml")
    assert report.outcomes[-1].status == "failed"
    assert post.status == Status.FAILED
    assert "Media URL broken" in (post.result.last_error or "")


@respx.mock
def test_auth_error_fails_immediately_and_hides_token(
    settings: Settings, make_post: Callable[..., Path], ig_factory: Any
) -> None:
    folder = make_post()
    mock_media_ok(respx.mock)
    routes = mock_ig_happy(respx.mock)
    routes["create"].mock(
        return_value=httpx.Response(400, json={"error": {"message": f"Invalid token {TOKEN}", "code": 190}})
    )
    report = publisher(settings, ig_factory).run(at(settings, "2026-10-19 18:31"))
    post = load_post(folder / "post.yaml")
    assert report.outcomes[-1].status == "failed"
    assert post.status == Status.FAILED
    assert TOKEN not in (post.result.last_error or "")
    assert TOKEN not in (folder / "post.yaml").read_text()


@respx.mock
def test_duplicate_caption_is_recorded_not_reposted(
    settings: Settings, make_post: Callable[..., Path], ig_factory: Any
) -> None:
    # A previous attempt exists (e.g. a local run that crashed after posting), so the agent checks for duplicates.
    folder = make_post(extra={"result": {"attempts": 1, "last_error": "crashed"}})
    mock_media_ok(respx.mock)
    caption = load_post(folder / "post.yaml").caption()
    recent = [{"id": "OLD", "caption": caption, "permalink": "https://ig/p/old", "timestamp": "2026-10-19T16:30:20+0000"}]
    routes = mock_ig_happy(respx.mock, recent=recent)
    report = publisher(settings, ig_factory).run(at(settings, "2026-10-19 18:31"))
    assert report.outcomes[0].status == "published"
    assert not routes["create"].called and not routes["publish"].called
    post = load_post(folder / "post.yaml")
    assert post.result.instagram is not None and post.result.instagram.media_id == "OLD"


@respx.mock
def test_quota_exhausted_waits_without_counting_attempt(
    settings: Settings, make_post: Callable[..., Path], ig_factory: Any
) -> None:
    folder = make_post()
    routes = mock_ig_happy(respx.mock)
    full = {"data": [{"quota_usage": 100, "config": {"quota_total": 100}}]}
    routes["quota"].mock(return_value=httpx.Response(200, json=full))
    report = publisher(settings, ig_factory).run(at(settings, "2026-10-19 18:31"))
    assert report.outcomes[0].status == "retrying"
    post = load_post(folder / "post.yaml")
    assert post.status == Status.APPROVED and post.result.attempts == 0
    assert not routes["create"].called


@respx.mock
def test_media_url_must_be_public_jpeg(settings: Settings, make_post: Callable[..., Path], ig_factory: Any) -> None:
    folder = make_post()
    mock_ig_happy(respx.mock)
    respx.mock.head(url__startswith=MEDIA_BASE).mock(return_value=httpx.Response(404))
    report = publisher(settings, ig_factory).run(at(settings, "2026-10-19 18:31"))
    assert report.outcomes[0].status == "retrying"
    assert "HTTP 404" in (load_post(folder / "post.yaml").result.last_error or "")


@respx.mock
def test_reel_still_processing_is_resumed_next_run(
    settings: Settings, make_post: Callable[..., Path], ig_factory: Any
) -> None:
    folder = make_post(type="reel", media=["video.mp4"])
    mock_media_ok(respx.mock)
    routes = mock_ig_happy(respx.mock)
    routes["status"].mock(return_value=httpx.Response(200, json={"status_code": "IN_PROGRESS"}))

    clock = iter(range(0, 10_000, 100))

    def slow_factory() -> InstagramClient:
        graph = GraphClient(settings.ig_base, TOKEN, sleep=lambda s: None)
        return InstagramClient(graph, IG_ID, sleep=lambda s: None, clock=lambda: float(next(clock)))

    report = Publisher(settings, instagram=slow_factory).run(at(settings, "2026-10-19 18:31"))
    post = load_post(folder / "post.yaml")
    assert report.outcomes[-1].status == "processing"
    assert post.result.ig_pending_container == "C1"
    assert post.result.attempts == 0  # processing is not a failure
    sent = dict(httpx.QueryParams(routes["create"].calls[0].request.content.decode()))
    assert sent["media_type"] == "REELS" and sent["share_to_feed"] == "true"

    # Next run: processing finished → publish the same container, no new upload.
    routes["status"].mock(return_value=httpx.Response(200, json={"status_code": "FINISHED"}))
    creates_before = routes["create"].call_count
    report = Publisher(settings, instagram=ig_factory).run(at(settings, "2026-10-19 18:46"))
    post = load_post(folder / "post.yaml")
    assert report.outcomes[-1].status == "published"
    assert routes["create"].call_count == creates_before
    assert post.status == Status.PUBLISHED


@respx.mock
def test_partial_success_only_retries_missing_platform(
    settings: Settings, make_post: Callable[..., Path], ig_factory: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    folder = make_post(platforms=["instagram", "facebook"])
    mock_media_ok(respx.mock)
    routes = mock_ig_happy(respx.mock)
    fb_route = respx.mock.post("https://graph.facebook.com/v25.0/PAGE/photos").mock(
        return_value=httpx.Response(500, json={"error": {"message": "FB down", "code": 2}})
    )

    def fb_factory() -> FacebookClient:
        return FacebookClient(GraphClient("https://graph.facebook.com/v25.0", "FBTOKEN", sleep=lambda s: None), "PAGE")

    pub = Publisher(settings, instagram=ig_factory, facebook=fb_factory)
    pub.run(at(settings, "2026-10-19 18:31"))
    post = load_post(folder / "post.yaml")
    assert post.result.instagram is not None and post.result.facebook is None
    assert post.status == Status.APPROVED

    assert fb_route.call_count == 1  # a 5xx on a non-idempotent POST is never retried blindly
    respx.mock.get("https://graph.facebook.com/v25.0/PAGE/posts").mock(return_value=httpx.Response(200, json={"data": []}))
    fb_route.mock(return_value=httpx.Response(200, json={"id": "PH1", "post_id": "PAGE_POST1"}))
    publishes_before = routes["publish"].call_count
    Publisher(settings, instagram=ig_factory, facebook=fb_factory).run(at(settings, "2026-10-19 18:46"))
    post = load_post(folder / "post.yaml")
    assert post.status == Status.PUBLISHED
    assert post.result.facebook is not None and post.result.facebook.media_id == "PAGE_POST1"
    assert routes["publish"].call_count == publishes_before  # Instagram was not posted twice


@respx.mock
def test_invalid_approved_post_is_blocked(settings: Settings, make_post: Callable[..., Path], ig_factory: Any) -> None:
    folder = make_post(caption="Too many tags #a #b #c #d #e #f")
    report = publisher(settings, ig_factory).run(at(settings, "2026-10-19 18:31"))
    assert report.outcomes[0].status == "blocked"
    assert load_post(folder / "post.yaml").status == Status.FAILED
    assert not respx.mock.calls


@respx.mock
def test_publish_recovers_when_answer_is_lost(settings: Settings, make_post: Callable[..., Path], ig_factory: Any) -> None:
    folder = make_post()
    mock_media_ok(respx.mock)
    routes = mock_ig_happy(respx.mock)
    routes["publish"].mock(side_effect=httpx.ReadTimeout("lost"))
    # After the lost answer, the container reports PUBLISHED and the newest media is ours.
    routes["status"].mock(side_effect=[
        httpx.Response(200, json={"status_code": "FINISHED"}),
        httpx.Response(200, json={"status_code": "PUBLISHED"}),
    ])
    routes["recent"].mock(return_value=httpx.Response(
        200, json={"data": [{"id": "M1", "timestamp": "2026-10-19T16:31:30+0000"}]}
    ))
    report = publisher(settings, ig_factory).run(at(settings, "2026-10-19 18:31"))
    assert report.outcomes[0].status == "published"
    assert routes["publish"].call_count == 1  # never retried blindly
    assert load_post(folder / "post.yaml").result.instagram.media_id == "M1"  # type: ignore[union-attr]


@respx.mock
def test_carousel_and_story(settings: Settings, make_post: Callable[..., Path], ig_factory: Any) -> None:
    carousel = make_post("c", type="carousel", media=["a.jpg", "b.jpg", "c.jpg"])
    story = make_post("s", type="story", media=["story.jpg"], caption=None, scheduled_at="2026-10-19 18:35")
    mock_media_ok(respx.mock)
    routes = mock_ig_happy(respx.mock)
    ids = iter(["K1", "K2", "K3", "CAR", "ST"])
    routes["create"].mock(side_effect=lambda req: httpx.Response(200, json={"id": next(ids)}))
    respx.mock.get(path__regex=r"/v25\.0/(K1|K2|K3|CAR|ST)$").mock(
        return_value=httpx.Response(200, json={"status_code": "FINISHED"})
    )

    report = publisher(settings, ig_factory).run(at(settings, "2026-10-19 18:40"))
    assert [o.status for o in report.outcomes] == ["published", "published"]
    bodies = [dict(httpx.QueryParams(c.request.content.decode())) for c in routes["create"].calls]
    assert all(b.get("is_carousel_item") == "true" for b in bodies[:3])
    assert bodies[3]["media_type"] == "CAROUSEL" and bodies[3]["children"] == "K1,K2,K3"
    assert bodies[4]["media_type"] == "STORIES" and "caption" not in bodies[4]
    assert load_post(carousel / "post.yaml").status == Status.PUBLISHED
    assert load_post(story / "post.yaml").status == Status.PUBLISHED


@respx.mock
def test_facebook_album(settings: Settings, make_post: Callable[..., Path], ig_factory: Any) -> None:
    folder = make_post(type="carousel", media=["a.jpg", "b.jpg"], platforms=["facebook"])
    mock_media_ok(respx.mock)
    photo_ids = iter(["P1", "P2"])
    respx.mock.post("https://graph.facebook.com/v25.0/PAGE/photos").mock(
        side_effect=lambda req: httpx.Response(200, json={"id": next(photo_ids)})
    )
    feed = respx.mock.post("https://graph.facebook.com/v25.0/PAGE/feed").mock(
        return_value=httpx.Response(200, json={"id": "PAGE_9"})
    )

    def fb_factory() -> FacebookClient:
        return FacebookClient(GraphClient("https://graph.facebook.com/v25.0", "FB", sleep=lambda s: None), "PAGE")

    Publisher(settings, instagram=ig_factory, facebook=fb_factory).run(at(settings, "2026-10-19 18:31"))
    body = dict(httpx.QueryParams(feed.calls[0].request.content.decode()))
    assert body["attached_media[0]"] == '{"media_fbid":"P1"}' and body["attached_media[1]"] == '{"media_fbid":"P2"}'
    assert load_post(folder / "post.yaml").status == Status.PUBLISHED


# ---- safety features added after review -------------------------------------------------------------


@respx.mock
def test_checkpoint_failure_means_nothing_is_published(
    settings: Settings, make_post: Callable[..., Path], ig_factory: Any
) -> None:
    folder = make_post()
    mock_media_ok(respx.mock)
    routes = mock_ig_happy(respx.mock)

    def broken_checkpoint(post: Any) -> None:
        raise RuntimeError("push rejected")

    report = Publisher(settings, instagram=ig_factory, checkpoint=broken_checkpoint).run(at(settings, "2026-10-19 18:31"))
    assert report.outcomes[-1].status == "retrying"
    assert not routes["publish"].called
    assert "nothing was published" in (load_post(folder / "post.yaml").result.last_error or "")


@respx.mock
def test_checkpoint_runs_before_publish(settings: Settings, make_post: Callable[..., Path], ig_factory: Any) -> None:
    make_post()
    mock_media_ok(respx.mock)
    routes = mock_ig_happy(respx.mock)
    seen: list[tuple[str | None, bool]] = []

    def checkpoint(post: Any) -> None:
        on_disk = load_post(post.folder / "post.yaml")
        seen.append((on_disk.result.ig_pending_container, routes["publish"].called))

    Publisher(settings, instagram=ig_factory, checkpoint=checkpoint).run(at(settings, "2026-10-19 18:31"))
    assert seen == [("C1", False)]  # container saved to disk, and publish not yet called


@respx.mock
def test_saved_published_container_is_recovered_never_reposted(
    settings: Settings, make_post: Callable[..., Path], ig_factory: Any
) -> None:
    folder = make_post()
    post = load_post(folder / "post.yaml")
    post.result.ig_pending_container = "C1"
    post.result.ig_pending_fingerprint = post.fingerprint()
    post.result.ig_attempt_started_at = "2026-10-19T16:30:00+00:00"
    from bloom_agent.queue import save_post

    save_post(post)
    routes = mock_ig_happy(respx.mock, recent=[{"id": "M7", "timestamp": "2026-10-19T16:30:40+0000", "permalink": "p"}])
    routes["status"].mock(return_value=httpx.Response(200, json={"status_code": "PUBLISHED"}))
    report = publisher(settings, ig_factory).run(at(settings, "2026-10-19 18:46"))
    assert report.outcomes[0].status == "published"
    assert not routes["create"].called and not routes["publish"].called
    assert load_post(folder / "post.yaml").result.instagram.media_id == "M7"  # type: ignore[union-attr]


@respx.mock
def test_changed_post_discards_old_container(settings: Settings, make_post: Callable[..., Path], ig_factory: Any) -> None:
    folder = make_post()
    post = load_post(folder / "post.yaml")
    post.result.ig_pending_container = "OLDC"
    post.result.ig_pending_fingerprint = "stale-fingerprint"
    from bloom_agent.queue import save_post

    save_post(post)
    mock_media_ok(respx.mock)
    routes = mock_ig_happy(respx.mock)
    respx.mock.get(f"{IG_BASE}/OLDC").mock(return_value=httpx.Response(200, json={"status_code": "FINISHED"}))
    publisher(settings, ig_factory).run(at(settings, "2026-10-19 18:31"))
    assert routes["create"].called
    assert routes["publish"].calls[0].request.content.decode().count("creation_id=C1") == 1


@respx.mock
def test_changed_post_whose_old_container_went_live_is_not_reposted(
    settings: Settings, make_post: Callable[..., Path], ig_factory: Any
) -> None:
    folder = make_post()
    post = load_post(folder / "post.yaml")
    post.result.ig_pending_container = "OLDC"
    post.result.ig_pending_fingerprint = "from-before-the-edit"
    post.result.ig_attempt_started_at = "2026-10-19T16:30:00+00:00"
    from bloom_agent.queue import save_post

    save_post(post)
    routes = mock_ig_happy(respx.mock, recent=[{"id": "LIVE", "timestamp": "2026-10-19T16:30:30+0000", "permalink": "p"}])
    respx.mock.get(f"{IG_BASE}/OLDC").mock(return_value=httpx.Response(200, json={"status_code": "PUBLISHED"}))
    publisher(settings, ig_factory).run(at(settings, "2026-10-19 18:46"))
    assert not routes["create"].called and not routes["publish"].called
    assert load_post(folder / "post.yaml").result.instagram.media_id == "LIVE"  # type: ignore[union-attr]


@respx.mock
def test_unapproved_during_checkpoint_is_not_posted(
    settings: Settings, make_post: Callable[..., Path], ig_factory: Any
) -> None:
    folder = make_post()
    mock_media_ok(respx.mock)
    routes = mock_ig_happy(respx.mock)

    def checkpoint_that_pulls_user_edit(post: Any) -> None:
        # Simulates the rebase in commit-back.sh bringing in the user's "status: draft" edit.
        path = post.folder / "post.yaml"
        path.write_text(path.read_text().replace("status: approved", "status: draft"))

    report = Publisher(settings, instagram=ig_factory, checkpoint=checkpoint_that_pulls_user_edit).run(
        at(settings, "2026-10-19 18:31")
    )
    assert report.outcomes[-1].status == "skipped"
    assert not routes["publish"].called
    assert load_post(folder / "post.yaml").status == Status.DRAFT  # the user's edit is kept


@respx.mock
def test_expired_container_long_after_schedule_is_missed(
    settings: Settings, make_post: Callable[..., Path], ig_factory: Any
) -> None:
    folder = make_post(type="reel", media=["video.mp4"])
    post = load_post(folder / "post.yaml")
    post.result.ig_pending_container = "OLDC"
    post.result.ig_pending_fingerprint = post.fingerprint()
    from bloom_agent.queue import save_post

    save_post(post)
    routes = mock_ig_happy(respx.mock)
    respx.mock.get(f"{IG_BASE}/OLDC").mock(return_value=httpx.Response(200, json={"status_code": "EXPIRED"}))
    report = publisher(settings, ig_factory).run(at(settings, "2026-10-20 18:40"))
    assert report.outcomes[-1].status == "missed"
    assert load_post(folder / "post.yaml").status == Status.MISSED
    assert not routes["create"].called


@respx.mock
def test_unknown_container_status_does_not_create_new_post(
    settings: Settings, make_post: Callable[..., Path], ig_factory: Any
) -> None:
    folder = make_post()
    post = load_post(folder / "post.yaml")
    post.result.ig_pending_container = "OLDC"
    post.result.ig_pending_fingerprint = post.fingerprint()
    from bloom_agent.queue import save_post

    save_post(post)
    routes = mock_ig_happy(respx.mock)
    respx.mock.get(f"{IG_BASE}/OLDC").mock(return_value=httpx.Response(200, json={}))
    report = publisher(settings, ig_factory).run(at(settings, "2026-10-19 18:46"))
    assert report.outcomes[-1].status == "retrying"
    assert not routes["create"].called


@respx.mock
def test_old_identical_caption_does_not_block_new_post(
    settings: Settings, make_post: Callable[..., Path], ig_factory: Any
) -> None:
    folder = make_post()
    caption = load_post(folder / "post.yaml").caption()
    mock_media_ok(respx.mock)
    routes = mock_ig_happy(respx.mock, recent=[{"id": "OLD", "caption": caption, "timestamp": "2026-09-01T16:30:00+0000"}])
    publisher(settings, ig_factory).run(at(settings, "2026-10-19 18:31"))
    assert routes["publish"].called
    assert load_post(folder / "post.yaml").result.instagram.media_id == "M1"  # type: ignore[union-attr]


@respx.mock
def test_reapproved_failed_post_gets_fresh_attempts(
    settings: Settings, make_post: Callable[..., Path], ig_factory: Any
) -> None:
    folder = make_post(extra={"result": {"attempts": 3, "last_error": "old problem"}})
    mock_media_ok(respx.mock)
    routes = mock_ig_happy(respx.mock)
    routes["create"].mock(return_value=httpx.Response(400, json={"error": {"message": "temporary glitch", "code": 9004}}))
    report = publisher(settings, ig_factory).run(at(settings, "2026-10-19 18:31"))
    post = load_post(folder / "post.yaml")
    assert report.outcomes[-1].status == "retrying"
    assert post.status == Status.APPROVED and post.result.attempts == 1


@respx.mock
def test_unreadable_post_and_unconverted_png_are_reported(
    settings: Settings, make_post: Callable[..., Path], ig_factory: Any, repo: Path
) -> None:
    from .conftest import make_image

    make_post("png-post", media=["image.png"], create_media=False)
    make_image(repo / "posts/png-post/image.png")
    broken = repo / "posts" / "broken"
    broken.mkdir()
    (broken / "post.yaml").write_text("id: broken\nstatus: aproved\nscheduled_at: 2026-10-19 18:00\nmedia: [a.jpg]\n")
    report = publisher(settings, ig_factory).run(at(settings, "2026-10-19 18:31"))
    statuses = {o.post_id: o.status for o in report.outcomes}
    assert statuses == {"broken": "blocked", "png-post": "waiting"}
    assert load_post(repo / "posts/png-post/post.yaml").status == Status.APPROVED
    assert report.failures and report.failures[0].post_id == "broken"


@respx.mock
def test_facebook_without_config_fails_post_not_run(
    settings: Settings, make_post: Callable[..., Path], ig_factory: Any
) -> None:
    fb_only = make_post("fb", platforms=["facebook"])
    ig_post = make_post("ig", scheduled_at="2026-10-19 18:35")
    mock_media_ok(respx.mock)
    mock_ig_happy(respx.mock)
    publisher(settings, ig_factory).run(at(settings, "2026-10-19 18:40"))
    assert load_post(fb_only / "post.yaml").status == Status.FAILED
    assert load_post(ig_post / "post.yaml").status == Status.PUBLISHED  # the other post still went out


def test_dst_night_is_compared_in_utc(settings: Settings, make_post: Callable[..., Path]) -> None:
    """On 2026-10-25 Budapest clocks go 03:00 CEST → 02:00 CET; 02:30 happens twice."""
    from datetime import UTC

    from bloom_agent.queue import due_posts, load_queue

    make_post("night", scheduled_at="2026-10-25 02:30")
    posts, _ = load_queue(settings.posts_dir)
    before = datetime(2026, 10, 25, 0, 20, tzinfo=UTC)  # 02:20 CEST
    after = datetime(2026, 10, 25, 1, 40, tzinfo=UTC)  # 02:40 CET
    assert due_posts(posts, before, settings.timezone) == []
    assert [p.id for p in due_posts(posts, after, settings.timezone)] == ["night"]


def test_scheduled_at_accepts_what_people_type(repo: Path, make_post: Callable[..., Path]) -> None:
    folder = make_post()
    text = (folder / "post.yaml").read_text().replace("scheduled_at: 2026-10-19 18:30", "scheduled_at: 2026-10-19 18:30:00")
    (folder / "post.yaml").write_text(text)  # YAML turns this into a datetime object
    assert load_post(folder / "post.yaml").scheduled_at == "2026-10-19 18:30"
    (folder / "post.yaml").write_text(text.replace("2026-10-19 18:30:00", "2026-10-19"))
    with pytest.raises(Exception, match="needs a time"):
        load_post(folder / "post.yaml")
