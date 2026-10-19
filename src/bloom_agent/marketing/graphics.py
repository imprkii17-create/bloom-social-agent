"""Branded Instagram graphics (1080×1350 JPEG) drawn with Pillow, matching the Bloom Models look.

Four layouts: `statement` and `list` on deep plum, `photo` (a library photo with the text over a shade) and `light`
(blush background). Text is fitted automatically: it shrinks until it fits, and raises an error rather than ever
cutting words off.
"""

from __future__ import annotations

import io
import math
from collections.abc import Callable
from dataclasses import dataclass, field
from functools import cache
from pathlib import Path
from typing import Literal

import yaml
from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageOps

W, H = 1080, 1350
PAD = 72
SS = 2  # supersampling for the logo mark

PLUM = (36, 26, 30)
PLUM_2 = (61, 36, 48)
INK = (61, 36, 48)
BLUSH = (250, 237, 240)
WHITE = (253, 246, 243)
ROSE = (209, 96, 127)
PINK = (237, 168, 182)
PEACH = (234, 182, 143)
TERRACOTTA = (201, 128, 90)
MUTED_DARK = (232, 207, 214)
MUTED_LIGHT = (124, 87, 99)

Template = Literal["statement", "list", "photo", "light"]
TEMPLATES: tuple[str, ...] = ("statement", "list", "photo", "light")

CTA = {"hu": "Jelentkezz · link a bióban  →", "en": "Apply · link in bio  →"}


class GraphicError(ValueError):
    """The text doesn't fit or the spec is incomplete."""


@dataclass
class GraphicSpec:
    template: str
    headline: str
    eyebrow: str = ""
    accent: str = ""  # italic second line in pink
    body: str = ""
    items: list[str] = field(default_factory=list)
    footnote: str = ""  # small print, e.g. the earnings disclaimer
    photo: str = ""  # file name in brand/photos


# ---- fonts -----------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class FontFiles:
    serif: Path
    serif_italic: Path
    sans: Path


def font_files(repo_root: Path) -> FontFiles:
    d = repo_root / "brand" / "fonts"
    return FontFiles(d / "Fraunces-Variable.ttf", d / "Fraunces-Italic-Variable.ttf", d / "PlusJakartaSans-Variable.ttf")


