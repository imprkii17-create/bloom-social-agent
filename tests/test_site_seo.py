"""Website build, facts, and every SEO check (offline; network calls are mocked)."""

from __future__ import annotations

import base64
import json
import re
from datetime import date, datetime
from pathlib import Path

import httpx
import pytest
import respx
from bs4 import BeautifulSoup
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from bloom_agent.config import load_settings
from bloom_agent.facts import check_text, load_facts, normalise, numbers_in
from bloom_agent.models import Post, Status
from bloom_agent.seo.findings import FindingList
from bloom_agent.seo.instagram_seo import InstagramSeoConfig, post_seo_issues, profile_findings, queue_findings
from bloom_agent.seo.onpage import PageExpectations, _contains_keyword, check_page
from bloom_agent.seo.pagespeed import parse_pagespeed, speed_findings
from bloom_agent.seo.search_console import (
    INSPECT_URL,
    TOKEN_URL,
    Row,
    SearchConsole,
    SearchConsoleError,
    SearchSummary,
    analyse,
    make_jwt,
    search_findings,
)
from bloom_agent.seo.sitecheck import Fetched, check_site
from bloom_agent.site.build import SiteBuildError, build_site
from bloom_agent.site.config import load_site_config

TODAY = date(2026, 10, 11)


# ---- facts ----------------------------------------------------------------------------------------------------------


def test_normalise_number_formats() -> None:
    for raw in ("2000", "2 000", "2.000", "2,000", "2k", "2K", "€2,000+"):
        assert normalise(raw.lstrip("€")) == "2000"
    assert normalise("80%") == "80"
    assert normalise("1.5k") == "1500"


def test_numbers_in_ignores_times_years_keycaps_and_small_counts() -> None:
    text = "18:30-kor, 2026-ban, 24/7, 3️⃣ lépés, 4 tévhit, #top5 @bloom5 https://x.hu/2000"
    assert numbers_in(text) == []
    found = [n for n, _ in numbers_in("Havi 3000 € és 90%-ig, 5 órás blokk, 2 perc")]
    assert found == ["3000", "90", "5", "2"]


def test_check_text_flags_unknown_numbers_and_missing_disclaimer(full_repo: Path) -> None:
    facts = load_facts(full_repo)
    assert check_text("Havi 2000 € felett reális cél. A kereset változó, nem garantált. 18+", facts) == []
    kinds = [i.kind for i in check_text("Keress havi 5000 €-t!", facts)]
    assert kinds == ["unknown-number"]
    kinds = [i.kind for i in check_text("Havi 2000 € felett reális cél, akár 80%.", facts)]
    assert kinds == ["missing-disclaimer"]


@pytest.mark.parametrize(
    ("text", "kinds"),
    [
        ("Garantáljuk: havi 2000 € fizetés!", {"guarantee", "missing-disclaimer"}),
        ("Guaranteed €2,000 a month", {"guarantee", "missing-disclaimer"}),
        ("Légy anyagilag független: havi 2000 €", {"missing-disclaimer"}),
        ("Keress 80 €-t óránként", {"unknown-number"}),
        ("Earn 100 € per stream", {"unknown-number"}),
        ("Havi 2 millió forint", {"unknown-number"}),
        ("Akár €2K havonta", {"missing-disclaimer"}),
        ("Havi 2.000 € is lehet", {"missing-disclaimer"}),
        ("Earnings vary and are never guaranteed — €2,000+ a month", set()),
        ("akár 80%-a, 18+, heti 20 óra, 5 órás blokk, 72 órán belül, 2 perc, 12 hónapon belül, 100%", set()),
        ("5th district, V. kerület, 4 tévhit, 24/7, 2026, 18:30", set()),
    ],
)
def test_facts_units_guarantees_and_disclaimers(full_repo: Path, text: str, kinds: set[str]) -> None:
    assert {i.kind for i in check_text(text, load_facts(full_repo))} == kinds


# ---- site build -------------------------------------------------------------------------------------------------------


