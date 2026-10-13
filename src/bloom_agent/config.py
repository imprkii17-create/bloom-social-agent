"""Runtime configuration, read from environment variables (GitHub Actions secrets/vars or a local .env)."""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from zoneinfo import ZoneInfo

DEFAULT_GRAPH_VERSION = "v25.0"
DEFAULT_TIMEZONE = "Europe/Budapest"
DEFAULT_CLAUDE_MODEL = "claude-sonnet-5-5"


def _env(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(name)
    if value is None or value.strip() == "":
        return default
    return value.strip()


def _load_dotenv(path: Path) -> None:
    """Minimal .env loader for local runs. Never overrides variables that are already set."""
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def _git_head(root: Path) -> str | None:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=root, capture_output=True, text=True, timeout=10, check=True
        )
    except (OSError, subprocess.SubprocessError):
        return None
    sha = out.stdout.strip()
    return sha if len(sha) == 40 else None


@dataclass(frozen=True)
class Settings:
    repo_root: Path
    posts_dir: Path
    reports_dir: Path
    timezone: ZoneInfo
    graph_version: str
    ig_user_id: str | None
    ig_access_token: str | None
    fb_page_id: str | None
    fb_page_token: str | None
    media_base_url: str | None
    max_lateness_minutes: int
    max_attempts: int
    anthropic_api_key: str | None
    claude_model: str
    github_token: str | None
    github_admin_token: str | None
    github_repository: str | None
    gsc_service_account_json: str | None = None
    gsc_property: str = "sc-domain:bloommodels.hu"
    pagespeed_api_key: str | None = None
    banned_words: tuple[str, ...] = field(default_factory=tuple)
    telegram_bot_token: str | None = None
    telegram_chat_id: str | None = None
    dashboard_url: str | None = None
    bot_max_usd_per_run: float = 0.5

    @property
    def ig_base(self) -> str:
        return f"https://graph.instagram.com/{self.graph_version}"

    @property
    def fb_base(self) -> str:
        return f"https://graph.facebook.com/{self.graph_version}"

    def require_instagram(self) -> tuple[str, str]:
        if not self.ig_user_id or not self.ig_access_token:
            raise ConfigError(
                "Instagram is not configured. Set the IG_USER_ID and IG_ACCESS_TOKEN secrets "
                "(see SETUP.md, step 5)."
            )
        return self.ig_user_id, self.ig_access_token

    def require_facebook(self) -> tuple[str, str]:
        if not self.fb_page_id or not self.fb_page_token:
            raise ConfigError(
                "Facebook is not configured. Set FB_PAGE_ID and FB_PAGE_ACCESS_TOKEN, "
                "or remove 'facebook' from the post's platforms."
            )
        return self.fb_page_id, self.fb_page_token


class ConfigError(RuntimeError):
    """Raised when a required setting is missing."""


# Words that make Instagram/Meta treat an account as adult-content recruitment. Matched as whole words.
DEFAULT_BANNED_WORDS = (
    "webcam", "web cam", "cam girl", "camgirl", "adult", "nsfw", "sex", "sexy", "nude", "naked", "escort",
    "onlyfans", "webkamera", "felnőtt", "szex", "szexi", "meztelen",
)


def load_settings(repo_root: Path | None = None) -> Settings:
    root = (repo_root or Path(_env("BLOOM_REPO_ROOT", os.getcwd()) or ".")).resolve()
    _load_dotenv(root / ".env")

    media_base = _env("MEDIA_BASE_URL")
    repo = _env("GITHUB_REPOSITORY")
    if media_base is None and repo:
        # Default for GitHub Actions: serve committed media straight from the repository, pinned to the commit
        # that is actually checked out (GITHUB_SHA can be older than the checkout for queued scheduled runs).
        sha = _git_head(root) or _env("GITHUB_SHA")
        if sha:
            media_base = f"https://raw.githubusercontent.com/{repo}/{sha}"

    extra_banned = tuple(w.strip().lower() for w in (_env("BANNED_WORDS", "") or "").split(",") if w.strip())

    dashboard = _env("DASHBOARD_URL")
    if dashboard is None and repo and "/" in repo:
        owner, name = repo.split("/", 1)
        dashboard = f"https://{owner.lower()}.github.io/{name}/"
    try:
        bot_max = float(_env("BOT_MAX_USD_PER_RUN", "0.5") or 0.5)
    except ValueError:
        bot_max = 0.5

    return Settings(
        repo_root=root,
        posts_dir=root / "posts",
        reports_dir=root / "reports",
        timezone=ZoneInfo(_env("TIMEZONE", DEFAULT_TIMEZONE) or DEFAULT_TIMEZONE),
        graph_version=_env("GRAPH_API_VERSION", DEFAULT_GRAPH_VERSION) or DEFAULT_GRAPH_VERSION,
        ig_user_id=_env("IG_USER_ID"),
        ig_access_token=_env("IG_ACCESS_TOKEN"),
        fb_page_id=_env("FB_PAGE_ID"),
        fb_page_token=_env("FB_PAGE_ACCESS_TOKEN"),
        media_base_url=media_base.rstrip("/") if media_base else None,
        max_lateness_minutes=int(_env("MAX_LATENESS_MINUTES", "180") or 180),
        max_attempts=int(_env("MAX_ATTEMPTS", "3") or 3),
        anthropic_api_key=_env("ANTHROPIC_API_KEY"),
        claude_model=_env("CLAUDE_MODEL", DEFAULT_CLAUDE_MODEL) or DEFAULT_CLAUDE_MODEL,
        github_token=_env("GITHUB_TOKEN"),
        github_admin_token=_env("GH_ADMIN_TOKEN"),
        github_repository=repo,
        gsc_service_account_json=_env("GSC_SERVICE_ACCOUNT_JSON"),
        gsc_property=_env("GSC_PROPERTY", "sc-domain:bloommodels.hu") or "sc-domain:bloommodels.hu",
        pagespeed_api_key=_env("PAGESPEED_API_KEY"),
        banned_words=DEFAULT_BANNED_WORDS + extra_banned,
        telegram_bot_token=_env("TELEGRAM_BOT_TOKEN"),
        telegram_chat_id=_env("TELEGRAM_CHAT_ID"),
        dashboard_url=dashboard,
        bot_max_usd_per_run=bot_max,
    )
