"""Makes media Instagram-ready: PNG/WEBP → JPEG, sRGB, max 1440 px wide. Safe to run repeatedly."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageOps

from .models import Post, Status
from .queue import save_post

log = logging.getLogger(__name__)

MAX_WIDTH = 1440
JPEG_QUALITY = 92
CONVERT = {".png", ".webp"}


@dataclass
class PrepareResult:
    converted: list[str]
    resized: list[str]


def _to_rgb(im: Image.Image) -> Image.Image:
    im = ImageOps.exif_transpose(im)
    if im.mode in ("RGBA", "LA") or (im.mode == "P" and "transparency" in im.info):
        im = im.convert("RGBA")
        background = Image.new("RGB", im.size, (255, 255, 255))
        background.paste(im, mask=im.split()[-1])
        return background
    return im.convert("RGB")


def _fit(im: Image.Image) -> tuple[Image.Image, bool]:
    if im.width <= MAX_WIDTH:
        return im, False
    height = round(im.height * MAX_WIDTH / im.width)
    return im.resize((MAX_WIDTH, height), Image.Resampling.LANCZOS), True


def prepare_post(post: Post) -> PrepareResult:
    result = PrepareResult(converted=[], resized=[])
    if post.status in (Status.PUBLISHED, Status.SKIPPED):
        return result
    new_media: list[str] = []
    changed = False
    for name in post.media:
        src = post.folder / name
        ext = src.suffix.lower()
        if not src.is_file():
            new_media.append(name)
            continue
        if ext in CONVERT:
            dst_name = str(Path(name).with_suffix(".jpg"))
            dst = post.folder / dst_name
            with Image.open(src) as im:
                rgb, resized = _fit(_to_rgb(im))
                rgb.save(dst, "JPEG", quality=JPEG_QUALITY, optimize=True, progressive=True)
            src.unlink()
            new_media.append(dst_name)
            result.converted.append(f"{post.id}/{name} → {dst_name}")
            if resized:
                result.resized.append(f"{post.id}/{dst_name}")
            changed = True
        elif ext in {".jpg", ".jpeg"}:
            with Image.open(src) as im:
                needs = im.mode not in ("RGB", "L") or im.width > MAX_WIDTH
                if needs:
                    rgb, resized = _fit(_to_rgb(im))
            if needs:
                rgb.save(src, "JPEG", quality=JPEG_QUALITY, optimize=True, progressive=True)
                result.resized.append(f"{post.id}/{name}")
            new_media.append(name)
        else:
            new_media.append(name)
    if changed:
        post.media = new_media
        save_post(post)
    return result


def prepare_all(posts: list[Post]) -> PrepareResult:
    total = PrepareResult(converted=[], resized=[])
    for post in posts:
        r = prepare_post(post)
        total.converted.extend(r.converted)
        total.resized.extend(r.resized)
    return total
