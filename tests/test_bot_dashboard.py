"""Dashboard snapshot and the Telegram bot (Telegram, Claude and the website are mocked)."""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
import respx

from bloom_agent import bot
from bloom_agent.cli import main
from bloom_agent.config import Settings, load_settings
from bloom_agent.dashboard import build_snapshot
from bloom_agent.queue import load_post
from bloom_agent.seo.sitecheck import Fetched
from bloom_agent.telegram import TelegramClient, TelegramError, send_to_owner, split_message

BOT_TOKEN = "123456:SECRET-BOT-TOKEN"
TG = f"https://api.telegram.org/bot{BOT_TOKEN}"
OWNER = "424242"
NOW = datetime.fromisoformat("2026-10-16T12:00:00+02:00")


class FakeFetcher:
    def __init__(self, status: int = 200, text: str = "<html>ok</html>") -> None:
        self.status, self.text, self.urls = status, text, []  # type: ignore[var-annotated]

    def get(self, url: str) -> Fetched:
        self.urls.append(url)
        return Fetched(self.status, self.text, {}, url, 0.25)


@pytest.fixture
def tg_settings(full_repo: Path, monkeypatch: pytest.MonkeyPatch) -> Settings:
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", BOT_TOKEN)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", OWNER)
    monkeypatch.setenv("GITHUB_REPOSITORY", "owner/bloom-social-agent")
    return load_settings(full_repo)


def queue_repo(make_post: Callable[..., Path]) -> None:
    make_post("2026-10-19-a", status="approved", scheduled_at="2026-10-19 18:30")
    make_post("2026-10-20-b", status="draft", scheduled_at="2026-10-20 18:30")
    make_post("2026-10-21-c", status="draft", scheduled_at="2026-10-21 18:30")


def msg(text: str, chat: str = OWNER, update_id: int = 1, **extra: Any) -> dict[str, Any]:
    return {"update_id": update_id, "message": {"message_id": update_id * 10, "chat": {"id": int(chat), "type": "private"},
                                                 "text": text, **extra}}


# ---- telegram client --------------------------------------------------------------------------------------------------


def test_split_message_respects_the_limit() -> None:
    text = "\n".join(f"line {i} " + "x" * 50 for i in range(200))
    parts = split_message(text, 1000)
    assert all(len(p) <= 1000 for p in parts) and "\n".join(parts) == text


@respx.mock
def test_telegram_errors_never_contain_the_token() -> None:
    bad = httpx.Response(400, json={"ok": False, "description": f"bad {BOT_TOKEN}"})
    respx.post(f"{TG}/sendMessage").mock(return_value=bad)
    respx.post(f"{TG}/getMe").mock(side_effect=httpx.ConnectError(f"cannot reach {TG}/getMe"))
    with TelegramClient(BOT_TOKEN) as tg:
        with pytest.raises(TelegramError) as e1:
            tg.send(OWNER, "hi")
        with pytest.raises(TelegramError) as e2:
            tg.me()
    assert BOT_TOKEN not in str(e1.value) and BOT_TOKEN not in str(e2.value)


@respx.mock
def test_send_to_owner_is_quiet_without_setup_and_never_raises(settings: Settings, tg_settings: Settings) -> None:
    assert send_to_owner(settings, "hi") is False  # no token configured: nothing happens
    route = respx.post(f"{TG}/sendMessage").mock(return_value=httpx.Response(200, json={"ok": True, "result": {}}))
    assert send_to_owner(tg_settings, "<b>hi</b>") is True
    sent = json.loads(route.calls[0].request.content)
    assert sent["chat_id"] == OWNER and sent["parse_mode"] == "HTML"
    route.mock(return_value=httpx.Response(500, json={"ok": False, "description": "down"}))
    assert send_to_owner(tg_settings, "hi") is False


# ---- dashboard ------------------------------------------------------------------------------------------------------


