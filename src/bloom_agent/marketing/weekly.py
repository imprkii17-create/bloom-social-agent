"""The weekly head-of-marketing run.

1. Gather the week's data: Instagram results, the SEO report (site, speed, Google, Instagram search, AI search), the
   post queue, the funnel numbers you log, running experiments, last weeks' briefs.
2. Plan: Claude (if ANTHROPIC_API_KEY is set) researches and writes the plan; otherwise a rule-based plan with
   posts from marketing/library.yaml.
3. Draft next week's posts with graphics → pull request "next week's posts" (merging it approves them).
4. Website improvements (Claude only, data-backed) → separate pull request, with a Netlify preview.
5. Write the brief to reports/marketing/<year>-W<week>.md.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import yaml

from ..config import Settings
from ..facts import load_facts
from ..models import Post, Status
from ..queue import load_queue
from ..seo.findings import FindingList
from ..seo.run import SeoReport, render_markdown, run_seo, write_report
from ..site.config import LANGS, faq_pairs, load_site_config, load_strings
from .drafts import PlannedPost, build_week, load_library, next_week_slots, pick_from_library
from .graphics import photo_library

log = logging.getLogger(__name__)

DEFAULT_MAX_USD = 2.0
SITE_BRANCH = "agent/site"  # one rolling branch: a new proposal replaces an unmerged older one
STORY_IDEAS = [
    "Poll: „Mi tartana vissza attól, hogy kipróbáld?” (tapasztalat / diszkréció / idő / más)",
    "Behind the scenes: 10 seconds of the studio lights going on",
    "Question box: „Kérdezz bármit az online modell munkáról” — answer 3 in Stories, best one becomes a FAQ entry",
    "Repost today's feed post with a „link a bióban” sticker",
    "„Egy nap a stúdióban” — 3 frames: arrival, setup, first stream starting",
]
REEL_IDEAS = [
    {
        "title": "Myth vs. reality (5 seconds each)",
        "hook": "„Ezt mindenki hiszi az online modell munkáról…”",
        "shots": ["Text on screen: myth 1 → reality", "Studio detail shots between myths", "End card: link a bióban · 18+"],
        "caption_idea": "3 tévhit, amit minden héten hallunk.",
    },
    {
        "title": "Studio walk-through",
        "hook": "„Így néz ki egy profi streamelő stúdió Budapesten”",
        "shots": [
            "Door opens to the studio",
            "Lights switching on",
            "Camera + laptop setup",
            "Window view toward the Basilica",
        ],
        "caption_idea": "Profi fények, profi technika — neked csak jönnöd kell.",
    },
]


@dataclass
class WeeklyResult:
    week: str
    mode: str  # "claude" or "library"
    plan: dict[str, Any]
    posts: list[Post] = field(default_factory=list)
    dropped: list[str] = field(default_factory=list)
    posts_pr: str = ""
    site_pr: str = ""
    site_applied: list[dict[str, str]] = field(default_factory=list)
    site_rejected: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    spend_usd: float = 0.0
    spend_lines: list[str] = field(default_factory=list)
    brief_path: str = ""
    seo_path: str = ""


# ---- data gathering ---------------------------------------------------------------------------------------------


def week_label(d: date) -> str:
    y, w, _ = d.isocalendar()
    return f"{y}-W{w:02d}"


def _read_yaml(path: Path) -> Any:
    if not path.is_file():
        return None
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def instagram_snapshot(settings: Settings, ig_factory: Any, now: datetime) -> dict[str, Any]:
    if not (settings.ig_user_id and settings.ig_access_token and ig_factory):
        return {"connected": False}
    from ..report import collect

    try:
        stats, account, followers = collect(settings, ig_factory(), 7, now.astimezone(UTC))
    except Exception as exc:
        return {"connected": True, "error": str(exc)[:200]}
    posts = [
        {
            "id": s.post.id,
            "type": s.post.type.value,
            "score": s.score,
            "metrics": s.metrics,
            "notes": (s.post.notes or "")[:120],
        }
        for s in sorted(stats, key=lambda x: x.score, reverse=True)
    ]
    return {"connected": True, "followers": followers, "account_7d": account, "posts_7d": posts}


def funnel_numbers(root: Path) -> list[dict[str, Any]]:
    data = _read_yaml(root / "marketing" / "funnel.yaml") or {}
    weeks = data.get("weeks") or []
    return [w for w in weeks if isinstance(w, dict)][-8:]


def experiments(root: Path) -> list[dict[str, Any]]:
    data = _read_yaml(root / "marketing" / "experiments.yaml") or {}
    return [e for e in data.get("experiments") or [] if isinstance(e, dict)]


def previous_briefs(root: Path, n: int = 2) -> list[str]:
    d = root / "reports" / "marketing"
    if not d.is_dir():
        return []
    files = sorted(d.glob("*.md"))[-n:]
    return [f.name + ":\n" + "\n".join(f.read_text(encoding="utf-8").splitlines()[:30]) for f in files]


def site_snapshot(root: Path) -> dict[str, Any]:
    cfg = load_site_config(root)
    strings = load_strings(root)
    return {
        lang: {
            "title": cfg.pages[lang].title,
            "description": cfg.pages[lang].description,
            "og_title": cfg.pages[lang].og_title,
            "faq": [{"n": i + 1, "q": q, "a": a} for i, (q, a) in enumerate(faq_pairs(strings[lang]))],
        }
        for lang in LANGS
    }


# ---- rule-based plan (no API key) -----------------------------------------------------------------------------------


def bottleneck(ig: dict[str, Any]) -> tuple[str, str]:
    acc = ig.get("account_7d") or {}
    reach = acc.get("reach") or 0
    visits = acc.get("profile_views") or sum(
        (p.get("metrics") or {}).get("profile_visits", 0) for p in ig.get("posts_7d", [])
    )
    clicks = acc.get("website_clicks") or 0
    if not ig.get("connected") or not reach:
        return "unknown", "No Instagram numbers yet."
    if visits / reach < 0.02:
        return (
            "interest",
            f"Profile visits are {visits / reach:.1%} of reach (target ≥ 2%): "
            "content gets seen but doesn't make people curious.",
        )
    if visits and clicks / visits < 0.10:
        return (
            "intent",
            f"Link clicks are {clicks / visits:.0%} of profile visits (target ≥ 10%): the profile doesn't convince.",
        )
    if reach < 1000:
        return "reach", f"Weekly reach is {reach}: too few people see the content yet."
    return "application", "Reach and clicks look healthy; check that site visitors apply (log applications in funnel.yaml)."


def rule_based_plan(seo: SeoReport | None, ig: dict[str, Any], funnel: list[dict[str, Any]]) -> dict[str, Any]:
    neck, why = bottleneck(ig)
    wins: list[str] = []
    problems: list[str] = []
    decisions: list[str] = []
    if ig.get("posts_7d"):
        best = ig["posts_7d"][0]
        wins.append(f"Best post: {best['id']} (score {best['score']}). Make more like it.")
    if not ig.get("connected"):
        decisions.append("Connect Instagram (SETUP.md steps 1–5) so the agent can measure results.")
    if not funnel:
        decisions.append("Log last week's applications in marketing/funnel.yaml — it's how the agent knows what works.")
    seo_actions = []
    if seo:
        for f in seo.findings.problems()[:8]:
            line = f"{f.area}: {f.title}" + (f" — {f.fix}" if f.fix else "")
            if f.severity == "critical":
                problems.append(line)
            seo_actions.append(line)
            if f.code in ("same-as", "job-expiring", "job-expired", "gsc-error"):
                decisions.append(line)
        if seo.skipped:
            decisions.extend(f"Not checked: {s}" for s in seo.skipped if "Console" in s)
    summary = (
        "Rule-based plan (no Claude API key): next week's posts come from the ready-made library, and the "
        "recommendations below come straight from the SEO and Instagram checks."
    )
    return {
        "summary": summary,
        "bottleneck": neck,
        "bottleneck_reason": why,
        "wins": wins,
        "problems": problems,
        "decisions_for_you": decisions,
        "posts": [],
        "reel_ideas": REEL_IDEAS,
        "story_ideas": STORY_IDEAS[:4],
        "experiments": [],
        "experiment_updates": [],
        "site_changes": [],
        "seo_actions": seo_actions,
    }


# ---- Claude plan ------------------------------------------------------------------------------------------------------


def _system_texts(root: Path) -> tuple[str, str, str]:
    def read(rel: str) -> str:
        p = root / rel
        return p.read_text(encoding="utf-8") if p.is_file() else ""

    return read("brand/playbook.md"), read("brand/rules.md"), read("brand/seo.yaml")


def claude_context(
    *,
    today: date,
    slots: list[datetime],
    ig: dict[str, Any],
    seo: SeoReport | None,
    posts: list[Post],
    funnel: list[dict[str, Any]],
    exps: list[dict[str, Any]],
    briefs: list[str],
    site: dict[str, Any],
    library_examples: list[dict[str, Any]],
    photos: dict[str, str],
    research: str,
) -> str:
    upcoming = [
        {"id": p.id, "when": p.scheduled_at, "status": p.status.value, "notes": (p.notes or "")[:100]}
        for p in sorted(posts, key=lambda p: p.scheduled_at)
        if p.status in (Status.APPROVED, Status.DRAFT) and p.scheduled_at >= today.isoformat()
    ][:15]
    seo_json = seo.to_json() if seo else None
    data = {
        "today": today.isoformat(),
        "free_slots_next_week": [s.strftime("%Y-%m-%d %H:%M (%A)") for s in slots],
        "instagram_last_7_days": ig,
        "funnel_weeks": funnel,
        "seo": seo_json,
        "upcoming_posts": upcoming,
        "experiments": exps,
        "site_now": site,
        "photo_library_instagram_ok": photos,
        "voice_examples": library_examples,
        "research_notes": research or "(no research this week)",
        "previous_briefs": briefs,
    }
    return (
        "Here is this week's data. Write the weekly plan.\n"
        f"- Create exactly {len(slots)} posts, one per free slot, in slot order.\n"
        "- Decide the bottleneck stage from the numbers; if numbers are missing, say 'unknown' and what to log.\n"
        "- Propose website changes only if the SEO data supports them.\n"
        "- Update running experiments if their end date has passed (with the data); propose at most one new one.\n\n"
        + json.dumps(data, ensure_ascii=False, indent=1, default=str)
    )


RESEARCH_BRIEF = (
    "Do quick research for this week's plan (max 5 searches). Report concise bullet notes with source URLs:\n"
    "1. Any Instagram changes in the last 30 days that matter for a small recruitment account (reach, Reels, "
    "hashtags, search, policy).\n"
    "2. Hungarian dates, events or news in the next 14 days relevant to people looking for flexible work "
    "(students, holidays, payday timing).\n"
    "3. AI/Google visibility check: search for 'online modell munka Budapest' and 'online modeling agency Budapest'. "
    "Say whether bloommodels.hu or Bloom Models appears and which agencies do. End with a line exactly "
    "'BLOOM_MENTIONED: yes' or 'BLOOM_MENTIONED: no'."
)


# ---- main --------------------------------------------------------------------------------------------------------------


def run_weekly(
    settings: Settings,
    *,
    now: datetime | None = None,
    use_ai: bool = True,
    open_prs: bool = True,
    seo_live: bool = True,
    ig_factory: Any = None,
    brain_factory: Any = None,
    pr_opener: Any = None,
) -> WeeklyResult:
    now = now or datetime.now(settings.timezone)
    today = now.date()
    root = settings.repo_root
    next_monday = today + timedelta(days=7 - today.weekday())
    label = week_label(next_monday)
    facts = load_facts(root)
    posts, _ = load_queue(settings.posts_dir)

    try:
        seo = run_seo(
            settings, live=seo_live, speed=seo_live, search=True, instagram=True, now=now, instagram_factory=ig_factory
        )
    except Exception as exc:
        log.warning("SEO report failed: %s", exc)
        seo = SeoReport(created=now.isoformat(timespec="minutes"), live=seo_live, findings=FindingList())
        seo.findings.add("Website", "info", "seo-failed", "The SEO report couldn't run this week", str(exc)[:200])

    ig = instagram_snapshot(settings, ig_factory, now)
    funnel = funnel_numbers(root)
    exps = experiments(root)
    slots = next_week_slots(posts, today, settings.timezone)
    library = load_library(root)

    result = WeeklyResult(week=label, mode="library", plan={})
    planned: list[PlannedPost] = []
    raw_posts: list[dict[str, Any]] = []
    brain = None

    if use_ai and settings.anthropic_api_key:
        try:
            from .brain import Brain

            try:
                max_usd = float(os.environ.get("MARKETING_MAX_COST_USD") or DEFAULT_MAX_USD)
            except ValueError:
                max_usd = DEFAULT_MAX_USD
            brain = brain_factory() if brain_factory else Brain(settings, max_usd=max_usd)
            playbook, rules, seo_yaml = _system_texts(root)
            system = brain.system(playbook, rules, facts.as_prompt(), seo_yaml)
            research = ""
            if (os.environ.get("MARKETING_RESEARCH") or "1") != "0":
                try:
                    research = brain.research(system, RESEARCH_BRIEF)
                except Exception as exc:
                    result.notes.append(f"Research skipped: {exc}")
            photos = {n: p.alt_en for n, p in photo_library(root).items() if p.instagram_ok}
            context = claude_context(
                today=today,
                slots=slots,
                ig=ig,
                seo=seo,
                posts=posts,
                funnel=funnel,
                exps=exps,
                briefs=previous_briefs(root),
                site=site_snapshot(root),
                library_examples=library[:2],
                photos=photos,
                research=research,
            )
            plan = brain.plan(system, context)
            result.plan, result.mode = plan, "claude"
            if research:
                plan["research_notes"] = research
            raw_posts = list(plan.get("posts") or [])
            planned = [PlannedPost.from_dict(p) for p in raw_posts]
            planned = _repair_round(brain, system, planned, raw_posts, settings, facts, result)
        except Exception as exc:
            log.warning("Claude plan failed: %s", exc)
            result.notes.append(f"Claude plan failed ({type(exc).__name__}: {str(exc)[:200]}); used the rule-based plan.")
            result.mode = "library"
        if brain is not None:
            result.spend_usd = brain.spend.usd
            result.spend_lines = list(brain.spend.calls)

    if result.mode == "library":
        result.plan = rule_based_plan(seo, ig, funnel)
        planned = pick_from_library(library, posts, len(slots))
    else:
        # top up from the library if Claude's posts were dropped
        if len(planned) < len(slots):
            planned += pick_from_library(library, posts, len(slots) - len(planned))

    # ---- next week's posts ----
    status = Status.APPROVED if open_prs else Status.DRAFT
    made, dropped = build_week(planned, slots, settings, facts, status=status)
    result.posts, result.dropped = made, dropped
    if made and open_prs:
        result.posts_pr = _open_pr(
            settings,
            f"agent/posts-{label}",
            ["posts"],
            f"Next week's posts ({label}) — merging approves them",
            _posts_pr_body(settings, result, label, raw_posts),
            pr_opener,
            result,
        )

    # ---- website changes ----
    proposals = list(result.plan.get("site_changes") or [])
    if proposals:
        from .site_changes import apply_site_changes

        sc = apply_site_changes(root, proposals, facts, today, settings.banned_words)
        result.site_applied = [
            {"field": c.field, "lang": c.lang, "old": c.old_value, "new": c.new_value, "reason": c.reason}
            for c in sc.applied
        ]
        result.site_rejected = sc.rejected
        if sc.applied and open_prs:
            result.site_pr = _open_pr(
                settings,
                SITE_BRANCH,
                ["site"],
                f"Website SEO improvements ({label})",
                _site_pr_body(result, sc.score_before, sc.score_after),
                pr_opener,
                result,
            )

    # ---- experiments, brief, reports ----
    _update_experiments(root, result.plan, today)
    result.seo_path = str(write_report(settings, seo, name=week_label(today)).relative_to(root))
    result.brief_path = _write_brief(settings, result, seo, ig)
    return result


def _repair_round(
    brain: Any,
    system: Any,
    planned: list[PlannedPost],
    raw: list[dict[str, Any]],
    settings: Settings,
    facts: Any,
    result: WeeklyResult,
) -> list[PlannedPost]:
    from .drafts import check_planned

    failed = [
        (raw[i], probs) for i, p in enumerate(planned) if i < len(raw) and (probs := check_planned(p, settings, facts))
    ]
    if not failed:
        return planned
    try:
        fixed = {PlannedPost.from_dict(d).slug: PlannedPost.from_dict(d) for d in brain.repair(system, failed)}
    except Exception as exc:
        result.notes.append(f"Repair round failed: {exc}")
        return planned
    return [fixed.get(p.slug, p) for p in planned]


def _open_pr(
    settings: Settings, branch: str, paths: list[str], title: str, body: str, opener: Any, result: WeeklyResult
) -> str:
    from ..github import GitHubError, PullRequestsNotAllowed, compare_url, create_or_update_pr, open_or_update_issue
    from .gitops import GitError, commit_to_branch, current_branch

    root = settings.repo_root
    try:
        base = current_branch(root)
        if not commit_to_branch(root, branch, paths, f"bloom-agent: {title}"):
            return ""
    except GitError as exc:
        result.notes.append(f"Couldn't push {branch}: {exc}")
        return ""
    if opener is not None:
        return str(opener(branch, base, title, body))
    if not (settings.github_token and settings.github_repository):
        result.notes.append(f"Branch {branch} pushed; no GitHub token to open the pull request.")
        return ""
    try:
        return create_or_update_pr(
            settings.github_repository, settings.github_token, head=branch, base=base, title=title, body=body
        )
    except PullRequestsNotAllowed as exc:
        url = compare_url(settings.github_repository, base, branch)
        try:
            open_or_update_issue(
                settings.github_repository,
                settings.github_token,
                f"Bloom agent: {title}",
                f"{exc}\n\nUntil then, open it yourself (one click): {url}\n\n---\n\n{body}",
            )
        except GitHubError as exc2:
            result.notes.append(f"Couldn't open an issue either: {exc2}")
        result.notes.append(str(exc))
        return url
    except GitHubError as exc:
        result.notes.append(f"Couldn't open the pull request: {exc}")
        return ""


def _raw_url(settings: Settings, branch: str, rel: str) -> str:
    return (
        f"https://raw.githubusercontent.com/{settings.github_repository}/{branch}/{rel}"
        if settings.github_repository
        else rel
    )


def _posts_pr_body(settings: Settings, r: WeeklyResult, label: str, raw: list[dict[str, Any]]) -> str:
    branch = f"agent/posts-{label}"
    why = {PlannedPost.from_dict(d).slug: str(d.get("why", "")) for d in raw}
    lines = [
        f"## Next week's posts ({label})",
        "",
        "**Merging this pull request approves these posts**: they will be published automatically at the times "
        "below. To drop one, delete its folder in this pull request (Files changed → … → Delete file) or close the "
        "pull request to reject the whole week.",
        "",
        f"⏰ **Merge before {r.posts[0].scheduled_at}** (Budapest time): a post whose time has already passed when "
        "you merge is marked `missed` instead of going out late."
        if r.posts
        else "",
        "",
        f"Plan made by: **{'Claude' if r.mode == 'claude' else 'the ready-made library (no Claude key)'}**. "
        "✅ Checked by the agent before opening this pull request: content rules, facts and earnings disclaimer, "
        "banned words, hashtags, fonts, image size and format (`bloom validate`). GitHub doesn't run CI on pull "
        "requests the agent opens, so these checks happen here instead.",
        "",
    ]
    for p in r.posts:
        rel = f"posts/{p.id}"
        cap = p.caption()
        slug = p.id[11:]
        lines += [
            f"### {p.scheduled_at} — `{p.id}`",
            "",
            f'<img src="{_raw_url(settings, branch, rel + "/image.jpg")}" width="300"> '
            f'<img src="{_raw_url(settings, branch, rel + "/image-en.jpg")}" width="300">',
            "",
        ]
        if why.get(slug):
            lines += [f"_Why: {why[slug]}_", ""]
        lines += ["<details><summary>Caption</summary>", "", "```", cap, "```", "", "</details>", ""]
    if r.dropped:
        lines += ["### Dropped by the checks", ""] + [f"- {d}" for d in r.dropped] + [""]
    return "\n".join(lines)


def _site_pr_body(r: WeeklyResult, before: int, after: int) -> str:
    lines = [
        "## Website SEO improvements",
        "",
        "Netlify adds a **Deploy Preview** link to this pull request: open it to see the change before it goes "
        "live. Merging publishes it to bloommodels.hu (one Netlify deploy).",
        "",
        f"SEO check score of the built site: {before} → {after}. ✅ Rebuilt and checked by the agent "
        "(`bloom site build`, `bloom seo --offline`). If an older website pull request was still open, this one "
        "replaces it.",
        "",
        "| Field | Lang | Before | After | Why |",
        "|---|---|---|---|---|",
    ]
    for c in r.site_applied:
        lines.append(f"| `{c['field']}` | {c['lang']} | {c['old'][:80]} | {c['new'][:120]} | {c['reason'][:120]} |")
    if r.site_rejected:
        lines += ["", "Not included:"] + [f"- {x}" for x in r.site_rejected]
    return "\n".join(lines)


def _update_experiments(root: Path, plan: dict[str, Any], today: date) -> None:
    new = plan.get("experiments") or []
    updates = {u.get("id"): u for u in plan.get("experiment_updates") or []}
    if not new and not updates:
        return
    path = root / "marketing" / "experiments.yaml"
    data = _read_yaml(path) or {}
    exps = [e for e in data.get("experiments") or [] if isinstance(e, dict)]
    for e in exps:
        u = updates.get(e.get("id"))
        if u:
            e["status"] = u.get("status", e.get("status"))
            e["result"] = u.get("result", "")
    running = sum(1 for e in exps if e.get("status") == "running")
    for x in new[: max(0, 2 - running)]:
        weeks = int(x.get("duration_weeks") or 2)
        exps.append(
            {
                "id": f"{today:%Y%m%d}-{len(exps) + 1}",
                "title": x.get("title", ""),
                "hypothesis": x.get("hypothesis", ""),
                "metric": x.get("metric", ""),
                "start": today.isoformat(),
                "end": (today + timedelta(weeks=max(1, weeks))).isoformat(),
                "status": "running",
                "result": "",
            }
        )
    header = "# Experiments the marketing agent runs and evaluates. You can edit or stop any of them.\n"
    path.write_text(
        header + yaml.safe_dump({"experiments": exps}, allow_unicode=True, sort_keys=False, width=110), encoding="utf-8"
    )


def _write_brief(settings: Settings, r: WeeklyResult, seo: SeoReport, ig: dict[str, Any]) -> str:
    p = r.plan
    lines = [f"# Marketing brief — week {r.week}", ""]
    lines += [p.get("summary", ""), ""]
    lines += [f"**Bottleneck:** {p.get('bottleneck', 'unknown')} — {p.get('bottleneck_reason', '')}", ""]
    acc = ig.get("account_7d") or {}
    if acc or ig.get("followers") is not None:
        lines += [
            "## Last 7 days on Instagram",
            "",
            "| Followers | Reach | Profile views | Link clicks |",
            "|---|---|---|---|",
            f"| {ig.get('followers', '–')} | {acc.get('reach', '–')} | {acc.get('profile_views', '–')} | "
            f"{acc.get('website_clicks', '–')} |",
            "",
        ]
    if seo.search:
        s = seo.search
        lines += [
            f"**Google (28 days):** {s.impressions:.0f} impressions, {s.clicks:.0f} clicks, avg. position {s.position:.1f}.",
            "",
        ]
    scores = seo.area_scores()
    if scores:
        link = f" ([full report](../seo/{Path(r.seo_path).name}))" if r.seo_path else ""
        lines += ["**SEO scores:** " + " · ".join(f"{a} {v}" for a, v in scores.items()) + link, ""]

    def section(title: str, items: list[str], checkbox: bool = False) -> None:
        nonlocal lines
        if items:
            lines += [f"## {title}", ""] + [f"- {'[ ] ' if checkbox else ''}{i}" for i in items] + [""]

    section("Decisions for you", list(p.get("decisions_for_you") or []), checkbox=True)
    section("Wins", list(p.get("wins") or []))
    section("Problems", list(p.get("problems") or []))
    if r.posts:
        lines += ["## Next week's posts", ""]
        if r.posts_pr:
            lines += [f"Review and merge: {r.posts_pr}", ""]
        lines += [f"- {x.scheduled_at} — `{x.id}`" for x in r.posts] + [""]
    elif not r.dropped:
        lines += ["## Next week's posts", "", "Next week is already fully planned.", ""]
    section("Dropped drafts", r.dropped)
    reels = p.get("reel_ideas") or []
    if reels:
        lines += ["## Reels to film", ""]
        for rd in reels:
            lines += [f"**{rd.get('title', '')}** — hook: {rd.get('hook', '')}"]
            lines += [f"  {i}. {shot}" for i, shot in enumerate(rd.get("shots") or [], 1)]
            if rd.get("caption_idea"):
                lines.append(f"  Caption idea: {rd['caption_idea']}")
            lines.append("")
    section("Stories", list(p.get("story_ideas") or []))
    section("SEO actions", list(p.get("seo_actions") or []))
    if r.site_applied:
        lines += ["## Website changes proposed", ""]
        if r.site_pr:
            lines += [f"Pull request (with Netlify preview): {r.site_pr}", ""]
        lines += [f"- `{c['field']}` ({c['lang']}): {c['new']}" for c in r.site_applied] + [""]
    section("Website changes not made", r.site_rejected)
    exps = p.get("experiments") or []
    section("New experiments", [f"{e.get('title')}: {e.get('hypothesis')} (metric: {e.get('metric')})" for e in exps])
    if p.get("research_notes"):
        lines += ["## Research notes", "", str(p["research_notes"]), ""]
    section("Run notes", r.notes)
    if r.spend_lines:
        lines += [f"_Claude cost this run: ${r.spend_usd:.2f}_ ({'; '.join(r.spend_lines)})", ""]
    out = settings.repo_root / "reports" / "marketing" / f"{r.week}.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(lines), encoding="utf-8")
    state = settings.repo_root / "state"
    state.mkdir(exist_ok=True)
    (state / "marketing-latest.json").write_text(
        json.dumps(
            {
                "week": r.week,
                "mode": r.mode,
                "plan": r.plan,
                "posts": [x.id for x in r.posts],
                "dropped": r.dropped,
                "posts_pr": r.posts_pr,
                "site_pr": r.site_pr,
                "spend_usd": r.spend_usd,
            },
            ensure_ascii=False,
            indent=1,
            default=str,
        )
        + "\n",
        encoding="utf-8",
    )
    return str(out.relative_to(settings.repo_root))


__all__ = ["WeeklyResult", "render_markdown", "run_weekly"]
