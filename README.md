# Bloom Models — marketing agent

Your head of marketing, running for free on GitHub Actions:

- **Publishes** approved Instagram posts at the right time, safely (never twice, never late, never off-brand).
- **Runs the SEO**: bloommodels.hu (Hungarian and English pages, structured data, speed), Google Search Console,
  Instagram search, and visibility in AI assistants (ChatGPT, Claude, Perplexity).
- **Plans every week**: a brief with the bottleneck and decisions for you, next week's posts with branded graphics
  (as a pull request: merging approves them), Reels and Stories ideas, experiments, and data-backed website
  improvements (as a pull request with a Netlify preview).
- **Watches**: daily website health, weekly Instagram report, token renewal, alerts as GitHub issues.
- **Reports to you in one place**: a live **dashboard** (GitHub Pages) and a **Telegram bot** that messages you
  when posts go live or something breaks, takes commands (`/status`, `/queue`, `/approve`) and, with Claude,
  answers questions.

```
Friday   ─► SEO report + brief + next week's posts (PR) + website ideas (PR)    you: review, merge
Every day ─► posts go out at 08:30, 13:00 and 19:30, results saved back into each post            you: reply to DMs, log applications
Daily    ─► website health check (alert if down, paused, not indexable)
Monday   ─► Instagram report, token renewal
```

**First time?** [SETUP.md](SETUP.md). **How the agent thinks:** [brand/playbook.md](brand/playbook.md).

---

## What runs when

| Workflow | When | What it does |
|---|---|---|
| **Publish** | every 15 min | Publishes approved posts whose time has come. Never two at once, never twice. |
| **Weekly marketing plan** | Fridays | SEO report, brief, next week's posts (PR), website improvements (PR). |
| **Website health** | daily | Live site up, HTTPS, indexable, not paused by Netlify. Opens an issue if not. |
| **Weekly report** | Mondays | Instagram reach, saves, shares, profile visits per post. |
| **Refresh Instagram token** | Mondays | Renews the 60-day token so it never expires. |
| **CI** | every push / PR | Tests, converts images, validates posts, builds and SEO-checks the website. |
| **Check connection** | by hand | Confirms the Instagram account, quota and settings. |
| **Dashboard** | after each publish run, hourly | Refreshes the dashboard page (branch `gh-pages`). |
| **Telegram bot** | every 5 min | Answers your Telegram messages; saves approvals made there. |
| **Connect Telegram** | by hand, once | Links your Telegram chat to the bot. |

## Safety built in

- Only `status: approved` posts are published (merging the weekly posts PR approves them), only after
  `scheduled_at`, and a checkpoint commit before publishing makes double posts impossible.
- A post more than 3 hours late is marked `missed`, not posted at a random time. Failures retry 3×, then alert you.
- Every caption is checked: max 5 hashtags, no words that get recruitment accounts restricted (also in Hungarian),
  18+, and every number against **[brand/facts.yaml](brand/facts.yaml)** — the single list of promises and figures
  we make in public. Earnings always come with the disclaimer.
- Posts the agent writes itself are held to stricter rules (no engagement bait, graphics checked too, only
  Instagram-approved photos); anything that fails is dropped, never "mostly fine".
- Website changes by the agent are limited to titles, descriptions and FAQ text, rebuilt and SEO-checked; a change
  that makes the check worse is thrown away. Everything reaches the site only through a pull request you merge.
- Claude spending is capped per run (default $2). Tokens never appear in logs, files or messages.

## The website

`site/src` is the source (the page template, `i18n/hu.json` + `en.json`, `site.yaml`), `site/public` is what
Netlify serves. `bloom site build` turns one into the other:

- **Separate Hungarian (`/`) and English (`/en/`) pages** with hreflang, so Google indexes both languages
  (before, Google could only ever see one language).
- Titles and descriptions per language with the target keywords; canonical URLs; social cards with a 1200×630 image.
- Structured data: Organization, WebSite, WebPage, **FAQPage**, and the **JobPosting** for Google for Jobs (left out
  automatically once `valid_through` has passed; the agent warns you 30 days before).
- A **FAQ section** in both languages: answers what applicants ask, and what AI assistants quote.
- Responsive **WebP** images with sizes (no layout shift), lazy loading, the hero image prioritised.
- **Self-hosted fonts** (faster, and no visitor data sent to Google Fonts), `sitemap.xml` with language versions,
  `robots.txt` that welcomes AI search crawlers, `llms.txt`, a bilingual `404.html`, security and cache headers.
- Fixes a bug in the old page: the photo-upload label and the form's "sending…/received" messages showed nothing.

## The weekly plan

