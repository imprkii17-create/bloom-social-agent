"""Builds site/public from site/src: one static page per language, so Google indexes Hungarian and English separately.

What the build adds on top of the hand-written template:
- the page text in its language (no JavaScript needed to read it), `<html lang>`, title, description, canonical,
  hreflang alternates, social cards;
- structured data: Organization, WebSite, WebPage, FAQPage and (until it expires) JobPosting;
- responsive WebP images with width/height (no layout shift), lazy loading below the fold;
- self-hosted fonts (faster, and no visitor data sent to Google Fonts);
- sitemap.xml with language alternates, robots.txt, llms.txt, 404.html, Netlify headers.

The build is deterministic: run it twice and nothing changes. `check=True` reports what is out of date instead of
writing, which CI uses to make sure site/public always matches site/src.
"""

from __future__ import annotations

import copy
import hashlib
import html
import json
import re
import shutil
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from bs4 import BeautifulSoup, Comment, Tag

from . import images, markdown
from .config import LANGS, Guide, SiteConfig, faq_pairs, load_guides, load_site_config, load_strings, site_dir

MANIFEST = ".build-manifest.json"
OG_IMAGE = "images/og-image.jpg"
STRINGS_RE = re.compile(r"""\bt\("([\w.]+)"\)""")
I18N_ATTRS = ("data-i18n", "data-i18n-html", "data-i18n-placeholder", "data-i18n-alt", "data-i18n-aria")
SIZES = "(max-width: 860px) 100vw, 50vw"

FONT_FACES: tuple[tuple[str, str, str, str], ...] = (
    # family, file stem pattern, weight, style
    ("Fraunces", "fraunces-{subset}-opsz-normal", "100 900", "normal"),
    ("Fraunces", "fraunces-{subset}-opsz-italic", "100 900", "italic"),
    *(
        ("Plus Jakarta Sans", f"plus-jakarta-sans-{{subset}}-{w}-normal", str(w), "normal")
        for w in (400, 500, 600, 700, 800)
    ),
)
UNICODE_RANGES = {
    "latin": "U+0000-00FF,U+0131,U+0152-0153,U+02BB-02BC,U+02C6,U+02DA,U+02DC,U+0304,U+0308,U+0329,U+2000-206F,"
    "U+20AC,U+2122,U+2191,U+2193,U+2212,U+2215,U+FEFF,U+FFFD",
    "latin-ext": "U+0100-02BA,U+02BD-02C5,U+02C7-02CC,U+02CE-02D7,U+02DD-02FF,U+0304,U+0308,U+0329,U+1D00-1DBF,"
    "U+1E00-1E9F,U+1EF2-1EFF,U+2020,U+20A0-20AB,U+20AD-20C0,U+2113,U+2C60-2C7F,U+A720-A7FF",
}
PRELOAD_FONTS = ("fraunces-latin-opsz-normal", "plus-jakarta-sans-latin-500-normal")


class SiteBuildError(RuntimeError):
    """The site source has a problem the build can't work around (missing translation, bad template…)."""


@dataclass
class BuildResult:
    changed: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def up_to_date(self) -> bool:
        return not self.changed and not self.removed


# ---- public entry point ------------------------------------------------------------------------------