def test_site_build_is_deterministic_and_up_to_date(full_repo: Path) -> None:
    first = build_site(full_repo, today=TODAY)
    second = build_site(full_repo, today=date(2026, 12, 1), check=True)
    assert second.up_to_date, second.changed
    assert "sitemap.xml" in first.changed or first.up_to_date


def test_site_pages_are_static_per_language(full_repo: Path) -> None:
    build_site(full_repo, today=TODAY)
    public = full_repo / "site" / "public"
    hu = BeautifulSoup((public / "index.html").read_text(encoding="utf-8"), "html.parser")
    en = BeautifulSoup((public / "en" / "index.html").read_text(encoding="utf-8"), "html.parser")
    assert hu.html is not None and hu.html["lang"] == "hu" and en.html is not None and en.html["lang"] == "en"
    assert "reflektorfénybe" in hu.get_text() and "spotlight" in en.get_text()
    for soup, lang in ((hu, "hu"), (en, "en")):
        alts = {lk["hreflang"]: lk["href"] for lk in soup.find_all("link", rel="alternate")}
        assert alts == {"hu": "https://bloommodels.hu/", "en": "https://bloommodels.hu/en/",
                        "x-default": "https://bloommodels.hu/en/"}
        assert not soup.select("[data-i18n]"), "i18n attributes must be removed from the output"
        active = soup.select_one("a.lang-btn.active")
        assert active is not None and active["aria-current"] == "page" and active.get_text() == lang.upper()
        strings = json.loads(soup.select_one("#bloom-strings").string)  # type: ignore[union-attr]
        assert strings["form.uploadLabelDefault"] and strings["form.successMsg"]
        ld = json.loads(soup.find("script", type="application/ld+json").string)  # type: ignore[union-attr]
        types = {n["@type"] for n in ld["@graph"]}
        assert {"Organization", "WebSite", "WebPage", "FAQPage", "JobPosting"} <= types
        assert "fonts.googleapis.com" not in str(soup)
        hero = soup.find("img")
        assert hero is not None and hero["src"].endswith(".webp") and hero["fetchpriority"] == "high"
        assert hero["width"] and hero["height"] and "srcset" in hero.attrs
    sitemap = (public / "sitemap.xml").read_text(encoding="utf-8")
    assert sitemap.count("<url>") == 2 and 'hreflang="x-default"' in sitemap
    assert "Sitemap: https://bloommodels.hu/sitemap.xml" in (public / "robots.txt").read_text(encoding="utf-8")
    assert "## Frequently asked questions" in (public / "llms.txt").read_text(encoding="utf-8")


def test_site_build_drops_expired_job_posting(full_repo: Path) -> None:
    result = build_site(full_repo, today=date(2027, 1, 5))
    assert any("expired" in w for w in result.warnings)
    page = (full_repo / "site" / "public" / "index.html").read_text(encoding="utf-8")
    assert '"JobPosting"' not in page


def test_site_build_refuses_missing_translation(full_repo: Path) -> None:
    path = full_repo / "site" / "src" / "i18n" / "en.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    del data["faq.q3"]
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(SiteBuildError, match=r"faq\.q3"):
        build_site(full_repo, today=TODAY)


def test_site_check_mode_reports_stale_output(full_repo: Path) -> None:
    path = full_repo / "site" / "src" / "i18n" / "hu.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["faq.h2"] = "Új címsor az online modell munkáról"
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    result = build_site(full_repo, today=TODAY, check=True)
    assert "index.html" in result.changed
    assert "Új címsor" not in (full_repo / "site" / "public" / "index.html").read_text(encoding="utf-8")


# ---- on-page --------------------------------------------------------------------------------------------------------


def _page(title: str = "Online modell munka Budapesten | Bloom Models", extra_head: str = "", body: str = "") -> str:
    words = " ".join(["online modell munka budapest rugalmas"] * 80)
    return f"""<html lang="hu"><head><title>{title}</title>
<meta name="description" content="{"Streaming modell munka Budapesten, ingyenes betanítás és rugalmas beosztás. " * 2}">
<link rel="canonical" href="https://bloommodels.hu/">{extra_head}</head>
<body><h1>Fő cím</h1><h2>Online modell munka</h2><p>{words}</p>{body}</body></html>"""


