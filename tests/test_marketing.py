"""The marketing agent: graphics, drafting, the Claude plan (with a fake client), website changes, branches and PRs."""

from __future__ import annotations

import json
import subprocess
from datetime import date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
import respx
import yaml
from PIL import Image

from bloom_agent.config import Settings, load_settings
from bloom_agent.facts import load_facts
from bloom_agent.github import PullRequestsNotAllowed, create_or_update_pr
from bloom_agent.marketing.brain import Brain, Spend, plan_schema
from bloom_agent.marketing.drafts import (
    PlannedPost,
    assemble_caption,
    build_week,
    check_planned,
    load_library,
    next_week_slots,
    pick_from_library,
)
from bloom_agent.marketing.gitops import BranchHasHumanEdits, commit_to_branch
from bloom_agent.marketing.graphics import GraphicError, GraphicSpec, render
from bloom_agent.marketing.site_changes import _dump_site_yaml, apply_site_changes
from bloom_agent.marketing.weekly import run_weekly
from bloom_agent.models import Post, Status
from bloom_agent.queue import load_queue
from bloom_agent.site.build import build_site
from bloom_agent.site.config import load_site_config, load_strings

TODAY = date(2026, 10, 9)  # a Friday: plans the week of Oct 12


def good_post(**over: Any) -> dict[str, Any]:
    d: dict[str, Any] = {
        "slug": "first-week", "pillar": "how-it-works", "template": "statement", "photo": "",
        "hu": {"eyebrow": "Online modell munka", "headline": "Nem kell tapasztalat.", "accent": "Betanítunk.",
               "body": "Az első streamtől melletted vagyunk.", "items": [], "footnote": ""},
        "en": {"eyebrow": "Online modeling", "headline": "No experience needed.", "accent": "We train you.",
               "body": "We're with you from your first stream.", "items": [], "footnote": ""},
        "caption_hu": "Online modell munka tapasztalat nélkül: így indulsz. Jelentkezés: link a bióban.",
        "caption_en": "Online modeling with no experience: here's how you start. Apply: link in bio.",
        "hashtags": ["#bloommodels", "#onlinemodell", "#budapest"],
        "alt_text": "", "why": "Most DMs ask about experience.",
    }
    d.update(over)
    return d


# ---- graphics ---------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("template", ["statement", "list", "light"])
def test_graphics_render_1080x1350(full_repo: Path, template: str) -> None:
    spec = GraphicSpec(template, "Így néz ki az első heted", eyebrow="Online modell munka", accent="Lépésről lépésre",
                       body="Rövid beszélgetés, betanítás, első stream.", items=["Beszélgetés", "Betanítás", "Stream"])
    data = render(spec, "hu", full_repo)
    with Image.open(__import__("io").BytesIO(data)) as im:
        assert im.size == (1080, 1350) and im.format == "JPEG" and im.mode == "RGB"


def test_graphics_photo_rules(full_repo: Path) -> None:
    ok = GraphicSpec("photo", "Profi stúdió", eyebrow="Stúdió", body="V. kerület", photo="studio.jpg")
    assert render(ok, "en", full_repo)
    with pytest.raises(GraphicError, match="instagram_ok"):
        render(GraphicSpec("photo", "x", photo="hero-3.jpg"), "hu", full_repo)
    with pytest.raises(GraphicError, match="not in"):
        render(GraphicSpec("photo", "x", photo="nope.jpg"), "hu", full_repo)


def test_graphics_refuse_characters_the_fonts_lack(full_repo: Path) -> None:
    with pytest.raises(GraphicError, match="can't draw"):
        render(GraphicSpec("statement", "Rugalmas beosztás ✨", items=["✓ betanítás"]), "hu", full_repo)
    bad = PlannedPost.from_dict(good_post(hu={"headline": "Tapasztalat nélkül ✓"}))
    assert any("can't draw" in p for p in check_planned(bad, load_settings(full_repo), load_facts(full_repo)))


def test_graphics_never_cut_text(full_repo: Path) -> None:
    with pytest.raises(GraphicError):
        render(GraphicSpec("statement", "Szuperhosszúszóamiegyáltalánnemférkiegysorban" * 3), "hu", full_repo)


