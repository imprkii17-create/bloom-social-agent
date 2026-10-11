"""On-page SEO checks for one HTML page (a built file in site/public, or the live page)."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

from bs4 import BeautifulSoup, Tag

from ..facts import Facts, check_text
from ..site.config import DESCRIPTION_MAX, DESCRIPTION_MIN, TITLE_MAX, TITLE_MIN
from .findings import FindingList

AREA = "Website"
JOB_WARN_DAYS = 30
HTML_WEIGHT_WARN = 200_000
MODERN_IMAGE_EXT = (".webp", ".avif", ".svg")


@dataclass
class PageExpectations:
    url: str
    lang: str
    keywords: list[str] = field(default_factory=list)
    alternates: dict[str, str] = field(default_factory=dict)  # hreflang → url, incl. x-default
    today: date | None = None


@dataclass
class PageFacts:
    """What the checks learned about the page, for the site-level checks and the marketing agent."""

    url: str
    title: str = ""
    description: str = ""
    h1: str = ""
    lang: str = ""
    canonical: str = ""
    hreflang: dict[str, str] = field(default_factory=dict)
    schema_types: list[str] = field(default_factory=list)
    same_as: list[str] = field(default_factory=list)
    word_count: int = 0
    faq_count: int = 0
    job_valid_through: str = ""


def norm(text: str) -> str:
    return " ".join(text.split()).strip()


_WORD_RE = re.compile(r"\w+", re.UNICODE)


def _stem(word: str) -> str:
    """Hungarian adds endings after the word and lengthens a final a/e (munka → munkáról, modell → modellt),
    so match on the word without its final a/e."""
    return word[:-1] if len(word) > 3 and word[-1] in "ae" else word


def _contains_keyword(text: str, keywords: list[str]) -> str | None:
    """True if the keyword's words appear in order (allowing word endings), e.g. 'online modell munkáról'."""
    tokens = [t.lower() for t in _WORD_RE.findall(text)]
    for kw in keywords:
        words = [_stem(w.lower()) for w in _WORD_RE.findall(kw)]
        if not words:
            continue
        for i in range(len(tokens) - len(words) + 1):
            if all(tokens[i + j].startswith(words[j]) for j in range(len(words))):
                return kw
    return None


