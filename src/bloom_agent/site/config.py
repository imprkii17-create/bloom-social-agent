"""site/src/site.yaml and the two language dictionaries."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field, field_validator

LANGS: tuple[str, ...] = ("hu", "en")
Lang = Literal["hu", "en"]

# SEO limits (characters). Google truncates by pixel width; these are the usual safe lengths.
TITLE_MAX = 60
TITLE_MIN = 25
DESCRIPTION_MAX = 160
DESCRIPTION_MIN = 110


class PageMeta(BaseModel):
    path: str
    locale: str
    title: str
    description: str
    og_title: str
    og_image_alt: str

    @field_validator("path")
    @classmethod
    def _path(cls, v: str) -> str:
        if not v.startswith("/") or not v.endswith("/"):
            raise ValueError("path must start and end with '/' (e.g. / or /en/)")
        return v

    @field_validator("title", "description", "og_title", "og_image_alt")
    @classmethod
    def _one_line(cls, v: str) -> str:
        return " ".join(v.split())


class JobPostingConfig(BaseModel):
    enabled: bool = True
    date_posted: date
    valid_through: date | None = None
    employment_type: list[str] = Field(default_factory=lambda: ["CONTRACTOR", "PART_TIME"])
    title: dict[str, str]


class SiteConfig(BaseModel):
    base_url: str
    default_lang: Lang = "hu"
    x_default: Lang = "en"
    instagram_url: str = ""
    contact_email: str = ""
    pages: dict[str, PageMeta]
    job_posting: JobPostingConfig | None = None
    og_image_source: str = "images/hero-2.jpg"
    image_widths: list[int] = Field(default_factory=lambda: [480, 960, 1440])

    @field_validator("base_url")
    @classmethod
    def _base(cls, v: str) -> str:
        v = v.rstrip("/")
        if not v.startswith("https://"):
            raise ValueError("base_url must start with https://")
        return v

    @field_validator("pages")
    @classmethod
    def _langs(cls, v: dict[str, PageMeta]) -> dict[str, PageMeta]:
        missing = [lang for lang in LANGS if lang not in v]
        if missing:
            raise ValueError(f"pages is missing: {', '.join(missing)}")
        return v

    def url(self, lang: str) -> str:
        return self.base_url + self.pages[lang].path


def site_dir(repo_root: Path) -> Path:
    return repo_root / "site"


def load_site_config(repo_root: Path) -> SiteConfig:
    path = site_dir(repo_root) / "src" / "site.yaml"
    return SiteConfig.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))


def load_strings(repo_root: Path) -> dict[str, dict[str, str]]:
    out: dict[str, dict[str, str]] = {}
    for lang in LANGS:
        path = site_dir(repo_root) / "src" / "i18n" / f"{lang}.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in data.items()):
            raise ValueError(f"{path.name} must be a flat JSON object of text strings")
        out[lang] = data
    return out


def faq_pairs(strings: dict[str, str]) -> list[tuple[str, str]]:
    """FAQ items are faq.q1/faq.a1, faq.q2/faq.a2, … in order, stopping at the first gap."""
    pairs = []
    n = 1
    while f"faq.q{n}" in strings and f"faq.a{n}" in strings:
        pairs.append((strings[f"faq.q{n}"], strings[f"faq.a{n}"]))
        n += 1
    return pairs