# ---- drafting ---------------------------------------------------------------------------------------------------------


def test_next_week_slots_skip_taken_days(full_repo: Path, make_post) -> None:  # type: ignore[no-untyped-def]
    settings = load_settings(full_repo)
    make_post("2026-10-13-taken", status="approved", scheduled_at="2026-10-13 12:00")
    posts, _ = load_queue(settings.posts_dir)
    slots = next_week_slots(posts, TODAY, settings.timezone)
    assert [s.day for s in slots] == [12, 14, 15, 16]
    assert all(s.hour == 18 and s.minute == 30 for s in slots)


def test_library_is_valid_and_picked_least_recently_used(full_repo: Path) -> None:
    settings = load_settings(full_repo)
    facts = load_facts(full_repo)
    library = load_library(full_repo)
    assert len(library) >= 10
    for entry in library:
        assert check_planned(PlannedPost.from_dict(entry, "library:x"), settings, facts) == [], entry["id"]
    used = Post(id="2026-10-01-first-week", scheduled_at="2026-10-01 18:30", media=["image.jpg"],
                notes="… From library: first-week")
    picked = pick_from_library(library, [used], 5)
    assert "first-week" not in [p.slug for p in picked]
    assert len({p.pillar for p in picked}) == 5


def test_check_planned_catches_rule_breaks(full_repo: Path) -> None:
    settings, facts = load_settings(full_repo), load_facts(full_repo)
    bad = PlannedPost.from_dict(good_post(caption_hu="Sexy online modell munka, havi 5000 € #extra"))
    problems = " | ".join(check_planned(bad, settings, facts))
    assert "risk the account" in problems and "5000" in problems and "hashtags in the hashtags list" in problems
    money = PlannedPost.from_dict(good_post(hu={"headline": "Havi 2000 € felett", "footnote": ""}))
    assert any("disclaimer" in p for p in check_planned(money, settings, facts))
    bait = PlannedPost.from_dict(good_post(caption_en="Tag a friend who needs this! 18+"))
    assert any("bait" in p for p in check_planned(bait, settings, facts))


def test_build_week_writes_valid_posts(full_repo: Path) -> None:
    settings, facts = load_settings(full_repo), load_facts(full_repo)
    tz = settings.timezone
    slots = [datetime(2026, 10, 12, 18, 30, tzinfo=tz), datetime(2026, 10, 13, 18, 30, tzinfo=tz)]
    planned = [PlannedPost.from_dict(good_post(slug="bad", hashtags=[])), PlannedPost.from_dict(good_post()),
               PlannedPost.from_dict(good_post(slug="second"))]
    made, dropped = build_week(planned, slots, settings, facts, status=Status.APPROVED)
    assert [p.id for p in made] == ["2026-10-12-first-week", "2026-10-13-second"]
    assert len(dropped) == 1 and dropped[0].startswith("bad")
    post = made[0]
    assert (post.folder / "image.jpg").is_file() and (post.folder / "image-en.jpg").is_file()
    assert "18+" in post.caption() and "#bloommodels" in post.caption()
    assert post.alt_text and "Bloom Models" in post.alt_text
    assert yaml.safe_load((post.folder / "post.yaml").read_text())["status"] == "approved"


def test_assemble_caption_matches_hand_made_format() -> None:
    cap = assemble_caption(PlannedPost.from_dict(good_post()))
    assert cap.startswith("----- CAPTION (copy from here) -----") and cap.rstrip().endswith("----- END CAPTION -----")
    assert "\n—\n" in cap and "18+ | Budapest" in cap


# ---- Claude plan ------------------------------------------------------------------------------------------------------


def _all_objects(schema: Any) -> list[dict[str, Any]]:
    out = []
    if isinstance(schema, dict):
        if schema.get("type") == "object":
            out.append(schema)
        for v in schema.values():
            out += _all_objects(v)
    elif isinstance(schema, list):
        for v in schema:
            out += _all_objects(v)
    return out


def test_plan_schema_is_strict() -> None:
    for obj in _all_objects(plan_schema()):
        assert obj["additionalProperties"] is False
        assert sorted(obj["required"]) == sorted(obj["properties"])


