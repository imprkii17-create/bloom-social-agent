"""Whole-site checks: every language page, robots.txt, sitemap, llms.txt, redirects, 404s, headers, AI crawlers.

Runs against the built files in site/public (no network; used in CI and before the agent opens a website pull
request) or against the live site (daily health check and the weekly report).
"""

from __future__ import annotations

import time
import urllib.robotparser
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Protocol

import httpx
import yaml

from ..facts import Facts, load_facts
from ..site.config import LANGS, Guide, SiteConfig, load_guides, load_site_config, site_dir
from .findings import FindingList
from .onpage import PageExpectations, PageFacts, check_page

AREA = "Website"
AI_AREA = "AI search"
SEARCH_BOTS = ("Googlebot", "Bingbot")
AI_SEARCH_BOTS = ("OAI-SearchBot", "ChatGPT-User", "Claude-SearchBot", "Claude-User", "PerplexityBot")
NOT_FOUND_PROBE = "/bloom-seo-check-404-probe/"
SLOW_SECONDS = 2.5
PAUSED_MARKERS = ("site not available", "usage limits", "this site has been paused")


@dataclass
class Fetched:
    status: int
    text: str = ""
    headers: dict[str, str] = field(default_factory=dict)
    url: str = ""
    elapsed: float = 0.0
    error: str = ""


class Fetcher(Protocol):
    def get(self, url: str) -> Fetched: ...


class LocalFetcher:
    """Serves site/public like Netlify would (pretty URLs, 404.html for missing pages)."""

    def __init__(self, public: Path, base_url: str) -> None:
        self.public = public
        self.base = base_url.rstrip("/")

    def get(self, url: str) -> Fetched:
        path = url[len(self.base) :] if url.startswith(self.base) else url
        path = path.split("#")[0].split("?")[0] or "/"
        rel = path.lstrip("/")
        candidates = [rel + "index.html"] if path.endswith("/") else [rel, rel + "/index.html"]
        for c in candidates:
            target = self.public / c
            if target.is_file():
                return Fetched(200, target.read_text(encoding="utf-8", errors="replace"), {}, url)
        nf = self.public / "404.html"
        return Fetched(404, nf.read_text(encoding="utf-8") if nf.is_file() else "", {}, url)


class HttpFetcher:
    def __init__(self, client: httpx.Client | None = None) -> None:
        self.client = client or httpx.Client(
            timeout=20.0,
            follow_redirects=True,
            headers={"User-Agent": "BloomModels-SEO-check/1.0 (+https://bloommodels.hu)"},
        )

    def get(self, url: str) -> Fetched:
        start = time.monotonic()
        try:
            r = self.client.get(url)
        except httpx.HTTPError as exc:
            return Fetched(0, error=f"{type(exc).__name__}: {exc}", url=url)
        return Fetched(
            r.status_code, r.text, {k.lower(): v for k, v in r.headers.items()}, str(r.url), time.monotonic() - start
        )


@dataclass
class SiteReport:
    findings: FindingList
    pages: dict[str, PageFacts]
    live: bool


def load_keywords(repo_root: Path) -> dict[str, list[str]]:
    path = repo_root / "brand" / "seo.yaml"
    if not path.is_file():
        return {}
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    kws = (data.get("site") or {}).get("keywords") or {}
    return {str(k): [str(x) for x in v or []] for k, v in kws.items()}


def check_site(
    repo_root: Path,
    *,
    live: bool,
    fetcher: Fetcher | None = None,
    today: date | None = None,
    cfg: SiteConfig | None = None,
    facts: Facts | None = None,
) -> SiteReport:
    cfg = cfg or load_site_config(repo_root)
    facts = facts or load_facts(repo_root)
    keywords = load_keywords(repo_root)
    fetcher = fetcher or (HttpFetcher() if live else LocalFetcher(site_dir(repo_root) / "public", cfg.base_url))
    f = FindingList()
    pages: dict[str, PageFacts] = {}
    alternates = {lang: cfg.url(lang) for lang in LANGS} | {"x-default": cfg.url(cfg.x_default)}

    for lang in LANGS:
        url = cfg.url(lang)
        r = fetcher.get(url)
        if r.status == 0:
            f.add(
                AREA,
                "critical",
                "unreachable",
                "The website can't be reached",
                r.error,
                "Check the domain and Netlify (Domain management) — is the DNS still pointing to Netlify?",
                url,
            )
            continue
        if any(m in r.text.lower() for m in PAUSED_MARKERS) or r.status == 503:
            f.add(
                AREA,
                "critical",
                "site-paused",
                "Netlify has paused the website",
                "Visitors see 'Site not available' instead of the page.",
                "Open Netlify → Usage. The free plan pauses sites that run out of credits until the next month.",
                url,
            )
            continue
        if r.status != 200:
            f.add(
                AREA,
                "critical",
                "page-status",
                f"Page answers {r.status} instead of 200",
                fix="Check the latest Netlify deploy.",
                page=url,
            )
            continue
        if live and r.elapsed > SLOW_SECONDS:
            f.add(AREA, "warning", "slow-response", f"The page took {r.elapsed:.1f}s to respond", page=url)
        findings, pf = check_page(r.text, PageExpectations(url, lang, keywords.get(lang, []), alternates, today), facts)
        f.extend(findings)
        pages[lang] = pf

    guides = _load_guides_safely(repo_root, f)
    _check_guides(cfg, guides, fetcher, f, pages, facts, today, live)

    _cross_page(cfg, pages, f)
    robots = _robots(cfg, fetcher, f)
    _sitemap(cfg, fetcher, f, guides)
    _ai_visibility(cfg, fetcher, robots, pages, f)
    if live:
        _live_only(cfg, fetcher, f)
    return SiteReport(f, pages, live)