def build_site(repo_root: Path, *, check: bool = False, today: date | None = None) -> BuildResult:
    root = site_dir(repo_root)
    src = root / "src"
    public = root / "public"
    cfg = load_site_config(repo_root)
    strings = load_strings(repo_root)
    template = (src / "index.html").read_text(encoding="utf-8")
    today = today or datetime.now(ZoneInfo("Europe/Budapest")).date()

    result = BuildResult()
    _check_strings(template, strings)
    old = _read_manifest(public)
    new_manifest: dict[str, Any] = {"version": 1, "photos": {}, "pages": {}}
    out: dict[str, bytes] = {}
    stale_images: list[str] = []

    # static files (fonts, favicons, licences) are copied; photos are converted
    photos: dict[str, images.PhotoOutputs] = {}
    for path in sorted((src / "static").rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(src / "static").as_posix()
        if images.is_photo(rel):
            key = images.source_key(path, cfg.image_widths)
            prev = old.get("photos", {}).get(rel)
            if prev and prev.get("key") == key and all((public / p).is_file() for p in _photo_files(prev)):
                photos[rel] = _photo_from_manifest(rel, prev)
                for p in _photo_files(prev):
                    out[p] = (public / p).read_bytes()
            else:
                stale_images.append(rel)
                outputs, files = images.render(path, rel, cfg.image_widths)
                photos[rel] = outputs
                out.update(files)
            new_manifest["photos"][rel] = _photo_to_manifest(photos[rel], images.source_key(path, cfg.image_widths))
        else:
            out[rel] = path.read_bytes()

    og_src = src / "static" / cfg.og_image_source
    if not og_src.is_file():
        raise SiteBuildError(f"og_image_source {cfg.og_image_source} not found in site/src/static")
    og_key = hashlib.sha256(og_src.read_bytes() + images.PIPELINE_VERSION.encode()).hexdigest()
    if old.get("og", {}).get("key") == og_key and (public / OG_IMAGE).is_file():
        out[OG_IMAGE] = (public / OG_IMAGE).read_bytes()
    else:
        out[OG_IMAGE] = images.og_card(og_src)
    new_manifest["og"] = {"key": og_key, "path": OG_IMAGE}

    fonts_css = _font_css(src / "static" / "fonts")
    job_expired = _job_expired(cfg, today)
    if job_expired:
        result.warnings.append(
            "The JobPosting (Google for Jobs) has expired, so it was left out. Update job_posting.valid_through "
            "in site/src/site.yaml if you are still hiring."
        )

    guides = load_guides(repo_root)
    home_paths = {cfg.pages[lang].path for lang in LANGS}
    for gid, by_lang in guides.items():
        for g in by_lang.values():
            if g.meta.path in home_paths:
                raise SiteBuildError(f"guide '{gid}' uses the path {g.meta.path}, which is a home page")

    def add_page(rel: str, page: str) -> None:
        data = page.encode("utf-8")
        out[rel] = data
        sha = hashlib.sha256(data).hexdigest()
        prev_page = old.get("pages", {}).get(rel, {})
        lastmod = prev_page.get("lastmod") if prev_page.get("sha") == sha else today.isoformat()
        new_manifest["pages"][rel] = {"sha": sha, "lastmod": lastmod}

    for lang in LANGS:
        page = _render_page(template, cfg, strings, lang, photos, fonts_css, include_job=not job_expired, guides=guides)
        add_page(_page_file(cfg.pages[lang].path), page)

    if guides:
        guide_template_path = src / "guide.html"
        if not guide_template_path.is_file():
            raise SiteBuildError("site/src/guide.html (the guide page template) is missing")
        guide_template = guide_template_path.read_text(encoding="utf-8")
        _check_strings(guide_template, strings)
        for gid in guides:
            for lang in LANGS:
                page = _render_guide(guide_template, cfg, strings, guides, gid, lang, fonts_css)
                add_page(_page_file(guides[gid][lang].meta.path), page)

    out["404.html"] = _render_404(cfg, strings, fonts_css).encode("utf-8")
    out["sitemap.xml"] = _sitemap(cfg, new_manifest["pages"], guides).encode("utf-8")
    out["robots.txt"] = _robots(cfg).encode("utf-8")
    out["llms.txt"] = _llms(cfg, strings, guides).encode("utf-8")
    out["_headers"] = HEADERS.encode("utf-8")
    out[MANIFEST] = (json.dumps(new_manifest, indent=1, sort_keys=True) + "\n").encode("utf-8")

    # compare with what is on disk
    existing = {p.relative_to(public).as_posix() for p in public.rglob("*") if p.is_file()} if public.is_dir() else set()
    for rel, data in sorted(out.items()):
        target = public / rel
        if not target.is_file() or target.read_bytes() != data:
            result.changed.append(rel)
            if not check:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(data)
    for rel in sorted(existing - set(out)):
        result.removed.append(rel)
        if not check:
            (public / rel).unlink()
    if not check:
        _prune_empty_dirs(public)
    if check:
        result.warnings.extend(f"image {rel} needs converting" for rel in stale_images)
    return result


# ---- pages ---------------------------------------------------------------------------------------------


def _page_file(path: str) -> str:
    return (path.strip("/") + "/index.html").lstrip("/")


def _check_strings(template: str, strings: dict[str, dict[str, str]]) -> None:
    hu, en = set(strings["hu"]), set(strings["en"])
    if hu != en:
        only_hu, only_en = sorted(hu - en), sorted(en - hu)
        raise SiteBuildError(f"Translations don't match. Only in hu.json: {only_hu}. Only in en.json: {only_en}.")
    soup = BeautifulSoup(template, "html.parser")
    used = {str(el.get(a)) for a in I18N_ATTRS for el in soup.select(f"[{a}]")}
    used |= set(STRINGS_RE.findall(template))
    missing = sorted(used - hu)
    if missing:
        raise SiteBuildError(f"The page uses text keys that are missing from the translations: {missing}")


def _render_page(
    template: str,
    cfg: SiteConfig,
    strings: dict[str, dict[str, str]],
    lang: str,
    photos: dict[str, images.PhotoOutputs],
    fonts_css: str,
    *,
    include_job: bool,
    guides: dict[str, dict[str, Guide]] | None = None,
) -> str:
    t = strings[lang]
    soup = BeautifulSoup(template, "html.parser")
    _expand_faq(soup, len(faq_pairs(t)))
    if soup.find(string=lambda s: isinstance(s, Comment) and s.strip().startswith("bloom:guides")):
        _replace_marker(soup, "bloom:guides", _guide_cards(guides or {}, lang, t))
    _apply_i18n(soup, t, lang)

    for a in soup.select("a.lang-btn"):
        target_lang = str(a.get("data-lang"))
        if target_lang in cfg.pages:
            a["href"] = cfg.pages[target_lang].path
        classes = [c for c in a.get_attribute_list("class") if c and c != "active"]
        if target_lang == lang:
            classes.append("active")
            a["aria-current"] = "page"
        a["class"] = " ".join(classes)
        del a["data-lang"]

    _responsive_images(soup, photos)
    _script_strings(soup, template, t)
    _replace_marker(soup, "bloom:seo", _head_tags(cfg, lang))
    _replace_marker(soup, "bloom:fonts", fonts_css)
    _replace_marker(soup, "bloom:structured-data", _structured_data(cfg, strings, lang, include_job=include_job))
    return str(soup)


def _apply_i18n(soup: BeautifulSoup, t: dict[str, str], lang: str) -> None:
    html_el = soup.find("html")
    if not isinstance(html_el, Tag):
        raise SiteBuildError("template has no <html> element")
    html_el["lang"] = lang

    for el in soup.select("[data-i18n]"):
        el.clear()
        el.append(t[str(el["data-i18n"])])
    for el in soup.select("[data-i18n-html]"):
        el.clear()
        frag = BeautifulSoup(t[str(el["data-i18n-html"])], "html.parser")
        for child in list(frag.contents):
            el.append(child)
    for attr, target in (
        ("data-i18n-placeholder", "placeholder"),
        ("data-i18n-alt", "alt"),
        ("data-i18n-aria", "aria-label"),
    ):
        for el in soup.select(f"[{attr}]"):
            el[target] = t[str(el[attr])]
    for attr in I18N_ATTRS:
        for el in soup.select(f"[{attr}]"):
            del el[attr]


def _script_strings(soup: BeautifulSoup, template: str, t: dict[str, str]) -> None:
    """The strings the page script needs at runtime (form messages), in this language only."""
    script_keys = sorted(set(STRINGS_RE.findall(template)))
    payload = json.dumps({k: t[k] for k in script_keys}, ensure_ascii=False, sort_keys=True).replace("</", "<\\/")
    data_tag = soup.new_tag("script", attrs={"type": "application/json", "id": "bloom-strings"})
    data_tag.string = payload
    first_script = next((s for s in soup.find_all("script") if not s.get("type")), None)
    body = soup.find("body")
    if first_script is not None:
        first_script.insert_before(data_tag)
    elif isinstance(body, Tag):
        body.append(data_tag)


def _expand_faq(soup: BeautifulSoup, count: int) -> None:
    """The template has one FAQ item; repeat it for every faq.qN/faq.aN pair, so the visible FAQ always matches
    the FAQPage structured data (Google requires marked-up questions to be visible)."""
    box = soup.select_one("[data-faq]")
    if box is None:
        return
    items = box.select(".faq-item")
    if not items:
        raise SiteBuildError("the [data-faq] block needs one .faq-item to repeat")
    proto = copy.copy(items[0])
    for item in items:
        item.extract()
    for comment in box.find_all(string=lambda s: isinstance(s, Comment)):
        comment.extract()
    for n in range(1, count + 1):
        item = copy.copy(proto)
        q, a = item.select_one("summary"), item.select_one("p")
        if q is None or a is None:
            raise SiteBuildError("a .faq-item needs a <summary> (question) and a <p> (answer)")
        q["data-i18n"] = f"faq.q{n}"
        a["data-i18n"] = f"faq.a{n}"
        box.append("\n      ")
        box.append(item)
    box.append("\n    ")
    del box["data-faq"]


def _replace_marker(soup: BeautifulSoup, name: str, markup: str) -> None:
    marker = soup.find(string=lambda s: isinstance(s, Comment) and s.strip().startswith(name))
    if marker is None:
        raise SiteBuildError(f"template is missing the <!-- {name} --> marker")
    frag = BeautifulSoup(markup, "html.parser")
    for child in list(frag.contents):
        marker.insert_before(child)
    marker.extract()


def _responsive_images(soup: BeautifulSoup, photos: dict[str, images.PhotoOutputs]) -> None:
    first = True
    for img in soup.find_all("img"):
        src = str(img.get("src") or "")
        if not src.startswith("/"):
            continue
        rel = src.lstrip("/")
        photo = photos.get(rel)
        if photo is None:
            continue
        big = photo.largest
        img["src"] = "/" + big.path
        img["srcset"] = ", ".join(f"/{v.path} {v.width}w" for v in photo.variants)
        if not img.get("sizes"):
            img["sizes"] = SIZES
        img["width"] = str(big.width)
        img["height"] = str(big.height)
        img["decoding"] = "async"
        if first:
            img["fetchpriority"] = "high"
            first = False
        else:
            img["loading"] = "lazy"


def _head_tags(cfg: SiteConfig, lang: str) -> str:
    meta = cfg.pages[lang]
    alternates = {other: cfg.url(other) for other in LANGS}
    return _head(
        cfg,
        lang,
        title=meta.title,
        description=meta.description,
        og_title=meta.og_title,
        og_image_alt=meta.og_image_alt,
        url=cfg.url(lang),
        alternates=alternates,
        og_type="website",
    )


def _head(
    cfg: SiteConfig,
    lang: str,
    *,
    title: str,
    description: str,
    og_title: str,
    og_image_alt: str,
    url: str,
    alternates: dict[str, str],
    og_type: str,
) -> str:
    og_url = f"{cfg.base_url}/{OG_IMAGE}"
    e = html.escape
    lines = [
        f"<title>{e(title)}</title>",
        f'<meta name="description" content="{e(description)}">',
        '<meta name="robots" content="index, follow, max-image-preview:large">',
        f'<link rel="canonical" href="{e(url)}">',
    ]
    for other in LANGS:
        lines.append(f'<link rel="alternate" hreflang="{other}" href="{e(alternates[other])}">')
    lines.append(f'<link rel="alternate" hreflang="x-default" href="{e(alternates[cfg.x_default])}">')
    lines += [
        '<meta name="theme-color" content="#d1607f">',
        f'<meta property="og:type" content="{og_type}">',
        '<meta property="og:site_name" content="Bloom Models">',
        f'<meta property="og:title" content="{e(og_title)}">',
        f'<meta property="og:description" content="{e(description)}">',
        f'<meta property="og:url" content="{e(url)}">',
        f'<meta property="og:locale" content="{e(cfg.pages[lang].locale)}">',
    ]
    for other in LANGS:
        if other != lang:
            lines.append(f'<meta property="og:locale:alternate" content="{e(cfg.pages[other].locale)}">')
    lines += [
        f'<meta property="og:image" content="{e(og_url)}">',
        f'<meta property="og:image:width" content="{images.OG_SIZE[0]}">',
        f'<meta property="og:image:height" content="{images.OG_SIZE[1]}">',
        f'<meta property="og:image:alt" content="{e(og_image_alt)}">',
        '<meta name="twitter:card" content="summary_large_image">',
        f'<meta name="twitter:title" content="{e(og_title)}">',
        f'<meta name="twitter:description" content="{e(description)}">',
        f'<meta name="twitter:image" content="{e(og_url)}">',
    ]
    return "\n".join(lines) + "\n"


def _font_css(font_dir: Path) -> str:
    preload = [
        f'<link rel="preload" href="/fonts/{name}.woff2" as="font" type="font/woff2" crossorigin>' for name in PRELOAD_FONTS
    ]
    faces = []
    for family, pattern, weight, style in FONT_FACES:
        for subset in ("latin-ext", "latin"):
            stem = pattern.format(subset=subset)
            if not (font_dir / f"{stem}.woff2").is_file():
                raise SiteBuildError(f"font file fonts/{stem}.woff2 is missing")
            faces.append(
                f"@font-face{{font-family:'{family}';font-style:{style};font-display:swap;font-weight:{weight};"
                f"src:url(/fonts/{stem}.woff2) format('woff2');unicode-range:{UNICODE_RANGES[subset]}}}"
            )
    return "\n".join(preload) + "\n<style>\n" + "\n".join(faces) + "\n</style>\n"


# ---- structured data -------------------------------------------------------------------------------------


def _job_expired(cfg: SiteConfig, today: date) -> bool:
    jp = cfg.job_posting
    return bool(jp and jp.enabled and jp.valid_through and jp.valid_through < today)


def _structured_data(cfg: SiteConfig, strings: dict[str, dict[str, str]], lang: str, *, include_job: bool) -> str:
    t = strings[lang]
    meta = cfg.pages[lang]
    url = cfg.url(lang)
    org_id = f"{cfg.base_url}/#organization"
    site_id = f"{cfg.base_url}/#website"
    address = {
        "@type": "PostalAddress",
        "addressLocality": "Budapest",
        "addressRegion": "V. kerület" if lang == "hu" else "District V",
        "addressCountry": "HU",
    }
    org: dict[str, Any] = {
        "@type": "Organization",
        "@id": org_id,
        "name": "Bloom Models",
        "url": cfg.base_url + "/",
        "logo": f"{cfg.base_url}/images/favicon-512.png",
        "description": t.get("hero.lede", meta.description),
        "address": address,
        "areaServed": "HU",
    }
    if cfg.contact_email:
        org["email"] = cfg.contact_email
    if cfg.instagram_url:
        org["sameAs"] = [cfg.instagram_url]
    graph: list[dict[str, Any]] = [
        org,
        {
            "@type": "WebSite",
            "@id": site_id,
            "url": cfg.base_url + "/",
            "name": "Bloom Models",
            "inLanguage": list(LANGS),
            "publisher": {"@id": org_id},
        },
        {
            "@type": "WebPage",
            "@id": url + "#webpage",
            "url": url,
            "name": meta.title,
            "description": meta.description,
            "inLanguage": lang,
            "isPartOf": {"@id": site_id},
            "about": {"@id": org_id},
            "primaryImageOfPage": f"{cfg.base_url}/{OG_IMAGE}",
        },
    ]
    pairs = faq_pairs(t)
    if pairs:
        graph.append(
            {
                "@type": "FAQPage",
                "@id": url + "#faq",
                "inLanguage": lang,
                "mainEntity": [
                    {"@type": "Question", "name": q, "acceptedAnswer": {"@type": "Answer", "text": a}} for q, a in pairs
                ],
            }
        )
    jp = cfg.job_posting
    if include_job and jp and jp.enabled:
        desc_parts = [t.get("hero.lede", "")] + [a for _, a in pairs[:5]]
        job: dict[str, Any] = {
            "@type": "JobPosting",
            "title": jp.title.get(lang) or next(iter(jp.title.values())),
            "description": "".join(f"<p>{html.escape(p)}</p>" for p in desc_parts if p),
            "datePosted": jp.date_posted.isoformat(),
            "employmentType": jp.employment_type,
            "hiringOrganization": {
                "@type": "Organization",
                "name": "Bloom Models",
                "sameAs": cfg.base_url + "/",
                "logo": f"{cfg.base_url}/images/favicon-512.png",
            },
            "jobLocation": {"@type": "Place", "address": address},
            "directApply": True,
            "url": url + "#apply",
        }
        if jp.valid_through:
            job["validThrough"] = f"{jp.valid_through.isoformat()}T23:59:59+01:00"
        graph.append(job)
    payload = json.dumps({"@context": "https://schema.org", "@graph": graph}, ensure_ascii=False, indent=1)
    return '<script type="application/ld+json">\n' + payload.replace("</", "<\\/") + "\n</script>\n"


# ---- guide pages ---------------------------------------------------------------------------------------------

MONTHS = {
    "hu": ("január", "február", "március", "április", "május", "június", "július", "augusztus", "szeptember",
           "október", "november", "december"),
    "en": ("January", "February", "March", "April", "May", "June", "July", "August", "September", "October",
           "November", "December"),
}


def _month_year(d: date, lang: str) -> str:
    month = MONTHS[lang][d.month - 1]
    return f"{d.year}. {month}" if lang == "hu" else f"{month} {d.year}"


def _guide_cards(guides: dict[str, dict[str, Guide]], lang: str, t: dict[str, str]) -> str:
    """Link cards to every guide, for the home page (internal links are how Google finds and weighs the guides)."""
    e = html.escape
    cards = []
    for by_lang in guides.values():
        m = by_lang[lang].meta
        cards.append(
            f'<a class="guide-card" href="{e(m.path)}"><span class="guide-card-title">{e(m.h1)}</span>'
            f'<span class="guide-card-text">{e(m.card or m.description)}</span>'
            f'<span class="guide-card-more">{e(t["guides.read"])} →</span></a>'
        )
    return "\n".join(cards)


def _render_guide(
    template: str,
    cfg: SiteConfig,
    strings: dict[str, dict[str, str]],
    guides: dict[str, dict[str, Guide]],
    gid: str,
    lang: str,
    fonts_css: str,
) -> str:
    t = strings[lang]
    guide = guides[gid][lang]
    m = guide.meta
    e = html.escape
    url = cfg.base_url + m.path
    home = cfg.pages[lang].path
    alternates = {other: cfg.base_url + guides[gid][other].meta.path for other in LANGS}

    soup = BeautifulSoup(template, "html.parser")
    _apply_i18n(soup, t, lang)
    for a in soup.select("a.lang-btn"):
        target_lang = str(a.get("data-lang"))
        if target_lang in guides[gid]:
            a["href"] = guides[gid][target_lang].meta.path
        classes = [c for c in a.get_attribute_list("class") if c and c != "active"]
        if target_lang == lang:
            classes.append("active")
            a["aria-current"] = "page"
        a["class"] = " ".join(classes)
        del a["data-lang"]
    for a in soup.select("[data-home]"):
        anchor = str(a["data-home"])
        a["href"] = home + (f"#{anchor}" if anchor else "")
        del a["data-home"]

    body_html, toc = markdown.render(guide.body)
    faq_id = "gyik" if lang == "hu" else "faq"
    if m.faq:
        toc.append((faq_id, t["guide.faq"]))
    toc_html = "".join(f'<li><a href="#{hid}">{markdown.inline(text)}</a></li>' for hid, text in toc)
    faq_html = ""
    if m.faq:
        items = "".join(
            f'<details class="faq-item"><summary>{e(f.q)}</summary><p>{e(f.a)}</p></details>' for f in m.faq
        )
        faq_html = f'<section class="guide-faq" aria-labelledby="{faq_id}"><h2 id="{faq_id}">{e(t["guide.faq"])}</h2>' \
            f'<div class="faq-list">{items}</div></section>'
    updated = f'<time datetime="{m.updated.isoformat()}">{e(_month_year(m.updated, lang))}</time>'
    article = f"""<nav class="crumbs" aria-label="{e(t['guide.crumbs'])}"><a href="{e(home)}">{e(t['guide.home'])}</a>
<span aria-hidden="true">›</span> <span aria-current="page">{e(m.h1)}</span></nav>
<header class="guide-head">
<h1>{e(m.h1)}</h1>
<p class="lede">{e(m.lede)}</p>
<p class="guide-meta">Bloom Models · {e(t['guide.updated'])}: {updated}</p>
</header>
<div class="guide-layout">
<nav class="toc" aria-label="{e(t['guide.toc'])}"><p class="label">{e(t['guide.toc'])}</p><ol>{toc_html}</ol></nav>
<div class="prose">
{body_html}
{faq_html}
<div class="guide-cta"><p class="cta-title">{e(t['guide.cta.h'])}</p><p>{e(t['guide.cta.p'])}</p>
<a class="btn btn-primary" href="{e(home)}#apply">{e(t['guide.cta.btn'])}</a></div>
</div>
</div>
"""
    related = [g[lang].meta for other_id, g in guides.items() if other_id != gid]
    related_html = ""
    if related:
        links = "".join(
            f'<a class="guide-card" href="{e(r.path)}"><span class="guide-card-title">{e(r.h1)}</span>'
            f'<span class="guide-card-text">{e(r.card or r.description)}</span>'
            f'<span class="guide-card-more">{e(t["guides.read"])} →</span></a>'
            for r in related
        )
        related_html = f'<p class="label related-label">{e(t["guide.related"])}</p><div class="guide-grid">{links}</div>'

    _replace_marker(soup, "bloom:article", article)
    _replace_marker(soup, "bloom:related", related_html)
    _replace_marker(
        soup,
        "bloom:seo",
        _head(
            cfg,
            lang,
            title=m.title,
            description=m.description,
            og_title=m.h1,
            og_image_alt=cfg.pages[lang].og_image_alt,
            url=url,
            alternates=alternates,
            og_type="article",
        ),
    )
    _replace_marker(soup, "bloom:fonts", fonts_css)
    _replace_marker(soup, "bloom:structured-data", _guide_structured_data(cfg, strings, guide, url))
    return str(soup)


def _guide_structured_data(cfg: SiteConfig, strings: dict[str, dict[str, str]], guide: Guide, url: str) -> str:
    m = guide.meta
    t = strings[m.lang]
    org_id = f"{cfg.base_url}/#organization"
    site_id = f"{cfg.base_url}/#website"
    org: dict[str, Any] = {
        "@type": "Organization",
        "@id": org_id,
        "name": "Bloom Models",
        "url": cfg.base_url + "/",
        "logo": f"{cfg.base_url}/images/favicon-512.png",
    }
    if cfg.instagram_url:
        org["sameAs"] = [cfg.instagram_url]
    graph: list[dict[str, Any]] = [
        org,
        {
            "@type": "WebSite",
            "@id": site_id,
            "url": cfg.base_url + "/",
            "name": "Bloom Models",
            "inLanguage": list(LANGS),
            "publisher": {"@id": org_id},
        },
        {
            "@type": "WebPage",
            "@id": url + "#webpage",
            "url": url,
            "name": m.title,
            "description": m.description,
            "inLanguage": m.lang,
            "isPartOf": {"@id": site_id},
            "breadcrumb": {"@id": url + "#breadcrumb"},
            "primaryImageOfPage": f"{cfg.base_url}/{OG_IMAGE}",
        },
        {
            "@type": "Article",
            "@id": url + "#article",
            "headline": m.h1,
            "description": m.description,
            "inLanguage": m.lang,
            "datePublished": m.updated.isoformat(),
            "dateModified": m.updated.isoformat(),
            "author": {"@id": org_id},
            "publisher": {"@id": org_id},
            "mainEntityOfPage": {"@id": url + "#webpage"},
            "image": f"{cfg.base_url}/{OG_IMAGE}",
        },
        {
            "@type": "BreadcrumbList",
            "@id": url + "#breadcrumb",
            "itemListElement": [
                {"@type": "ListItem", "position": 1, "name": t["guide.home"], "item": cfg.url(m.lang)},
                {"@type": "ListItem", "position": 2, "name": m.h1, "item": url},
            ],
        },
    ]
    if m.faq:
        graph.append(
            {
                "@type": "FAQPage",
                "@id": url + "#faq",
                "inLanguage": m.lang,
                "mainEntity": [
                    {"@type": "Question", "name": f.q, "acceptedAnswer": {"@type": "Answer", "text": f.a}}
                    for f in m.faq
                ],
            }
        )
    payload = json.dumps({"@context": "https://schema.org", "@graph": graph}, ensure_ascii=False, indent=1)
    return '<script type="application/ld+json">\n' + payload.replace("</", "<\\/") + "\n</script>\n"


# ---- other files -------------------------------------------------------------------------------------------


def _sitemap(cfg: SiteConfig, pages: dict[str, dict[str, str]], guides: dict[str, dict[str, Guide]] | None = None) -> str:
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9" xmlns:xhtml="http://www.w3.org/1999/xhtml">',
    ]
    groups: list[dict[str, str]] = [{lang: cfg.pages[lang].path for lang in LANGS}]
    for by_lang in (guides or {}).values():
        groups.append({lang: by_lang[lang].meta.path for lang in LANGS})
    for group in groups:
        for lang in LANGS:
            rel = _page_file(group[lang])
            lines.append("  <url>")
            lines.append(f"    <loc>{html.escape(cfg.base_url + group[lang])}</loc>")
            lines.append(f"    <lastmod>{pages[rel]['lastmod']}</lastmod>")
            for other in LANGS:
                href = html.escape(cfg.base_url + group[other])
                lines.append(f'    <xhtml:link rel="alternate" hreflang="{other}" href="{href}"/>')
            x_default = html.escape(cfg.base_url + group[cfg.x_default])
            lines.append(f'    <xhtml:link rel="alternate" hreflang="x-default" href="{x_default}"/>')
            lines.append("  </url>")
    lines.append("</urlset>")
    return "\n".join(lines) + "\n"


