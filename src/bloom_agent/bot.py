"""The Telegram side of the agent: answers the owner's commands and questions.

GitHub runs `bloom bot poll` every few minutes. Each run reads the new messages, answers them and exits. Only the
chat saved in TELEGRAM_CHAT_ID is answered; everyone else is ignored (a bot is public, anyone can find it).

Commands change nothing except `/approve` and `/skip`, which edit one post.yaml exactly like you would on GitHub;
the workflow then commits it. Free-form questions go to Claude (if ANTHROPIC_API_KEY is set), which only answers:
it can't approve, post or change anything by itself.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from .config import Settings
from .dashboard import build_snapshot
from .models import Status
from .queue import load_queue, save_post
from .telegram import TelegramClient, TelegramError, esc
from .validate import count_hashtags, find_banned, validate_all

log = logging.getLogger(__name__)

MAX_AI_REPLIES_PER_RUN = 6
MAX_AI_TOKENS = 1500
CONNECT_CODE_MIN = 4

HELP = (
    "<b>Bloom agent</b>: I post to Instagram, watch the website and SEO, and plan every week.\n\n"
    "/status: is everything running?\n"
    "/queue: the next posts\n"
    "/approve: approve drafts (<code>/approve all</code>, <code>/approve next</code> or a post id)\n"
    "/skip &lt;post id&gt;: cancel a scheduled post\n"
    "/report: Instagram, Google and SEO numbers\n"
    "/brief: this week's plan and what I need from you\n"
    "/dashboard: link to the dashboard\n\n"
    "Or just write to me, for example <i>how did last week go?</i> or <i>write a caption about flexible hours</i>."
)

AI_SYSTEM = """You are the head of marketing for Bloom Models, an online modeling agency in Budapest that recruits adult
(18+) streaming models, chatting with the owner in Telegram. Bloom Models is a legitimate business: streaming models host
live online video shows (chatting, entertainment, glamour and lifestyle), and all public marketing is strictly
non-explicit and follows Instagram's rules. You run the Instagram account @bloommodelsagency and the
website bloommodels.hu through an automated agent; the data below is its current state.

How to answer:
- Answer in the language the owner writes in (Hungarian or English). Be brief and concrete: this is a phone chat.
- Plain text only: no markdown headings, no tables, no ** bold. Use short lines and "•" for lists.
- Base numbers only on the data given. If something isn't in the data, say so; never invent figures.
- You can't change anything yourself. When an action is needed, tell the owner the exact command
  (/approve <post id>, /skip <post id>) or what to do on GitHub.
- When you write captions or post ideas, follow the brand rules and facts below exactly (18+, at most 5 hashtags,
  no banned words, earnings only with the disclaimer, no guarantees, no fake testimonials).

Brand rules:
{rules}

Facts we may state in public (nothing else):
{facts}