def check_page(html: str, exp: PageExpectations, facts: Facts | None = None) -> tuple[FindingList, PageFacts]:
    f = FindingList()
    soup = BeautifulSoup(html, "html.parser")
    pf = PageFacts(url=exp.url)
    page = exp.url
    today = exp.today or date.today()

    # ---- indexability ----
    robots = soup.find("meta", attrs={"name": re.compile("^robots$", re.I)})
    robots_content = str(robots.get("content", "")).lower() if isinstance(robots, Tag) else ""
    if "noindex" in robots_content:
        f.add(
            AREA,
            "critical",
            "noindex",
            "The page tells Google not to index it",
            f"robots meta: {robots_content}",
            "Remove 'noindex' from the robots meta tag.",
            page,
        )
    else:
        f.ok(AREA, "indexable", "Page can be indexed", page)

    # ---- language ----
    html_el = soup.find("html")
    pf.lang = str(html_el.get("lang", "")) if isinstance(html_el, Tag) else ""
    if pf.lang.split("-")[0].lower() != exp.lang:
        f.add(
            AREA,
            "warning",
            "html-lang",
            f"Page language is '{pf.lang or 'missing'}', expected '{exp.lang}'",
            fix=f'Set <html lang="{exp.lang}">.',
            page=page,
        )

    # ---- title ----
    title_el = soup.find("title")
    pf.title = norm(title_el.get_text()) if title_el else ""
    if not pf.title:
        f.add(
            AREA,
            "critical",
            "title-missing",
            "No <title>",
            fix="Add a title with the main keyword and 'Bloom Models'.",
            page=page,
        )
    else:
        n = len(pf.title)
        if n > TITLE_MAX:
            f.add(
                AREA,
                "warning",
                "title-long",
                f"Title is {n} characters; Google cuts it after about {TITLE_MAX}",
                pf.title,
                "Shorten it; keep the keyword at the start.",
                page,
            )
        elif n < TITLE_MIN:
            f.add(
                AREA,
                "warning",
                "title-short",
                f"Title is only {n} characters",
                pf.title,
                "Add the main keyword and location.",
                page,
            )
        if exp.keywords and not _contains_keyword(pf.title, exp.keywords):
            f.add(
                AREA,
                "warning",
                "title-keyword",
                "Title has none of the target keywords",
                pf.title,
                f"Work in one of: {', '.join(exp.keywords[:3])}.",
                page,
            )
        if "bloom" not in pf.title.lower():
            f.add(
                AREA, "info", "title-brand", "Title doesn't name the brand", pf.title, "End it with '| Bloom Models'.", page
            )

    # ---- description ----
    desc = soup.find("meta", attrs={"name": re.compile("^description$", re.I)})
    pf.description = norm(str(desc.get("content", ""))) if isinstance(desc, Tag) else ""
    if not pf.description:
        f.add(
            AREA,
            "critical",
            "description-missing",
            "No meta description",
            fix="Add a 110–160 character description that sells the click.",
            page=page,
        )
    else:
        n = len(pf.description)
        if n > DESCRIPTION_MAX:
            f.add(
                AREA,
                "warning",
                "description-long",
                f"Description is {n} characters (Google shows about {DESCRIPTION_MAX})",
                fix="Trim it; put the strongest reason to click first.",
                page=page,
            )
        elif n < DESCRIPTION_MIN:
            f.add(
                AREA,
                "info",
                "description-short",
                f"Description is only {n} characters",
                fix="Use the space: add a benefit and a call to action.",
                page=page,
            )
        if exp.keywords and not _contains_keyword(pf.description, exp.keywords):
            f.add(AREA, "info", "description-keyword", "Description has none of the target keywords", page=page)

    # ---- canonical & hreflang ----
    canon = soup.find("link", attrs={"rel": "canonical"})
    pf.canonical = str(canon.get("href", "")) if isinstance(canon, Tag) else ""
    if not pf.canonical:
        f.add(
            AREA,
            "warning",
            "canonical-missing",
            "No canonical link",
            fix=f'Add <link rel="canonical" href="{exp.url}">.',
            page=page,
        )
    elif pf.canonical != exp.url:
        f.add(
            AREA,
            "critical",
            "canonical-wrong",
            "Canonical points somewhere else",
            f"canonical={pf.canonical}, page={exp.url}",
            "Make the canonical point to the page itself.",
            page,
        )
    for link in soup.find_all("link", attrs={"rel": "alternate", "hreflang": True}):
        pf.hreflang[str(link["hreflang"]).lower()] = str(link.get("href", ""))
    for lang, url in exp.alternates.items():
        if pf.hreflang.get(lang) != url:
            f.add(
                AREA,
                "warning",
                "hreflang",
                f"hreflang '{lang}' is missing or wrong",
                f"expected {url}, found {pf.hreflang.get(lang) or 'nothing'}",
                "Each language version must list all versions, including itself and x-default.",
                page,
            )

    # ---- headings ----
    h1s = soup.find_all("h1")
    if len(h1s) != 1:
        f.add(AREA, "warning", "h1-count", f"{len(h1s)} H1 headings (should be exactly one)", page=page)
    if h1s:
        pf.h1 = norm(h1s[0].get_text())
    last = 0
    for h in soup.find_all(re.compile("^h[1-6]$")):
        level = int(h.name[1])
        if last and level > last + 1:
            f.add(
                AREA,
                "info",
                "heading-skip",
                f"Heading jumps from H{last} to H{level}",
                norm(h.get_text())[:80],
                "Use heading levels in order; screen readers and Google use them as an outline.",
                page,
            )
            break
        last = level

    # ---- body text ----
    for tag in soup(["script", "style", "noscript", "svg"]):
        tag.decompose()
    text = norm(soup.get_text(" "))
    pf.word_count = len(text.split())
    if pf.word_count < 300:
        f.add(
            AREA,
            "warning",
            "thin-content",
            f"Only {pf.word_count} words of text",
            fix="Add useful text (FAQ, how it works); thin pages rank poorly.",
            page=page,
        )
    if exp.keywords and not _contains_keyword(text, exp.keywords):
        f.add(
            AREA,
            "warning",
            "body-keyword",
            "The page text never uses a target keyword",
            fix=f"Use '{exp.keywords[0]}' naturally in a heading or the intro.",
            page=page,
        )
    h2_text = " ".join(norm(h.get_text()) for h in soup.find_all("h2"))
    if exp.keywords and not _contains_keyword(h2_text, exp.keywords):
        f.add(AREA, "info", "heading-keyword", "No H2 heading uses a target keyword", page=page)
    if facts is not None:
        for issue in check_text(text, facts):
            f.add(
                AREA,
                "warning",
                f"facts-{issue.kind}",
                "Text disagrees with brand/facts.yaml",
                issue.message,
                "Fix the text or the facts file so they match.",
                page,
            )
    pf.faq_count = len(soup.select("details summary")) or len(soup.select("[itemtype*=FAQPage] [itemprop=name]"))

    # ---- images (re-parse: decompose removed svg only) ----
    soup2 = BeautifulSoup(html, "html.parser")
    imgs = soup2.find_all("img")
    no_alt = [i for i in imgs if i.get("alt") is None]
    empty_alt = [
        i for i in imgs if i.get("alt") == "" and i.get("aria-hidden") != "true" and i.get("role") != "presentation"
    ]
    no_size = [i for i in imgs if not (i.get("width") and i.get("height"))]
    old_fmt = [i for i in imgs if not str(i.get("src", "")).lower().endswith(MODERN_IMAGE_EXT) and str(i.get("src", ""))]
    if no_alt:
        f.add(
            AREA,
            "warning",
            "img-alt",
            f"{len(no_alt)} image(s) without alt text",
            ", ".join(str(i.get("src")) for i in no_alt[:4]),
            "Describe each image in a short sentence.",
            page,
        )
    if empty_alt:
        f.add(
            AREA,
            "info",
            "img-alt-empty",
            f"{len(empty_alt)} image(s) with empty alt text",
            fix="Fine for decoration; otherwise describe them.",
            page=page,
        )
    if no_size:
        f.add(AREA, "info", "img-size", f"{len(no_size)} image(s) without width/height (causes layout shift)", page=page)
    if old_fmt:
        f.add(AREA, "info", "img-format", f"{len(old_fmt)} image(s) not in WebP/AVIF", page=page)

    # ---- social cards ----
    missing_og = [
        p for p in ("og:title", "og:description", "og:image", "og:url") if not soup2.find("meta", attrs={"property": p})
    ]
    if missing_og:
        f.add(
            AREA,
            "warning",
            "og-missing",
            "Social sharing tags missing: " + ", ".join(missing_og),
            fix="Links shared on Instagram/Messenger/WhatsApp look broken without them.",
            page=page,
        )

    # ---- performance hints ----
    if len(html.encode("utf-8")) > HTML_WEIGHT_WARN:
        f.add(
            AREA,
            "info",
            "html-weight",
            f"HTML is {len(html.encode()) // 1024} KB",
            fix="Move big inline data out.",
            page=page,
        )
    blocking = [
        str(lk.get("href"))
        for lk in soup2.find_all("link", attrs={"rel": "stylesheet"})
        if str(lk.get("href", "")).startswith("http")
    ]
    if blocking:
        f.add(
            AREA,
            "info",
            "third-party-css",
            "Stylesheet loaded from another site (slower, and sends visitor data)",
            ", ".join(blocking[:3]),
            "Self-host fonts and CSS.",
            page,
        )
    insecure = [str(t.get("src")) for t in soup2.find_all(src=True) if str(t.get("src", "")).startswith("http://")]
    if insecure:
        f.add(
            AREA,
            "critical",
            "mixed-content",
            "Page loads files over insecure http://",
            ", ".join(insecure[:3]),
            "Use https:// links.",
            page,
        )

    # ---- in-page links ----
    ids = {str(t.get("id")) for t in soup2.find_all(id=True)}
    broken = sorted(
        {
            str(a["href"])[1:]
            for a in soup2.find_all("a", href=True)
            if str(a["href"]).startswith("#") and len(str(a["href"])) > 1 and str(a["href"])[1:] not in ids
        }
    )
    if broken:
        f.add(
            AREA,
            "warning",
            "broken-anchor",
            "Links to sections that don't exist",
            ", ".join("#" + b for b in broken),
            page=page,
        )

    # ---- structured data ----
    _check_structured_data(soup2, f, pf, page, today)
    return f, pf


