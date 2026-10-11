"""Puts agent proposals on their own branch (for a pull request) without touching the main branch."""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path

log = logging.getLogger(__name__)

BOT_NAME = "bloom-agent[bot]"
BOT_EMAIL = "41898282+github-actions[bot]@users.noreply.github.com"


class GitError(RuntimeError):
    pass


class BranchHasHumanEdits(GitError):
    """Someone (not the agent) committed to the proposal branch; the agent won't overwrite their work."""


def _git(root: Path, *args: str) -> str:
    try:
        out = subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, timeout=120, check=True)
    except subprocess.CalledProcessError as exc:
        raise GitError(f"git {' '.join(args)} failed: {exc.stderr.strip()[:400]}") from exc
    return out.stdout.strip()


def current_branch(root: Path) -> str:
    return _git(root, "rev-parse", "--abbrev-ref", "HEAD")


def human_commits_on_remote(root: Path, branch: str) -> list[str]:
    """Commits on origin/<branch> that are not on the current branch and weren't made by the agent."""
    if not _git(root, "ls-remote", "--heads", "origin", branch):
        return []
    _git(root, "fetch", "-q", "origin", f"+refs/heads/{branch}:refs/remotes/origin/{branch}")
    log_out = _git(root, "log", "--format=%ae %h %s", f"HEAD..origin/{branch}")
    return [line for line in log_out.splitlines() if line and not line.startswith(BOT_EMAIL + " ")]


def commit_to_branch(root: Path, branch: str, paths: list[str], message: str, *, push: bool = True) -> bool:
    """Commits the working-tree changes under `paths` to `branch` (created from the current commit), pushes it,
    and switches back. Returns False when there was nothing to commit. Main stays exactly as it was.

    Raises BranchHasHumanEdits instead of overwriting a branch someone has edited (e.g. deleted a post in the PR)."""
    paths = [p for p in paths if (root / p).exists()]
    if not paths:
        return False
    if push:
        edits = human_commits_on_remote(root, branch)
        if edits:
            raise BranchHasHumanEdits(
                f"{branch} has your edits ({len(edits)} commit(s)); left it unchanged. Merge or close that pull "
                "request and the agent will propose again next week."
            )
    home = current_branch(root)
    _git(root, "checkout", "-q", "-B", branch)
    committed = False
    failed = True
    try:
        _git(root, "add", "-A", "--", *paths)
        if subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=root).returncode == 0:
            failed = False
            return False
        _git(root, "-c", f"user.name={BOT_NAME}", "-c", f"user.email={BOT_EMAIL}", "commit", "-q", "-m", message)
        committed = True
        if push:
            _git(root, "push", "--force", "-q", "origin", f"{branch}:{branch}")
        failed = False
        return True
    finally:
        # Back to the original branch. Committed proposals disappear from the working tree with it; anything
        # half-done under `paths` is thrown away. Other uncommitted work (reports, state) is left alone.
        _git(root, "checkout", "-q", home)
        if failed and not committed:
            for args in (("reset", "-q", "--", *paths), ("checkout", "-q", "--", *paths)):
                try:
                    _git(root, *args)
                except GitError as exc:  # e.g. a path that doesn't exist on the original branch
                    log.info("cleanup: %s", exc)
        _git(root, "clean", "-q", "-fd", "--", *paths)