With an `ANTHROPIC_API_KEY`, Claude acts as head of marketing: it reads the playbook, the facts, the week's
Instagram numbers, Search Console queries, SEO findings, your funnel numbers and running experiments, does a little
web research (Instagram changes, Hungarian calendar, whether AI search mentions Bloom), and writes the plan as
structured data. Posts that break a rule get one repair round, then are dropped.

Without a key, the plan is rule-based and the posts come from **[marketing/library.yaml](marketing/library.yaml)**
(12 evergreen bilingual posts, least recently used first). Everything else works the same.

**Your part, 10 minutes a week:** merge (or trim) the posts PR; tick the *Decisions for you* in the brief; once a
week add your application numbers to **[marketing/funnel.yaml](marketing/funnel.yaml)**. That's how the agent
learns what actually brings applicants, not just likes.

## Daily use

All of this works in the GitHub website or app.

- **Approve a post:** `posts/<post>/post.yaml` → pencil → `status: approved` → Commit. (Or merge the weekly PR.)
- **Change the time:** edit `scheduled_at: 2026-10-19 18:30` (Budapest time).
- **Change a fact** (e.g. the earnings figure): `brand/facts.yaml` first, then the website text and posts.
- **Edit website text:** `site/src/i18n/hu.json` / `en.json`. CI rebuilds, Netlify publishes.

### Add a post by hand

```
posts/2026-11-02-my-topic/
├── post.yaml      # see below
├── caption.txt    # the caption (our copy-paste caption files work as they are)
└── image.png      # PNG or JPEG; CI converts it to JPEG automatically
```

```yaml
id: 2026-11-02-my-topic        # must match the folder name
status: draft                  # draft → approved → published
type: image                    # image | carousel | reel | story
platforms: [instagram]         # add facebook once the Facebook page is set up
scheduled_at: 2026-11-02 18:30 # Budapest time
media: [image.png]             # carousel: 2–10 files; reel: one .mp4
caption_file: caption.txt
alt_text: Short description of the image for blind users
```

Need a graphic? `bloom graphic --template statement --eyebrow "GYIK" --headline "Nem budapesti vagy?" --accent
"Megoldjuk." --out image.jpg` (layouts: `statement`, `list`, `photo`, `light`).

### Status values
| status | meaning |
|---|---|
| `draft` | Being worked on. Never published. |
| `approved` | Will be published at `scheduled_at`. |
| `published` | Done. `result:` shows the Instagram link. |
| `failed` | Gave up after 3 tries. Read `result.last_error`, fix it, set `approved` again. |
| `missed` | Its time passed while the agent couldn't run. Change `scheduled_at`, set `approved` again. |
| `skipped` | Kept for the record, never published. |

## Costs

| Item | Cost |
|---|---|
| GitHub (public repository, Actions) | $0 |
| Netlify free plan | $0 (deploys only when the built site changes; about 1–5 deploys a month) |
| Google Search Console, PageSpeed | $0 |
| GitHub Pages (dashboard), Telegram | $0 |
| Claude (optional) | about $2–6 a month on `claude-sonnet-5-5` incl. the bot; capped per run |

## Commands (for running locally)

```bash
pip install -e ".[dev,ai]"
bloom validate              # check every post
bloom prepare               # convert images to Instagram-ready JPEG
bloom status                # show the queue
bloom approve <post-id>     # mark a post as approved
bloom publish --dry-run     # show what would be posted now
bloom site build            # build site/public from site/src (--check: only report)
bloom seo                   # full SEO report (--quick: health only, --offline: built files)
bloom marketing --no-pr     # weekly plan; drafts stay in posts/ as drafts
bloom graphic --headline …  # draw one branded graphic
bloom check                 # test the Instagram connection (needs .env)
bloom dashboard --offline   # write the dashboard data to state/dashboard.json
bloom bot test              # send yourself a Telegram test message (needs .env)
bloom report                # weekly Instagram report (needs .env)
pytest                      # 130 tests, all offline (Meta, Google, GitHub and Claude are mocked)
```

## Project layout

```
src/bloom_agent/        publisher, Instagram/Facebook clients, validation, tokens, reports
  site/                 website build (pages per language, structured data, images, sitemap…)
  seo/                  SEO checks: on-page, whole site, PageSpeed, Search Console, Instagram, AI search
  marketing/            weekly plan: Claude brain, rule-based fallback, graphics, drafts, site changes, PRs
brand/                  playbook.md (how the agent thinks), rules.md, facts.yaml, seo.yaml, fonts, photos
marketing/              library.yaml (ready-made posts), funnel.yaml (your numbers), experiments.yaml
site/                   src/ (edit) → public/ (served by Netlify), netlify.toml
posts/                  the content queue: one folder per post
reports/                weekly Instagram reports, reports/seo, reports/marketing (the briefs)
state/                  token expiry, latest SEO and plan data (never secrets)
tests/                  offline test suite
.github/workflows/      the schedules
```