def test_snapshot_shows_queue_attention_and_site(tg_settings: Settings, make_post: Callable[..., Path]) -> None:
    queue_repo(make_post)
    snap = build_snapshot(tg_settings, now=NOW, fetcher=FakeFetcher())
    q = snap["queue"]
    assert [c["id"] for c in q["upcoming"]] == ["2026-10-19-a", "2026-10-20-b", "2026-10-21-c"]
    assert q["next"]["id"] == "2026-10-19-a" and q["counts"] == {"approved": 1, "draft": 2}
    assert q["upcoming"][0]["image"] == (
        "https://raw.githubusercontent.com/owner/bloom-social-agent/main/posts/2026-10-19-a/image.jpg"
    )
    assert "18+" in q["upcoming"][0]["caption"]
    assert snap["site"]["ok"] is True and len(snap["site"]["pages"]) == 2
    texts = " | ".join(a["text"] for a in snap["attention"])
    assert "2 draft post(s)" in texts and "2026-10-20-b" in texts
    assert snap["features"]["telegram"] is True and snap["features"]["claude"] is False
    assert snap["dashboard_url"] == "https://owner.github.io/bloom-social-agent/"


def test_snapshot_flags_a_paused_site_and_survives_broken_parts(
    tg_settings: Settings, make_post: Callable[..., Path]
) -> None:
    queue_repo(make_post)
    (tg_settings.repo_root / "state").mkdir(exist_ok=True)
    (tg_settings.repo_root / "state" / "seo-latest.json").write_text("{not json", encoding="utf-8")

    def broken_ig() -> Any:
        raise RuntimeError("Instagram is down")

    snap = build_snapshot(tg_settings, now=NOW, fetcher=FakeFetcher(503, "This site has been paused"),
                          ig_factory=broken_ig)
    assert snap["site"]["ok"] is False and snap["site"]["pages"][0]["note"] == "paused by Netlify"
    assert snap["attention"][0]["level"] == "critical" and "website" in snap["attention"][0]["text"]
    assert "Instagram is down" in snap["instagram"]["live_error"]
    assert snap["seo"] == {}  # unreadable file → empty section, not a crash


def test_dashboard_cli_writes_json(full_repo: Path, make_post: Callable[..., Path]) -> None:
    queue_repo(make_post)
    out = full_repo / "_site" / "dashboard.json"
    assert main(["--root", str(full_repo), "dashboard", "--offline", "--out", str(out)]) == 0
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["version"] == 1 and data["queue"]["counts"]["draft"] == 2 and data["site"]["checked"] is False


# ---- bot ------------------------------------------------------------------------------------------------------------


class FakeTG:
    def __init__(self, updates: list[dict[str, Any]]) -> None:
        self._updates = updates
        self.sent: list[tuple[Any, str, bool]] = []
        self.acked: int | None = None

    def updates(self, offset: int | None = None, *, timeout: int = 0, limit: int = 50) -> list[dict[str, Any]]:
        return list(self._updates)

    def acknowledge(self, last_update_id: int) -> None:
        self.acked = last_update_id

    def send(self, chat_id: Any, text: str, *, html_mode: bool = True, reply_to: int | None = None) -> None:
        self.sent.append((chat_id, text, html_mode))

    def typing(self, chat_id: Any) -> None:
        pass

    def me(self) -> dict[str, Any]:
        return {"username": "bloom_test_bot"}


def run_poll(settings: Settings, updates: list[dict[str, Any]], brain: Any = None) -> tuple[FakeTG, bot.PollResult]:
    tg = FakeTG(updates)
    snap = lambda: build_snapshot(settings, now=NOW, fetcher=FakeFetcher())  # noqa: E731
    result = bot.poll(settings, tg, now=NOW, snapshot=snap, brain=brain)  # type: ignore[arg-type]
    return tg, result


