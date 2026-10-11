# Setup guide

About one evening. You only do this once. Steps 1–3 happen on Instagram/Meta, steps 4–8 on GitHub.

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

## Optional extras

**Claude reviews and report suggestions:** create a key at <https://console.anthropic.com> (pay-as-you-go, a $5
top-up lasts months) and add it as the secret `ANTHROPIC_API_KEY`.

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
