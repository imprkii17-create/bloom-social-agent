"""brand/facts.yaml: every promise and number Bloom Models makes in public. Text is checked against it so nothing
exaggerated or inconsistent goes out (a different earnings figure on Instagram than on the website, say).

Numbers are compared together with their unit: "80%" is an agreed fact, "80 €" is not; "2 perc" is, "2 millió Ft"
is not. A promise of guaranteed income is always flagged, and earnings figures need the disclaimer next to them.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

_NUM_RE = re.compile(
    r"(?<![\w#/.,:])"  # not part of a word, hashtag, URL path, decimal or time
    r"(\d{1,3}(?:[  .,]\d{3})+|\d+)"  # 2 000 / 2.000 / 2,000 / 2000
    r"(?:[.,]\d+)?"  # decimals
)
_SKIP_RE = re.compile(
    r"\b\d{1,2}:\d{2}\b"  # 18:30
    r"|\b\d+/\d+\b"  # 24/7
    r"|https?://\S+"  # links
    r"|#\w+"  # hashtags
    r"|@\w+"  # handles
)
_LIST_NUMBER_RE = re.compile(r"(?m)^\s*\d{1,2}[.)]\s")
_KEYCAP_RE = re.compile(r"\d️?⃣")  # 3️⃣

# What follows (or precedes) a number decides what kind of number it is.
_MULT_RE = re.compile(r"\s?(?:(k)\b|(millió|million|mio\b|m\b)|(ezer|thousand|e\b))", re.IGNORECASE)
_UNIT_RE = {
    "money": re.compile(r"\s?(?:€|\$|ft\b|huf\b|eur\b|euró|euro|forint|dollár|dollar)", re.IGNORECASE),
    "percent": re.compile(r"\s?(?:%|százalék|percent)", re.IGNORECASE),
    "time": re.compile(
        r"\s?(?:óra|órá|órás|hours?|hrs?\b|h\b|perc|minutes?|mins?\b|nap|days?|hét|het\b|weeks?|hónap|months?)",
        re.IGNORECASE,
    ),
}
_MONEY_BEFORE_RE = re.compile(r"(?:€|\$)\s?$")
_SPEC_RE = re.compile(r"^\s*([\d\s.,]+)\s*(k)?\s*(€|%|h|min|mo)?\s*$", re.IGNORECASE)
_SPEC_UNIT = {"€": "money", "%": "percent", "h": "time", "min": "time", "mo": "time"}

_GUARANTEE_RE = re.compile(r"\b(garant\w*|guarantee\w*)", re.IGNORECASE)
_NEGATION_RE = re.compile(r"(?:\bnem|\bsem|\bnot|n't|\bnever|\bno)\s+(?:\w+\s+){0,2}$", re.IGNORECASE)
_EARNINGS_WORDS_RE = re.compile(r"\b(keres\w*|bevétel\w*|fizet\w*|pénz\w*|income|earn\w*|pay\w*|salary)", re.IGNORECASE)


@dataclass(frozen=True)
class Number:
    value: float
    unit: str | None  # "money" | "percent" | "time" | None


@dataclass(frozen=True)
class Claim:
    id: str
    en: str
    hu: str
    numbers: tuple[Number, ...]
    needs_disclaimer: bool


@dataclass
class Facts:
    claims: list[Claim]
    disclaimer_en: str
    disclaimer_hu: str
    disclaimer_patterns: tuple[re.Pattern[str], ...]
    always_ok: set[Number] = field(default_factory=set)

    @property
    def allowed(self) -> set[Number]:
        out = set(self.always_ok)
        for c in self.claims:
            out.update(c.numbers)
        return out

    @property
    def disclaimer_numbers(self) -> set[Number]:
        return {n for c in self.claims if c.needs_disclaimer for n in c.numbers}

    def is_allowed(self, n: Number) -> bool:
        allowed = self.allowed
        if n in allowed:
            return True
        # A bare number ("havi 2000 felett") is fine if it matches an agreed figure of any kind.
        return n.unit is None and any(a.value == n.value for a in allowed)

    def needs_disclaimer(self, n: Number) -> bool:
        return any(d.value == n.value and (n.unit in (None, d.unit)) for d in self.disclaimer_numbers)

    def has_disclaimer(self, text: str) -> bool:
        return any(p.search(text) for p in self.disclaimer_patterns)

    def as_prompt(self) -> str:
        """The facts in a form Claude can follow when writing."""
        lines = [
            f"- {c.id}: EN “{c.en}” / HU “{c.hu}”" + (" (always add the disclaimer)" if c.needs_disclaimer else "")
            for c in self.claims
        ]
        lines.append(f"- disclaimer: EN “{self.disclaimer_en}” / HU “{self.disclaimer_hu}”")
        lines.append("- never promise or imply guaranteed income")
        return "\n".join(lines)


@dataclass(frozen=True)
class FactIssue:
    kind: str  # "unknown-number" | "missing-disclaimer" | "guarantee"
    message: str


def _to_float(raw: str) -> float | None:
    s = raw.strip().replace(" ", " ")
    s = re.sub(r"[ .,](?=\d{3}\b)", "", s)
    try:
        return float(s.replace(",", "."))
    except ValueError:
        return None


def _fmt(v: float) -> str:
    return str(int(v)) if float(v).is_integer() else str(v)


def normalise(raw: str) -> str:
    """'2 000', '2.000', '2,000', '2k', '80%' → '2000', '2000', '2000', '2000', '80'."""
    s = raw.strip().lower().rstrip("%+€ ")
    mult = 1000.0 if s.endswith("k") else 1.0
    v = _to_float(s.rstrip("k"))
    return s if v is None else _fmt(v * mult)


def parse_spec(spec: str) -> Number:
    """facts.yaml numbers: '2000€', '80%', '72h', '2min', '12mo', '18'."""
    m = _SPEC_RE.match(str(spec))
    if not m:
        raise ValueError(f"facts.yaml: can't read number '{spec}' (use e.g. 2000€, 80%, 72h, 2min, 12mo, 18)")
    v = _to_float(m.group(1))
    if v is None:
        raise ValueError(f"facts.yaml: can't read number '{spec}'")
    if m.group(2):
        v *= 1000
    unit = _SPEC_UNIT.get((m.group(3) or "").lower()) if m.group(3) else None
    return Number(v, unit)


def load_facts(repo_root: Path) -> Facts:
    path = repo_root / "brand" / "facts.yaml"
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    claims = [
        Claim(
            id=str(c["id"]),
            en=str(c.get("en", "")),
            hu=str(c.get("hu", "")),
            numbers=tuple(parse_spec(str(n)) for n in c.get("numbers") or []),
            needs_disclaimer=bool(c.get("needs_disclaimer", False)),
        )
        for c in data.get("claims") or []
    ]
    disc = data.get("disclaimer") or {}
    return Facts(
        claims=claims,
        disclaimer_en=str(disc.get("en", "")),
        disclaimer_hu=str(disc.get("hu", "")),
        disclaimer_patterns=tuple(re.compile(str(p), re.IGNORECASE) for p in disc.get("patterns") or []),
        always_ok={parse_spec(str(n)) for n in data.get("always_ok") or []},
    )


def _classify(text: str, start: int, end: int) -> tuple[float, str | None, int]:
    """Multiplier and unit of the number at text[start:end]; returns (multiplier, unit, end of the unit)."""
    mult, pos = 1.0, end
    m = _MULT_RE.match(text, pos)
    if m:
        mult = 1_000.0 if (m.group(1) or m.group(3)) else 1_000_000.0
        pos = m.end()
    for unit, rx in _UNIT_RE.items():
        u = rx.match(text, pos)
        if u:
            return mult, unit, u.end()
    if _MONEY_BEFORE_RE.search(text[:start]):
        return mult, "money", pos
    return mult, None, pos


def numbers_with_units(text: str) -> list[tuple[Number, str]]:
    """Every number that states something, with its unit and the text around it."""
    cleaned = _KEYCAP_RE.sub(" ", text)
    cleaned = _SKIP_RE.sub(" ", cleaned)
    cleaned = _LIST_NUMBER_RE.sub(" ", cleaned)
    found: list[tuple[Number, str]] = []
    for m in _NUM_RE.finditer(cleaned):
        base = _to_float(m.group(0))
        if base is None:
            continue
        mult, unit, unit_end = _classify(cleaned, m.start(), m.end())
        value = base * mult
        if value < 10 and unit is None and mult == 1:
            continue  # "4 tévhit", "3 lépés": counting, not a claim
        if unit is None and mult == 1 and value.is_integer() and 2010 <= value <= 2039:
            continue  # a year
        context = " ".join(cleaned[max(0, m.start() - 30) : min(len(cleaned), unit_end + 30)].split())
        found.append((Number(value, unit), context))
    return found


def numbers_in(text: str) -> list[tuple[str, str]]:
    """Compatibility helper: (normalised value, context)."""
    return [(_fmt(n.value), ctx) for n, ctx in numbers_with_units(text)]


def check_text(text: str, facts: Facts) -> list[FactIssue]:
    issues: list[FactIssue] = []
    seen: set[Number] = set()
    needs = False
    for n, context in numbers_with_units(text):
        if facts.needs_disclaimer(n):
            needs = True
        if facts.is_allowed(n) or n in seen:
            continue
        seen.add(n)
        unit = {"money": " (money)", "percent": "%", "time": " (time)"}.get(n.unit or "", "")
        issues.append(
            FactIssue(
                "unknown-number",
                f"“{_fmt(n.value)}{unit}” (…{context}…) isn't in brand/facts.yaml. Use an agreed figure, or add it "
                "to facts.yaml if it is true.",
            )
        )
    for m in _GUARANTEE_RE.finditer(text):
        before = text[max(0, m.start() - 30) : m.start()]
        if _NEGATION_RE.search(before):
            continue
        window = text[max(0, m.start() - 120) : m.end() + 120]
        if _EARNINGS_WORDS_RE.search(window) or any(facts.needs_disclaimer(n) for n, _ in numbers_with_units(window)):
            issues.append(
                FactIssue("guarantee", f"Promises guaranteed income (“{m.group(0)}”). Earnings are never guaranteed.")
            )
            break
    if needs and not facts.has_disclaimer(text):
        issues.append(
            FactIssue(
                "missing-disclaimer",
                f"Mentions earnings without the disclaimer. Add: “{facts.disclaimer_hu}” / “{facts.disclaimer_en}”",
            )
        )
    return issues
