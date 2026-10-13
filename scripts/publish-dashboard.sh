#!/usr/bin/env bash
# Publishes the dashboard (dashboard/index.html + fonts + the data file already in $1) to the gh-pages branch,
# replacing what was there. GitHub Pages serves that branch. Nothing is committed to main.
set -euo pipefail

out="${1:-_site}"
mkdir -p "$out/fonts"
cp dashboard/index.html "$out/"
for f in fraunces-latin-opsz-normal plus-jakarta-sans-latin-400-normal plus-jakarta-sans-latin-600-normal \
         plus-jakarta-sans-latin-ext-400-normal plus-jakarta-sans-latin-ext-600-normal; do
  cp "site/src/static/fonts/$f.woff2" "$out/fonts/"
done
cp site/src/static/images/favicon-32.png site/src/static/images/favicon-180.png "$out/"
printf 'User-agent: *\nDisallow: /\n' > "$out/robots.txt"
touch "$out/.nojekyll"

cd "$out"
git init -q -b gh-pages
git config user.name "bloom-agent[bot]"
git config user.email "41898282+github-actions[bot]@users.noreply.github.com"
git add -A
git commit -qm "Dashboard $(date -u +%Y-%m-%dT%H:%MZ)"
git push -fq "https://x-access-token:${GITHUB_TOKEN}@github.com/${GITHUB_REPOSITORY}.git" gh-pages
echo "Published the dashboard to the gh-pages branch."
