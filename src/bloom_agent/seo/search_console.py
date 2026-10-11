"""Google Search Console: what people actually search to find bloommodels.hu, and whether Google indexes the pages.

Authentication uses a Google Cloud service account (a robot Google account). You add its email address as a user
of the Search Console property; its JSON key goes in the GSC_SERVICE_ACCOUNT_JSON secret. SETUP.md has the clicks.
"""

from __future__ import annotations

import base64
import json
import time
import urllib.parse
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from .findings import FindingList

AREA = "Google Search"
TOKEN_URL = "https://oauth2.googleapis.com/token"
API = "https://www.googleapis.com/webmasters/v3"
INSPECT_URL = "https://searchconsole.googleapis.com/v1/urlInspection/index:inspect"
SCOPE = "https://www.googleapis.com/auth/webmasters"
DATA_LAG_DAYS = 3  # Search Console data is final after about 2–3 days

# Rough click-through rates by position for a small site; a page far below these needs a better title/description.
EXPECTED_CTR = ((1, 0.25), (2, 0.14), (3, 0.09), (5, 0.05), (10, 0.025), (20, 0.01))


class SearchConsoleError(RuntimeError):
    pass


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def make_jwt(service_account: dict[str, Any], scope: str = SCOPE, now: int | None = None) -> str:
    now = now or int(time.time())
    header = {"alg": "RS256", "typ": "JWT"}
    if service_account.get("private_key_id"):
        header["kid"] = str(service_account["private_key_id"])
    claims = {
        "iss": service_account["client_email"],
        "scope": scope,
        "aud": service_account.get("token_uri", TOKEN_URL),
        "iat": now,
        "exp": now + 3600,
    }
    signing_input = (
        _b64(json.dumps(header, separators=(",", ":")).encode())
        + "."
        + _b64(json.dumps(claims, separators=(",", ":")).encode())
    )
    key = serialization.load_pem_private_key(service_account["private_key"].encode(), password=None)
    if not isinstance(key, rsa.RSAPrivateKey):
        raise SearchConsoleError("The service account key is not an RSA key.")
    signature = key.sign(signing_input.encode(), padding.PKCS1v15(), hashes.SHA256())
    return signing_input + "." + _b64(signature)


def parse_service_account(raw: str) -> dict[str, Any]:
    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise SearchConsoleError("GSC_SERVICE_ACCOUNT_JSON is not valid JSON. Paste the whole key file.") from exc
    for k in ("client_email", "private_key"):
        if not data.get(k):
            raise SearchConsoleError(f"GSC_SERVICE_ACCOUNT_JSON has no '{k}'. Paste the whole key file.")
    return dict(data)


@dataclass
class Row:
    keys: list[str]
    clicks: float
    impressions: float
    ctr: float
    position: float


@dataclass
class SearchSummary:
    start: date
    end: date
    clicks: float = 0
    impressions: float = 0
    ctr: float = 0
    position: float = 0
    prev_clicks: float = 0
    prev_impressions: float = 0
    queries: list[Row] = field(default_factory=list)
    pages: list[Row] = field(default_factory=list)
    countries: list[Row] = field(default_factory=list)
    striking_distance: list[Row] = field(default_factory=list)
    low_ctr: list[Row] = field(default_factory=list)
    new_queries: list[str] = field(default_factory=list)


