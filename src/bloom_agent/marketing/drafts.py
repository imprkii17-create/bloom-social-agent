"""Turns planned posts (from Claude or from marketing/library.yaml) into post folders with graphics, and checks them
harder than hand-made posts: anything the agent wrote must pass every rule, or it is dropped."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import yaml

from ..config import Settings
from ..facts import Facts, check_text
from ..models import Platform, Post, PostType, Status
from ..queue import save_post
from ..validate import HASHTAG_RE, MAX_HASHTAGS, find_banned, validate_post
from .graphics import TEMPLATES, GraphicError, GraphicSpec, missing_glyphs, photo_library, render

SLUG_RE = re.compile(r"[^a-z0-9]+")
POST_TIMES = ("08:30", "13:00", "19:30")  # morning, afternoon, evening (Budapest time)
DEFAULT_TIME = POST_TIMES[-1]  # the evening slot: used alone when the week comes from the library
POST_DAYS = (0, 1, 2, 3, 4, 5, 6)  # every day, Monday–Sunday
SLOT_GAP = timedelta(minutes=90)  # a feed post this close to a slot already fills it
FOOTER = "18+ | Budapest, V. ker. / District V"
LIBRARY_NOTE = "From library: "
AI_NOTE_RE = re.compile(r"\bAI\b|\bMI-generált|mesterséges intelligencia", re.IGNORECASE)


@dataclass
class TextSet:
    headline: str
    eyebrow: str = ""
    accent: str = ""
    body: str = ""
    items: list[str] = field(default_factory=list)
    footnote: str = ""

    @classmethod
    def from_dict(cls, d: dict[str, Any] | None) -> TextSet:
        d = d or {}
        return cls(
            headline=str(d.get("headline", "")).strip(),
            eyebrow=str(d.get("eyebrow", "")).strip(),
            accent=str(d.get("accent", "")).strip(),
            body=str(d.get("body", "")).strip(),
            items=[str(i).strip() for i in d.get("items") or [] if str(i).strip()],
            footnote=str(d.get("footnote", "")).strip(),
        )

    def all_text(self) -> str:
        return "\n".join([self.eyebrow, self.headline, self.accent, self.body, *self.items, self.footnote])


@dataclass
class PlannedPost:
    slug: str
    pillar: str
    template: str
    hu: TextSet
    en: TextSet
    caption_hu: str
    caption_en: str
    hashtags: list[str]
    photo: str = ""
    alt_text: str = ""
    when: datetime | None = None
    source: str = "claude"  # or "library:<id>"

    @classmethod
    def from_dict(cls, d: dict[str, Any], source: str = "claude") -> PlannedPost:
        g = d.get("graphic") or {}
        return cls(
            slug=slugify(str(d.get("slug") or d.get("id") or "post")),
            pillar=str(d.get("pillar", "")),
            template=str(d.get("template") or g.get("template") or "statement"),
            hu=TextSet.from_dict(d.get("hu") or g.get("hu")),
            en=TextSet.from_dict(d.get("en") or g.get("en")),
            caption_hu=str(d.get("caption_hu", "")).strip(),
            caption_en=str(d.get("caption_en", "")).strip(),
            hashtags=[normalise_tag(t) for t in d.get("hashtags") or [] if str(t).strip()],
            photo=str(d.get("photo") or g.get("photo") or ""),
            alt_text=str(d.get("alt_text", "")).strip(),
            source=source,
        )

    def spec(self, lang: str) -> GraphicSpec:
        t = self.hu if lang == "hu" else self.en
        return GraphicSpec(self.template, t.headline, t.eyebrow, t.accent, t.body, t.items, t.footnote, self.photo)


def slugify(text: str) -> str:
    table = str.maketrans("áéíóöőúüű", "aeiooouuu")
    s = SLUG_RE.sub("-", text.lower().translate(table)).strip("-")
    return s[:40] or "post"


def normalise_tag(tag: str) -> str:
    tag = str(tag).strip()
    return tag if tag.startswith("#") else "#" + tag


# ---- scheduling ------------------------------------------------------------------------------------------------


def next_week_slots(
    posts: list[Post], today: date, tz: ZoneInfo, times: tuple[str, ...] = POST_TIMES
) -> list[datetime]:
    """Every posting time on every posting day of next week, skipping slots that already have a feed post nearby."""
    monday = today + timedelta(days=7 - today.weekday())
    taken = [
        p.scheduled_dt(tz)
        for p in posts
        if p.type != PostType.STORY and p.status not in (Status.SKIPPED, Status.FAILED, Status.MISSED)
    ]
    slots = []
    for offset in POST_DAYS:
        day = monday + timedelta(days=offset)
        for time_of_day in times:
            hh, mm = (int(x) for x in time_of_day.split(":"))
            slot = datetime(day.year, day.month, day.day, hh, mm, tzinfo=tz)
            if not any(abs(t - slot) < SLOT_GAP for t in taken):
                slots.append(slot)
    return slots


# ---- library ------------------------------------------------------------------------------------------------------


def load_library(repo_root: Path) -> list[dict[str, Any]]:
    path = repo_root / "marketing" / "library.yaml"
    if not path.is_file():
        return []
    return list((yaml.safe_load(path.read_text(encoding="utf-8")) or {}).get("posts") or [])


def library_usage(posts: list[Post]) -> dict[str, str]:
    """library id → most recent scheduled_at it was used for."""
    used: dict[str, str] = {}
    for p in posts:
        note = p.notes or ""
        if LIBRARY_NOTE in note:
            lib_id = note.split(LIBRARY_NOTE, 1)[1].split()[0].strip(".,")
            used[lib_id] = max(used.get(lib_id, ""), p.scheduled_at)
    return used


def pick_from_library(library: list[dict[str, Any]], posts: list[Post], n: int) -> list[PlannedPost]:
    """Least recently used first; avoid repeating a pillar within the same week when possible."""
    used = library_usage(posts)
    ordered = sorted(library, key=lambda e: (used.get(str(e["id"]), ""), str(e["id"])))
    chosen: list[dict[str, Any]] = []
    pillars: set[str] = set()
    for e in ordered:
        if len(chosen) == n:
            break
        if e.get("pillar") not in pillars:
            chosen.append(e)
            pillars.add(str(e.get("pillar")))
    for e in ordered:
        if len(chosen) == n:
            break
        if e not in chosen:
            chosen.append(e)
    return [PlannedPost.from_dict(e, source=f"library:{e['id']}") for e in chosen]


# ---- building ------------------------------------------------------------------------------------------------------


def assemble_caption(p: PlannedPost) -> str:
    tags = " ".join(p.hashtags[:MAX_HASHTAGS])
    return (
        "----- CAPTION (copy from here) -----\n\n"
        f"{p.caption_hu.strip()}\n\n—\n\n{p.caption_en.strip()}\n\n{FOOTER}\n\n{tags}\n\n"
        "----- END CAPTION -----\n"
    )


def auto_alt(p: PlannedPost, repo_root: Path) -> str:
    """Hungarian alt text for the published (Hungarian) image, describing what's on it."""
    if p.alt_text:
        return p.alt_text
    t = p.hu
    words = " ".join(x for x in (t.headline, t.accent) if x)
    lib = photo_library(repo_root)
    if p.template == "photo" and p.photo in lib:
        base = lib[p.photo].alt_hu
        return f"{base}. A képen a felirat: „{words}” — Bloom Models, online modell munka Budapesten."
    bg = "világos rózsaszín" if p.template == "light" else "sötét bordó"
    extra = f" Lista: {'; '.join(t.items)}." if t.items else ""
    return f"Bloom Models grafika {bg} háttéren, a felirat: „{words}”.{extra} Online modell munka Budapesten."


