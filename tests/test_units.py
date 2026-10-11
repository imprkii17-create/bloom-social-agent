from __future__ import annotations

import base64
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
from nacl import public
from PIL import Image

from bloom_agent.cli import main
from bloom_agent.config import Settings, load_settings
from bloom_agent.github import encrypt_secret, open_or_update_issue, set_actions_secret
from bloom_agent.graph import GraphClient, GraphError
from bloom_agent.instagram import InstagramClient
from bloom_agent.models import Platform, Post, PostType, Status, extract_caption
from bloom_agent.prepare import prepare_all
from bloom_agent.queue import load_post, load_queue
from bloom_agent.report import PostStats, render
from bloom_agent.review import parse_review
from bloom_agent.tokens import days_left, refresh_instagram_token, write_status
from bloom_agent.validate import count_hashtags, find_banned, validate_all

from .conftest import CAPTION_FILE, IG_BASE, IG_ID, TOKEN, make_image

# ---- models / queue ------------------------------------------------------------------------------------


def test_extract_caption_strips_markers_and_alt_text() -> None:
    text = extract_caption(CAPTION_FILE)
    assert text.startswith("Rugalmas") and text.endswith("#flexiblework")
    assert extract_caption("  plain caption \n") == "plain caption"


def test_yaml_roundtrip_and_platform_logic(make_post: Callable[..., Path]) -> None:
    folder = make_post(type="reel", media=["video.mp4"], platforms=["instagram", "facebook"])
    post = load_post(folder / "post.yaml")
    assert post.supported_platforms() == [Platform.INSTAGRAM]  # facebook can't take reels here
    assert post.remaining_platforms() == [Platform.INSTAGRAM]
    reloaded = Post.model_validate(__import__("yaml").safe_load(post.to_yaml()))
    assert reloaded.type == PostType.REEL and reloaded.status == Status.APPROVED


def test_bad_yaml_is_reported_not_crashing(repo: Path, make_post: Callable[..., Path]) -> None:
    make_post("good")
    bad = repo / "posts" / "bad"
    bad.mkdir()
    (bad / "post.yaml").write_text("id: bad\nscheduled_at: tomorrow\nmedia: [a.jpg]\n")
    posts, errors = load_queue(repo / "posts")
    assert [p.id for p in posts] == ["good"]
    assert errors and "scheduled_at" in errors[0].message


def test_id_must_match_folder(repo: Path, make_post: Callable[..., Path]) -> None:
    folder = make_post("x")
    (folder / "post.yaml").write_text((folder / "post.yaml").read_text().replace("id: x", "id: y"))
    _, errors = load_queue(repo / "posts")
    assert "must match" in errors[0].message


# ---- validation ----------------------------------------------------------------------------------------


def test_hashtag_count_ignores_html_entities_and_urls() -> None:
    assert count_hashtags("#a #b text#c &#39; #d") == 3


def test_banned_words_are_whole_words_only() -> None:
    banned = ("sex", "webcam", "felnőtt")
    assert find_banned("Visit Sussex. Great sextant.", banned) == []
    assert find_banned("Join our WEBCAM team", banned) == ["webcam"]
    assert find_banned("felnőtt tartalom", banned) == ["felnőtt"]


def _errors(settings: Settings) -> list[str]:
    posts, errs = load_queue(settings.posts_dir)
    return [i.message for i in validate_all(posts, errs, settings) if i.level == "error"]


def test_validate_good_post_passes(settings: Settings, make_post: Callable[..., Path]) -> None:
    make_post()
    assert _errors(settings) == []


def test_validate_catches_common_mistakes(settings: Settings, make_post: Callable[..., Path], repo: Path) -> None:
    make_post("tags", caption="Hello #1 #2 #3 #4 #5 #6")
    make_post("banned", caption="Best webcam jobs #bloom")
    make_post("png", media=["image.png"], create_media=False)
    make_image(repo / "posts/png/image.png")
    make_post("wide", media=["wide.jpg"], create_media=False)
    make_image(repo / "posts/wide/wide.jpg", size=(2000, 800))
    make_post("missing", media=["nope.jpg"], create_media=False)
    make_post("carousel1", type="carousel", media=["a.jpg"])
    errors = " | ".join(_errors(settings))
    assert "6 hashtags" in errors
    assert "webcam" in errors
    assert "only accepts JPEG" in errors
    assert "aspect ratio 2000x800" in errors
    assert "not found: nope.jpg" in errors
    assert "carousel needs 2–10" in errors


