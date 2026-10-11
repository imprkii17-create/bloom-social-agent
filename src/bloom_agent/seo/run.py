"""Runs every SEO check that is configured and writes the report.

Each part is optional and fails soft: no Search Console key → that section says how to connect it; PageSpeed down →
noted, everything else still runs. Nothing here changes the website; fixes are proposed by the marketing agent.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any

from ..config import Settings
from ..facts import load_facts
from ..queue import load_queue
from ..site.config import LANGS, load_site_config
from .findings import Finding, FindingList
from .instagram_seo import load_ig_seo, profile_findings, queue_findings
from .pagespeed import SpeedResult, run_pagespeed, speed_findings
from .search_console import SearchConsole, SearchConsoleError, SearchSummary, parse_service_account, search_findings
from .sitecheck import Fetcher, check_site

log = logging.getLogger(__name__)

AREAS = ("Website", "Speed", "Google Search", "Instagram", "AI search")


@dataclass
class SeoReport:
    created: str
    live: bool
    findings: FindingList
    pages: dict[str, Any] = field(default_factory=dict)
    speed: list[SpeedResult] = field(default_factory=list)
    search: SearchSummary | None = None
    profile: dict[str, Any] = field(default_factory=dict)
    skipped: list[str] = field(default_factory=list)

    def area_scores(self) -> dict[str, int]:
        present = {f.area for f in self.findings.items}
        return {a: self.findings.score(a) for a in AREAS if a in present}

    def to_json(self) -> dict[str, Any]:
        return {
            "created": self.created,
            "live": self.live,
            "scores": self.area_scores(),
            "findings": [f.to_dict() for f in self.findings.problems()],
            "pages": self.pages,
            "speed": [asdict(s) for s in self.speed],
            "search": _search_json(self.search),
            "profile": {
                k: v
                for k, v in self.profile.items()
                if k in ("username", "name", "biography", "website", "followers_count", "media_count")
            },
            "skipped": self.skipped,
        }


def run_seo(
    settings: Settings,
    *,
    live: bool = True,
    speed: bool = True,
    search: bool = True,
    instagram: bool = True,
    now: datetime | None = None,
    fetcher: Fetcher | None = None,
    instagram_factory: Any = None,
) -> SeoReport:
    now = now or datetime.now(settings.timezone)
    today = now.date()
    root = settings.repo_root
    cfg = load_site_config(root)
    facts = load_facts(root)
    f = FindingList()
    report = SeoReport(created=now.isoformat(timespec="minutes"), live=live, findings=f)

    site = check_site(root, live=live, fetcher=fetcher, today=today, cfg=cfg, facts=facts)
    f.extend(site.findings)
    report.pages = {lang: asdict(pf) for lang, pf in site.pages.items()}

    if speed and live:
        for lang in LANGS[:1] if not settings.pagespeed_api_key else LANGS:
            for strategy in ("mobile", "desktop"):
                report.speed.append(run_pagespeed(cfg.url(lang), strategy, api_key=settings.pagespeed_api_key))
        f.extend(speed_findings(report.speed))
    elif speed:
        report.skipped.append("Speed (only measured on the live site)")

    if search:
        _search_console(settings, cfg, today, report, f)

    if instagram:
        ig_cfg = load_ig_seo(root)
        posts, _ = load_queue(settings.posts_dir)
        captions = {}
        for p in posts:
            try:
                captions[p.id] = p.caption()
            except (OSError, ValueError):
                captions[p.id] = ""
        f.extend(queue_findings(posts, captions, ig_cfg, now, settings.timezone))
        if settings.ig_user_id and settings.ig_access_token and instagram_factory is not None:
            try:
                report.profile = dict(instagram_factory().profile())
                f.extend(profile_findings(report.profile, ig_cfg, facts))
            except Exception as exc:
                f.add("Instagram", "info", "ig-profile-error", "Couldn't read the Instagram profile", str(exc)[:200])
        else:
            report.skipped.append("Instagram profile (connect Instagram first)")
    return report


def _search_console(settings: Settings, cfg: Any, today: date, report: SeoReport, f: FindingList) -> None:
    if not settings.gsc_service_account_json:
        report.skipped.append("Google Search Console (add the GSC_SERVICE_ACCOUNT_JSON secret — SETUP.md step 11)")
        return
    try:
        gsc = SearchConsole(parse_service_account(settings.gsc_service_account_json), settings.gsc_property)
        report.search = gsc.summary(today)
        inspections = {}
        for lang in LANGS:
            try:
                inspections[cfg.url(lang)] = gsc.inspect(cfg.url(lang))
            except SearchConsoleError as exc:
                log.info("URL inspection failed for %s: %s", lang, exc)
        f.extend(search_findings(report.search, inspections))
        try:
            submitted = {str(s.get("path")) for s in gsc.sitemaps()}
            sitemap = f"{cfg.base_url}/sitemap.xml"
            if sitemap not in submitted:
                gsc.submit_sitemap(sitemap)
                f.add("Google Search", "info", "sitemap-submitted", "Submitted the sitemap to Google", sitemap)
        except SearchConsoleError as exc:
            f.add(
                "Google Search",
                "info",
                "sitemap-submit",
                "Couldn't check/submit the sitemap",
                str(exc)[:200],
                "Give the service account Full permission, or submit sitemap.xml by hand in Search Console.",
            )
    except SearchConsoleError as exc:
        f.extend(search_findings(None, {}, str(exc)))


def _search_json(s: SearchSummary | None) -> dict[str, Any] | None:
    if s is None:
        return None
    d = asdict(s)
    d["start"], d["end"] = s.start.isoformat(), s.end.isoformat()
    for k in ("queries", "pages", "countries", "striking_distance", "low_ctr"):
        d[k] = d[k][:25]
    return d


# ---- writing ------------------------------------------------------------------------------------------------

SEV_ICON = {"critical": "🔴", "warning": "🟠", "info": "🔵", "pass": "✅"}


def render_markdown(r: SeoReport) -> str:
    lines = [f"# SEO report — {r.created[:10]}", ""]
    scores = r.area_scores()
    if scores:
        lines += ["| Area | Score |", "|---|---|"] + [f"| {a} | {s}/100 |" for a, s in scores.items()] + [""]
    probs = r.findings.problems()
    crit = [p for p in probs if p.severity == "critical"]
    lines.append(
        f"**{len(crit)} critical, {len(r.findings.by_severity('warning'))} warnings, "
        f"{len(r.findings.by_severity('info'))} suggestions.**"
    )
    lines.append("")
    if r.search:
        s = r.search
        delta = ""
        if s.prev_impressions:
            delta = f" ({(s.impressions - s.prev_impressions) / s.prev_impressions:+.0%} vs previous 28 days)"
        lines += [
            "## Google search (last 28 days)",
            "",
            f"{s.start} – {s.end}: **{s.impressions:.0f} impressions**{delta}, **{s.clicks:.0f} clicks**, "
            f"CTR {s.ctr:.1%}, average position {s.position:.1f}.",
            "",
        ]
        if s.queries:
            lines += ["| Search | Impressions | Clicks | Position |", "|---|---|---|---|"]
            lines += [f"| {q.keys[0]} | {q.impressions:.0f} | {q.clicks:.0f} | {q.position:.1f} |" for q in s.queries[:15]]
            lines.append("")
        if s.striking_distance:
            lines.append(
                "**Almost there** (page 1–2, a small push could win the click): "
                + ", ".join(f"“{q.keys[0]}” (#{q.position:.0f})" for q in s.striking_distance[:8])
            )
            lines.append("")
        if s.new_queries:
            lines += ["**New searches this month:** " + ", ".join(f"“{q}”" for q in s.new_queries[:10]), ""]
    if r.speed:
        lines += [
            "## Speed",
            "",
            "| Page | Device | Score | Main content (LCP) | Layout shift (CLS) |",
            "|---|---|---|---|---|",
        ]
        for sp in r.speed:
            if sp.error:
                lines.append(f"| {sp.url} | {sp.strategy} | – | {sp.error} | |")
                continue
            lcp = sp.field_data.get("lcp_ms", sp.lab.get("lcp_ms"))
            cls = sp.field_data.get("cls", sp.lab.get("cls"))
            lines.append(
                f"| {sp.url} | {sp.strategy} | {sp.score if sp.score is not None else '–'} | "
                f"{f'{lcp / 1000:.1f}s' if lcp is not None else '–'} | {f'{cls:.2f}' if cls is not None else '–'} |"
            )
        lines.append("")
    if probs:
        lines += ["## To do", ""]
        for p in probs:
            where = f" — {p.page}" if p.page else ""
            lines.append(f"- {SEV_ICON[p.severity]} **{p.area}: {p.title}**{where}")
            if p.detail:
                lines.append(f"  - {p.detail}")
            if p.fix:
                lines.append(f"  - Fix: {p.fix}")
        lines.append("")
    if r.skipped:
        lines += ["## Not checked", ""] + [f"- {s}" for s in r.skipped] + [""]
    return "\n".join(lines)


def write_report(settings: Settings, r: SeoReport, *, name: str) -> Path:
    out_dir = settings.reports_dir / "seo"
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{name}.md"
    path.write_text(render_markdown(r), encoding="utf-8")
    state = settings.repo_root / "state"
    state.mkdir(exist_ok=True)
    data = r.to_json()
    (state / "seo-latest.json").write_text(
        json.dumps(data, indent=1, ensure_ascii=False, default=str) + "\n", encoding="utf-8"
    )
    history = {
        "date": r.created[:10],
        "scores": data["scores"],
        "impressions": r.search.impressions if r.search else None,
        "clicks": r.search.clicks if r.search else None,
        "speed": {f"{s.strategy}:{s.url}": s.score for s in r.speed if s.score is not None},
    }
    with (state / "seo-history.jsonl").open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(history, ensure_ascii=False) + "\n")
    return path


def critical_findings(r: SeoReport) -> list[Finding]:
    return r.findings.by_severity("critical")
