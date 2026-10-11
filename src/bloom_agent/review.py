"""Optional Claude features (only used when ANTHROPIC_API_KEY is set).

- review_post: Claude looks at the image + caption and flags problems before you approve a post.
- summarise_week: turns the weekly numbers into 3–5 concrete suggestions.
Costs are tiny: a review is roughly 2–3k input tokens and a few hundred output tokens.
"""

from __future__ import annotations

import base64
import io
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from PIL import Image

from .config import Settings
from .models import Post
from .validate import count_hashtags

RULES_FILE = "brand/rules.md"


@dataclass
class Review:
    verdict: str  # "ok" | "fix"
    issues: list[str]
    suggestions: list[str]

    def to_markdown(self, post_id: str, model: str) -> str:
        head = "✅ Looks good" if self.verdict == "ok" else "✏️ Needs changes"
        lines = [f"# Review: {post_id}", "", f"**{head}** — reviewed by `{model}`", ""]
        if self.issues:
            lines += ["## Issues", *[f"- {i}" for i in self.issues], ""]
        if self.suggestions:
            lines += ["## Suggestions", *[f"- {s}" for s in self.suggestions], ""]
        return "\n".join(lines)


def _client(settings: Settings) -> Any:
    try:
        import anthropic
    except ImportError as exc:  # pragma: no cover - depends on install
        raise RuntimeError("Install the AI extra: pip install '.[ai]'") from exc
    if not settings.anthropic_api_key:
        raise RuntimeError("ANTHROPIC_API_KEY is not set")
    return anthropic.Anthropic(api_key=settings.anthropic_api_key)


def _rules(settings: Settings) -> str:
    path = settings.repo_root / RULES_FILE
    return path.read_text(encoding="utf-8") if path.is_file() else ""


def _image_block(path: Path) -> dict[str, Any] | None:
    if path.suffix.lower() not in {".jpg", ".jpeg", ".png", ".webp"}:
        return None
    with Image.open(path) as src:
        rgb = src.convert("RGB")
    rgb.thumbnail((1024, 1024))  # plenty for review, keeps cost down
    buf = io.BytesIO()
    rgb.save(buf, "JPEG", quality=85)
    return {
        "type": "image",
        "source": {"type": "base64", "media_type": "image/jpeg", "data": base64.b64encode(buf.getvalue()).decode()},
    }


SYSTEM = (
    "You review Instagram posts for Bloom Models, a Budapest online modeling agency recruiting streaming models "
    "(18+). Check the image and caption against the brand rules. Be strict about: anything sexually explicit or "
    "suggestive, words that could get the account restricted, misleading or guaranteed earnings claims, missing "
    "18+ note, more than 5 hashtags, engagement bait ('tag a friend', 'comment YES'), spelling/grammar in "
    "Hungarian and English, and text in the image that is cut off or unreadable. "
    'Reply with JSON only: {"verdict": "ok" | "fix", "issues": [..], "suggestions": [..]}. Keep each item short.'
)


def review_post(settings: Settings, post: Post) -> Review:
    client = _client(settings)
    caption = post.caption()
    content: list[dict[str, Any]] = []
    for path in post.media_paths()[:3]:
        block = _image_block(path)
        if block:
            content.append(block)
    content.append({
        "type": "text",
        "text": (
            f"Brand rules:\n{_rules(settings)}\n\nPost type: {post.type.value}\nScheduled: {post.scheduled_at}\n"
            f"Hashtags counted: {count_hashtags(caption)}\nAlt text: {post.alt_text or '(none)'}\n\n"
            f"Caption:\n{caption}"
        ),
    })
    msg = client.messages.create(
        model=settings.claude_model, max_tokens=700, system=SYSTEM, messages=[{"role": "user", "content": content}]
    )
    text = "".join(getattr(b, "text", "") for b in msg.content)
    return parse_review(text)


def parse_review(text: str) -> Review:
    match = re.search(r"\{.*\}", text, re.S)
    if not match:
        return Review("fix", ["Claude's answer could not be read; review manually."], [])
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError:
        return Review("fix", ["Claude's answer could not be read; review manually."], [])
    verdict = "ok" if str(data.get("verdict", "")).lower() == "ok" else "fix"
    return Review(
        verdict,
        [str(x) for x in data.get("issues", []) if x],
        [str(x) for x in data.get("suggestions", []) if x],
    )


def summarise_week(settings: Settings, report_md: str) -> str:
    client = _client(settings)
    msg = client.messages.create(
        model=settings.claude_model,
        max_tokens=600,
        system=(
            "You are the social media lead for Bloom Models (Budapest online modeling agency, 18+ streaming models). "
            "From this weekly Instagram report, give 3–5 short, concrete suggestions for next week's posts and Reels. "
            "Focus on what drives profile visits, link clicks and applications. Plain markdown bullets, no preamble."
        ),
        messages=[{"role": "user", "content": report_md}],
    )
    return "".join(getattr(b, "text", "") for b in msg.content).strip()
