# Setup guide

You only do this once. **Part 1** (steps 1–8, about one evening) gets Instagram posting working.
**Part 2** (steps 9–13, about an hour) connects the website and Google so the agent can work as your head of
marketing. Part 2 can wait until Part 1 works.

> Screens on Meta's developer site change often. If a button has a slightly different name, look for the
> closest match. The goal of steps 2–3 is always the same: **an Instagram access token and your Instagram user ID**.

---

## 1. Instagram: switch to a professional account

In the Instagram app: **Settings → Account type and tools → Switch to professional account** → choose
**Business** (or Creator) → category *Modeling Agency* (or *Talent Agent*).

You do **not** need a Facebook Page for this setup.

## 2. Meta developer account

Go to <https://developers.facebook.com> and log in. Meta's developer site normally requires a Facebook login.
If you don't have Facebook yet, create a personal account first (it can stay private). It is only used to
sign in to the developer site.

Click **Get started** / **Register** and follow the steps (verify email/phone).

## 3. Create the app and get the token

1. **My Apps → Create app**.
2. Use case: choose the one for **Instagram** (e.g. *Manage messaging & content on Instagram*). If you are asked
   for an app type, choose **Business**. Name it e.g. `Bloom Agent`.
3. In the app, open **Instagram → API setup with Instagram login** (it may be called *Instagram business login*).
4. Under **Generate access tokens**, click **Add account** and log in with the Bloom Models Instagram account.
   - If Meta asks you to add the account as a **tester**: *App roles → Roles → Add people → Instagram tester*, then
     accept the invite in Instagram (**Settings → Website permissions / Apps and websites → Tester invites**).
5. Make sure these permissions are granted: `instagram_business_basic`,
   `instagram_business_content_publish`, `instagram_business_manage_insights`.
6. Click **Generate token** next to the account. Copy two things somewhere safe for a few minutes:
   - the **access token** (a long string),
   - the **Instagram user ID** shown next to the account (a long number, e.g. `17841…`).

Keep the app in **Development** mode. Because only your own account uses it, it doesn't need Meta's App Review.

## 4. Put the code on GitHub

1. Install **GitHub Desktop** and sign in with your GitHub account.
2. *File → Add local repository* → choose the unzipped `bloom-social-agent` folder (it is already a Git repository
   with one commit, so Desktop accepts it as it is).
3. Click **Publish repository** → keep the name `bloom-social-agent` → **untick "Keep this code private"**
   (see the note below) → Publish.
   (Don't create the repository on the GitHub website first: Desktop would then refuse the name. And don't use the
   web uploader: it can skip the hidden `.github` folder, which holds the schedules.)

> **Why public?** Instagram downloads each image from a public web address. With a public repository, GitHub
> serves the images for free, and GitHub Actions minutes are free and unlimited. Only finished posts and captions
> are in the repository, and they'll be public on Instagram anyway. Your tokens are stored as encrypted
> **secrets**, never in the files.
>
> Prefer private? It works if you set the `MEDIA_BASE_URL` variable to another public place that hosts the same
> files, and change the Publish schedule in `.github/workflows/publish.yml` to every 30 minutes
> (`"7,37 * * * *"`) to stay within the 2,000 free minutes a month.

## 5. Add the secrets

In the repository: **Settings → Secrets and variables → Actions → New repository secret**.

| Name | Value |
|---|---|
| `IG_USER_ID` | the Instagram user ID from step 3 |
| `IG_ACCESS_TOKEN` | the access token from step 3 |

## 6. Let the agent renew its own token

Instagram tokens expire after 60 days. The agent renews them every Monday, and needs permission to save the new one.

1. GitHub → your profile picture → **Settings → Developer settings → Personal access tokens → Fine-grained tokens →
   Generate new token**.
2. Name `bloom-agent secrets`, **Repository access: Only select repositories → bloom-social-agent**.
3. **Repository permissions → Secrets: Read and write**. Nothing else.
4. Expiration: 1 year (put a reminder in your calendar).
5. Copy the token and add it as a new repository secret named **`GH_ADMIN_TOKEN`**.

## 7. Test the connection

**Actions** tab → **Check connection** → **Run workflow**. You should see
`✅ Instagram connected — Account: @your_account`.

Then **Actions → Publish → Run workflow** with *Dry run* ticked: it shows what would be posted and changes nothing.

If GitHub asks you to enable workflows on the Actions tab, click **I understand… enable them**.

## 8. Approve your first post

Open `posts/2026-10-19-flexible-hours/post.yaml` → pencil icon → `status: approved` → **Commit changes**.
It goes out at 18:30 Budapest time on that day. After it's posted, `result:` in the file shows the Instagram link.

The ten posts from weeks 1 and 2 are already in `posts/` as **drafts**, scheduled Mon–Fri at 18:30 from
19 October. Change the dates if you start later, then approve them one by one or all at once.

---

# Part 2 — website, Google and the marketing agent

## 9. Let the agent open pull requests

The agent proposes next week's posts and website improvements as **pull requests** (you look, then merge or close).
GitHub has to allow that once:

**Settings → Actions → General → Workflow permissions** → tick **"Allow GitHub Actions to create and approve pull
requests"** → **Save**.

(GitHub doesn't run the CI checks on pull requests the agent opens, so the agent runs the same checks itself before
opening them and says so in the pull request. Don't add "required status checks" to `main`, or you won't be able
to merge them.)

## 10. Connect the website to GitHub (Netlify)

Until now you uploaded the site by drag-and-drop. From now on the website lives in this repository (`site/`), so
the agent can improve it and every change can be previewed before it goes live.

1. Netlify → the **bloommodels.hu** project → **Project configuration → Build & deploy → Continuous deployment →
   Link repository** → **GitHub** → allow access to `bloom-social-agent` → choose it.