def test_validate_cli_exit_codes(repo: Path, make_post: Callable[..., Path]) -> None:
    make_post()
    assert main(["--root", str(repo), "validate"]) == 0
    make_post("bad", caption="#1 #2 #3 #4 #5 #6")
    assert main(["--root", str(repo), "validate"]) == 1


# ---- prepare -------------------------------------------------------------------------------------------


def test_prepare_converts_png_with_transparency(settings: Settings, make_post: Callable[..., Path], repo: Path) -> None:
    folder = make_post(media=["post.png"], create_media=False)
    make_image(folder / "post.png", size=(2160, 2700), mode="RGBA")
    posts, _ = load_queue(settings.posts_dir)
    result = prepare_all(posts)
    assert result.converted == ["2026-10-19-mon/post.png → post.jpg"]
    assert not (folder / "post.png").exists()
    with Image.open(folder / "post.jpg") as im:
        assert im.format == "JPEG" and im.mode == "RGB" and im.size == (1440, 1800)
    assert load_post(folder / "post.yaml").media == ["post.jpg"]
    assert _errors(settings) == []
    # Running again is a no-op.
    posts, _ = load_queue(settings.posts_dir)
    again = prepare_all(posts)
    assert again.converted == [] and again.resized == []


def test_prepare_leaves_published_posts_alone(settings: Settings, make_post: Callable[..., Path]) -> None:
    folder = make_post(status="published", media=["post.png"], create_media=False)
    make_image(folder / "post.png")
    posts, _ = load_queue(settings.posts_dir)
    assert prepare_all(posts).converted == []
    assert (folder / "post.png").exists()


# ---- graph client --------------------------------------------------------------------------------------


@respx.mock
def test_graph_retries_transient_get_and_redacts_token() -> None:
    route = respx.get(f"{IG_BASE}/me").mock(side_effect=[
        httpx.Response(500, json={"error": {"message": "temporary", "code": 2, "is_transient": True}}),
        httpx.Response(200, json={"id": "1"}),
    ])
    sleeps: list[float] = []
    g = GraphClient(IG_BASE, TOKEN, sleep=sleeps.append)
    assert g.get("me") == {"id": "1"}
    assert route.call_count == 2 and sleeps == [2]

    respx.get(f"{IG_BASE}/bad").mock(
        return_value=httpx.Response(400, json={"error": {"message": f"bad token {TOKEN}", "code": 190}})
    )
    with pytest.raises(GraphError) as info:
        g.get("bad")
    assert TOKEN not in str(info.value) and info.value.is_auth_error


@respx.mock
def test_graph_never_retries_publish_on_network_error() -> None:
    route = respx.post(f"{IG_BASE}/{IG_ID}/media_publish").mock(side_effect=httpx.ConnectError("boom"))
    g = GraphClient(IG_BASE, TOKEN, sleep=lambda s: None)
    with pytest.raises(GraphError):
        g.post(f"{IG_ID}/media_publish", {"creation_id": "1"})
    assert route.call_count == 1


@respx.mock
def test_insights_parsing_skips_unsupported_metrics() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        metric = request.url.params["metric"]
        if metric == "reach":
            return httpx.Response(200, json={"data": [{"name": "reach", "values": [{"value": 120}]}]})
        if metric == "views":
            return httpx.Response(200, json={"data": [{"name": "views", "total_value": {"value": 300}}]})
        return httpx.Response(400, json={"error": {"message": "unsupported metric", "code": 100}})

    respx.get(f"{IG_BASE}/M1/insights").mock(side_effect=handler)
    ig = InstagramClient(GraphClient(IG_BASE, TOKEN, sleep=lambda s: None), IG_ID)
    assert ig.media_insights("M1", ["reach", "views", "shares"]) == {"reach": 120, "views": 300}


# ---- tokens & github -----------------------------------------------------------------------------------


@respx.mock
def test_token_refresh_success_and_failure(tmp_path: Path) -> None:
    route = respx.get("https://graph.instagram.com/refresh_access_token").mock(
        return_value=httpx.Response(200, json={"access_token": "NEW", "token_type": "bearer", "expires_in": 5183944})
    )
    new = refresh_instagram_token("OLD")
    assert new.access_token == "NEW"
    assert route.calls[0].request.url.params["grant_type"] == "ig_refresh_token"
    write_status(tmp_path, new.expires_at)
    assert days_left(tmp_path) in (59, 60)
    assert "NEW" not in (tmp_path / "state/token-status.json").read_text()

    route.mock(return_value=httpx.Response(400, json={"error": {"message": "Session expired for OLD", "code": 190}}))
    with pytest.raises(GraphError) as info:
        refresh_instagram_token("OLD")
    assert "OLD" not in str(info.value)


