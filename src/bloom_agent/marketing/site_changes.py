"""Website changes proposed by the marketing agent. Only text the agent is allowed to touch: page titles and
descriptions (site/src/site.yaml) and the FAQ (site/src/i18n/*.json). Every change is checked, the site is rebuilt and
re-audited, and anything that makes the SEO check worse is thrown away. The result goes out as a pull request."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

import yaml

from ..config import DEFAULT_BANNED_WORDS
from ..facts import Facts, check_text
from ..seo.sitecheck import check_site
from ..site.build import build_site
from ..site.config import DESCRIPTION_MAX, DESCRIPTION_MIN, LANGS, TITLE_MAX, load_site_config, load_strings, site_dir
from ..validate import find_banned

META_FIELDS = ("title", "description", "og_title")
FAQ_RE = re.compile(r"^faq\.(q|a)(\d{1,2})$")
MAX_CHANGES = 6
MAX_FAQ = 12


@dataclass
class SiteChange:
    field: str
    lang: str
    new_value: str
    reason: str = ""
    old_value: str = ""

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> SiteChange:
        return cls(
            str(d.get("field", "")),
            str(d.get("lang", "")),
            " ".join(str(d.get("new_value", "")).split()),
            str(d.get("reason", "")),
        )


@dataclass
class SiteChangeResult:
    applied: list[SiteChange]
    rejected: list[str]
    score_before: int
    score_after: int


def _check_one(c: SiteChange, facts: Facts, banned: tuple[str, ...]) -> str | None:
    if c.lang not in LANGS:
        return f"unknown language {c.lang}"
    if not c.new_value:
        return "empty text"
    if c.field in META_FIELDS:
        n = len(c.new_value)
        if c.field in ("title", "og_title") and n > TITLE_MAX + (20 if c.field == "og_title" else 0):
            return f"{c.field} is {n} characters"
        if c.field == "description" and not DESCRIPTION_MIN <= n <= DESCRIPTION_MAX:
            return f"description is {n} characters (needs {DESCRIPTION_MIN}–{DESCRIPTION_MAX})"
        if c.field == "title" and "bloom" not in c.new_value.lower():
            return "title must name Bloom Models"
    elif c.field in ("faq.h2", "faq.p") or FAQ_RE.match(c.field):
        m = FAQ_RE.match(c.field)
        if m and int(m.group(2)) > MAX_FAQ:
            return f"at most {MAX_FAQ} FAQ entries"
        if len(c.new_value) > 600:
            return "FAQ text over 600 characters"
    else:
        return f"the agent may not change '{c.field}'"
    if "<" in c.new_value or ">" in c.new_value:
        return "no HTML allowed"
    hits = find_banned(c.new_value, banned)
    if hits:
        return "words the agent may not add on its own (your decision, see brand/seo.yaml): " + ", ".join(hits)
    # Short meta texts can't carry the disclaimer (the page itself does); everything else is checked fully.
    issues = [i for i in check_text(c.new_value, facts) if not (c.field in META_FIELDS and i.kind == "missing-disclaimer")]
    if issues:
        return issues[0].message
    return None


def apply_site_changes(
    repo_root: Path,
    proposals: list[dict[str, Any]],
    facts: Facts,
    today: date,
    banned: tuple[str, ...] = DEFAULT_BANNED_WORDS,
) -> SiteChangeResult:
    before = check_site(repo_root, live=False, today=today, facts=facts).findings
    snapshot = _snapshot(site_dir(repo_root))
    score_before = before.score()
    crit_before = {f.code for f in before.by_severity("critical")}
    src = site_dir(repo_root) / "src"
    cfg_path = src / "site.yaml"
    cfg_text = cfg_path.read_text(encoding="utf-8")
    cfg_data = yaml.safe_load(cfg_text)
    strings = load_strings(repo_root)
    originals = {lang: dict(strings[lang]) for lang in LANGS}

    applied: list[SiteChange] = []
    rejected: list[str] = []
    for d in proposals[:MAX_CHANGES]:
        c = SiteChange.from_dict(d)
        problem = _check_one(c, facts, banned)
        if problem:
            rejected.append(f"{c.field} ({c.lang}): {problem}")
            continue
        if c.field in META_FIELDS:
            c.old_value = str(cfg_data["pages"][c.lang].get(c.field, ""))
            cfg_data["pages"][c.lang][c.field] = c.new_value
        else:
            c.old_value = strings[c.lang].get(c.field, "")
            strings[c.lang][c.field] = c.new_value
        if c.old_value == c.new_value:
            continue
        applied.append(c)

    # FAQ entries must exist in both languages, as complete question/answer pairs, with no gaps
    for lang in LANGS:
        nums = sorted({int(m.group(2)) for k in strings[lang] if (m := FAQ_RE.match(k))})
        if nums and nums != list(range(1, nums[-1] + 1)):
            rejected.append(f"FAQ numbering has gaps in {lang}; FAQ changes dropped")
            strings = {lg: dict(originals[lg]) for lg in LANGS}
            applied = [c for c in applied if c.field in META_FIELDS]
            break
    if set(strings["hu"]) != set(strings["en"]):
        rejected.append("a FAQ entry was added in only one language; FAQ changes dropped")
        strings = {lg: dict(originals[lg]) for lg in LANGS}
        applied = [c for c in applied if c.field in META_FIELDS]

    if not applied:
        return SiteChangeResult([], rejected, score_before, score_before)

    if any(c.field in META_FIELDS for c in applied):
        cfg_path.write_text(_dump_site_yaml(cfg_text, cfg_data), encoding="utf-8")
    for lang in LANGS:
        if strings[lang] != originals[lang]:
            (src / "i18n" / f"{lang}.json").write_text(
                json.dumps(dict(sorted(strings[lang].items())), ensure_ascii=False, indent=1) + "\n", encoding="utf-8"
            )
    load_site_config(repo_root)  # raises if the YAML became invalid
    build_site(repo_root, today=today)
    after = check_site(repo_root, live=False, today=today, facts=facts).findings
    score_after = after.score()
    new_crit = {f.code for f in after.by_severity("critical")} - crit_before
    if score_after < score_before or new_crit:
        _restore(site_dir(repo_root), snapshot)
        rejected.append(f"changes made the SEO check worse ({score_before} → {score_after}); all reverted")
        return SiteChangeResult([], rejected, score_before, score_before)
    return SiteChangeResult(applied, rejected, score_before, score_after)


def _snapshot(root: Path) -> dict[str, bytes]:
    """Every file under site/ (src and the built public/), so a rejected change is undone byte for byte."""
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob("*") if p.is_file()}


def _restore(root: Path, snapshot: dict[str, bytes]) -> None:
    for p in [p for p in root.rglob("*") if p.is_file()]:
        if p.relative_to(root).as_posix() not in snapshot:
            p.unlink()
    for rel, data in snapshot.items():
        target = root / rel
        if not target.is_file() or target.read_bytes() != data:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)


def _dump_site_yaml(original: str, data: dict[str, Any]) -> str:
    """Rewrite only the `pages:` values in place so the comments in site.yaml survive."""
    out = original
    for lang, page in data["pages"].items():
        for key in META_FIELDS:
            value = str(page[key])
            pattern = re.compile(
                rf"(^  {re.escape(lang)}:\n(?:    .*\n|      .*\n)*?)(    {key}: (?:>-\n(?:      .*\n)+|.*\n))", re.M
            )
            m = pattern.search(out)
            if not m:
                return yaml.safe_dump(data, allow_unicode=True, sort_keys=False, width=120)
            replacement = f"    {key}: {json.dumps(value, ensure_ascii=False)}\n"
            out = out[: m.start(2)] + replacement + out[m.end(2) :]
    return out