def _robots(cfg: SiteConfig) -> str:
    return (
        "# Everyone may crawl the whole site, including AI search assistants\n"
        "# (OAI-SearchBot, Claude-SearchBot, PerplexityBot…), so they can find and recommend Bloom Models.\n"
        "User-agent: *\n"
        "Allow: /\n\n"
        f"Sitemap: {cfg.base_url}/sitemap.xml\n"
    )


def _llms(cfg: SiteConfig, strings: dict[str, dict[str, str]], guides: dict[str, dict[str, Guide]] | None = None) -> str:
    en, hu = strings["en"], strings["hu"]
    lines = [
        "# Bloom Models",
        "",
        f"> {en.get('hero.lede', cfg.pages['en'].description)}",
        "",
        "Bloom Models recruits adults (18+) in Hungary for online streaming model work. The website is available "
        "in Hungarian (main market) and English.",
        "",
        "## Pages",
        "",
        f"- [Bloom Models (Hungarian)]({cfg.url('hu')}): {cfg.pages['hu'].description}",
        f"- [Bloom Models (English)]({cfg.url('en')}): {cfg.pages['en'].description}",
        f"- [Apply]({cfg.url('en')}#apply): two-minute application form with one photo.",
        "",
    ]
    if guides:
        lines += ["## Guides", ""]
        for by_lang in guides.values():
            for lang in LANGS:
                gm = by_lang[lang].meta
                lines.append(f"- [{gm.h1}]({cfg.base_url}{gm.path}) ({lang}): {gm.description}")
        lines.append("")
    lines += [
        "## Frequently asked questions",
        "",
    ]
    for q, a in faq_pairs(en):
        lines += [f"### {q}", "", a, ""]
    if faq_pairs(hu):
        lines += ["## Gyakori kérdések (magyarul)", ""]
        for q, a in faq_pairs(hu):
            lines += [f"### {q}", "", a, ""]
    lines += ["## Contact", ""]
    if cfg.contact_email:
        lines.append(f"- Email: {cfg.contact_email}")
    if cfg.instagram_url:
        lines.append(f"- Instagram: {cfg.instagram_url}")
    lines.append(f"- Website: {cfg.base_url}/")
    return "\n".join(lines).rstrip() + "\n"