def test_secret_encryption_roundtrip() -> None:
    private = public.PrivateKey.generate()
    pub_b64 = base64.b64encode(bytes(private.public_key)).decode()
    sealed = base64.b64decode(encrypt_secret(pub_b64, "s3cret"))
    assert public.SealedBox(private).decrypt(sealed) == b"s3cret"


@respx.mock
def test_set_actions_secret_and_issue_dedupe() -> None:
    private = public.PrivateKey.generate()
    respx.get("https://api.github.com/repos/me/repo/actions/secrets/public-key").mock(
        return_value=httpx.Response(200, json={"key_id": "K1", "key": base64.b64encode(bytes(private.public_key)).decode()})
    )
    put = respx.put("https://api.github.com/repos/me/repo/actions/secrets/IG_ACCESS_TOKEN").mock(
        return_value=httpx.Response(204)
    )
    set_actions_secret("me/repo", "ADMIN", "IG_ACCESS_TOKEN", "tok")
    body = json.loads(put.calls[0].request.content)
    assert body["key_id"] == "K1"
    assert public.SealedBox(private).decrypt(base64.b64decode(body["encrypted_value"])) == b"tok"

    respx.get("https://api.github.com/repos/me/repo/issues").mock(
        return_value=httpx.Response(200, json=[{"number": 7, "title": "Alert", "html_url": "https://gh/7"}])
    )
    comment = respx.post("https://api.github.com/repos/me/repo/issues/7/comments").mock(
        return_value=httpx.Response(201, json={})
    )
    assert open_or_update_issue("me/repo", "T", "Alert", "again") == "https://gh/7"
    assert comment.called


# ---- report / review / cli -----------------------------------------------------------------------------


def test_report_renders_ranking(settings: Settings, make_post: Callable[..., Path]) -> None:
    a = load_post(make_post("a") / "post.yaml")
    b = load_post(make_post("b") / "post.yaml")
    md = render(
        [PostStats(a, {"reach": 10, "likes": 1}), PostStats(b, {"reach": 50, "shares": 5, "profile_visits": 4})],
        {"reach": 70, "website_clicks": 3}, 12, days=7, token_days=5,
    )
    assert md.index("| b |") < md.index("| a |")
    assert "Link clicks:** 3" in md and "expires in 5 days" in md and "Best this week:** `b`" in md


def test_parse_review_is_robust() -> None:
    ok = parse_review('Here you go: {"verdict": "ok", "issues": [], "suggestions": ["More emoji"]}')
    assert ok.verdict == "ok" and ok.suggestions == ["More emoji"]
    assert parse_review("no json at all").verdict == "fix"


def test_cli_new_and_approve(repo: Path) -> None:
    assert main(["--root", str(repo), "new", "2026-11-02-mon", "--at", "2026-11-02 18:30"]) == 0
    post = load_post(repo / "posts/2026-11-02-mon/post.yaml")
    assert post.status == Status.DRAFT
    assert main(["--root", str(repo), "approve", "2026-11-02-mon"]) == 0
    assert load_post(repo / "posts/2026-11-02-mon/post.yaml").status == Status.APPROVED