class SearchConsole:
    def __init__(self, service_account: dict[str, Any], site: str, client: httpx.Client | None = None) -> None:
        self.sa = service_account
        self.site = site
        self.client = client or httpx.Client(timeout=60.0)
        self._token: str | None = None
        self._token_exp = 0.0

    # ---- auth ----
    def token(self) -> str:
        if self._token and time.time() < self._token_exp - 60:
            return self._token
        try:
            r = self.client.post(
                self.sa.get("token_uri", TOKEN_URL),
                data={"grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer", "assertion": make_jwt(self.sa)},
            )
            if r.status_code != 200:
                raise SearchConsoleError(f"Google refused the service account key ({r.status_code}): {r.text[:200]}")
            body = r.json()
            self._token = str(body["access_token"])
            self._token_exp = time.time() + float(body.get("expires_in", 3600))
        except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
            raise SearchConsoleError(f"Couldn't sign in to Google ({type(exc).__name__}: {str(exc)[:120]})") from exc
        return self._token

    def _request(self, method: str, url: str, **kw: Any) -> dict[str, Any]:
        token = self.token()
        try:
            r = self.client.request(method, url, headers={"Authorization": f"Bearer {token}"}, **kw)
        except httpx.HTTPError as exc:
            raise SearchConsoleError(f"Search Console didn't answer ({type(exc).__name__})") from exc
        if r.status_code == 403:
            raise SearchConsoleError(
                f"No access to {self.site}. In Search Console → Settings → Users and permissions, add "
                f"{self.sa.get('client_email')} (Full permission)."
            )
        if r.status_code >= 400:
            raise SearchConsoleError(f"Search Console error {r.status_code}: {r.text[:300]}")
        try:
            return dict(r.json()) if r.content else {}
        except ValueError as exc:
            raise SearchConsoleError("Search Console sent an unreadable answer") from exc

    # ---- API ----
    def query(
        self,
        start: date,
        end: date,
        dimensions: list[str],
        row_limit: int = 250,
        filters: list[dict[str, str]] | None = None,
    ) -> list[Row]:
        body: dict[str, Any] = {
            "startDate": start.isoformat(),
            "endDate": end.isoformat(),
            "dimensions": dimensions,
            "rowLimit": row_limit,
            "type": "web",
        }
        if filters:
            body["dimensionFilterGroups"] = [{"groupType": "and", "filters": filters}]
        site = urllib.parse.quote(self.site, safe="")
        data = self._request("POST", f"{API}/sites/{site}/searchAnalytics/query", json=body)
        return [
            Row(
                [str(k) for k in r.get("keys", [])],
                float(r.get("clicks", 0)),
                float(r.get("impressions", 0)),
                float(r.get("ctr", 0)),
                float(r.get("position", 0)),
            )
            for r in data.get("rows") or []
        ]

    def inspect(self, url: str) -> dict[str, Any]:
        data = self._request("POST", INSPECT_URL, json={"inspectionUrl": url, "siteUrl": self.site, "languageCode": "en-US"})
        return dict((data.get("inspectionResult") or {}).get("indexStatusResult") or {})

    def sitemaps(self) -> list[dict[str, Any]]:
        site = urllib.parse.quote(self.site, safe="")
        return list(self._request("GET", f"{API}/sites/{site}/sitemaps").get("sitemap") or [])

    def submit_sitemap(self, sitemap_url: str) -> None:
        site = urllib.parse.quote(self.site, safe="")
        feed = urllib.parse.quote(sitemap_url, safe="")
        self._request("PUT", f"{API}/sites/{site}/sitemaps/{feed}")

    # ---- analysis ----
    def summary(self, today: date, days: int = 28) -> SearchSummary:
        end = today - timedelta(days=DATA_LAG_DAYS)
        start = end - timedelta(days=days - 1)
        prev_end = start - timedelta(days=1)
        prev_start = prev_end - timedelta(days=days - 1)
        s = SearchSummary(start, end)
        total = self.query(start, end, [], 1)
        if total:
            s.clicks, s.impressions, s.ctr, s.position = (
                total[0].clicks,
                total[0].impressions,
                total[0].ctr,
                total[0].position,
            )
        prev = self.query(prev_start, prev_end, [], 1)
        if prev:
            s.prev_clicks, s.prev_impressions = prev[0].clicks, prev[0].impressions
        s.queries = self.query(start, end, ["query"], 250)
        s.pages = self.query(start, end, ["page"], 50)
        s.countries = self.query(start, end, ["country"], 10)
        prev_queries = {r.keys[0] for r in self.query(prev_start, prev_end, ["query"], 1000)}
        analyse(s, prev_queries)
        return s


def expected_ctr(position: float) -> float:
    for pos, ctr in EXPECTED_CTR:
        if position <= pos:
            return ctr
    return 0.005


def analyse(s: SearchSummary, prev_queries: set[str]) -> None:
    s.queries.sort(key=lambda r: r.impressions, reverse=True)
    # Ranking on page 1–2 but not at the top: a small push (title, a heading, a FAQ answer) can move these.
    s.striking_distance = [r for r in s.queries if 4 <= r.position <= 20 and r.impressions >= 10][:15]
    # Seen but rarely clicked: the title/description isn't convincing for this search.
    s.low_ctr = [r for r in s.queries if r.impressions >= 30 and r.ctr < expected_ctr(r.position) / 2][:10]
    s.new_queries = [r.keys[0] for r in s.queries if r.keys and r.keys[0] not in prev_queries][:20]


def search_findings(s: SearchSummary | None, inspections: dict[str, dict[str, Any]], error: str = "") -> FindingList:
    f = FindingList()
    if error:
        f.add(AREA, "info", "gsc-error", "Couldn't read Search Console", error)
        return f
    if s is not None:
        if s.impressions == 0:
            f.add(
                AREA,
                "info",
                "gsc-no-data",
                "No Google search impressions yet",
                f"{s.start} – {s.end}",
                "Normal for a new site: it takes a few weeks. Keep the sitemap submitted.",
            )
        elif s.prev_impressions and s.impressions < s.prev_impressions * 0.7:
            f.add(
                AREA,
                "warning",
                "gsc-drop",
                "Google impressions dropped by more than 30%",
                f"{s.prev_impressions:.0f} → {s.impressions:.0f}",
                "Check indexing below and the latest site changes.",
            )
        for r in s.low_ctr[:3]:
            f.add(
                AREA,
                "info",
                "gsc-low-ctr",
                f"Seen but rarely clicked: “{r.keys[0]}”",
                f"{r.impressions:.0f} impressions, {r.ctr:.1%} clicks at position {r.position:.1f}",
                "Make the title/description answer this search more directly.",
            )
    for url, res in inspections.items():
        verdict = str(res.get("verdict", ""))
        if not res:
            continue
        if verdict == "PASS":
            f.ok(AREA, "indexed", "Indexed by Google", url)
        else:
            f.add(
                AREA,
                "warning" if verdict in ("NEUTRAL", "") else "critical",
                "not-indexed",
                f"Google hasn't indexed this page: {res.get('coverageState', verdict)}",
                f"last crawl: {res.get('lastCrawlTime', 'never')}",
                "In Search Console use URL inspection → Request indexing. New sites take days to weeks.",
                url,
            )
        g, u = res.get("googleCanonical"), res.get("userCanonical")
        if g and u and g != u:
            f.add(
                AREA,
                "warning",
                "canonical-mismatch",
                "Google chose a different canonical URL",
                f"Google: {g}, page: {u}",
                page=url,
            )
    return f
