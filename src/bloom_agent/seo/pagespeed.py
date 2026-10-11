"""Speed and Core Web Vitals from Google PageSpeed Insights (free; an API key only raises the rate limit)."""

from __future__ import annotations

import contextlib
from dataclasses import dataclass, field
from typing import Any

import httpx

from .findings import FindingList, Severity

AREA = "Speed"
PSI_URL = "https://www.googleapis.com/pagespeedonline/v5/runPagespeed"

# Google's "good" thresholds (75th percentile of real visits).
LCP_GOOD_MS = 2500
LCP_POOR_MS = 4000
INP_GOOD_MS = 200
INP_POOR_MS = 500
CLS_GOOD = 0.1
CLS_POOR = 0.25


@dataclass
class SpeedResult:
    url: str
    strategy: str
    score: int | None = None  # Lighthouse performance 0–100
    lab: dict[str, float] = field(default_factory=dict)  # lcp_ms, cls, tbt_ms, fcp_ms
    field_data: dict[str, float] = field(default_factory=dict)  # real-user p75: lcp_ms, inp_ms, cls
    opportunities: list[str] = field(default_factory=list)
    error: str = ""


def run_pagespeed(
    url: str, strategy: str = "mobile", *, api_key: str | None = None, client: httpx.Client | None = None
) -> SpeedResult:
    params: dict[str, Any] = {"url": url, "strategy": strategy, "category": "performance"}
    if api_key:
        params["key"] = api_key
    own = client is None
    client = client or httpx.Client(timeout=90.0)
    try:
        r = client.get(PSI_URL, params=params)
        if r.status_code != 200:
            msg = ""
            with contextlib.suppress(ValueError):
                msg = str(r.json().get("error", {}).get("message", ""))
            return SpeedResult(url, strategy, error=f"PageSpeed answered {r.status_code} {msg}".strip())
        return parse_pagespeed(url, strategy, r.json())
    except httpx.HTTPError as exc:
        return SpeedResult(url, strategy, error=f"PageSpeed request failed: {type(exc).__name__}")
    finally:
        if own:
            client.close()


def parse_pagespeed(url: str, strategy: str, data: dict[str, Any]) -> SpeedResult:
    res = SpeedResult(url, strategy)
    lh = data.get("lighthouseResult") or {}
    perf = ((lh.get("categories") or {}).get("performance") or {}).get("score")
    if isinstance(perf, int | float):
        res.score = round(perf * 100)
    audits = lh.get("audits") or {}
    for key, name in (
        ("largest-contentful-paint", "lcp_ms"),
        ("cumulative-layout-shift", "cls"),
        ("total-blocking-time", "tbt_ms"),
        ("first-contentful-paint", "fcp_ms"),
    ):
        v = (audits.get(key) or {}).get("numericValue")
        if isinstance(v, int | float):
            res.lab[name] = float(v)
    for key, audit in audits.items():
        details = audit.get("details") or {}
        saving = details.get("overallSavingsMs")
        if details.get("type") == "opportunity" and isinstance(saving, int | float) and saving >= 300:
            res.opportunities.append(f"{audit.get('title', key)} (saves ~{saving / 1000:.1f}s)")
    metrics = (data.get("loadingExperience") or {}).get("metrics") or {}
    for key, name in (
        ("LARGEST_CONTENTFUL_PAINT_MS", "lcp_ms"),
        ("INTERACTION_TO_NEXT_PAINT", "inp_ms"),
        ("CUMULATIVE_LAYOUT_SHIFT_SCORE", "cls"),
    ):
        p = (metrics.get(key) or {}).get("percentile")
        if isinstance(p, int | float):
            res.field_data[name] = float(p) / 100 if name == "cls" else float(p)
    return res


def speed_findings(results: list[SpeedResult]) -> FindingList:
    f = FindingList()
    for r in results:
        label = f"{r.strategy} ({r.url})"
        if r.error:
            f.add(AREA, "info", "psi-error", f"Couldn't measure speed for {label}", r.error, page=r.url)
            continue
        if r.score is not None:
            if r.score < 50:
                f.add(
                    AREA,
                    "critical",
                    "perf-score",
                    f"Speed score {r.score}/100 on {r.strategy}",
                    "; ".join(r.opportunities[:3]),
                    "Fix the biggest opportunities listed.",
                    r.url,
                )
            elif r.score < 90:
                f.add(
                    AREA,
                    "warning",
                    "perf-score",
                    f"Speed score {r.score}/100 on {r.strategy}",
                    "; ".join(r.opportunities[:3]),
                    page=r.url,
                )
            else:
                f.ok(AREA, "perf-score", f"Speed score {r.score}/100 on {r.strategy}", r.url)
        src = r.field_data or {}
        lcp = src.get("lcp_ms", r.lab.get("lcp_ms"))
        cls = src.get("cls", r.lab.get("cls"))
        where = "real visitors" if r.field_data else "test run"
        if lcp is not None and lcp > LCP_GOOD_MS:
            sev: Severity = "critical" if lcp > LCP_POOR_MS else "warning"
            f.add(
                AREA,
                sev,
                "lcp",
                f"Main content appears after {lcp / 1000:.1f}s on {r.strategy} ({where})",
                fix="Under 2.5s is 'good'. Usually: smaller hero image, fewer render-blocking files.",
                page=r.url,
            )
        if cls is not None and cls > CLS_GOOD:
            sev = "critical" if cls > CLS_POOR else "warning"
            f.add(
                AREA,
                sev,
                "cls",
                f"Layout shifts while loading (CLS {cls:.2f}) on {r.strategy}",
                fix="Give images width/height; avoid inserting content above what's visible.",
                page=r.url,
            )
        inp = src.get("inp_ms")
        if inp is not None and inp > INP_GOOD_MS:
            sev = "critical" if inp > INP_POOR_MS else "warning"
            f.add(AREA, sev, "inp", f"Taps respond slowly ({inp:.0f} ms) for real visitors on {r.strategy}", page=r.url)
    return f