def _load_guides_safely(repo_root: Path, f: FindingList) -> dict[str, dict[str, Guide]]:
    try:
        return load_guides(repo_root)
    except (ValueError, OSError) as exc:
        f.add(AREA, "warning", "guides-invalid", "A guide page in site/src/guides can't be read", str(exc))
        return {}


def _check_guides(
    cfg: SiteConfig,
    guides: dict[str, dict[str, Guide]],
    fetcher: Fetcher,
    f: FindingList,
    pages: dict[str, PageFacts],
    facts: Facts,
    today: date | None,
    live: bool,
) -> None:
    """The guide pages (site/src/guides): same on-page checks as the home pages, plus: each language version links
    to the others (hreflang), and the home page links to every guide (otherwise Google barely finds them)."""
    homes = {lang: fetcher.get(cfg.url(lang)) for lang in LANGS if guides}
    for gid, by_lang in guides.items():
        alternates = {lang: cfg.base_url + by_lang[lang].meta.path for lang in LANGS}
        alternates["x-default"] = alternates[cfg.x_default]
        for lang in LANGS:
            meta = by_lang[lang].meta
            url = alternates[lang]
            r = fetcher.get(url)
            if r.status != 200:
                f.add(
                    AREA,
                    "warning" if live else "critical",
                    "guide-status",
                    f"Guide page answers {r.status} instead of 200",
                    fix="Rebuild the site (bloom site build) and check the latest Netlify deploy.",
                    page=url,
                )
                continue
            findings, pf = check_page(r.text, PageExpectations(url, lang, meta.keywords, alternates, today), facts)
            f.extend(findings)
            pages[f"{lang}:{gid}"] = pf
            home = homes.get(lang)
            if home is not None and home.status == 200 and f'href="{meta.path}"' not in home.text:
                f.add(
                    AREA,
                    "warning",
                    "guide-orphan",
                    "The home page doesn't link to this guide",
                    fix="Keep the guides section (<!-- bloom:guides -->) on the home page.",
                    page=url,
                )


def _cross_page(cfg: SiteConfig, pages: dict[str, PageFacts], f: FindingList) -> None:
    langs = [lang for lang in LANGS if lang in pages]
    for a in langs:
        for b in langs:
            if a != b and pages[b].hreflang.get(a) != cfg.url(a):
                f.add(
                    AREA,
                    "warning",
                    "hreflang-return",
                    f"The {b} page doesn't link back to the {a} page",
                    fix="hreflang links must be reciprocal or Google ignores them.",
                    page=cfg.url(b),
                )
    titles = [pages[lang].title for lang in langs if pages[lang].title]
    if len(titles) != len(set(titles)):
        f.add(
            AREA,
            "warning",
            "duplicate-title",
            "Two language versions have the same title",
            fix="Write each title in its own language.",
        )


def _robots(cfg: SiteConfig, fetcher: Fetcher, f: FindingList) -> urllib.robotparser.RobotFileParser | None:
    r = fetcher.get(cfg.base_url + "/robots.txt")
    if r.status != 200:
        f.add(AREA, "warning", "robots-missing", "No robots.txt", fix="Add one that allows crawling and lists the sitemap.")
        return None
    rp = urllib.robotparser.RobotFileParser()
    rp.parse(r.text.splitlines())
    for bot in SEARCH_BOTS:
        for lang in LANGS:
            if not rp.can_fetch(bot, cfg.url(lang)):
                f.add(
                    AREA,
                    "critical",
                    "robots-blocks",
                    f"robots.txt blocks {bot} from {cfg.pages[lang].path}",
                    fix="Remove the Disallow rule; the page can't rank while it is blocked.",
                )
    if "sitemap:" not in r.text.lower():
        f.add(
            AREA,
            "info",
            "robots-sitemap",
            "robots.txt doesn't point to the sitemap",
            fix=f"Add 'Sitemap: {cfg.base_url}/sitemap.xml'.",
        )
    return rp


