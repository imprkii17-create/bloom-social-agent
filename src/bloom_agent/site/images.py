"""Website images: responsive WebP versions of each photo, a lighter JPEG fallback, and the 1200×630 social card."""

from __future__ import annotations

import hashlib
import io
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageOps

WEBP_QUALITY = 78
JPEG_QUALITY = 82
FALLBACK_MAX = 1200
OG_SIZE = (1200, 630)
PHOTO_EXT = {".jpg", ".jpeg", ".png", ".webp"}
# Small brand images (favicons) are copied as they are.
COPY_ONLY_PREFIXES = ("favicon",)

# Bump when the conversion settings change, so cached outputs are rebuilt.
PIPELINE_VERSION = "1"


@dataclass(frozen=True)
class Variant:
    width: int
    height: int
    path: str  # relative to public/, e.g. images/hero-1-960.webp


@dataclass(frozen=True)
class PhotoOutputs:
    source: str
    width: int
    height: int
    variants: tuple[Variant, ...]
    fallback: str  # JPEG at the original path, re-compressed

    @property
    def largest(self) -> Variant:
        return self.variants[-1]


def source_key(path: Path, widths: list[int]) -> str:
    h = hashlib.sha256(path.read_bytes())
    h.update(f"|{PIPELINE_VERSION}|{sorted(widths)}".encode())
    return h.hexdigest()


def is_photo(rel: str) -> bool:
    p = Path(rel)
    return p.suffix.lower() in PHOTO_EXT and not p.name.startswith(COPY_ONLY_PREFIXES)


def _open(path: Path) -> Image.Image:
    with Image.open(path) as raw:
        im: Image.Image = ImageOps.exif_transpose(raw)
        if im.mode not in ("RGB", "L"):
            bg = Image.new("RGB", im.size, (255, 255, 255))
            rgba = im.convert("RGBA")
            bg.paste(rgba, mask=rgba.split()[-1])
            return bg
        return im.convert("RGB")


def plan(rel: str, width: int, height: int, widths: list[int]) -> PhotoOutputs:
    stem = rel.rsplit(".", 1)[0]
    targets = sorted({w for w in widths if w < width} | {min(max(widths), width)})
    variants = tuple(Variant(w, round(height * w / width), f"{stem}-{w}.webp") for w in targets)
    return PhotoOutputs(source=rel, width=width, height=height, variants=variants, fallback=stem + ".jpg")


def render(src: Path, rel: str, widths: list[int]) -> tuple[PhotoOutputs, dict[str, bytes]]:
    im = _open(src)
    outputs = plan(rel, im.width, im.height, widths)
    files: dict[str, bytes] = {}
    for v in outputs.variants:
        buf = io.BytesIO()
        im.resize((v.width, v.height), Image.Resampling.LANCZOS).save(buf, "WEBP", quality=WEBP_QUALITY, method=6)
        files[v.path] = buf.getvalue()
    fb = im
    if im.width > FALLBACK_MAX:
        fb = im.resize((FALLBACK_MAX, round(im.height * FALLBACK_MAX / im.width)), Image.Resampling.LANCZOS)
    buf = io.BytesIO()
    fb.save(buf, "JPEG", quality=JPEG_QUALITY, optimize=True, progressive=True)
    files[outputs.fallback] = buf.getvalue()
    return outputs, files


def og_card(src: Path) -> bytes:
    """Crop the photo to 1200×630, keeping the upper part (where faces usually are)."""
    im = _open(src)
    target_ratio = OG_SIZE[0] / OG_SIZE[1]
    if im.width / im.height > target_ratio:
        new_w = round(im.height * target_ratio)
        left = (im.width - new_w) // 2
        box = (left, 0, left + new_w, im.height)
    else:
        new_h = round(im.width / target_ratio)
        top = min(round(im.height * 0.12), im.height - new_h)
        box = (0, top, im.width, top + new_h)
    card = im.crop(box).resize(OG_SIZE, Image.Resampling.LANCZOS)
    buf = io.BytesIO()
    card.save(buf, "JPEG", quality=JPEG_QUALITY, optimize=True, progressive=True)
    return buf.getvalue()