Marketing playbook (how we work):
{playbook}
"""


SHORT_SYSTEM = """You are the marketing assistant of Bloom Models, a legitimate online modeling agency in Budapest that
recruits adults (18+) for non-explicit live streaming work (chatting, entertainment, glamour and lifestyle). Answer the
owner briefly, in their language, in plain text. Follow these brand rules for anything meant for Instagram:
{rules}
"""


@dataclass
class PollResult:
    handled: int = 0
    ignored: int = 0
    changed_posts: list[str] = field(default_factory=list)
    ai_replies: int = 0
    spend_usd: float = 0.0
    notes: list[str] = field(default_factory=list)


# ---- formatting -----------------------------------------------------------------------------------------------------


def _when(iso: str | None) -> str:
    if not iso:
        return "–"
    try:
        return datetime.fromisoformat(iso).strftime("%a %d %b %H:%M")
    except ValueError:
        return iso


def _github(settings: Settings, path: str) -> str | None:
    return f"https://github.com/{settings.github_repository}/blob/main/{path}" if settings.github_repository else None


def _dashboard_line(settings: Settings) -> str:
    return f'\n<a href="{esc(settings.dashboard_url)}">Open the dashboard</a>' if settings.dashboard_url else ""


STATUS_ICON = {"approved": "✅", "draft": "✏️", "published": "📸", "failed": "❌", "missed": "⏰", "skipped": "⏭"}


def status_text(settings: Settings, snap: dict[str, Any]) -> str:
    lines = [f"<b>Status</b> · {esc(_when(snap.get('generated_at')))}", ""]
    site = snap.get("site") or {}
    if site.get("checked"):
        parts = [f"{p['url'].split('//')[-1]} {'✅' if p.get('ok') else '❌ ' + esc(p.get('note') or p.get('status'))}"
                 + (f" {p['ms']} ms" if p.get("ok") else "") for p in site.get("pages", [])]
        lines.append(f"🌐 Website: {'up' if site.get('ok') else '<b>PROBLEM</b>'} · " + " · ".join(parts))
    ig = snap.get("instagram") or {}
    live = ig.get("live") or {}
    ig_bits = []
    if live.get("username"):
        ig_bits.append(f"@{esc(live['username'])}")
    if live.get("followers") is not None:
        ig_bits.append(f"{live['followers']} followers")
    token = ig.get("token") or {}
    if token.get("days_left") is not None:
        ig_bits.append(f"token {token['days_left']} days")
    if not ig.get("configured"):
        ig_bits.append("not connected")
    lines.append("📸 Instagram: " + (" · ".join(ig_bits) or "–"))
    q = snap.get("queue") or {}
    nxt = q.get("next")
    lines.append(
        "🗓 Next post: " + (f"{esc(_when(nxt['when']))} · {esc(nxt['id'])}" if nxt else "nothing approved yet")
    )
    counts = q.get("counts") or {}
    if counts:
        lines.append("📋 Queue: " + " · ".join(f"{v} {k}" for k, v in sorted(counts.items())))
    scores = (snap.get("seo") or {}).get("scores") or {}
    if scores:
        lines.append("🔎 SEO: " + " · ".join(f"{esc(k)} {v}" for k, v in scores.items()))
    attention = snap.get("attention") or []
    if attention:
        lines += ["", "<b>Needs you</b>"]
        icon = {"critical": "🔴", "warning": "🟠", "info": "🔵"}
        lines += [f"{icon.get(a['level'], '•')} {esc(a['text'])}" for a in attention[:8]]
    else:
        lines += ["", "Nothing needs you right now."]
    return "\n".join(lines) + _dashboard_line(settings)


def queue_text(settings: Settings, snap: dict[str, Any]) -> str:
    q = snap.get("queue") or {}
    lines = ["<b>Next posts</b> (two weeks)", ""]
    upcoming = q.get("upcoming") or []
    if not upcoming:
        lines.append("Nothing scheduled.")
    for c in upcoming:
        lines.append(f"{STATUS_ICON.get(c['status'], '•')} {esc(_when(c['when']))} · <code>{esc(c['id'])}</code>")
    recent = [c for c in q.get("recent") or [] if c.get("permalink")][:3]
    if recent:
        lines += ["", "<b>Recently posted</b>"]
        lines += [f'📸 <a href="{esc(c["permalink"])}">{esc(c["id"])}</a>' for c in recent]
    if any(c["status"] == "draft" for c in upcoming):
        lines += ["", "✏️ = draft, not going out until approved. <code>/approve all</code> approves every draft."]
    return "\n".join(lines)


def report_text(settings: Settings, snap: dict[str, Any]) -> str:
    lines = ["<b>Numbers</b>", ""]
    ig = snap.get("instagram") or {}
    live = ig.get("live") or {}
    week = ig.get("week") or {}
    if live.get("followers") is not None:
        lines.append(f"Followers now: <b>{live['followers']}</b>")
    if week:
        acc = week.get("account") or {}
        lines.append(f"Instagram, last {week.get('days', 7)} days: "
                     + (" · ".join(f"{esc(k)} {v}" for k, v in acc.items()) or "no account data yet"))
        if week.get("best"):
            lines.append(f"Best post: <code>{esc(week['best'])}</code>")
    else:
        lines.append("Instagram: the first weekly report comes on Monday.")
    seo = snap.get("seo") or {}
    search = seo.get("search") or {}
    if search:
        lines.append(
            f"Google (28 days): {search.get('impressions', 0)} impressions · {search.get('clicks', 0)} clicks"
            + (f" · avg. position {search['position']:.1f}" if isinstance(search.get("position"), int | float)
               and search.get("position") else "")
        )
    if seo.get("scores"):
        lines.append("SEO scores: " + " · ".join(f"{esc(k)} {v}" for k, v in seo["scores"].items()))
    for f in (seo.get("findings") or [])[:3]:
        lines.append(f"• {esc(f.get('title'))}")
    funnel = snap.get("funnel") or []
    if funnel:
        last = funnel[-1]
        lines.append(f"Applications {esc(last.get('week'))}: {last.get('applications', '–')}"
                     f" · interviews {last.get('interviews', '–')} · started {last.get('started', '–')}")
    return "\n".join(lines) + _dashboard_line(settings)


def brief_text(settings: Settings, snap: dict[str, Any]) -> str:
    m = snap.get("marketing") or {}
    if not m:
        return "No weekly plan yet. The next one is written on Friday morning."
    lines = [f"<b>Plan {esc(m.get('week'))}</b>", ""]
    if m.get("summary"):
        lines += [esc(m["summary"]), ""]
    if m.get("bottleneck") and m["bottleneck"] != "unknown":
        lines.append(f"Bottleneck: <b>{esc(m['bottleneck'])}</b>: {esc(m.get('bottleneck_reason') or '')}")
    if m.get("decisions"):
        lines += ["<b>Decisions for you</b>"] + [f"• {esc(d)}" for d in m["decisions"]] + [""]
    if m.get("reels"):
        lines += ["<b>Reels to film</b>"] + [f"🎬 {esc(r.get('title'))}" for r in m["reels"]] + [""]
    if m.get("posts_pr"):
        lines.append(f'<a href="{esc(m["posts_pr"])}">Next week\'s posts: review and merge</a>')
    if m.get("site_pr"):
        lines.append(f'<a href="{esc(m["site_pr"])}">Website improvements: review and merge</a>')
    link = _github(settings, m["brief"]) if m.get("brief") else None
    if link:
        lines.append(f'<a href="{esc(link)}">Full brief</a>')
    return "\n".join(lines)


# ---- actions ---------------------------------------------------------------------------------------------------------


def approve(settings: Settings, arg: str, now: datetime) -> tuple[str, list[str]]:
    """Approve drafts. Returns (reply, changed post ids). Every post must pass the same checks as on GitHub."""
    tz = settings.timezone
    posts, errors = load_queue(settings.posts_dir)
    drafts = sorted((p for p in posts if p.status == Status.DRAFT and p.scheduled_dt(tz) > now),
                    key=lambda p: p.scheduled_dt(tz))
    arg = arg.strip()
    if not drafts:
        return "There are no drafts waiting for approval.", []
    if not arg:
        lines = ["<b>Drafts waiting</b>", ""] + [
            f"✏️ {esc(p.scheduled_dt(tz).strftime('%a %d %b %H:%M'))} · <code>{esc(p.id)}</code>" for p in drafts[:15]
        ]
        lines += ["", "Send <code>/approve all</code>, <code>/approve next</code> or <code>/approve &lt;id&gt;</code>."]
        return "\n".join(lines), []
    if arg.lower() == "all":
        targets = drafts
    elif arg.lower() == "next":
        targets = drafts[:1]
    else:
        targets = [p for p in drafts if p.id == arg] or [p for p in drafts if p.id.startswith(arg)]
        if not targets:
            return f"No draft matches <code>{esc(arg)}</code>. Send /approve to see the list.", []
        if len(targets) > 1 and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", arg):
            listed = ", ".join(f"<code>{esc(p.id)}</code>" for p in targets[:10])
            return f"<code>{esc(arg)}</code> matches {len(targets)} drafts: {listed}. Send the full id.", []
    issues = validate_all(posts, errors, settings, now)
    changed: list[str] = []
    refused: list[str] = []
    for post in targets:
        errs = [i.message for i in issues if i.post_id == post.id and i.level == "error"]
        if errs:
            refused.append(f"❌ <code>{esc(post.id)}</code>: {esc(errs[0])}")
            continue
        post.status = Status.APPROVED
        post.result.attempts = 0
        post.result.last_error = None
        save_post(post)
        changed.append(post.id)
    lines = [f"✅ Approved <code>{esc(pid)}</code>" for pid in changed] + refused
    if changed:
        lines.append("\nSaved to GitHub in a moment. They go out at their scheduled time.")
    return "\n".join(lines), changed


def skip(settings: Settings, arg: str) -> tuple[str, list[str]]:
    arg = arg.strip()
    if not arg:
        return "Which post? <code>/skip &lt;post id&gt;</code> (see /queue).", []
    posts, _ = load_queue(settings.posts_dir)
    match = [p for p in posts if p.id == arg] or [p for p in posts if p.id.startswith(arg)]
    if len(match) != 1:
        return f"{'No post' if not match else 'More than one post'} matches <code>{esc(arg)}</code>.", []
    post = match[0]
    if post.status not in (Status.DRAFT, Status.APPROVED):
        return f"<code>{esc(post.id)}</code> is {esc(post.status.value)}; nothing to cancel.", []
    post.status = Status.SKIPPED
    save_post(post)
    return f"⏭ <code>{esc(post.id)}</code> won't be posted. (Set it to approved again on GitHub to undo.)", [post.id]


# ---- Claude ----------------------------------------------------------------------------------------------------------


def _read(settings: Settings, rel: str, limit: int) -> str:
    path = settings.repo_root / rel
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return ""
    return text if len(text) <= limit else text[:limit] + "\n[…]"


def compact_snapshot(snap: dict[str, Any]) -> str:
    """The snapshot without the bulky bits, for Claude's context."""
    slim = json.loads(json.dumps(snap, default=str))
    for card in (slim.get("queue") or {}).get("upcoming", []) + (slim.get("queue") or {}).get("recent", []):
        card.pop("image", None)
    search = (slim.get("seo") or {}).get("search") or {}
    for key in ("pages", "countries"):
        search.pop(key, None)
    return json.dumps(slim, ensure_ascii=False, separators=(",", ":"))[:24000]