def test_spend_tracking_and_budget() -> None:
    spend = Spend(limit=0.10)
    usage = SimpleNamespace(input_tokens=10_000, output_tokens=2_000, cache_creation_input_tokens=0,
                            cache_read_input_tokens=20_000, server_tool_use=SimpleNamespace(web_search_requests=2))
    spend.add("claude-sonnet-5-5", usage, "plan")
    assert spend.usd == pytest.approx((10_000 * 2 + 20_000 * 0.2 + 2_000 * 10) / 1e6 + 0.02)
    with pytest.raises(Exception, match="limit"):
        spend.check(0.1)


class FakeMessages:
    def __init__(self, replies: list[dict[str, Any] | str]) -> None:
        self.replies = replies
        self.calls: list[dict[str, Any]] = []

    def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        reply = self.replies.pop(0)
        text = reply if isinstance(reply, str) else json.dumps(reply)
        usage = SimpleNamespace(input_tokens=1000, output_tokens=500, cache_creation_input_tokens=0,
                                cache_read_input_tokens=0, server_tool_use=None)
        return SimpleNamespace(content=[SimpleNamespace(type="text", text=text, citations=None)], usage=usage,
                               stop_reason="end_turn")


def fake_plan(n_posts: int, site_changes: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    posts = [good_post(slug=f"p{i}") for i in range(n_posts)]
    posts[0] = good_post(slug="p0", caption_hu="Szexi munka!")  # breaks a rule → repaired
    return {
        "summary": "Reach is fine; profile visits are low.", "bottleneck": "interest",
        "bottleneck_reason": "1% of reach", "wins": ["x"], "problems": [], "decisions_for_you": ["Set instagram_url"],
        "posts": posts, "reel_ideas": [], "story_ideas": ["poll"],
        "experiments": [{"title": "Lunch posting", "hypothesis": "12:00 beats 18:30", "metric": "reach",
                         "duration_weeks": 2}],
        "experiment_updates": [], "site_changes": site_changes or [], "seo_actions": ["Add FAQ"],
    }


def git(root: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=root, check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def git_repo(full_repo: Path, tmp_path_factory: pytest.TempPathFactory) -> Path:
    remote = tmp_path_factory.mktemp("remote") / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(remote)], check=True)
    git(full_repo, "init", "-q", "-b", "main")
    git(full_repo, "-c", "user.name=t", "-c", "user.email=t@t", "add", "-A")
    git(full_repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "init")
    git(full_repo, "remote", "add", "origin", str(remote))
    git(full_repo, "push", "-q", "origin", "main")
    return full_repo