def codes(f: FindingList) -> set[str]:
    return {x.code for x in f.problems()}


def test_onpage_flags_noindex_wrong_canonical_and_long_title() -> None:
    exp = PageExpectations("https://bloommodels.hu/", "hu", ["online modell munka"], today=TODAY)
    f, pf = check_page(_page(extra_head='<meta name="robots" content="noindex">'), exp)
    assert "noindex" in codes(f)
    f, _ = check_page(_page().replace('href="https://bloommodels.hu/"', 'href="https://other.hu/"'), exp)
    assert "canonical-wrong" in codes(f)
    f, _ = check_page(_page(title="Bloom Models " + "x" * 70), exp)
    assert {"title-long", "title-keyword"} <= codes(f)
    assert pf.word_count > 300


def test_onpage_hreflang_anchors_images_and_job_expiry() -> None:
    alternates = {"hu": "https://bloommodels.hu/", "en": "https://bloommodels.hu/en/"}
    exp = PageExpectations("https://bloommodels.hu/", "hu", [], alternates, today=TODAY)
    job = {"@context": "https://schema.org", "@type": "JobPosting", "title": "x", "description": "y",
           "datePosted": "2026-09-01", "hiringOrganization": {"name": "B"}, "jobLocation": {"x": 1},
           "validThrough": "2026-10-20T23:59:59+01:00"}
    body = (f'<a href="#missing">x</a><img src="/a.jpg"><script type="application/ld+json">{json.dumps(job)}</script>'
            '<script type="application/ld+json">{broken</script>')
    f, pf = check_page(_page(body=body), exp)
    c = codes(f)
    assert {"hreflang", "broken-anchor", "img-alt", "job-expiring", "jsonld-invalid"} <= c
    assert pf.job_valid_through.startswith("2026-10-20")


def test_faq_markup_must_match_visible_questions() -> None:
    exp = PageExpectations("https://bloommodels.hu/", "hu", [], today=TODAY)
    faq = {"@context": "https://schema.org", "@type": "FAQPage", "mainEntity": [
        {"@type": "Question", "name": f"q{i}", "acceptedAnswer": {"@type": "Answer", "text": "a"}} for i in range(3)]}
    body = (f'<script type="application/ld+json">{json.dumps(faq)}</script>'
            "<details><summary>q0</summary><p>a</p></details><details><summary>q1</summary><p>a</p></details>")
    f, _ = check_page(_page(body=body), exp)
    assert "faq-mismatch" in codes(f)


def test_site_renders_one_faq_item_per_translation_pair(full_repo: Path) -> None:
    for lang in ("hu", "en"):
        path = full_repo / "site" / "src" / "i18n" / f"{lang}.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        data["faq.q8"], data["faq.a8"] = "Nyolcadik kérdés?", "Nyolcadik válasz."
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    build_site(full_repo, today=TODAY)
    page = (full_repo / "site" / "public" / "index.html").read_text(encoding="utf-8")
    soup = BeautifulSoup(page, "html.parser")
    assert len(soup.select("details.faq-item summary")) == 8 and "Nyolcadik kérdés?" in soup.get_text()
    assert not check_site(full_repo, live=False, today=TODAY).findings.by_severity("critical")


def test_keyword_matching_understands_hungarian_endings() -> None:
    assert _contains_keyword("Kérdések az online modell munkáról", ["online modell munka"])
    assert _contains_keyword("Modellügynökség Budapesten", ["modellügynökség budapest"])
    assert not _contains_keyword("online modern munka", ["online modell munka"])


# ---- whole site -------------------------------------------------------------------------------------------------------


def test_built_site_passes_offline_seo_check(full_repo: Path) -> None:
    build_site(full_repo, today=TODAY)
    report = check_site(full_repo, live=False, today=TODAY)
    assert not report.findings.by_severity("critical"), report.findings.problems()
    assert set(report.pages) == {"hu", "en"}
    assert report.findings.score("Website") >= 90


class FakeFetcher:
    def __init__(self, pages: dict[str, Fetched]) -> None:
        self.pages = pages

    def get(self, url: str) -> Fetched:
        return self.pages.get(url, Fetched(404, "", {}, url))


