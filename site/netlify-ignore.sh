#!/usr/bin/env bash
# Netlify runs this before each build. Exit 0 = skip the build, exit 1 = build and deploy.
# Production deploys cost credits on the free plan, so production only deploys when the built website
# (site/public) changed. Deploy Previews for pull requests are free and always build.
if [ "${CONTEXT:-}" != "production" ]; then exit 1; fi
if [ -z "${CACHED_COMMIT_REF:-}" ] || [ "${CACHED_COMMIT_REF}" = "${COMMIT_REF:-}" ]; then exit 1; fi
if git diff --quiet "$CACHED_COMMIT_REF" "$COMMIT_REF" -- public; then exit 0; fi
exit 1
