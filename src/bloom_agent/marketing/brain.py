"""The Claude side of the marketing agent: research (with web search), the weekly plan (structured output), and one
repair round for drafts that failed the checks. Optional: everything works without an API key (see fallback.py).

Costs are tracked from the API's own token counts and capped per run (MARKETING_MAX_COST_USD, default $2).
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any

from ..config import Settings

log = logging.getLogger(__name__)

# USD per million tokens (input, output). Cache reads cost 10% of input, cache writes 125%.
PRICES: dict[str, tuple[float, float]] = {
    "claude-opus-5-5": (4.0, 20.0),
    "claude-sonnet-5-5": (2.0, 10.0),
    "claude-haiku-5-5": (0.10, 0.50),
    "claude-fable-5-1": (10.0, 50.0),
}
WEB_SEARCH_USD = 0.01
MAX_TOKENS_PLAN = 32000  # room for 21 bilingual posts a week
MAX_TOKENS_RESEARCH = 4000

SITE_FIELDS = ["title", "description", "og_title", "faq.h2", "faq.p"] + [
    f"faq.{k}{n}" for n in range(1, 13) for k in ("q", "a")
]
TEMPLATES = ["statement", "list", "photo", "light"]
BOTTLENECKS = ["reach", "interest", "intent", "application", "unknown"]


def price_for(model: str) -> tuple[float, float]:
    """Prices by model family (also for dated ids like claude-opus-5-5-20260101); unknown → most expensive."""
    for name, price in sorted(PRICES.items(), key=lambda kv: -len(kv[0])):
        if model.startswith(name):
            return price
    return max(PRICES.values())


class BudgetExceeded(RuntimeError):
    pass


@dataclass
class Spend:
    limit: float
    usd: float = 0.0
    calls: list[str] = field(default_factory=list)

    def add(self, model: str, usage: Any, label: str) -> None:
        price_in, price_out = price_for(model)
        inp = float(getattr(usage, "input_tokens", 0) or 0)
        out = float(getattr(usage, "output_tokens", 0) or 0)
        cache_w = float(getattr(usage, "cache_creation_input_tokens", 0) or 0)
        cache_r = float(getattr(usage, "cache_read_input_tokens", 0) or 0)
        server = getattr(usage, "server_tool_use", None)
        searches = float(getattr(server, "web_search_requests", 0) or 0) if server else 0.0
        cost = (inp * price_in + cache_w * price_in * 1.25 + cache_r * price_in * 0.1 + out * price_out) / 1e6
        cost += searches * WEB_SEARCH_USD
        self.usd += cost
        self.calls.append(
            f"{label}: ${cost:.3f} ({int(inp + cache_w + cache_r)} in / {int(out)} out"
            + (f", {int(searches)} searches" if searches else "")
            + ")"
        )

    def check(self, upcoming_estimate: float = 0.0) -> None:
        if self.usd + upcoming_estimate > self.limit:
            raise BudgetExceeded(f"Stopped at ${self.usd:.2f}: the next step would pass the ${self.limit:.2f} limit.")


def _strict(obj: dict[str, Any]) -> dict[str, Any]:
    """Structured outputs need additionalProperties: false and every property required."""
    obj["additionalProperties"] = False
    obj["required"] = list(obj["properties"].keys())
    return obj


def _texts() -> dict[str, Any]:
    s = {"type": "string"}
    return _strict(
        {
            "type": "object",
            "properties": {
                "eyebrow": s,
                "headline": s,
                "accent": s,
                "body": s,
                "items": {"type": "array", "items": s},
                "footnote": s,
            },
        }
    )


def post_schema() -> dict[str, Any]:
    s = {"type": "string"}
    return _strict(
        {
            "type": "object",
            "properties": {
                "slug": {"type": "string", "description": "short english kebab-case id, e.g. first-week"},
                "pillar": {"type": "string", "description": "message pillar from the playbook"},
                "template": {"type": "string", "enum": TEMPLATES},
                "photo": {"type": "string", "description": "library photo file name for the photo template, else empty"},
                "hu": _texts(),
                "en": _texts(),
                "caption_hu": s,
                "caption_en": s,
                "hashtags": {"type": "array", "items": s},
                "alt_text": {"type": "string", "description": "Hungarian alt text describing the Hungarian image"},
                "why": {"type": "string", "description": "one sentence: which data or playbook rule this post acts on"},
            },
        }
    )


def plan_schema() -> dict[str, Any]:
    s = {"type": "string"}
    arr = {"type": "array", "items": s}
    return _strict(
        {
            "type": "object",
            "properties": {
                "summary": {"type": "string", "description": "3–5 sentences for the owner: what happened and what matters"},
                "bottleneck": {"type": "string", "enum": BOTTLENECKS},
                "bottleneck_reason": s,
                "wins": arr,
                "problems": arr,
                "decisions_for_you": {"type": "array", "items": s, "description": "things only the owner can decide or do"},
                "posts": {"type": "array", "items": post_schema()},
                "reel_ideas": {
                    "type": "array",
                    "items": _strict(
                        {"type": "object", "properties": {"title": s, "hook": s, "shots": arr, "caption_idea": s}}
                    ),
                },
                "story_ideas": arr,
                "experiments": {
                    "type": "array",
                    "items": _strict(
                        {
                            "type": "object",
                            "properties": {"title": s, "hypothesis": s, "metric": s, "duration_weeks": {"type": "integer"}},
                        }
                    ),
                },
                "experiment_updates": {
                    "type": "array",
                    "items": _strict(
                        {
                            "type": "object",
                            "properties": {
                                "id": s,
                                "status": {"type": "string", "enum": ["running", "won", "lost", "inconclusive"]},
                                "result": s,
                            },
                        }
                    ),
                },
                "site_changes": {
                    "type": "array",
                    "items": _strict(
                        {
                            "type": "object",
                            "properties": {
                                "field": {"type": "string", "enum": SITE_FIELDS},
                                "lang": {"type": "string", "enum": ["hu", "en"]},
                                "new_value": s,
                                "reason": s,
                            },
                        }
                    ),
                },
                "seo_actions": arr,
            },
        }
    )


def repair_schema() -> dict[str, Any]:
    return _strict({"type": "object", "properties": {"posts": {"type": "array", "items": post_schema()}}})


class Brain:
    def __init__(self, settings: Settings, *, max_usd: float, client: Any = None) -> None:
        self.settings = settings
        self.model = settings.claude_model
        self.spend = Spend(max_usd)
        if client is None:
            import anthropic

            client = anthropic.Anthropic(api_key=settings.anthropic_api_key, max_retries=3, timeout=600)
        self.client = client

    # ---- system prompt (cached: it's the same every week) ----
    def system(self, playbook: str, rules: str, facts: str, seo: str) -> list[dict[str, Any]]:
        text = (
            "You are the head of marketing for Bloom Models, an online streaming-model agency in Budapest that recruits "
            "adults (18+). You are rigorous, data-driven, honest and protective of the brand's Instagram account. "
            "You write natural Hungarian (informal 'te') and concise English.\n\n"
            "# Playbook\n\n"
            + playbook
            + "\n\n# Content rules (enforced in code)\n\n"
            + rules
            + "\n\n# The only facts and numbers you may use (brand/facts.yaml)\n\n"
            + facts
            + "\n\n# SEO targets (brand/seo.yaml)\n\n"
            + seo
            + "\n\n# Output rules\n"
            "- Base every recommendation on the data you are given; say when data is missing instead of guessing.\n"
            "- Never invent numbers, testimonials, reviews, people or results. Never promise income.\n"
            "- Posts: exactly as many as there are free slots. Captions without hashtags (hashtags go in the list, 3–5, "
            "from the pool where possible). Hungarian caption starts with a hook that contains a search keyword.\n"
            "- Graphic text is short: headline ≤ 45 characters, accent ≤ 40, body ≤ 140, list items ≤ 45, 3–4 items. "
            "A graphic that mentions earnings must carry the disclaimer as its footnote.\n"
            "- Use the photo template only with a photo from the provided library list.\n"
            "- Website changes: only when the data supports them (Search Console queries, SEO findings); at most 4 per "
            "week; always change both languages together for FAQ entries; titles ≤ 60 characters, descriptions "
            "110–160 characters.\n"
        )
        return [{"type": "text", "text": text, "cache_control": {"type": "ephemeral"}}]

    def _create(self, label: str, estimate: float, **kwargs: Any) -> Any:
        self.spend.check(estimate)
        resp = self.client.messages.create(model=self.model, **kwargs)
        self.spend.add(self.model, resp.usage, label)
        return resp

    # ---- steps ----
    def research(self, system: list[dict[str, Any]], brief: str) -> str:
        """Free-text research notes with sources (web search can't be combined with structured output)."""
        tools = [
            {
                "type": "web_search_20250305",
                "name": "web_search",
                "max_uses": 5,
                "user_location": {"type": "approximate", "city": "Budapest", "country": "HU", "timezone": "Europe/Budapest"},
            }
        ]
        messages: list[dict[str, Any]] = [{"role": "user", "content": brief}]
        responses = [
            self._create("research", 0.25, max_tokens=MAX_TOKENS_RESEARCH, system=system, tools=tools, messages=messages)
        ]
        # Server tools can pause a long turn; continue it (at most twice) instead of keeping half an answer.
        for _ in range(2):
            last = responses[-1]
            if getattr(last, "stop_reason", "") != "pause_turn":
                break
            messages = [*messages, {"role": "assistant", "content": last.content}]
            responses.append(
                self._create(
                    "research (continued)",
                    0.15,
                    max_tokens=MAX_TOKENS_RESEARCH,
                    system=system,
                    tools=tools,
                    messages=messages,
                )
            )
        parts = []
        for resp in responses:
            for block in resp.content:
                if getattr(block, "type", "") == "text":
                    parts.append(block.text)
                    for c in getattr(block, "citations", None) or []:
                        url = getattr(c, "url", "")
                        if url:
                            parts.append(f" [{url}]")
        return "".join(parts).strip()

    def plan(self, system: list[dict[str, Any]], context: str) -> dict[str, Any]:
        resp = self._create(
            "plan",
            0.6,
            max_tokens=MAX_TOKENS_PLAN,
            system=system,
            output_config={"format": {"type": "json_schema", "schema": plan_schema()}},
            messages=[{"role": "user", "content": context}],
        )
        return _json(resp)

    def repair(self, system: list[dict[str, Any]], failed: list[tuple[dict[str, Any], list[str]]]) -> list[dict[str, Any]]:
        payload = [{"post": p, "problems": probs} for p, probs in failed]
        resp = self._create(
            "repair",
            0.3,
            max_tokens=8000,
            system=system,
            output_config={"format": {"type": "json_schema", "schema": repair_schema()}},
            messages=[
                {
                    "role": "user",
                    "content": (
                        "These planned posts failed the automatic checks. Return corrected versions of the same posts "
                        "(same slugs), fixing every listed problem and changing nothing else that works.\n\n"
                        + json.dumps(payload, ensure_ascii=False, indent=1)
                    ),
                }
            ],
        )
        return list(_json(resp).get("posts") or [])


def _json(resp: Any) -> dict[str, Any]:
    if getattr(resp, "stop_reason", "") in ("max_tokens", "refusal"):
        raise ValueError(f"Claude stopped early ({resp.stop_reason}); no usable plan.")
    text = next((b.text for b in resp.content if getattr(b, "type", "") == "text"), "")
    data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError("Claude returned something other than a JSON object")
    return data