def test_settings_default_media_url_in_actions(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MEDIA_BASE_URL")
    monkeypatch.setenv("GITHUB_REPOSITORY", "me/bloom")
    monkeypatch.setenv("GITHUB_SHA", "abc123")
    assert load_settings(repo).media_base_url == "https://raw.githubusercontent.com/me/bloom/abc123"


def test_publish_cli_writes_summary(repo: Path, make_post: Callable[..., Path], monkeypatch: pytest.MonkeyPatch) -> None:
    make_post()
    summary = repo / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    assert main(["--root", str(repo), "publish", "--dry-run", "--now", "2026-10-19 18:31"]) == 0
    assert "would publish image" in summary.read_text()


# ---- cli commands against mocked services --------------------------------------------------------------


@respx.mock
def test_cli_check_and_refresh(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    respx.get(f"{IG_BASE}/me").mock(return_value=httpx.Response(
        200, json={"username": "bloommodels.budapest", "account_type": "BUSINESS", "followers_count": 5, "media_count": 3}
    ))
    respx.get(f"{IG_BASE}/{IG_ID}/content_publishing_limit").mock(
        return_value=httpx.Response(200, json={"data": [{"quota_usage": 0, "config": {"quota_total": 100}}]})
    )
    summary = repo / "s.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    assert main(["--root", str(repo), "check"]) == 0
    assert "@bloommodels.budapest" in summary.read_text()

    respx.get("https://graph.instagram.com/refresh_access_token").mock(
        return_value=httpx.Response(200, json={"access_token": "NEW", "expires_in": 5183944})
    )
    # Without GH_ADMIN_TOKEN the new token can't be stored → clear message, non-zero exit.
    assert main(["--root", str(repo), "refresh-token"]) == 1
    assert "GH_ADMIN_TOKEN" in summary.read_text()

    private = public.PrivateKey.generate()
    monkeypatch.setenv("GH_ADMIN_TOKEN", "ADMIN")
    monkeypatch.setenv("GITHUB_REPOSITORY", "me/repo")
    respx.get("https://api.github.com/repos/me/repo/actions/secrets/public-key").mock(
        return_value=httpx.Response(200, json={"key_id": "K", "key": base64.b64encode(bytes(private.public_key)).decode()})
    )
    respx.put("https://api.github.com/repos/me/repo/actions/secrets/IG_ACCESS_TOKEN").mock(
        return_value=httpx.Response(201)
    )
    assert main(["--root", str(repo), "refresh-token"]) == 0
    assert (repo / "state/token-status.json").is_file()


@respx.mock
def test_cli_report(repo: Path, make_post: Callable[..., Path]) -> None:
    from datetime import UTC, datetime

    folder = make_post(status="published")
    post = load_post(folder / "post.yaml")
    from bloom_agent.models import PlatformResult
    from bloom_agent.queue import save_post

    post.result.instagram = PlatformResult(
        media_id="M9", permalink="https://ig/p/9", published_at=datetime.now(UTC).isoformat(timespec="seconds")
    )
    save_post(post)
    respx.get(f"{IG_BASE}/M9/insights").mock(
        return_value=httpx.Response(200, json={"data": [{"values": [{"value": 42}]}]})
    )
    respx.get(f"{IG_BASE}/{IG_ID}/insights").mock(
        return_value=httpx.Response(200, json={"data": [{"total_value": {"value": 7}}]})
    )
    respx.get(f"{IG_BASE}/me").mock(return_value=httpx.Response(200, json={"followers_count": 33}))
    assert main(["--root", str(repo), "report"]) == 0
    report = next((repo / "reports").glob("*.md")).read_text()
    assert "[2026-10-19-mon](https://ig/p/9)" in report and "Followers:** 33" in report


def test_review_post_with_fake_claude(settings: Settings, make_post: Callable[..., Path], monkeypatch: Any) -> None:
    import bloom_agent.review as review_mod

    folder = make_post()
    sent: dict[str, Any] = {}

    class FakeMessages:
        def create(self, **kwargs: Any) -> Any:
            sent.update(kwargs)
            block = type("B", (), {"text": '{"verdict": "fix", "issues": ["Add 18+"], "suggestions": []}'})()
            return type("M", (), {"content": [block]})()

    monkeypatch.setattr(review_mod, "_client", lambda s: type("C", (), {"messages": FakeMessages()})())
    result = review_mod.review_post(settings, load_post(folder / "post.yaml"))
    assert result.verdict == "fix" and result.issues == ["Add 18+"]
    content = sent["messages"][0]["content"]
    assert content[0]["type"] == "image" and content[0]["source"]["media_type"] == "image/jpeg"
    assert "Hashtags counted: 5" in content[-1]["text"]
    assert sent["model"] == settings.claude_model


def test_state_merge_keeps_person_edits(tmp_path: Path) -> None:
    from bloom_agent.statemerge import merge_post_yaml

    (tmp_path / "image.jpg").write_bytes(b"x")
    ours = "id: p\nstatus: approved\nmedia: [image.jpg]\nresult:\n  attempts: 2\n  last_error: glitch\n"
    theirs = "id: p\nstatus: draft\nmedia: [image.png]\ncaption_file: new.txt\n"
    merged = __import__("yaml").safe_load(merge_post_yaml(ours, theirs, tmp_path))
    assert merged["status"] == "draft"  # the person's un-approval wins over the agent's stale "approved"
    assert merged["caption_file"] == "new.txt"
    assert merged["result"]["attempts"] == 2
    assert merged["media"] == ["image.jpg"]  # the png the person's copy points to was converted and is gone

    published = ours.replace("status: approved", "status: published")
    merged = __import__("yaml").safe_load(merge_post_yaml(published, theirs, tmp_path))
    assert merged["status"] == "published"  # a real outcome is never lost
