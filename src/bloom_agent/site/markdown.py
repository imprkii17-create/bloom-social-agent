"""A small Markdown renderer for the guide pages (site/src/guides/*.md).

Only what the guides need, so there is no extra dependency and the output is predictable:
## and ### headings, paragraphs, "-" lists, "1." lists, "> " callouts, | tables |, **bold**, *italic* and
[links](/path/). Text is HTML-escaped; there is no raw HTML.
"""

from __future__ import annotations

import html
import re

_LINK_RE = re.compile(r"\[([^\]]+)\]\(([^)\s]+)\)")
_BOLD_RE = re.compile(r"\*\*(.+?)\*\*")
_ITALIC_RE = re.compile(r"(?<![*\w])\*(?!\s)(.+?)(?<!\s)\*(?![*\w])")
_HEADING_RE = re.compile(r"^(#{2,3})\s+(.+?)\s*$")
_UL_RE = re.compile(r"^[-*]\s+(.+)$")
_OL_RE = re.compile(r"^\d+[.)]\s+(.+)$")
_TABLE_SEP_RE = re.compile(r"^\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)*\|?$")


def slugify(text: str) -> str:
    table = str.maketrans("áéíóöőúüűÁÉÍÓÖŐÚÜŰ", "aeiooouuuAEIOOOUUU")
    s = re.sub(r"[^a-z0-9]+", "-", text.translate(table).lower()).strip("-")
    return s[:60] or "section"


def inline(text: str) -> str:
    """Escape, then apply links, bold and italic."""
    out = html.escape(text, quote=False)

    def link(m: re.Match[str]) -> str:
        href = html.unescape(m.group(2))
        external = href.startswith(("http://", "https://"))
        attrs = ' rel="noopener" target="_blank"' if external else ""
        return f'<a href="{html.escape(href)}"{attrs}>{m.group(1)}</a>'

    out = _LINK_RE.sub(link, out)
    out = _BOLD_RE.sub(r"<strong>\1</strong>", out)
    out = _ITALIC_RE.sub(r"<em>\1</em>", out)
    return out


def render(md: str) -> tuple[str, list[tuple[str, str]]]:
    """Returns (html, table of contents as [(id, heading text)] for the ## headings)."""
    lines = md.replace("\r\n", "\n").split("\n")
    out: list[str] = []
    toc: list[tuple[str, str]] = []
    used: set[str] = set()
    para: list[str] = []
    i = 0

    def flush_para() -> None:
        if para:
            out.append(f"<p>{inline(' '.join(para))}</p>")
            para.clear()

    while i < len(lines):
        line = lines[i].rstrip()
        stripped = line.strip()
        if not stripped:
            flush_para()
            i += 1
            continue
        m = _HEADING_RE.match(stripped)
        if m:
            flush_para()
            level = len(m.group(1))
            text = m.group(2)
            if level == 2:
                hid = slugify(text)
                n = 2
                base = hid
                while hid in used:
                    hid = f"{base}-{n}"
                    n += 1
                used.add(hid)
                toc.append((hid, text))
                out.append(f'<h2 id="{hid}">{inline(text)}</h2>')
            else:
                out.append(f"<h3>{inline(text)}</h3>")
            i += 1
            continue
        if _UL_RE.match(stripped) or _OL_RE.match(stripped):
            flush_para()
            ordered = bool(_OL_RE.match(stripped))
            rx = _OL_RE if ordered else _UL_RE
            items: list[str] = []
            while i < len(lines) and rx.match(lines[i].strip()):
                item = rx.match(lines[i].strip())
                assert item is not None
                items.append(f"<li>{inline(item.group(1))}</li>")
                i += 1
            tag = "ol" if ordered else "ul"
            out.append(f"<{tag}>" + "".join(items) + f"</{tag}>")
            continue
        if stripped.startswith(">"):
            flush_para()
            quote: list[str] = []
            while i < len(lines) and lines[i].strip().startswith(">"):
                quote.append(lines[i].strip()[1:].strip())
                i += 1
            paras: list[list[str]] = [[]]
            for q in quote:
                if q:
                    paras[-1].append(q)
                elif paras[-1]:
                    paras.append([])
            body_html = "".join(f"<p>{inline(' '.join(p))}</p>" for p in paras if p)
            out.append(f'<aside class="callout">{body_html}</aside>')
            continue
        if stripped.startswith("|") and i + 1 < len(lines) and _TABLE_SEP_RE.match(lines[i + 1].strip()):
            flush_para()
            head = _cells(stripped)
            i += 2
            rows: list[list[str]] = []
            while i < len(lines) and lines[i].strip().startswith("|"):
                rows.append(_cells(lines[i].strip()))
                i += 1
            th = "".join(f'<th scope="col">{inline(c)}</th>' for c in head)
            body = "".join("<tr>" + "".join(f"<td>{inline(c)}</td>" for c in r) + "</tr>" for r in rows)
            out.append(f'<div class="table-wrap"><table><thead><tr>{th}</tr></thead><tbody>{body}</tbody></table></div>')
            continue
        para.append(stripped)
        i += 1
    flush_para()
    return "\n".join(out), toc


def _cells(row: str) -> list[str]:
    return [c.strip() for c in row.strip().strip("|").split("|")]