def test_live_check_detects_paused_site_and_missing_https(full_repo: Path) -> None:
    paused = Fetched(503, "<h1>Site not available</h1>", {}, "https://bloommodels.hu/")
    fetcher = FakeFetcher({
        "https://bloommodels.hu/": paused,
        "https://bloommodels.hu/en/": paused,
        "http://bloommodels.hu/": Fetched(200, "", {}, "http://bloommodels.hu/"),
        "https://www.bloommodels.hu/": Fetched(0, error="ConnectError"),
    })
    report = check_site(full_repo, live=True, fetcher=fetcher, today=TODAY)
    c = codes(report.findings)
    assert {"site-paused", "no-https-redirect", "www-unreachable"} <= c


# ---- PageSpeed --------------------------------------------------------------------------------------------------------


def test_pagespeed_parse_and_findings() -> None:
    data = {
        "lighthouseResult": {
            "categories": {"performance": {"score": 0.42}},
            "audits": {
                "largest-contentful-paint": {"numericValue": 5200},
                "cumulative-layout-shift": {"numericValue": 0.02},
                "render-blocking-resources": {"title": "Eliminate render-blocking resources",
                                              "details": {"type": "opportunity", "overallSavingsMs": 900}},
            },
        },
        "loadingExperience": {"metrics": {"INTERACTION_TO_NEXT_PAINT": {"percentile": 650}}},
    }
    r = parse_pagespeed("https://bloommodels.hu/", "mobile", data)
    assert r.score == 42 and r.lab["lcp_ms"] == 5200 and r.field_data["inp_ms"] == 650
    assert r.opportunities and "render-blocking" in r.opportunities[0]
    sev = {f.code: f.severity for f in speed_findings([r]).problems()}
    assert sev["perf-score"] == "critical" and sev["lcp"] == "critical" and sev["inp"] == "critical"


# ---- Search Console ---------------------------------------------------------------------------------------------------


@pytest.fixture
def service_account() -> dict[str, str]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
    return {"client_email": "bloom@proj.iam.gserviceaccount.com", "private_key": pem.decode(), "private_key_id": "k1",
            "token_uri": TOKEN_URL}


def test_jwt_is_signed_correctly(service_account: dict[str, str]) -> None:
    token = make_jwt(service_account, now=1_700_000_000)
    head, claims, sig = token.split(".")
    pad = lambda s: s + "=" * (-len(s) % 4)  # noqa: E731
    body = json.loads(base64.urlsafe_b64decode(pad(claims)))
    assert body["iss"] == service_account["client_email"] and body["exp"] - body["iat"] == 3600
    key = serialization.load_pem_private_key(service_account["private_key"].encode(), password=None)
    key.public_key().verify(  # type: ignore[union-attr, call-arg]
        base64.urlsafe_b64decode(pad(sig)), f"{head}.{claims}".encode(), padding.PKCS1v15(), hashes.SHA256()  # type: ignore[arg-type]
    )