def _iter_nodes(data: Any) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    if isinstance(data, list):
        for item in data:
            out.extend(_iter_nodes(item))
    elif isinstance(data, dict):
        if "@graph" in data:
            out.extend(_iter_nodes(data["@graph"]))
        if "@type" in data:
            out.append(data)
    return out


def _check_structured_data(soup: BeautifulSoup, f: FindingList, pf: PageFacts, page: str, today: date) -> None:
    nodes: list[dict[str, Any]] = []
    for tag in soup.find_all("script", attrs={"type": "application/ld+json"}):
        try:
            nodes.extend(_iter_nodes(json.loads(tag.string or "")))
        except ValueError as exc:
            f.add(
                AREA,
                "critical",
                "jsonld-invalid",
                "Structured data is not valid JSON",
                str(exc)[:120],
                "Fix the JSON-LD; Google ignores the whole block.",
                page,
            )
    types: list[Any] = []
    for n in nodes:
        t = n.get("@type")
        types.extend(t if isinstance(t, list) else [t])
    pf.schema_types = sorted({str(t) for t in types})
    for wanted, why in (
        ("Organization", "Google and AI assistants use it to understand who you are"),
        ("WebSite", "it names the site in search results"),
        ("FAQPage", "AI assistants quote FAQ answers"),
    ):
        if wanted not in pf.schema_types:
            f.add(AREA, "info", f"schema-{wanted.lower()}", f"No {wanted} structured data", fix=f"Add it: {why}.", page=page)
    visible = len(soup.select("details summary"))
    for n in nodes:
        if n.get("@type") == "FAQPage":
            marked = len(n.get("mainEntity") or [])
            if marked != visible:
                f.add(
                    AREA,
                    "warning",
                    "faq-mismatch",
                    f"FAQ structured data has {marked} questions but the page shows {visible}",
                    fix="Google requires every marked-up question to be visible on the page.",
                    page=page,
                )
    for n in nodes:
        if n.get("@type") == "Organization":
            same = n.get("sameAs") or []
            pf.same_as = same if isinstance(same, list) else [same]
        if n.get("@type") == "JobPosting":
            _check_job(n, f, pf, page, today)