def check_planned(p: PlannedPost, settings: Settings, facts: Facts) -> list[str]:
    """Problems that make the agent drop a planned post (before anything is drawn)."""
    problems: list[str] = []
    if p.template not in TEMPLATES:
        problems.append(f"unknown template {p.template}")
    if not p.hu.headline or not p.en.headline:
        problems.append("graphic needs a headline in both languages")
    if not p.caption_hu or not p.caption_en:
        problems.append("caption needs Hungarian and English text")
    if not 3 <= len(p.hashtags) <= MAX_HASHTAGS:
        problems.append(f"needs 3–{MAX_HASHTAGS} hashtags, has {len(p.hashtags)}")
    if p.template == "photo":
        lib = photo_library(settings.repo_root)
        if p.photo not in lib or not lib[p.photo].instagram_ok:
            problems.append(f"photo '{p.photo}' is not an Instagram-approved library photo")
        elif lib[p.photo].ai_generated:
            if not (AI_NOTE_RE.search(p.hu.footnote) and AI_NOTE_RE.search(p.en.footnote)):
                problems.append(f"photo '{p.photo}' is AI-generated: say so in the graphic's footnote (both languages)")
            if not (AI_NOTE_RE.search(p.caption_hu) and AI_NOTE_RE.search(p.caption_en)):
                problems.append(f"photo '{p.photo}' is AI-generated: say so in both captions")
    everything = "\n".join([p.caption_hu, p.caption_en, p.hu.all_text(), p.en.all_text(), " ".join(p.hashtags), p.alt_text])
    banned = find_banned(everything, settings.banned_words)
    if banned:
        problems.append("uses words that risk the account: " + ", ".join(sorted(set(banned))))
    for issue in check_text(p.caption_hu + "\n" + p.caption_en, facts):
        problems.append(issue.message)
    for lang, t in (("hu", p.hu), ("en", p.en)):
        for issue in check_text(t.all_text(), facts):
            hint = " (put it in the graphic's footnote)" if issue.kind == "missing-disclaimer" else ""
            problems.append(f"graphic ({lang}): {issue.message}{hint}")
    missing = missing_glyphs(p.spec("hu"), settings.repo_root) | missing_glyphs(p.spec("en"), settings.repo_root)
    if missing:
        problems.append(
            "graphic text uses characters the brand fonts can't draw (emoji/symbols): " + " ".join(sorted(missing))
        )
    bait = re.search(r"\b(tag a friend|comment yes|like if|jelöld meg|kommenteld|lájkold ha)\b", everything, re.I)
    if bait:
        problems.append(f"engagement bait: “{bait.group(0)}”")
    if len(HASHTAG_RE.findall(p.caption_hu + p.caption_en)) > 0:
        problems.append("put hashtags in the hashtags list, not inside the caption text")
    return problems