def test_weekly_with_claude_opens_both_pull_requests(git_repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.setenv("MARKETING_RESEARCH", "0")
    settings = load_settings(git_repo)
    site_change = [{"field": "faq.h2", "lang": "en", "new_value": "Online modeling jobs in Budapest: your questions",
                    "reason": "query 'online modeling jobs budapest' at position 8"},
                   {"field": "description", "lang": "hu", "new_value": "túl rövid", "reason": "x"}]
    repaired = {"posts": [good_post(slug="p0")]}
    messages = FakeMessages([fake_plan(5, site_change), repaired])
    brain = Brain(settings, max_usd=1.0, client=SimpleNamespace(messages=messages))
    opened: list[tuple[str, str, str]] = []

    def opener(branch: str, base: str, title: str, body: str) -> str:
        opened.append((branch, base, body))
        return f"https://github.com/x/y/pull/{len(opened)}"

    result = run_weekly(settings, now=datetime(2026, 10, 9, 9, 37, tzinfo=settings.timezone), seo_live=False,
                        brain_factory=lambda: brain, pr_opener=opener)
    assert result.mode == "claude", result.notes
    assert len(result.posts) == 5 and not result.dropped
    assert [b for b, _, _ in opened] == ["agent/posts-2026-W42", "agent/site"]
    # main is untouched: the posts and the website change live only on their branches
    assert not list((git_repo / "posts").glob("2026-10-1*"))
    assert git(git_repo, "rev-parse", "--abbrev-ref", "HEAD") == "main"
    assert "Online modeling jobs in Budapest" not in (git_repo / "site/src/i18n/en.json").read_text()
    branch_files = git(git_repo, "ls-tree", "-r", "--name-only", "origin/agent/posts-2026-W42", "posts")
    assert "posts/2026-10-12-p0/image.jpg" in branch_files
    site_diff = git(git_repo, "diff", "--name-only", "main", "origin/agent/site")
    assert "site/src/i18n/en.json" in site_diff and "site/public/en/index.html" in site_diff
    assert any("description" in r for r in result.site_rejected)
    assert "Merging this pull request approves" in opened[0][2]
    brief = (git_repo / result.brief_path).read_text()
    assert "Set instagram_url" in brief and "pull/1" in brief and "Claude cost" in brief
    exps = yaml.safe_load((git_repo / "marketing/experiments.yaml").read_text())["experiments"]
    assert exps[0]["status"] == "running" and exps[0]["title"] == "Lunch posting"
    assert messages.calls[0]["output_config"]["format"]["type"] == "json_schema"
    assert messages.calls[0]["system"][0]["cache_control"] == {"type": "ephemeral"}


def test_weekly_without_key_uses_library(full_repo: Path) -> None:
    settings = load_settings(full_repo)
    result = run_weekly(settings, now=datetime(2026, 10, 9, 9, 37, tzinfo=settings.timezone), seo_live=False,
                        open_prs=False)
    assert result.mode == "library" and len(result.posts) == 5
    assert all(p.status == Status.DRAFT for p in result.posts)
    assert all("From library:" in (p.notes or "") for p in result.posts)
    assert (full_repo / result.brief_path).is_file() and (full_repo / result.seo_path).is_file()


def test_weekly_falls_back_when_claude_fails(full_repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.setenv("MARKETING_RESEARCH", "0")
    settings = load_settings(full_repo)
    brain = Brain(settings, max_usd=1.0, client=SimpleNamespace(messages=FakeMessages(["not json"])))
    result = run_weekly(settings, now=datetime(2026, 10, 9, 9, 37, tzinfo=settings.timezone), seo_live=False,
                        open_prs=False, brain_factory=lambda: brain)
    assert result.mode == "library" and len(result.posts) == 5
    assert any("Claude plan failed" in n for n in result.notes)


# ---- website changes ---------------------------------------------------------------------------------------------------


def test_site_changes_apply_validate_and_rebuild(full_repo: Path) -> None:
    facts = load_facts(full_repo)
    proposals = [
        {"field": "title", "lang": "en", "new_value": "Online Modeling Jobs Budapest | Bloom Models", "reason": "r"},
        {"field": "faq.q8", "lang": "hu", "new_value": "Kell saját gép?", "reason": "r"},
        {"field": "faq.a8", "lang": "hu", "new_value": "Nem, a stúdióban minden megvan.", "reason": "r"},
        {"field": "hero.h1", "lang": "hu", "new_value": "x", "reason": "r"},
        {"field": "description", "lang": "en", "new_value": "Earn 9000 € " * 12, "reason": "r"},
        {"field": "faq.p", "lang": "en", "new_value": "Webcam questions answered.", "reason": "r"},
    ]
    res = apply_site_changes(full_repo, proposals, facts, date(2026, 10, 9))
    assert [c.field for c in res.applied] == ["title"]
    joined = " | ".join(res.rejected)
    assert "hero.h1" in joined and "only one language" in joined and "description" in joined and "webcam" in joined
    assert load_site_config(full_repo).pages["en"].title == "Online Modeling Jobs Budapest | Bloom Models"
    assert "Online Modeling Jobs Budapest | Bloom Models" in (full_repo / "site/public/en/index.html").read_text()
    assert "faq.q8" not in load_strings(full_repo)["hu"]
    assert "# Settings for the bloommodels.hu website build" in (full_repo / "site/src/site.yaml").read_text()
    assert build_site(full_repo, today=date(2026, 10, 9), check=True).up_to_date


def test_dump_site_yaml_keeps_comments() -> None:
    original = (Path(__file__).resolve().parents[1] / "site/src/site.yaml").read_text(encoding="utf-8")
    data = yaml.safe_load(original)
    data["pages"]["hu"]["description"] = "Új leírás: online modell munka Budapesten."
    out = _dump_site_yaml(original, data)
    assert "# Fill in once the Instagram account is public" in out
    assert yaml.safe_load(out)["pages"]["hu"]["description"] == "Új leírás: online modell munka Budapesten."
    assert yaml.safe_load(out)["pages"]["en"] == data["pages"]["en"]


# ---- git & GitHub -----------------------------------------------------------------------------------------------------


def test_commit_to_branch_leaves_main_and_other_work_alone(git_repo: Path) -> None:
    (git_repo / "posts" / "new-post").mkdir()
    (git_repo / "posts" / "new-post" / "post.yaml").write_text("id: new-post\n")
    (git_repo / "reports").mkdir(exist_ok=True)
    (git_repo / "reports" / "brief.md").write_text("not committed yet")
    assert commit_to_branch(git_repo, "agent/test", ["posts"], "msg") is True
    assert not (git_repo / "posts" / "new-post").exists()
    assert (git_repo / "reports" / "brief.md").read_text() == "not committed yet"
    assert "posts/new-post/post.yaml" in git(git_repo, "ls-tree", "-r", "--name-only", "origin/agent/test")
    assert commit_to_branch(git_repo, "agent/empty", ["posts"], "msg") is False


def test_commit_to_branch_never_overwrites_human_edits(git_repo: Path, tmp_path: Path) -> None:
    (git_repo / "posts" / "a").mkdir(parents=True)
    (git_repo / "posts" / "a" / "post.yaml").write_text("id: a\n")
    assert commit_to_branch(git_repo, "agent/w", ["posts"], "agent proposal")
    # the owner deletes a post in the pull request (a commit by them on the branch)
    clone = tmp_path / "clone"
    subprocess.run(["git", "clone", "-q", "-b", "agent/w", git(git_repo, "remote", "get-url", "origin"), str(clone)],
                   check=True)
    git(clone, "rm", "-q", "posts/a/post.yaml")
    git(clone, "-c", "user.name=Owner", "-c", "user.email=owner@example.com", "commit", "-q", "-m", "drop a")
    git(clone, "push", "-q", "origin", "agent/w")
    (git_repo / "posts" / "b").mkdir(parents=True)
    (git_repo / "posts" / "b" / "post.yaml").write_text("id: b\n")
    with pytest.raises(BranchHasHumanEdits):
        commit_to_branch(git_repo, "agent/w", ["posts"], "agent proposal again")
    assert "drop a" in git(git_repo, "log", "-1", "--format=%s", "origin/agent/w")
    assert (git_repo / "posts" / "b" / "post.yaml").is_file()  # nothing was thrown away on main either


@respx.mock
def test_create_or_update_pr() -> None:
    respx.get("https://api.github.com/repos/o/r/pulls").mock(return_value=httpx.Response(200, json=[]))
    respx.post("https://api.github.com/repos/o/r/pulls").mock(
        return_value=httpx.Response(201, json={"html_url": "https://github.com/o/r/pull/7"}))
    assert create_or_update_pr("o/r", "t", head="agent/x", base="main", title="T", body="B").endswith("/7")


@respx.mock
def test_update_existing_pr_and_permission_error() -> None:
    respx.get("https://api.github.com/repos/o/r/pulls").mock(
        return_value=httpx.Response(200, json=[{"number": 3, "html_url": "https://github.com/o/r/pull/3"}]))
    patch = respx.patch("https://api.github.com/repos/o/r/pulls/3").mock(return_value=httpx.Response(200, json={}))
    assert create_or_update_pr("o/r", "t", head="agent/x", base="main", title="T", body="B").endswith("/3")
    assert patch.called
    respx.get("https://api.github.com/repos/o/r/pulls").mock(return_value=httpx.Response(200, json=[]))
    respx.post("https://api.github.com/repos/o/r/pulls").mock(return_value=httpx.Response(403, json={}))
    with pytest.raises(PullRequestsNotAllowed, match="Allow GitHub Actions"):
        create_or_update_pr("o/r", "t", head="agent/x", base="main", title="T", body="B")


def test_settings_type(settings: Settings) -> None:  # keeps the fixture import used
    assert settings.gsc_property == "sc-domain:bloommodels.hu" and timedelta(days=1)