def _check_job(n: dict[str, Any], f: FindingList, pf: PageFacts, page: str, today: date) -> None:
    for req in ("title", "description", "datePosted", "hiringOrganization", "jobLocation"):
        if not n.get(req):
            f.add(
                AREA,
                "warning",
                "job-field",
                f"JobPosting is missing '{req}'",
                fix="Google for Jobs ignores incomplete postings.",
                page=page,
            )
    vt = str(n.get("validThrough") or "")
    pf.job_valid_through = vt
    if not vt:
        return
    try:
        until = datetime.fromisoformat(vt).date()
    except ValueError:
        f.add(AREA, "warning", "job-date", f"JobPosting validThrough is not a date: {vt}", page=page)
        return
    days = (until - today).days
    if days < 0:
        f.add(
            AREA,
            "critical",
            "job-expired",
            "The Google for Jobs listing has expired",
            f"validThrough {until.isoformat()}",
            "Update job_posting.valid_through in site/src/site.yaml, or turn it off.",
            page,
        )
    elif days <= JOB_WARN_DAYS:
        f.add(
            AREA,
            "warning",
            "job-expiring",
            f"The Google for Jobs listing expires in {days} days",
            f"validThrough {until.isoformat()}",
            "Extend job_posting.valid_through in site/src/site.yaml if still hiring.",
            page,
        )