@respx.mock
def test_search_console_summary_and_inspection(service_account: dict[str, str]) -> None:
    respx.post(TOKEN_URL).mock(return_value=httpx.Response(200, json={"access_token": "ya29", "expires_in": 3600}))

    def analytics(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == "Bearer ya29"
        body = json.loads(request.content)
        dims = body["dimensions"]
        if not dims:
            return httpx.Response(200, json={"rows": [{"keys": [], "clicks": 12, "impressions": 400, "ctr": 0.03,
                                                       "position": 9.5}]})
        if dims == ["query"]:
            return httpx.Response(200, json={"rows": [
                {"keys": ["online modell munka"], "clicks": 1, "impressions": 120, "ctr": 0.008, "position": 7.2},
                {"keys": ["bloom models"], "clicks": 10, "impressions": 30, "ctr": 0.33, "position": 1.1},
            ]})
        return httpx.Response(200, json={"rows": []})

    respx.post(url__regex=r".*/searchAnalytics/query").mock(side_effect=analytics)
    respx.post(INSPECT_URL).mock(return_value=httpx.Response(200, json={"inspectionResult": {"indexStatusResult": {
        "verdict": "NEUTRAL", "coverageState": "Discovered - currently not indexed"}}}))
    gsc = SearchConsole(service_account, "sc-domain:bloommodels.hu")
    s = gsc.summary(TODAY)
    assert s.impressions == 400 and s.end == date(2026, 10, 8)
    assert [r.keys[0] for r in s.striking_distance] == ["online modell munka"]
    assert [r.keys[0] for r in s.low_ctr] == ["online modell munka"]
    f = search_findings(s, {"https://bloommodels.hu/": gsc.inspect("https://bloommodels.hu/")})
    assert {"not-indexed", "gsc-low-ctr"} <= codes(f)


@respx.mock
def test_search_console_permission_error_explains_fix(service_account: dict[str, str]) -> None:
    respx.post(TOKEN_URL).mock(return_value=httpx.Response(200, json={"access_token": "t", "expires_in": 3600}))
    respx.post(url__regex=r".*/searchAnalytics/query").mock(return_value=httpx.Response(403, json={}))
    with pytest.raises(SearchConsoleError, match="Users and permissions"):
        SearchConsole(service_account, "sc-domain:bloommodels.hu").query(TODAY, TODAY, ["query"])


def test_analyse_marks_new_queries() -> None:
    s = SearchSummary(TODAY, TODAY, queries=[Row(["új keresés"], 0, 15, 0, 12.0), Row(["régi"], 0, 5, 0, 3.0)])
    analyse(s, {"régi"})
    assert s.new_queries == ["új keresés"]


# ---- Instagram SEO ----------------------------------------------------------------------------------------------------


def test_instagram_profile_findings(full_repo: Path) -> None:
    cfg = InstagramSeoConfig(name_field="Bloom Models | Online Modeling", keywords=["online modell", "online modeling"])
    f = profile_findings({"name": "Bloom", "biography": "Szia!", "website": "", "account_type": "PERSONAL"}, cfg,
                         load_facts(full_repo))
    assert {"ig-not-pro", "ig-name-keyword", "ig-bio-18", "ig-website"} <= codes(f)
    good = profile_findings({"name": "Bloom Models | Online Modeling", "biography": "Online modell · 18+",
                             "website": "https://bloommodels.hu/", "account_type": "BUSINESS"}, cfg)
    assert not good.problems()


def test_instagram_queue_findings(full_repo: Path, make_post) -> None:  # type: ignore[no-untyped-def]
    settings = load_settings(full_repo)
    make_post("2026-10-12-a", status="draft", scheduled_at="2026-10-12 18:30")
    make_post("2026-10-20-b", status="draft", scheduled_at="2026-10-20 18:30")
    from bloom_agent.queue import load_queue

    posts, _ = load_queue(settings.posts_dir)
    now = datetime(2026, 10, 11, 9, 0, tzinfo=settings.timezone)
    f = queue_findings(posts, {p.id: p.caption() for p in posts}, InstagramSeoConfig(keywords=["online modell"]), now,
                       settings.timezone)
    assert {"ig-nothing-approved", "ig-gap", "ig-no-reels", "ig-post-seo"} <= codes(f)


def test_post_seo_issues_for_good_post(full_repo: Path) -> None:
    post = Post(id="x", status=Status.DRAFT, scheduled_at="2026-10-12 18:30", media=["image.jpg"],
                alt_text="Bloom Models grafika: online modell munka Budapesten, rugalmas beosztással.")
    cap = "Online modell munka Budapesten — így indul. #bloommodels #onlinemodell #budapest"
    assert post_seo_issues(post, cap, InstagramSeoConfig(keywords=["online modell"], hashtag_pool=["#bloommodels"])) == []


def test_site_config_loads(full_repo: Path) -> None:
    cfg = load_site_config(full_repo)
    assert cfg.url("en") == "https://bloommodels.hu/en/"
    assert all(len(p.title) <= 60 and 110 <= len(p.description) <= 160 for p in cfg.pages.values())
    assert re.match(r"^\d{4}-\d{2}-\d{2}$", cfg.job_posting.date_posted.isoformat())  # type: ignore[union-attr]