def write_post(p: PlannedPost, when: datetime, settings: Settings, *, status: Status) -> Post:
    """Draws both graphics and writes posts/<date>-<slug>/. Raises GraphicError if the text doesn't fit."""
    root = settings.repo_root
    post_id = f"{when:%Y-%m-%d}-{p.slug}"
    folder = settings.posts_dir / post_id
    n = 2
    while folder.exists():
        post_id = f"{when:%Y-%m-%d}-{p.slug}-{n}"
        folder = settings.posts_dir / post_id
        n += 1
    hu_img = render(p.spec("hu"), "hu", root)
    en_img = render(p.spec("en"), "en", root)
    folder.mkdir(parents=True)
    (folder / "image.jpg").write_bytes(hu_img)
    (folder / "image-en.jpg").write_bytes(en_img)
    (folder / "caption.txt").write_text(assemble_caption(p), encoding="utf-8")
    note = "Hungarian image is published. For the English version, change media to [image-en.jpg]."
    if p.source.startswith("library:"):
        note += f" {LIBRARY_NOTE}{p.source.split(':', 1)[1]}"
    else:
        note += f" Written by the marketing agent ({p.pillar})."
    post = Post(
        id=post_id,
        status=status,
        type=PostType.IMAGE,
        platforms=[Platform.INSTAGRAM],
        scheduled_at=when.strftime("%Y-%m-%d %H:%M"),
        media=["image.jpg"],
        caption_file="caption.txt",
        alt_text=auto_alt(p, root),
        notes=note,
    )
    post._folder = folder
    save_post(post)
    return post


def build_week(
    planned: list[PlannedPost], slots: list[datetime], settings: Settings, facts: Facts, *, status: Status
) -> tuple[list[Post], list[str]]:
    """Writes as many valid posts as there are slots. Returns (posts, notes about dropped ones)."""
    made: list[Post] = []
    dropped: list[str] = []
    queue = list(planned)
    for when in slots:
        while queue:
            p = queue.pop(0)
            problems = check_planned(p, settings, facts)
            if problems:
                dropped.append(f"{p.slug}: " + "; ".join(problems))
                continue
            try:
                post = write_post(p, when, settings, status=status)
            except GraphicError as exc:
                dropped.append(f"{p.slug}: {exc}")
                continue
            errors = [i.message for i in validate_post(post, settings) if i.level == "error"]
            if errors:
                _remove(post)
                dropped.append(f"{p.slug}: " + "; ".join(errors))
                continue
            made.append(post)
            break
    return made, dropped


def _remove(post: Post) -> None:
    folder = post.folder
    for f in folder.iterdir():
        f.unlink()
    folder.rmdir()