def _render_404(cfg: SiteConfig, strings: dict[str, dict[str, str]], fonts_css: str) -> str:
    hu, en = strings["hu"], strings["en"]
    e = html.escape
    return f"""<!doctype html>
<html lang="hu">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex">
<title>{e(hu["notfound.title"])} · Bloom Models</title>
<link rel="icon" href="/favicon.ico">
{fonts_css}<style>
  :root {{ --bg:#fff; --ink:#4a2a34; --muted:#7c5763; --rose:#d1607f; --border:#f3dde3; }}
  @media (prefers-color-scheme: dark) {{
    :root {{ --bg:#241a1e; --ink:#faedf0; --muted:#d1a8b3; --rose:#eda8b6; --border:#55323c; }}
  }}
  body {{ margin:0; background:var(--bg); color:var(--ink); font-family:"Plus Jakarta Sans",system-ui,sans-serif;
         min-height:100vh; display:grid; place-items:center; padding:32px 16px; box-sizing:border-box; }}
  main {{ max-width:34rem; display:grid; gap:28px; text-align:center; }}
  h1 {{ font-family:"Fraunces",serif; font-weight:600; font-size:clamp(1.8rem,5vw,2.6rem); margin:0; text-wrap:balance; }}
  p {{ color:var(--muted); line-height:1.6; margin:8px 0 0; }}
  a {{ display:inline-block; margin-top:14px; padding:12px 22px; border-radius:999px; background:var(--rose); color:#fff;
      text-decoration:none; font-weight:700; }}
  a:focus-visible {{ outline:2px solid var(--ink); outline-offset:3px; }}
  hr {{ border:0; border-top:1px solid var(--border); width:60%; margin:0 auto; }}
</style>
</head>
<body>
<main>
  <section lang="hu"><h1>{e(hu["notfound.title"])}</h1><p>{e(hu["notfound.p"])}</p>
    <a href="{e(cfg.pages["hu"].path)}">{e(hu["notfound.cta"])}</a></section>
  <hr>
  <section lang="en"><h1>{e(en["notfound.title"])}</h1><p>{e(en["notfound.p"])}</p>
    <a href="{e(cfg.pages["en"].path)}">{e(en["notfound.cta"])}</a></section>
</main>
</body>
</html>
"""


