from __future__ import annotations

from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
import yaml
from PIL import Image

from bloom_agent.config import Settings, load_settings
from bloom_agent.graph import GraphClient
from bloom_agent.instagram import InstagramClient

IG_BASE = "https://graph.instagram.com/v25.0"
MEDIA_BASE = "https://media.example.com"
TOKEN = "SECRET-TOKEN-123"
IG_ID = "17841400000000000"

ENV_KEYS = [
    "IG_USER_ID", "IG_ACCESS_TOKEN", "FB_PAGE_ID", "FB_PAGE_ACCESS_TOKEN", "MEDIA_BASE_URL", "GITHUB_REPOSITORY",
    "GITHUB_SHA", "GITHUB_TOKEN", "GH_ADMIN_TOKEN", "ANTHROPIC_API_KEY", "TIMEZONE", "MAX_LATENESS_MINUTES",
    "MAX_ATTEMPTS", "BANNED_WORDS", "GITHUB_STEP_SUMMARY", "BLOOM_REPO_ROOT", "GRAPH_API_VERSION",
    "GSC_SERVICE_ACCOUNT_JSON", "GSC_PROPERTY", "PAGESPEED_API_KEY", "MARKETING_MAX_COST_USD", "MARKETING_RESEARCH",
    "CLAUDE_MODEL", "GITHUB_ACTIONS", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "DASHBOARD_URL", "BOT_MAX_USD_PER_RUN",
]
REAL_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def clean_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    for key in ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    yield


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    (tmp_path / "posts").mkdir()
    monkeypatch.setenv("IG_USER_ID", IG_ID)
    monkeypatch.setenv("IG_ACCESS_TOKEN", TOKEN)
    monkeypatch.setenv("MEDIA_BASE_URL", MEDIA_BASE)
    return tmp_path


@pytest.fixture
def settings(repo: Path) -> Settings:
    return load_settings(repo)


def make_image(path: Path, size: tuple[int, int] = (1080, 1350), mode: str = "RGB", fmt: str | None = None) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    color: Any = (200, 120, 140) if mode == "RGB" else (200, 120, 140, 128)
    Image.new(mode, size, color).save(path, fmt or ("PNG" if path.suffix.lower() == ".png" else "JPEG"))
    return path


CAPTION_FILE = """HÉTFŐ / MONDAY
----- CAPTION (copy from here) -----

Rugalmas beosztás, heti kb. 20 óra. 🌸

—

Flexible schedule, about 20 hours a week. 🌸

18+

#bloommodels #budapestjobs #rugalmasmunka #onlinemodel #flexiblework

----- END CAPTION -----

Alt text: ignored by the agent
"""


@pytest.fixture
def make_post(repo: Path) -> Callable[..., Path]:
    def _make(
        post_id: str = "2026-10-19-mon",
        *,
        status: str = "approved",
        type: str = "image",
        scheduled_at: str = "2026-10-19 18:30",
        media: list[str] | None = None,
        platforms: list[str] | None = None,
        caption: str | None = CAPTION_FILE,
        create_media: bool = True,
        extra: dict[str, Any] | None = None,
    ) -> Path:
        folder = repo / "posts" / post_id
        folder.mkdir(parents=True, exist_ok=True)
        media = media or (["video.mp4"] if type == "reel" else ["image.jpg"])
        if create_media:
            for name in media:
                if name.endswith(".mp4"):
                    (folder / name).write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"0" * 2048)
                else:
                    size = (1080, 1920) if type == "story" else (1080, 1350)
                    make_image(folder / name, size)
        data: dict[str, Any] = {
            "id": post_id, "status": status, "type": type, "platforms": platforms or ["instagram"],
            "scheduled_at": scheduled_at, "media": media,
            "caption_file": "caption.txt" if caption is not None else None,
            "alt_text": "A smiling woman on a green sofa" if type == "image" else None,
        }
        data = {k: v for k, v in data.items() if v is not None}
        if extra:
            data.update(extra)
        (folder / "post.yaml").write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")
        if caption is not None:
            (folder / "caption.txt").write_text(caption, encoding="utf-8")
        return folder

    return _make


@pytest.fixture
def ig_factory(settings: Settings) -> Callable[[], InstagramClient]:
    def _factory() -> InstagramClient:
        graph = GraphClient(settings.ig_base, TOKEN, sleep=lambda s: None)
        return InstagramClient(graph, IG_ID, sleep=lambda s: None)

    return _factory


@pytest.fixture
def full_repo(repo: Path) -> Path:
    """The test repo plus the real brand/, site/ (with built public/, so images aren't re-converted) and marketing/."""
    import shutil

    for name in ("brand", "site", "marketing"):
        shutil.copytree(REAL_ROOT / name, repo / name)
    return repo