class Brain:
    def __init__(self, settings: Settings, client: Any = None) -> None:
        from .marketing.brain import Spend

        self.settings = settings
        self.spend = Spend(limit=settings.bot_max_usd_per_run)
        if client is None:
            import anthropic

            client = anthropic.Anthropic(api_key=settings.anthropic_api_key, max_retries=2, timeout=120)
        self.client = client
        self._system: list[dict[str, Any]] | None = None

    def system(self) -> list[dict[str, Any]]:
        if self._system is None:
            s = self.settings
            text = AI_SYSTEM.format(
                rules=_read(s, "brand/rules.md", 4000),
                facts=_read(s, "brand/facts.yaml", 4000),
                playbook=_read(s, "brand/playbook.md", 16000),
            )
            self._system = [{"type": "text", "text": text, "cache_control": {"type": "ephemeral"}}]
        return self._system

    def answer(self, question: str, snap: dict[str, Any], replied_to: str | None) -> str:
        week = (snap.get("marketing") or {}).get("week")
        brief = _read(self.settings, f"reports/marketing/{week}.md", 6000) if week else ""
        context = f"Current state (JSON):\n{compact_snapshot(snap)}\n\nLatest weekly brief:\n{brief or '(none yet)'}"
        if replied_to:
            context += f"\n\nThe owner is replying to this earlier message of yours:\n{replied_to[:2000]}"
        prompt = f"{context}\n\nOwner's message:\n{question}"
        text, why = self._ask(self.system(), prompt)
        if not text:
            # Rare: Claude returned no text. Try once more with the short rules only (no playbook, no data dump).
            log.warning("Claude returned no text (%s); retrying with a short context", why)
            text, why = self._ask(self.short_system(), f"Owner's message:\n{question}")
        if not text:
            raise EmptyAnswer(why)
        return text

    def _ask(self, system: list[dict[str, Any]], prompt: str) -> tuple[str, str]:
        self.spend.check(0.05)
        msg = self.client.messages.create(
            model=self.settings.claude_model,
            max_tokens=MAX_AI_TOKENS,
            system=system,
            thinking={"type": "disabled"},  # short chat answers: all tokens go to the reply
            messages=[{"role": "user", "content": prompt}],
        )
        self.spend.add(self.settings.claude_model, getattr(msg, "usage", None), "telegram")
        text = "".join(getattr(b, "text", "") or "" for b in (msg.content or [])).strip()
        kinds = ",".join(str(getattr(b, "type", "?")) for b in (msg.content or [])) or "none"
        why = f"stop_reason={getattr(msg, 'stop_reason', None)}, blocks={kinds}"
        print(f"Claude: {why}, {len(text)} characters")
        return text, why

    def short_system(self) -> list[dict[str, Any]]:
        return [{"type": "text", "text": SHORT_SYSTEM.format(rules=_read(self.settings, "brand/rules.md", 4000))}]


