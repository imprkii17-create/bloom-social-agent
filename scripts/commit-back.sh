#!/usr/bin/env bash
# Commits files the agent changed (post status, converted images, reports) back to the repository.
# Safe to run when nothing changed. On a push race it rebases; if that fails it re-applies ONLY the files the
# agent itself changed on top of the latest branch, so a user's own commits are never undone.
set -euo pipefail

message="${1:-bloom-agent: update}"
shift || true
paths=("$@")
if [ ${#paths[@]} -eq 0 ]; then paths=(posts state reports); fi

git config user.name "bloom-agent[bot]"
git config user.email "41898282+github-actions[bot]@users.noreply.github.com"

existing=()
for p in "${paths[@]}"; do [ -e "$p" ] && existing+=("$p"); done
if [ ${#existing[@]} -eq 0 ]; then echo "Nothing to commit."; exit 0; fi

git add -A -- "${existing[@]}"
if git diff --cached --quiet; then
  echo "Nothing to commit."
  exit 0
fi
git commit -m "$message" --quiet

# Remember exactly what the agent changed in this commit (content of changed files, list of deleted ones).
stash="$(mktemp -d)"
trap 'rm -rf "$stash"' EXIT
git diff -z --name-only --no-renames HEAD~1 HEAD > "$stash/.changed"
while IFS= read -r -d '' f; do
  if [ -e "$f" ]; then
    mkdir -p "$stash/files/$(dirname "$f")"
    cp -p "$f" "$stash/files/$f"
  fi
done < "$stash/.changed"

branch="$(git rev-parse --abbrev-ref HEAD)"
for attempt in 1 2 3 4 5; do
  if git push --quiet origin "HEAD:$branch"; then
    echo "Pushed: $message"
    exit 0
  fi
  echo "Push rejected (attempt $attempt); updating from the latest $branch…"
  sleep $((attempt * 3))
  git fetch --quiet origin "$branch"
  # Same lines edited by both sides: the agent's state wins ("theirs" is the replayed commit during a rebase).
  if git rebase --quiet -X theirs "origin/$branch"; then
    continue
  fi
  git rebase --abort || true
  echo "Rebase failed; re-applying only the agent's own files on top of $branch" >&2
  git reset --quiet --hard "origin/$branch"
  while IFS= read -r -d '' f; do
    if [ -e "$stash/files/$f" ]; then
      mkdir -p "$(dirname "$f")"
      if [ "$(basename "$f")" = "post.yaml" ] && [ -e "$f" ] && command -v bloom >/dev/null 2>&1; then
        # Both sides edited this post: keep the person's edits, add only the agent's result/outcome.
        bloom merge-state "$stash/files/$f" "$f"
      else
        cp -p "$stash/files/$f" "$f"
      fi
    else
      rm -f -- "$f"
    fi
    git add -A -- "$f"
  done < "$stash/.changed"
  if git diff --cached --quiet; then
    echo "Nothing left to commit after updating."
    exit 0
  fi
  git commit -m "$message" --quiet
done
echo "Could not push after 5 attempts" >&2
exit 1
