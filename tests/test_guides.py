"""Guide pages (site/src/guides/*.md): loading, Markdown, build output, and the SEO checks that cover them."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest
from bs4 import BeautifulSoup

from bloom_agent.facts import check_text, load_facts
from bloom_agent.seo.sitecheck import check_site
from bloom_agent.site import markdown
from bloom_agent.site.build import build_site
from bloom_agent.site.config import DESCRIPTION_MAX, TITLE_MAX, load_guides

TODAY = date(2026, 10, 13)
BASE = "https://bloommodels.hu"


def _guide_text(md_body: str) -> str:
    html, _ = markdown.render(md_body)
    return BeautifulSoup(html, "html.parser").get_text(" ")


def test_markdown_renders_the_subset_and_escapes_html() -> None:
    html, toc = markdown.render(
        "## Első rész\n\nSzöveg **félkövér** és *dőlt*, [link](/x/) <script>.\n\n"
        "- egy\n- kettő\n\n1. első\n2. második\n\n> **Megjegyzés:** fontos.\n\n"
        "| A | B |\n|---|---|\n| 1 | 2 |\n\n### Alcím\n\n## Első rész\n"
    )
    assert '<h2 id="elso-resz">' in html and '<h2 id="elso-resz-2">' in html
    assert toc == [("elso-resz", "Első rész"), ("elso-resz-2", "Első rész")]
    assert "<strong>félkövér</strong>" in html and "<em>dőlt</em>" in html and '<a href="/x/">link</a>' in html
    assert "&lt;script&gt;" in html and "<script>" not in html
    assert "<ul><li>egy</li><li>kettő</li></ul>" in html and "<ol><li>első</li><li>második</li></ol>" in html
    assert '<aside class="callout"><p><strong>Megjegyzés:</strong> fontos.</p></aside>' in html
    assert '<th scope="col">A</th>' in html and "<td>2</td>" in html and "<h3>Alcím</h3>" in html


def test_external_links_open_safely() -> None:
    html, _ = markdown.render("Lásd [ezt](https://example.com/).")
    assert 'rel="noopener"' in html and 'target="_blank"' in html


def test_every_guide_exists_in_both_languages_and_fits_google_limits() -> None:
    guides = load_guides(Path(__file__).resolve().parents[1])
    assert guides, "there should be at least one guide"
    facts = load_facts(Path(__file__).resolve().parents[1])
    for gid, by_lang in guides.items():
        assert set(by_lang) == {"hu", "en"}, gid
        for lang, g in by_lang.items():
            m = g.meta
            assert len(m.title) <= TITLE_MAX, (gid, lang, m.title)
            assert len(m.description) <= DESCRIPTION_MAX, (gid, lang, len(m.description))
            assert m.keywords, (gid, lang)
            if lang == "en":
                assert m.path.startswith("/en/")
            text = " ".join([m.h1, m.lede, _guide_text(g.body)] + [f"{f.q} {f.a}" for f in m.faq])
            assert not check_text(text, facts), (gid, lang, [i.message for i in check_text(text, facts)])


def test_guide_with_a_missing_language_is_refused(tmp_path: Path) -> None:
    folder = tmp_path / "site" / "src" / "guides"
    folder.mkdir(parents=True)
    (folder / "x.hu.md").write_text(
        "---\nid: x\nlang: hu\npath: /x/\ntitle: T\ndescription: D\nh1: H\nlede: L\nupdated: 2026-10-13\n---\nSzöveg\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="missing its en version"):
        load_guides(tmp_path)


def test_guide_path_must_be_plain_ascii(tmp_path: Path) -> None:
    folder = tmp_path / "site" / "src" / "guides"
    folder.mkdir(parents=True)
    (folder / "x.hu.md").write_text(
        "---\nid: x\nlang: hu\npath: /kéz/\ntitle: T\ndescription: D\nh1: H\nlede: L\nupdated: 2026-10-13\n---\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="a-z"):
        load_guides(tmp_path)


def test_guide_pages_are_built_with_hreflang_schema_and_links(full_repo: Path) -> None:
    build_site(full_repo, today=TODAY)
    public = full_repo / "site" / "public"
    guides = load_guides(full_repo)
    homes = {
        "hu": (public / "index.html").read_text(encoding="utf-8"),
        "en": (public / "en" / "index.html").read_text(encoding="utf-8"),
    }
    sitemap = (public / "sitemap.xml").read_text(encoding="utf-8")
    llms = (public / "llms.txt").read_text(encoding="utf-8")
    for gid, by_lang in guides.items():
        for lang, g in by_lang.items():
            m = g.meta
            page = public / m.path.strip("/") / "index.html"
            soup = BeautifulSoup(page.read_text(encoding="utf-8"), "html.parser")
            assert soup.html is not None and soup.html["lang"] == lang
            assert soup.find("h1").get_text() == m.h1  # type: ignore[union-attr]
            assert len(soup.find_all("h1")) == 1
            canonical = soup.find("link", rel="canonical")
            assert canonical is not None and canonical["href"] == BASE + m.path
            alts = {lk["hreflang"]: lk["href"] for lk in soup.find_all("link", rel="alternate")}
            assert alts == {
                "hu": BASE + by_lang["hu"].meta.path,
                "en": BASE + by_lang["en"].meta.path,
                "x-default": BASE + by_lang["en"].meta.path,
            }
            other = "en" if lang == "hu" else "hu"
            switch = soup.select_one(f'a.lang-btn[hreflang="{other}"]')
            assert switch is not None and switch["href"] == by_lang[other].meta.path
            assert not soup.select("[data-i18n], [data-home], [data-lang]")
            ld = json.loads(soup.find("script", type="application/ld+json").string)  # type: ignore[union-attr]
            types = {n["@type"] for n in ld["@graph"]}
            assert {"Organization", "WebSite", "WebPage", "Article", "BreadcrumbList"} <= types
            faq_nodes = [n for n in ld["@graph"] if n["@type"] == "FAQPage"]
            if m.faq:
                assert len(faq_nodes[0]["mainEntity"]) == len(soup.select(".guide-faq summary")) == len(m.faq)
            apply = soup.select_one(".guide-cta a")
            assert apply is not None and apply["href"] == ("/" if lang == "hu" else "/en/") + "#apply"
            assert f'href="{m.path}"' in homes[lang], f"home page must link to {m.path}"
            assert f"<loc>{BASE}{m.path}</loc>" in sitemap
            assert f"{BASE}{m.path}" in llms
            for a in soup.select(".prose a[href^='/']"):
                target = a["href"].split("#")[0]
                assert (public / target.strip("/") / "index.html").is_file() or target == "/", (gid, lang, target)


def test_guides_pass_the_offline_seo_check(full_repo: Path) -> None:
    build_site(full_repo, today=TODAY)
    report = check_site(full_repo, live=False, today=TODAY)
    guide_urls = {BASE + g.meta.path for by_lang in load_guides(full_repo).values() for g in by_lang.values()}
    problems = [x for x in report.findings.items if x.severity in ("critical", "warning") and x.page in guide_urls]
    assert not problems, [(x.code, x.page) for x in problems]


def test_seo_check_warns_when_home_page_stops_linking_a_guide(full_repo: Path) -> None:
    build_site(full_repo, today=TODAY)
    home = full_repo / "site" / "public" / "index.html"
    guides = load_guides(full_repo)
    first = next(iter(guides.values()))["hu"].meta.path
    home.write_text(home.read_text(encoding="utf-8").replace(f'href="{first}"', 'href="/"'), encoding="utf-8")
    report = check_site(full_repo, live=False, today=TODAY)
    assert any(x.code == "guide-orphan" and x.page == BASE + first for x in report.findings.items)