class EmptyAnswer(RuntimeError):
    """Claude answered without any text (e.g. it declined)."""


def caption_warnings(settings: Settings, text: str) -> list[str]:
    """If Claude's answer contains a caption, flag what `bloom validate` would reject."""
    warnings = []
    banned = find_banned(text, settings.banned_words)
    if banned:
        warnings.append(f"contains words that get recruitment accounts restricted: {', '.join(banned)}")
    if count_hashtags(text) > 5:
        warnings.append(f"{count_hashtags(text)} hashtags (Instagram allows 5)")
    return warnings


# ---- the poll loop ----------------------------------------------------------------------------------------------------


COMMAND_RE = re.compile(r"^/(\w+)(?:@\w+)?(?:\s+(.*))?$", re.S)


class Session:
    """One run of the bot: lazy snapshot, lazy Claude, results."""

    def __init__(
        self, settings: Settings, tg: TelegramClient, now: datetime, *,
        snapshot: Callable[[], dict[str, Any]] | None = None, brain: Callable[[], Brain] | None = None,
    ) -> None:
        self.settings = settings
        self.tg = tg
        self.now = now
        self.result = PollResult()
        self._snapshot_factory = snapshot or (lambda: _live_snapshot(settings, now))
        self._snap: dict[str, Any] | None = None
        self._brain_factory = brain or (lambda: Brain(settings))
        self._brain: Brain | None = None

    @property
    def snap(self) -> dict[str, Any]:
        if self._snap is None:
            self._snap = self._snapshot_factory()
        return self._snap

    def refresh(self) -> None:
        self._snap = None

    def reply(self, chat_id: int | str, text: str, *, html_mode: bool = True, reply_to: int | None = None) -> None:
        try:
            self.tg.send(chat_id, text, html_mode=html_mode, reply_to=reply_to)
        except TelegramError as exc:
            self.result.notes.append(f"reply failed: {exc}")

    def handle(self, message: dict[str, Any]) -> None:
        chat_id = message["chat"]["id"]
        text = str(message.get("text") or "").strip()
        msg_id = message.get("message_id")
        if not text:
            self.reply(chat_id, "I can only read text messages for now.")
            return
        m = COMMAND_RE.match(text)
        if not m:
            self.ask(chat_id, text, message)
            return
        cmd, arg = m.group(1).lower(), (m.group(2) or "")
        s = self.settings
        if cmd in ("start", "help"):
            self.reply(chat_id, HELP)
        elif cmd == "status":
            self.tg.typing(chat_id)
            self.reply(chat_id, status_text(s, self.snap))
        elif cmd == "queue":
            self.reply(chat_id, queue_text(s, self.snap))
        elif cmd == "report":
            self.reply(chat_id, report_text(s, self.snap))
        elif cmd == "brief":
            self.reply(chat_id, brief_text(s, self.snap))
        elif cmd == "dashboard":
            self.reply(chat_id, _dashboard_line(s).strip() or "The dashboard address isn't known yet.")
        elif cmd == "approve":
            reply, changed = approve(s, arg, self.now)
            self.result.changed_posts += changed
            if changed:
                self.refresh()
            self.reply(chat_id, reply)
        elif cmd == "skip":
            reply, changed = skip(s, arg)
            self.result.changed_posts += changed
            if changed:
                self.refresh()
            self.reply(chat_id, reply)
        elif cmd == "ask":
            self.ask(chat_id, arg, message)
        else:
            self.reply(chat_id, f"I don't know /{esc(cmd)}.\n\n{HELP}", reply_to=msg_id)

    def ask(self, chat_id: int | str, question: str, message: dict[str, Any]) -> None:
        msg_id = message.get("message_id")
        if not question.strip():
            self.reply(chat_id, "Ask me anything after /ask, or just write a normal message.")
            return
        if not self.settings.anthropic_api_key:
            self.reply(chat_id, "To answer free questions I need Claude: add the ANTHROPIC_API_KEY secret on GitHub. "
                       "Until then, try /status, /queue, /report or /brief.")
            return
        if self.result.ai_replies >= MAX_AI_REPLIES_PER_RUN:
            self.reply(chat_id, "That's a lot of questions at once 🙂 I'll pick this up in a few minutes; please "
                       "send it again then.", reply_to=msg_id)
            return
        self.tg.typing(chat_id)
        replied = (message.get("reply_to_message") or {}).get("text")
        try:
            if self._brain is None:
                self._brain = self._brain_factory()
            answer = self._brain.answer(question, self.snap, replied)
        except Exception as exc:  # budget, network, API errors: tell the owner, don't crash the run
            from .marketing.brain import BudgetExceeded

            if isinstance(exc, BudgetExceeded):
                text = "I've reached this run's spending cap for Claude. Ask again in a few minutes."
            elif isinstance(exc, EmptyAnswer):
                text = ("Claude didn't give me an answer to that one. Try asking it a little differently, "
                        f"for example one question per message. ({exc})")
                log.warning("Claude gave no answer: %s", exc)
            else:
                text = f"Claude didn't answer ({type(exc).__name__}). Try again in a few minutes."
                log.warning("Claude error: %s", exc)
            self.reply(chat_id, text, html_mode=False, reply_to=msg_id)
            return
        warnings = caption_warnings(self.settings, answer)
        if warnings:
            answer += "\n\n⚠️ Before posting: " + "; ".join(warnings) + "."
        self.result.ai_replies += 1
        self.result.spend_usd = self._brain.spend.usd
        self.reply(chat_id, answer, html_mode=False, reply_to=msg_id)


