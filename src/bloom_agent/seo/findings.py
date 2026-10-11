"""A finding is one thing the SEO checks noticed, with a plain-language fix."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal

Severity = Literal["critical", "warning", "info", "pass"]
SEVERITY_ORDER: dict[str, int] = {"critical": 0, "warning": 1, "info": 2, "pass": 3}
# Points lost per finding when computing the 0–100 score.
PENALTY: dict[str, int] = {"critical": 25, "warning": 6, "info": 1, "pass": 0}


@dataclass(frozen=True)
class Finding:
    area: str  # "Website", "Speed", "Google Search", "Instagram", "AI search"
    severity: Severity
    code: str  # stable id, e.g. "title-length"
    title: str
    detail: str = ""
    fix: str = ""
    page: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class FindingList:
    items: list[Finding] = field(default_factory=list)

    def add(
        self, area: str, severity: Severity, code: str, title: str, detail: str = "", fix: str = "", page: str = ""
    ) -> None:
        self.items.append(Finding(area, severity, code, title, detail, fix, page))

    def ok(self, area: str, code: str, title: str, page: str = "") -> None:
        self.add(area, "pass", code, title, page=page)

    def extend(self, other: FindingList | list[Finding]) -> None:
        self.items.extend(other.items if isinstance(other, FindingList) else other)

    def problems(self) -> list[Finding]:
        return sorted((f for f in self.items if f.severity != "pass"), key=lambda f: (SEVERITY_ORDER[f.severity], f.area))

    def by_severity(self, severity: Severity) -> list[Finding]:
        return [f for f in self.items if f.severity == severity]

    def score(self, area: str | None = None) -> int:
        items = [f for f in self.items if area is None or f.area == area]
        return max(0, 100 - sum(PENALTY[f.severity] for f in items))