def test_bot_ignores_strangers_and_acknowledges_everything(tg_settings: Settings, make_post: Callable[..., Path]) -> None:
    queue_repo(make_post)
    tg, res = run_poll(tg_settings, [msg("/status", chat="999", update_id=5), msg("/status", update_id=6)])
    assert res.ignored == 1 and res.handled == 1 and tg.acked == 6
    assert len(tg.sent) == 1 and tg.sent[0][0] == int(OWNER)
    text = tg.sent[0][1]
    assert "Next post" in text and "2026-10-19-a" in text and "Needs you" in text and "github.io" in text


def test_bot_does_nothing_before_it_is_connected(full_repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", BOT_TOKEN)
    settings = load_settings(full_repo)
    tg, res = run_poll(settings, [msg("/status")])
    assert tg.acked is None and not tg.sent and "Connect Telegram" in res.notes[0]


def test_bot_queue_report_brief_and_help(tg_settings: Settings, make_post: Callable[..., Path]) -> None:
    queue_repo(make_post)
    tg, _ = run_poll(tg_settings, [msg("/queue", update_id=1), msg("/report", update_id=2), msg("/brief", update_id=3),
                                   msg("/help@bloom_test_bot", update_id=4), msg("/nonsense", update_id=5)])
    queue, report, brief, help_, unknown = (s[1] for s in tg.sent)
    assert "✏️" in queue and "/approve all" in queue
    assert "Numbers" in report
    assert "No weekly plan yet" in brief or "Plan" in brief
    assert "/approve" in help_ and "I don't know /nonsense" in unknown


def test_bot_approve_lists_then_approves_valid_drafts(tg_settings: Settings, make_post: Callable[..., Path]) -> None:
    queue_repo(make_post)
    make_post("2026-10-22-bad", status="draft", scheduled_at="2026-10-22 18:30", caption="no adult marker here #a")
    tg, res = run_poll(tg_settings, [msg("/approve", update_id=1)])
    assert "Drafts waiting" in tg.sent[0][1] and not res.changed_posts
    tg, res = run_poll(tg_settings, [msg("/approve all", update_id=2)])
    assert sorted(res.changed_posts) == ["2026-10-20-b", "2026-10-21-c"]
    assert "❌ <code>2026-10-22-bad</code>" in tg.sent[0][1]
    posts = tg_settings.posts_dir
    assert load_post(posts / "2026-10-20-b" / "post.yaml").status.value == "approved"
    assert load_post(posts / "2026-10-22-bad" / "post.yaml").status.value == "draft"


def test_bot_approve_needs_an_unambiguous_id(tg_settings: Settings, make_post: Callable[..., Path]) -> None:
    queue_repo(make_post)
    tg, res = run_poll(tg_settings, [msg("/approve 2026-10-2", update_id=1)])
    assert not res.changed_posts and "matches 2 drafts" in tg.sent[0][1]
    tg, res = run_poll(tg_settings, [msg("/approve 2026-10-20", update_id=2)])  # a whole day is fine
    assert res.changed_posts == ["2026-10-20-b"]


def test_bot_skip_cancels_one_post(tg_settings: Settings, make_post: Callable[..., Path]) -> None:
    queue_repo(make_post)
    tg, res = run_poll(tg_settings, [msg("/skip 2026-10-19", update_id=1)])
    assert res.changed_posts == ["2026-10-19-a"] and "won't be posted" in tg.sent[0][1]
    assert load_post(tg_settings.posts_dir / "2026-10-19-a" / "post.yaml").status.value == "skipped"


def test_free_text_without_claude_explains_how_to_turn_it_on(tg_settings: Settings) -> None:
    tg, res = run_poll(tg_settings, [msg("how did last week go?")])
    assert "ANTHROPIC_API_KEY" in tg.sent[0][1] and res.ai_replies == 0


class FakeClaude:
    def __init__(self, answer: str) -> None:
        self.answer = answer
        self.calls: list[dict[str, Any]] = []
        self.messages = self

    def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        usage = SimpleNamespace(input_tokens=6000, output_tokens=300, cache_creation_input_tokens=0,
                                cache_read_input_tokens=0, server_tool_use=None)
        return SimpleNamespace(content=[SimpleNamespace(text=self.answer)], usage=usage)


def test_free_text_goes_to_claude_with_context_and_warnings(
    tg_settings: Settings, make_post: Callable[..., Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    settings = load_settings(tg_settings.repo_root)
    queue_repo(make_post)
    fake = FakeClaude("Here is a caption: sexy streaming job #a #b #c #d #e #f")
    replied = {"text": "Earlier answer about reach"}
    tg, res = run_poll(settings, [msg("write a caption", reply_to_message=replied)],
                       brain=lambda: bot.Brain(settings, client=fake))
    assert res.ai_replies == 1 and res.spend_usd > 0
    _, text, html_mode = tg.sent[0]
    assert html_mode is False and "⚠️ Before posting" in text and "sexy" in text and "6 hashtags" in text
    call = fake.calls[0]
    content = call["messages"][0]["content"]
    assert "2026-10-19-a" in content and "Earlier answer about reach" in content
    assert call["system"][0]["cache_control"] == {"type": "ephemeral"} and "Brand rules" in call["system"][0]["text"]


def test_claude_answers_are_capped_per_run(
    tg_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    settings = load_settings(tg_settings.repo_root)
    fake = FakeClaude("ok")
    updates = [msg(f"question {i}", update_id=i) for i in range(1, bot.MAX_AI_REPLIES_PER_RUN + 3)]
    tg, res = run_poll(settings, updates, brain=lambda: bot.Brain(settings, client=fake))
    assert res.ai_replies == bot.MAX_AI_REPLIES_PER_RUN and len(fake.calls) == bot.MAX_AI_REPLIES_PER_RUN
    assert "a lot of questions" in tg.sent[-1][1]


def test_connect_saves_the_chat_that_sent_the_code(tg_settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GH_ADMIN_TOKEN", "gh-admin")
    settings = load_settings(tg_settings.repo_root)
    saved: list[tuple[str, str, str, str]] = []
    stranger = msg("/start", chat="111", update_id=1)
    mine = msg("/start Bloom-4821", chat="777", update_id=2)
    mine["message"]["chat"]["first_name"] = "Richard"
    tg = FakeTG([stranger, mine])
    res = bot.connect(settings, tg, "bloom-4821", save_secret=lambda *a: saved.append(a))  # type: ignore[arg-type]
    assert res.ok and res.chat_id == 777
    assert saved == [("owner/bloom-social-agent", "gh-admin", "TELEGRAM_CHAT_ID", "777")]
    assert tg.sent[0][0] == 777 and "Connected" in tg.sent[0][1] and tg.acked == 2
    assert not bot.connect(settings, FakeTG([stranger]), "bloom-4821", save_secret=lambda *a: None).ok  # type: ignore[arg-type]
    assert not bot.connect(settings, FakeTG([mine]), "abc").ok  # code too short


@respx.mock
def test_alerts_reach_telegram_once_per_new_alert(tg_settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    from bloom_agent.cli import notify

    monkeypatch.setenv("GITHUB_TOKEN", "gh")
    settings = load_settings(tg_settings.repo_root)
    api = "https://api.github.com/repos/owner/bloom-social-agent"
    issue = {"number": 3, "title": "Bloom agent: a post needs attention", "html_url": "https://github.com/x/3",
             "body": "same body", "comments": 0}
    respx.get(f"{api}/issues").mock(side_effect=[httpx.Response(200, json=[]), httpx.Response(200, json=[issue])])
    respx.post(f"{api}/issues").mock(return_value=httpx.Response(201, json=issue))
    sent = respx.post(f"{TG}/sendMessage").mock(return_value=httpx.Response(200, json={"ok": True, "result": {}}))
    notify(settings, "Bloom agent: a post needs attention", "same body")
    notify(settings, "Bloom agent: a post needs attention", "same body")  # unchanged alert 15 minutes later
    assert sent.call_count == 1
    text = json.loads(sent.calls[0].request.content)["text"]
    assert "A post needs attention" in text and "https://github.com/x/3" in text