def _live_snapshot(settings: Settings, now: datetime) -> dict[str, Any]:
    from .dashboard import live_fetcher

    factory = None
    if settings.ig_user_id and settings.ig_access_token:
        from .graph import GraphClient
        from .instagram import InstagramClient

        user_id, token = settings.require_instagram()

        def factory() -> InstagramClient:
            return InstagramClient(GraphClient(settings.ig_base, token), user_id)

    return build_snapshot(settings, now=now, fetcher=live_fetcher(), ig_factory=factory)


def poll(
    settings: Settings, tg: TelegramClient, *, now: datetime | None = None,
    snapshot: Callable[[], dict[str, Any]] | None = None, brain: Callable[[], Brain] | None = None,
) -> PollResult:
    now = now or datetime.now(settings.timezone)
    session = Session(settings, tg, now, snapshot=snapshot, brain=brain)
    owner = str(settings.telegram_chat_id or "")
    if not owner:
        session.result.notes.append("TELEGRAM_CHAT_ID isn't set yet: run the 'Connect Telegram' workflow first.")
        return session.result
    updates = tg.updates(timeout=0)
    if not updates:
        return session.result
    # Acknowledge first: if this run crashes halfway, a message is answered at most once, never twice.
    tg.acknowledge(max(int(u["update_id"]) for u in updates))
    for update in sorted(updates, key=lambda u: int(u["update_id"])):
        message = update.get("message")
        if not isinstance(message, dict) or "chat" not in message:
            continue
        if str(message["chat"].get("id")) != owner:
            session.result.ignored += 1
            continue
        session.result.handled += 1
        session.handle(message)
    return session.result