2. Settings: **Branch** `main` · **Base directory** `site` · **Build command** leave empty ·
   **Publish directory** `site/public`. → **Deploy**.
3. Open <https://bloommodels.hu> (Hungarian) and <https://bloommodels.hu/en/> (English) to check. The language
   buttons now switch between the two pages.

From now on, don't drag-and-drop uploads any more (they would be overwritten by the next GitHub deploy).
To change website text yourself, edit `site/src/i18n/hu.json` / `en.json` on GitHub; CI rebuilds the site and
Netlify publishes it.

> **Netlify credits:** the free plan charges about 15 credits per deploy (300 a month), and pauses the site if
> they run out. The setup only deploys when the built website actually changes, and the agent bundles its website
> ideas into one pull request a week. The daily *Website health* check warns you if Netlify ever pauses the site.

## 11. Google Search Console (what people search to find you)

**a) Verify the domain** (once; Google needs proof you own bloommodels.hu)

1. <https://search.google.com/search-console> → **Add property** → **Domain** → `bloommodels.hu` → Continue.
2. Copy the **TXT record** Google shows (`google-site-verification=…`).
3. Rackhost → **Domains → bloommodels.hu → DNS** → **New record**: type **TXT**, name `@` (or empty), value: the
   text you copied → Save.
4. Back in Search Console → **Verify**. If it says not found yet, wait an hour and try again (up to a day).

**b) A robot account for the agent** (so it can read the data without your password)

1. <https://console.cloud.google.com> → project picker → **New project** → name `bloom-agent` → Create.
2. **APIs & Services → Library** → search **Google Search Console API** → **Enable**.
3. **IAM & Admin → Service accounts → Create service account** → name `bloom-seo` → Create → skip the optional
   steps → Done.
4. Click the new account → **Keys → Add key → Create new key → JSON** → a file downloads.
5. Search Console → **Settings → Users and permissions → Add user** → paste the service account's email
   (ends with `…iam.gserviceaccount.com`) → permission **Full** → Add.
6. GitHub → **Settings → Secrets and variables → Actions → New repository secret** → name
   **`GSC_SERVICE_ACCOUNT_JSON`** → open the downloaded file in a text editor, copy **all** of it, paste → Save.
   Then delete the downloaded file.

Google needs a few days to collect data for a new site; the weekly report says so until then. The agent submits
the sitemap automatically.

## 12. Link the website and Instagram

Once the Instagram profile is public, open `site/src/site.yaml` on GitHub → pencil icon → set
`instagram_url: https://www.instagram.com/<your username>/` → Commit. This tells Google and AI assistants that
the website and the profile are the same brand. Also set the profile's website link in Instagram to
`https://bloommodels.hu/`.

## 13. Test the marketing agent

**Actions → Weekly marketing plan → Run workflow.** After a few minutes you'll have:
- a pull request **"Next week's posts (…)"** with the graphics and captions — **merging it approves them**;
- possibly a pull request **"Website SEO improvements"** (only with Claude, only when the data supports a change),
  with a Netlify **Deploy Preview** link to check before merging;
- the brief in `reports/marketing/` and the SEO report in `reports/seo/`.

It then runs by itself every Friday morning, so you have the weekend to review.

---

## Optional extras

**Claude as the head of marketing:** create a key at <https://console.anthropic.com> (pay-as-you-go) and add it as
the secret `ANTHROPIC_API_KEY`. With it the agent researches the week (web search), reads all the numbers, writes
fresh posts in your voice, proposes experiments and data-backed website changes, and reviews draft posts in pull
requests. Typical cost: $1–3 a month on the default model; the agent stops itself at $2 per weekly run (change with
the repository variable `MARKETING_MAX_COST_USD`). For sharper strategy, set the variable `CLAUDE_MODEL` to
`claude-opus-5-5` (about twice the cost). Without a key, everything else still works and posts come from
`marketing/library.yaml`.

**PageSpeed key (optional):** speed tests work without a key; a free key from Google Cloud (*PageSpeed Insights
API*) as the secret `PAGESPEED_API_KEY` lifts the rate limit and also tests the English page.

**Facebook Page (later):** once the page exists, add secrets `FB_PAGE_ID` and `FB_PAGE_ACCESS_TOKEN` (a Page access
token with `pages_manage_posts`), then add `facebook` to a post's `platforms`. Image and carousel posts are supported.

**Different time zone:** add a repository **variable** (not secret) `TIMEZONE`, e.g. `Europe/London`.

## When something goes wrong

You'll get a GitHub issue titled *Bloom agent: …* with the reason. Common ones:

| Message | Fix |
|---|---|
| `Instagram is not configured` | Step 5: secrets missing or misspelled. |
| `code 190` / *token expired* | Generate a new token (step 3) and update `IG_ACCESS_TOKEN`. |
| `media URL returned HTTP 404` | The repository is private or the file isn't committed. See step 4. |
| `aspect ratio … outside 4:5 to 1.91:1` | Feed images must be between portrait 4:5 and landscape 1.91:1. |
| `6 hashtags` | Instagram allows 5. |
| `missed` | GitHub couldn't run on time. Change `scheduled_at`, set `approved` again. |
| *GitHub Actions may not open pull requests* | Step 9. Until then the issue contains a one-click link to open it yourself. |
| *No access to sc-domain:bloommodels.hu* | Step 11 b 5: add the service account email as a **Full** user. |
| *Netlify has paused the website* | Netlify → Usage. Credits reset monthly; avoid manual redeploys. |
| *site/public is out of date* (CI on a pull request) | Run `bloom site build` locally, or merge: CI on `main` rebuilds it. |