def _sitemap(cfg: SiteConfig, fetcher: Fetcher, f: FindingList, guides: dict[str, dict[str, Guide]] | None = None) -> None:
    r = fetcher.get(cfg.base_url + "/sitemap.xml")
    if r.status != 200:
        f.add(AREA, "warning", "sitemap-missing", "No sitemap.xml", fix="Add one and submit it in Search Console.")
        return
    try:
        root = ET.fromstring(r.text.encode("utf-8"))
    except ET.ParseError as exc:
        f.add(AREA, "critical", "sitemap-invalid", "sitemap.xml is not valid XML", str(exc))
        return
    ns = {"s": "http://www.sitemaps.org/schemas/sitemap/0.9"}
    locs = {(u.findtext("s:loc", default="", namespaces=ns) or "").strip(): u for u in root.findall("s:url", ns)}
    urls = [cfg.url(lang) for lang in LANGS]
    urls += [cfg.base_url + g.meta.path for by_lang in (guides or {}).values() for g in by_lang.values()]
    for url in urls:
        if url not in locs:
            f.add(AREA, "warning", "sitemap-page", f"sitemap.xml doesn't list {url}")
        elif not locs[url].findtext("s:lastmod", default="", namespaces=ns):
            f.add(AREA, "info", "sitemap-lastmod", f"sitemap.xml has no lastmod for {url}")


def _ai_visibility(
    cfg: SiteConfig,
    fetcher: Fetcher,
    robots: urllib.robotparser.RobotFileParser | None,
    pages: dict[str, PageFacts],
    f: FindingList,
) -> None:
    if robots is not None:
        blocked = [bot for bot in AI_SEARCH_BOTS if not robots.can_fetch(bot, cfg.url(cfg.default_lang))]
        if blocked:
            f.add(
                AI_AREA,
                "warning",
                "ai-bots-blocked",
                "robots.txt blocks AI search assistants: " + ", ".join(blocked),
                fix="ChatGPT, Claude and Perplexity can't recommend a site they aren't allowed to read.",
            )
        else:
            f.ok(AI_AREA, "ai-bots", "AI search assistants may read the site")
    r = fetcher.get(cfg.base_url + "/llms.txt")
    if r.status != 200:
        f.add(AI_AREA, "info", "llms-txt", "No llms.txt", fix="A short plain-text summary that AI tools can read easily.")
    else:
        f.ok(AI_AREA, "llms-txt", "llms.txt is published")
    if pages and not any(p.faq_count for p in pages.values()):
        f.add(
            AI_AREA,
            "warning",
            "faq-missing",
            "No FAQ on the site",
            fix="Assistants answer questions like 'how much can an online model earn in Budapest' from FAQ-style text.",
        )
    if pages and not any(p.same_as for p in pages.values()):
        f.add(
            AI_AREA,
            "warning",
            "same-as",
            "The website doesn't link itself to the Instagram profile (sameAs)",
            fix="Set instagram_url in site/src/site.yaml; it tells Google and AI tools both belong to Bloom Models.",
        )
    if pages and not all("Organization" in p.schema_types for p in pages.values()):
        f.add(AI_AREA, "warning", "org-schema", "Organization structured data missing on a page")


def _live_only(cfg: SiteConfig, fetcher: Fetcher, f: FindingList) -> None:
    host = cfg.base_url.split("://", 1)[1]
    r = fetcher.get(f"http://{host}/")
    if r.status and not r.url.startswith("https://"):
        f.add(
            AREA,
            "critical",
            "no-https-redirect",
            "http:// doesn't redirect to https://",
            fix="Netlify → Domain management → HTTPS → enable 'Force HTTPS'.",
        )
    r = fetcher.get(f"https://www.{host}/")
    if r.status == 0:
        f.add(
            AREA,
            "warning",
            "www-unreachable",
            f"www.{host} doesn't work",
            r.error,
            "Add the CNAME record for www at Rackhost (see SETUP.md).",
        )
    elif not r.url.rstrip("/").endswith(host) or "://www." in r.url:
        f.add(
            AREA,
            "warning",
            "www-redirect",
            f"www.{host} doesn't redirect to {host}",
            fix="Netlify → Domain management: make the plain domain primary.",
        )
    r = fetcher.get(cfg.base_url + NOT_FOUND_PROBE)
    if r.status == 200:
        f.add(AREA, "warning", "soft-404", "Missing pages answer 200 instead of 404", fix="Make sure 404.html is published.")
    home = fetcher.get(cfg.url(cfg.default_lang))
    if home.status == 200 and "x-content-type-options" not in home.headers:
        f.add(
            AREA,
            "info",
            "security-headers",
            "Security headers are missing",
            fix="Check that site/public/_headers is deployed.",
        )