@cache
def _font(path: str, size: int, weight: int) -> ImageFont.FreeTypeFont:
    font = ImageFont.truetype(path, size)
    try:
        axes = font.get_variation_axes()
    except OSError:
        return font
    values: list[float] = []
    for axis in axes:
        name = axis["name"].decode() if isinstance(axis["name"], bytes) else str(axis["name"])
        lo, hi = float(axis["minimum"] or 0), float(axis["maximum"] or 0)
        if name == "Weight":
            values.append(max(lo, min(hi, weight)))
        elif name == "Optical Size":
            values.append(max(lo, min(hi, size // 2)))
        elif name in ("Softness", "Wonky"):
            values.append(lo)
        else:
            values.append(float(axis["default"] or 0))
    font.set_variation_by_axes(values)
    return font


class Fonts:
    def __init__(self, files: FontFiles) -> None:
        self.files = files

    def serif(self, size: int, weight: int = 600) -> ImageFont.FreeTypeFont:
        return _font(str(self.files.serif), size, weight)

    def italic(self, size: int, weight: int = 500) -> ImageFont.FreeTypeFont:
        return _font(str(self.files.serif_italic), size, weight)

    def sans(self, size: int, weight: int = 500) -> ImageFont.FreeTypeFont:
        return _font(str(self.files.sans), size, weight)


# ---- glyph coverage ------------------------------------------------------------------------------------------


def _glyph_pixels(path: str, ch: str) -> bytes:
    font = _font(path, 40, 600)
    im = Image.new("L", (90, 90), 0)
    ImageDraw.Draw(im).text((20, 10), ch, font=font, fill=255)
    return im.tobytes()


@cache
def _notdef_pixels(path: str) -> bytes:
    return _glyph_pixels(path, "\U0010fffd")  # a private-use code point: always drawn as the "missing" box


@cache
def _has_glyph(path: str, ch: str) -> bool:
    if ch.isspace():
        return True
    return _glyph_pixels(path, ch) != _notdef_pixels(path)


def missing_glyphs(spec: GraphicSpec, repo_root: Path) -> set[str]:
    """Characters in the graphic's text that its fonts would draw as empty boxes (emoji, ✓, → in the serif…)."""
    files = font_files(repo_root)
    serif_text = spec.headline + spec.accent + "".join(spec.items)
    sans_text = spec.eyebrow.upper() + spec.body + spec.footnote
    missing = {ch for ch in set(serif_text) if not _has_glyph(str(files.serif), ch)}
    missing |= {ch for ch in set(spec.accent) if not _has_glyph(str(files.serif_italic), ch)}
    missing |= {ch for ch in set(sans_text) if not _has_glyph(str(files.sans), ch)}
    return missing


# ---- text layout -------------------------------------------------------------------------------------------


def text_width(font: ImageFont.FreeTypeFont, text: str) -> float:
    return float(font.getlength(text))


def wrap(text: str, font: ImageFont.FreeTypeFont, max_width: float) -> list[str] | None:
    """Greedy word wrap. None if a single word is wider than the box."""
    lines: list[str] = []
    for para in text.split("\n"):
        words = para.split()
        if not words:
            lines.append("")
            continue
        line = words[0]
        if text_width(font, line) > max_width:
            return None
        for word in words[1:]:
            if text_width(font, word) > max_width:
                return None
            trial = f"{line} {word}"
            if text_width(font, trial) <= max_width:
                line = trial
            else:
                lines.append(line)
                line = word
        lines.append(line)
    return lines


@dataclass
class Block:
    lines: list[str]
    font: ImageFont.FreeTypeFont
    line_height: int

    @property
    def height(self) -> int:
        return self.line_height * len(self.lines)


def fit(
    text: str,
    make_font: Callable[[int], ImageFont.FreeTypeFont],
    max_width: float,
    max_height: float,
    start: int,
    minimum: int,
    leading: float = 1.08,
    max_lines: int = 99,
) -> Block:
    size = start
    while size >= minimum:
        font = make_font(size)
        lines = wrap(text, font, max_width)
        lh = math.ceil(size * leading)
        if lines is not None and len(lines) <= max_lines and lh * len(lines) <= max_height:
            return Block(lines, font, lh)
        size -= 2
    raise GraphicError(f"Text too long to fit: “{text[:60]}…”")


def draw_block(d: ImageDraw.ImageDraw, block: Block, x: int, y: int, fill: tuple[int, int, int]) -> int:
    for i, line in enumerate(block.lines):
        d.text((x, y + i * block.line_height), line, font=block.font, fill=fill)
    return y + block.height


# ---- brand elements ----------------------------------------------------------------------------------------------


Point = tuple[float, float]


def _cubic(p0: Point, p1: Point, p2: Point, p3: Point, n: int = 24) -> list[Point]:
    return [
        (
            (1 - t) ** 3 * p0[0] + 3 * (1 - t) ** 2 * t * p1[0] + 3 * (1 - t) * t**2 * p2[0] + t**3 * p3[0],
            (1 - t) ** 3 * p0[1] + 3 * (1 - t) ** 2 * t * p1[1] + 3 * (1 - t) * t**2 * p2[1] + t**3 * p3[1],
        )
        for t in (i / n for i in range(n + 1))
    ]


def bloom_mark(size: int, petal: tuple[int, int, int], center: tuple[int, int, int]) -> Image.Image:
    """The five-petal Bloom mark (same path as the logo SVG), anti-aliased."""
    big = size * 4
    img = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    s = big / 100
    petal_pts = _cubic((50, 52), (36, 40), (34, 16), (50, 6)) + _cubic((50, 6), (66, 16), (64, 40), (50, 52))[1:]
    for k in range(5):
        a = math.radians(72 * k)
        pts = []
        for x, y in petal_pts:
            dx, dy = x - 50, y - 50
            pts.append(((50 + dx * math.cos(a) - dy * math.sin(a)) * s, (50 + dx * math.sin(a) + dy * math.cos(a)) * s))
        d.polygon(pts, fill=petal)
    r = 7 * s
    d.ellipse((50 * s - r, 50 * s - r, 50 * s + r, 50 * s + r), fill=center)
    return img.resize((size, size), Image.Resampling.LANCZOS)


def _header(img: Image.Image, fonts: Fonts, dark: bool) -> None:
    d = ImageDraw.Draw(img)
    mark = bloom_mark(46, (242, 201, 206) if dark else ROSE, PLUM if dark else INK)
    img.paste(mark, (PAD, 64), mark)
    d.text((PAD + 60, 66), "Bloom Models", font=fonts.serif(32, 600), fill=WHITE if dark else INK)
    pill = fonts.sans(22, 700)
    label = "18+"
    w = int(text_width(pill, label)) + 36
    x1, y1 = W - PAD, 64
    box = (x1 - w, y1, x1, y1 + 44)
    if dark:
        d.rounded_rectangle(box, radius=22, fill=(70, 55, 62), outline=(140, 120, 128), width=2)
    else:
        d.rounded_rectangle(box, radius=22, fill=(243, 221, 227))
    d.text((box[0] + 18, y1 + 9), label, font=pill, fill=WHITE if dark else INK)


def _cta(img: Image.Image, fonts: Fonts, lang: str) -> tuple[int, int]:
    """Draws the CTA pill bottom-right; returns its top y and left x."""
    d = ImageDraw.Draw(img)
    font = fonts.sans(26, 700)
    label = CTA.get(lang, CTA["en"])
    w = int(text_width(font, label)) + 68
    h = 66
    x1, y1 = W - PAD, H - 64
    d.rounded_rectangle((x1 - w, y1 - h, x1, y1), radius=33, fill=ROSE)
    d.text((x1 - w + 34, y1 - h + 17), label, font=font, fill=(255, 255, 255))
    return y1 - h, x1 - w


def _footnote(img: Image.Image, fonts: Fonts, text: str, right: int, color: tuple[int, int, int]) -> None:
    if not text:
        return
    d = ImageDraw.Draw(img)
    block = fit(text, lambda s: fonts.sans(s, 500), right - PAD - 30, 80, 19, 15, leading=1.35, max_lines=3)
    draw_block(d, block, PAD, H - 64 - block.height, color)


def _glow(img: Image.Image, center: tuple[int, int], radius: int, color: tuple[int, int, int], alpha: int) -> None:
    # transparent pixels carry the same colour, so blurring doesn't darken the edges
    layer = Image.new("RGBA", img.size, (*color, 0))
    ImageDraw.Draw(layer).ellipse(
        (center[0] - radius, center[1] - radius, center[0] + radius, center[1] + radius), fill=(*color, alpha)
    )
    layer = layer.filter(ImageFilter.GaussianBlur(radius // 2))
    img.alpha_composite(layer)


# ---- photo library ------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class LibraryPhoto:
    name: str
    path: Path
    alt_en: str
    alt_hu: str
    instagram_ok: bool
    focus_y: float
    ai_generated: bool = False


def photo_library(repo_root: Path) -> dict[str, LibraryPhoto]:
    d = repo_root / "brand" / "photos"
    meta_path = d / "photos.yaml"
    if not meta_path.is_file():
        return {}
    data = (yaml.safe_load(meta_path.read_text(encoding="utf-8")) or {}).get("photos") or {}
    out = {}
    for name, m in data.items():
        if (d / name).is_file():
            out[name] = LibraryPhoto(
                name,
                d / name,
                str(m.get("alt_en", "")),
                str(m.get("alt_hu", "")),
                bool(m.get("instagram_ok", False)),
                float(m.get("focus_y", 0.35)),
                bool(m.get("ai_generated", False)),
            )
    return out


def _cover(photo: LibraryPhoto, size: tuple[int, int]) -> Image.Image:
    with Image.open(photo.path) as raw:
        im: Image.Image = ImageOps.exif_transpose(raw).convert("RGB")
        scale = max(size[0] / im.width, size[1] / im.height)
        im = im.resize((math.ceil(im.width * scale), math.ceil(im.height * scale)), Image.Resampling.LANCZOS)
        left = (im.width - size[0]) // 2
        top = int(max(0, min(im.height - size[1], photo.focus_y * im.height - size[1] * 0.3)))
        return im.crop((left, top, left + size[0], top + size[1]))


# ---- templates ------------------------------------------------------------------------------------------------------


def render(spec: GraphicSpec, lang: str, repo_root: Path) -> bytes:
    if spec.template not in TEMPLATES:
        raise GraphicError(f"Unknown template '{spec.template}'. Use one of: {', '.join(TEMPLATES)}.")
    if not spec.headline.strip():
        raise GraphicError("A graphic needs a headline.")
    missing = missing_glyphs(spec, repo_root)
    if missing:
        raise GraphicError("The brand fonts can't draw: " + " ".join(sorted(missing)) + " (remove emoji/symbols).")
    fonts = Fonts(font_files(repo_root))
    if spec.template == "photo":
        img = _photo(spec, lang, fonts, repo_root)
    elif spec.template == "light":
        img = _textual(spec, lang, fonts, dark=False)
    else:
        img = _textual(spec, lang, fonts, dark=True)
    buf = io.BytesIO()
    img.convert("RGB").save(buf, "JPEG", quality=90, optimize=True, progressive=True)
    return buf.getvalue()


def _textual(spec: GraphicSpec, lang: str, fonts: Fonts, *, dark: bool) -> Image.Image:
    img = Image.new("RGBA", (W, H), (*(PLUM if dark else BLUSH), 255))
    if dark:
        _glow(img, (90, H - 40), 420, (120, 50, 70), 120)
        _glow(img, (W - 60, 60), 300, (110, 50, 70), 70)
    else:
        _glow(img, (W - 80, H - 80), 420, (236, 170, 188), 90)
    _header(img, fonts, dark)
    d = ImageDraw.Draw(img)
    width = W - 2 * PAD
    cta_top, cta_left = _cta(img, fonts, lang)
    bottom_limit = cta_top - 50
    head_color = WHITE if dark else INK
    eyebrow_color = PEACH if dark else TERRACOTTA
    accent_color = PINK if dark else ROSE
    body_color = MUTED_DARK if dark else MUTED_LIGHT

    top = 190
    eb = (
        fit(spec.eyebrow.upper(), lambda s: fonts.sans(s, 800), width * 0.8, 40, 24, 18, max_lines=1)
        if spec.eyebrow
        else None
    )
    has_items = spec.template == "list" and bool(spec.items)
    avail = bottom_limit - top - (eb.height + 22 if eb else 0)
    head = fit(
        spec.headline,
        lambda s: fonts.serif(s, 600),
        width,
        avail * (0.42 if has_items else 0.55),
        104,
        52,
        leading=1.06,
        max_lines=5,
    )
    acc = (
        fit(spec.accent, lambda s: fonts.italic(s, 500), width, 230, int(head.font.size), 44, leading=1.08, max_lines=3)
        if spec.accent
        else None
    )
    used = head.height + (acc.height + 6 if acc else 0)

    if has_items:
        y = top
        if eb:
            _tracked(d, PAD, y, eb.lines[0], eb.font, eyebrow_color, 0.14)
            y += eb.height + 22
        y = draw_block(d, head, PAD, y, head_color)
        if acc:
            y = draw_block(d, acc, PAD, y + 6, accent_color)
        y += 50
        per = (bottom_limit - y) / max(1, len(spec.items))
        num_font = fonts.serif(44, 600)
        for i, item in enumerate(spec.items, start=1):
            d.line((PAD, y - 18, W - PAD, y - 18), fill=(90, 66, 76) if dark else (232, 210, 216), width=2)
            d.text((PAD, y - 4), str(i), font=num_font, fill=accent_color)
            blk = fit(item, lambda s: fonts.serif(s, 600), width - 80, per - 30, 40, 26, leading=1.18, max_lines=3)
            draw_block(d, blk, PAD + 80, y, head_color)
            y += max(int(per), blk.height + 36)
    else:
        body = None
        if spec.body:
            body = fit(spec.body, lambda s: fonts.sans(s, 500), width * 0.92, avail - used - 40, 38, 24, leading=1.42)
        total = (eb.height + 22 if eb else 0) + used + (body.height + 40 if body else 0)
        # sit slightly above the middle of the free space, like the hand-made posts
        y = top + max(0, int((bottom_limit - top - total) * 0.42))
        if eb:
            _tracked(d, PAD, y, eb.lines[0], eb.font, eyebrow_color, 0.14)
            y += eb.height + 22
        y = draw_block(d, head, PAD, y, head_color)
        if acc:
            y = draw_block(d, acc, PAD, y + 6, accent_color)
        if body:
            draw_block(d, body, PAD, y + 40, body_color)

    _footnote(img, fonts, spec.footnote, cta_left, (190, 160, 170) if dark else MUTED_LIGHT)
    return img


def _photo(spec: GraphicSpec, lang: str, fonts: Fonts, repo_root: Path) -> Image.Image:
    lib = photo_library(repo_root)
    photo = lib.get(spec.photo)
    if photo is None:
        raise GraphicError(f"Photo '{spec.photo}' is not in brand/photos/photos.yaml.")
    if not photo.instagram_ok:
        raise GraphicError(f"Photo '{spec.photo}' is marked instagram_ok: false.")
    img = _cover(photo, (W, H)).convert("RGBA")
    shade = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    sd = ImageDraw.Draw(shade)
    for yy in range(H):
        t = yy / H
        if t < 0.16:
            a = int(140 * (1 - t / 0.16))
        elif t < 0.38:
            a = 0
        else:
            a = int(min(1.0, (t - 0.38) / 0.36) * 245)
        sd.line((0, yy, W, yy), fill=(*PLUM, a))
    img.alpha_composite(shade)
    _header(img, fonts, True)
    d = ImageDraw.Draw(img)
    width = W - 2 * PAD
    cta_top, cta_left = _cta(img, fonts, lang)
    bottom = cta_top - (110 if spec.footnote else 46)

    # lay out from the bottom up
    blocks: list[tuple[Block, tuple[int, int, int], int]] = []
    body = fit(spec.body, lambda s: fonts.sans(s, 600), width, 150, 32, 24, leading=1.38, max_lines=3) if spec.body else None
    head = fit(spec.headline, lambda s: fonts.serif(s, 600), width, 330, 92, 50, leading=1.05, max_lines=4)
    acc = (
        fit(spec.accent, lambda s: fonts.italic(s, 500), width, 160, int(head.font.size), 44, leading=1.06, max_lines=2)
        if spec.accent
        else None
    )
    eb = (
        fit(spec.eyebrow.upper(), lambda s: fonts.sans(s, 800), width * 0.8, 40, 24, 18, max_lines=1)
        if spec.eyebrow
        else None
    )
    if eb:
        blocks.append((eb, PEACH, 18))
    blocks.append((head, WHITE, 6))
    if acc:
        blocks.append((acc, PINK, 22))
    if body:
        blocks.append((body, (243, 221, 227), 0))
    total = sum(b.height + gap for b, _, gap in blocks)
    y = bottom - total
    if y < H * 0.42:
        raise GraphicError("Too much text for a photo layout; shorten it or use the statement layout.")
    for blk, color, gap in blocks:
        if blk is eb:
            _tracked(d, PAD, y, blk.lines[0], blk.font, color, 0.14)
            y += blk.height + gap
        else:
            y = draw_block(d, blk, PAD, y, color) + gap
    _footnote(img, fonts, spec.footnote, cta_left, (220, 195, 203))
    return img


def _tracked(
    d: ImageDraw.ImageDraw,
    x: int,
    y: int,
    text: str,
    font: ImageFont.FreeTypeFont,
    fill: tuple[int, int, int],
    tracking: float,
) -> None:
    """Letter-spaced caps for eyebrows."""
    size = font.size
    cx = float(x)
    for ch in text:
        d.text((cx, y), ch, font=font, fill=fill)
        cx += text_width(font, ch) + size * tracking
