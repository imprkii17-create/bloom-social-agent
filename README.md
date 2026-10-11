# Bloom Models — social media agent

Posts your approved Instagram content automatically at the right time, keeps the Instagram connection alive,
and writes a weekly performance report. Runs for free on GitHub Actions. No server, no monthly subscription.

```
 you (or Claude) add a post ──► CI checks it ──► you set status: approved ──► agent posts it at 18:30
                                    │                                              │
                       converts PNG → JPEG,                          saves the Instagram link back into
                       blocks risky captions                         the post, alerts you if anything fails
```

**First time?** Follow [SETUP.md](SETUP.md) (about one evening).

---

## How it works

| Workflow | When | What it does |
|---|---|---|
| **Publish** | every 15 min | Publishes approved posts whose time has come. Never two at once, never twice. |
| **CI** | every push | Tests the code, converts images to JPEG, validates every post. |
| **Refresh Instagram token** | Mondays | Renews the 60-day Instagram token so it never expires. |
| **Weekly report** | Mondays | Writes `reports/<year>-W<week>.md` with reach, saves, shares, profile visits… |
| **Check connection** | by hand | Confirms the Instagram account, quota and settings. |

Safety built in:
- Only posts with `status: approved` are ever published, and only after `scheduled_at`.
- Before Instagram is asked to publish, the agent saves and pushes a checkpoint commit
  (`bloom-agent: publishing …`). Any later run sees it and checks that exact upload instead of posting again, so
  a crash or a lost answer can't cause a double post. If the checkpoint can't be saved, nothing is published.
- Progress is saved after each platform; a platform that already succeeded is never posted to again.
- A post that is more than 3 hours late (GitHub had an outage, say) is marked `missed`, not posted at a random time.
- Failures are retried up to 3 times, then marked `failed`, and you get a GitHub issue (email + app notification).
- Captions with more than 5 hashtags, or words that get accounts restricted (webcam, adult, sexy…, also in Hungarian)
  are blocked before they can go out. See [brand/rules.md](brand/rules.md).
- Tokens never appear in logs, files or error messages.

## Daily use

All of this works in the GitHub website or app. No coding needed.

### Approve a post
Open `posts/<post>/post.yaml` → pencil icon → change `status: draft` to `status: approved` → **Commit changes**.
That's it. It will go out at its `scheduled_at` time (Budapest time).

### Change the time
Edit `scheduled_at: 2026-10-19 18:30` in the same file.

### Add a new post
Create a folder in `posts/` named like `2026-11-02-my-topic` with:

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

Or, on your computer: `bloom new 2026-11-02-my-topic --at "2026-11-02 18:30"`.

### Status values
| status | meaning |
|---|---|
| `draft` | Being worked on. Never published. |
| `approved` | Will be published at `scheduled_at`. |
| `published` | Done. `result:` shows the Instagram link. |
| `failed` | Gave up after 3 tries. Read `result.last_error`, fix it, set `approved` again. |
| `missed` | Its time passed while the agent couldn't run. Change `scheduled_at`, set `approved` again. |
| `skipped` | Kept for the record, never published. |

## Optional: Claude features

Add an `ANTHROPIC_API_KEY` secret and the agent also:
- reviews new draft posts in pull requests (`bloom review`) and writes `review.md` next to each post;
- adds 3–5 suggestions for next week to the weekly report.

Cost: roughly $1–3 per month with `claude-sonnet-5-5` (change with the `CLAUDE_MODEL` variable).
Without the key, everything else works the same.

## Commands (for running locally)

```bash
pip install -e ".[dev,ai]"
bloom validate              # check every post
bloom prepare               # convert images to Instagram-ready JPEG
bloom status                # show the queue
bloom approve <post-id>     # mark a post as approved
bloom publish --dry-run     # show what would be posted now
bloom check                 # test the Instagram connection (needs .env)
bloom report                # weekly report (needs .env)
pytest                      # 55 tests, all offline, with a mocked Meta API
```

## Project layout

```
src/bloom_agent/   the agent (publisher, Instagram/Facebook clients, validation, tokens, reports, Claude review)
posts/             the content queue: one folder per post
brand/rules.md     content rules used by validation and Claude
reports/           weekly reports (written by the agent)
state/             token expiry date (never the token itself)
tests/             offline test suite with a mocked Meta API
.github/workflows/ the schedules
```