HEADERS = """/*
  X-Content-Type-Options: nosniff
  Referrer-Policy: strict-origin-when-cross-origin
  X-Frame-Options: DENY
  Permissions-Policy: camera=(), microphone=(), geolocation=()

/images/*
  Cache-Control: public, max-age=2592000

/fonts/*
  Cache-Control: public, max-age=31536000, immutable
"""


# ---- manifest helpers ----------------------------------------------------------------------------------------


def _read_manifest(public: Path) -> dict[str, Any]:
    try:
        data = json.loads((public / MANIFEST).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _photo_to_manifest(p: images.PhotoOutputs, key: str) -> dict[str, Any]:
    return {
        "key": key,
        "width": p.width,
        "height": p.height,
        "variants": [[v.width, v.height, v.path] for v in p.variants],
        "fallback": p.fallback,
    }


def _photo_from_manifest(rel: str, m: dict[str, Any]) -> images.PhotoOutputs:
    variants = tuple(images.Variant(int(w), int(h), str(path)) for w, h, path in m["variants"])
    return images.PhotoOutputs(rel, int(m["width"]), int(m["height"]), variants, str(m["fallback"]))


def _photo_files(m: dict[str, Any]) -> list[str]:
    return [str(v[2]) for v in m.get("variants", [])] + [str(m.get("fallback"))]


def _prune_empty_dirs(public: Path) -> None:
    for d in sorted((p for p in public.rglob("*") if p.is_dir()), key=lambda p: len(p.parts), reverse=True):
        if not any(d.iterdir()):
            shutil.rmtree(d)
