"""Three-way-ish merge for post.yaml when the agent and a person edited the same file at the same time.

Used by scripts/commit-back.sh only when a rebase fails. The person's version is the base; the agent contributes
only what it owns:
- `result` (attempts, errors, container ids, links) always comes from the agent;
- `status` comes from the agent only when the agent moved it to an outcome (published/failed/missed),
  so a person's switch to `draft` is never undone by the agent writing `approved` back;
- `media` comes from the agent when it converted files (the person's list points to files that no longer exist).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

AGENT_OUTCOMES = {"published", "failed", "missed"}


def merge_post_yaml(ours_text: str, theirs_text: str, folder: Path) -> str:
    ours: dict[str, Any] = yaml.safe_load(ours_text) or {}
    theirs: dict[str, Any] = yaml.safe_load(theirs_text) or {}
    merged = dict(theirs)
    if "result" in ours:
        merged["result"] = ours["result"]
    if ours.get("status") in AGENT_OUTCOMES:
        merged["status"] = ours["status"]
    their_media = theirs.get("media") or []
    our_media = ours.get("media") or []
    if our_media and their_media != our_media:
        theirs_missing = any(not (folder / m).exists() for m in their_media)
        ours_present = all((folder / m).exists() for m in our_media)
        if theirs_missing and ours_present:
            merged["media"] = our_media
    return yaml.safe_dump(merged, sort_keys=False, allow_unicode=True, width=100)