# ---- connecting ------------------------------------------------------------------------------------------------------


@dataclass
class ConnectResult:
    ok: bool
    message: str
    chat_id: int | None = None


def connect(
    settings: Settings, tg: TelegramClient, code: str,
    save_secret: Callable[[str, str, str, str], None] | None = None,
) -> ConnectResult:
    """Find the chat that sent `/start <code>` and save it as TELEGRAM_CHAT_ID."""
    code = code.strip()
    if len(code) < CONNECT_CODE_MIN:
        return ConnectResult(False, f"Choose a code of at least {CONNECT_CODE_MIN} characters.")
    updates = tg.updates(timeout=0, limit=100)
    wanted = {code.lower(), f"/start {code}".lower()}
    found: dict[str, Any] | None = None
    for update in sorted(updates, key=lambda u: int(u["update_id"])):
        message = update.get("message") or {}
        chat = message.get("chat") or {}
        text = re.sub(r"^/start@\w+", "/start", str(message.get("text") or "").strip()).lower()
        if chat.get("type") == "private" and text in wanted:
            found = message
    if found is None:
        return ConnectResult(
            False,
            f"No message '/start {code}' found. Open your bot in Telegram, send exactly `/start {code}`, then run this "
            "workflow again within 24 hours.",
        )
    chat = found["chat"]
    chat_id = int(chat["id"])
    who = chat.get("username") or chat.get("first_name") or "you"
    if not (settings.github_admin_token and settings.github_repository):
        return ConnectResult(False, "Found your chat, but GH_ADMIN_TOKEN is missing, so it can't be saved. "
                             f"Add a repository secret TELEGRAM_CHAT_ID with the value {chat_id}.", chat_id)
    if save_secret is None:
        from .github import set_actions_secret

        def save_secret(repo: str, token: str, name: str, value: str) -> None:
            set_actions_secret(repo, token, name, value)

    try:
        save_secret(settings.github_repository, settings.github_admin_token, "TELEGRAM_CHAT_ID", str(chat_id))
    except Exception as exc:
        return ConnectResult(False, f"Found your chat but couldn't save it: {exc}. Add a repository secret "
                             f"TELEGRAM_CHAT_ID with the value {chat_id}.", chat_id)
    tg.acknowledge(max(int(u["update_id"]) for u in updates))
    try:
        tg.send(chat_id, f"Connected ✅ Hi {esc(who)}! From now on I'll tell you here when posts go live, when "
                "something needs you, and when the weekly plan is ready.\n\n" + HELP)
    except TelegramError as exc:
        log.warning("welcome message failed: %s", exc)
    return ConnectResult(True, f"Connected to Telegram chat of {who}.", chat_id)
