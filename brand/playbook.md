# Bloom Models — Marketing Playbook

This is the head-of-marketing handbook. The agent reads it before every weekly plan, and so should any person
helping with Bloom's marketing. It's specific to Bloom: an online streaming-model agency in Budapest, recruiting
adults (18+), communicating in Hungarian first and English second, growing organically.

Rules marked **[HARD]** are enforced in code as well. Everything else is judgement: use the numbers to decide.

---

## 1. The one job

**Goal: more qualified applications each week, at no risk to the accounts.**

A qualified application comes from someone 18+, reachable, who understands what the work is and still wants it.
Every piece of marketing is judged by how it moves people down this funnel:

| Stage | What it means | Where we measure it |
|---|---|---|
| Reach | People who saw us | Instagram reach; Google impressions |
| Interest | People who looked closer | Profile visits; Google clicks; saves; shares |
| Intent | People who went to the site | Bio link clicks (`website_clicks`); site visits |
| Application | Submitted the form | Formspree submissions (see `marketing/funnel.yaml`) |
| Start | Began working | Entered by hand in `marketing/funnel.yaml` |

Rough targets for an account like ours in its first months (adjust once real data exists):
profile visits ≥ 2% of reach; bio link clicks ≥ 10% of profile visits; applications ≥ 5% of site visits.
When a stage underperforms, fix that stage first. More reach doesn't help if the profile doesn't convert.

**The weekly question:** what was the bottleneck stage last week, and what is the single change most likely to fix
it?

---

## 2. Who we talk to

Primary: adults in Hungary (Budapest and commuting distance, then the rest of the country) who want flexible,
well-paid work and are comfortable on camera, or curious whether they could be. Secondary: English-speaking adults
in or moving to Budapest; people from neighbouring countries for whom we help arrange travel and accommodation.

Typical situations: students who need income that fits around lectures and exams; people in a low-paid or rigid
job; people between jobs; people already creating content online who want structure and a studio; parents who need
hours that bend around family.

**What they want:** money that is real and arrives on time; control over their hours; to be treated with respect;
privacy.

**What stops them (and how we answer):**

| Fear | Our answer (content that addresses it) |
|---|---|
| "Is this a scam?" | Transparency: who we are, the studio, how payouts work, clear terms; a real team replying. Never vague. |
| "Someone I know will see me." | Discretion: they decide what's visible and to whom; takedown help (DMCA); privacy by design. |
| "I've never done this." | Training from the first stream; no experience needed; what week one looks like. |
| "Will I really earn?" | Honest numbers: €2,000+/month as a realistic goal, up to 80% share, monthly payouts, *earnings vary*. |
| "Will I be pressured?" | What you show and when is always your call. Friendly team, zero pressure. |
| "I'm not from Budapest." | We help with travel and accommodation; or work from home with a home-studio setup. |

Never assume gender or appearance in copy. Talk to "you". [HARD] Recruitment text must not discriminate (Hungary's
Equal Treatment Act applies to job ads): no "only women", no upper age limit, no body requirements. 18+ is a legal
requirement and is always stated.

---

## 3. Positioning and message

**Positioning:** *The friendly, professional, discreet way to start streaming in Budapest — with a real studio and
a real team behind you.*

Message pillars (rotate them; each week should touch at least four):

1. **Flexibility** — about 20 hours a week in 5-hour blocks; morning, afternoon, evening or night.
2. **Earnings, honestly** — €2,000+ a month as a realistic goal; up to 80%; monthly payouts; earnings vary.
3. **Discretion & safety** — you control visibility; privacy; takedown help.
4. **Support & training** — no experience needed; we train you; technical help around the clock.
5. **The studio** — District V, near St. Stephen's Basilica; professional light and equipment; or a home studio.
6. **People** — a young, friendly team; respect; no pressure.
7. **How it works** — the application, the first conversation, the first week. Demystify everything.

**Proof without lying:** [HARD] no fake testimonials, invented reviews, made-up numbers or stock "success stories".
Proof comes from transparency instead: show the studio, explain the process, publish the FAQ, answer DMs fast,
state exact terms. When real models agree to share their experience (with written consent), that becomes the
strongest content we have — collect it properly.

[HARD] Every number we use is in `brand/facts.yaml`. To use a new number, add it there first (and only if it's true).

---

## 4. Platform risk (read this twice)

Our category is sensitive. Losing the Instagram account would cost months. Rules:

- [HARD] Never use words that Meta treats as adult solicitation, in captions, alt text, hashtags or on images:
  webcam, adult, sexy, nude, OnlyFans and their Hungarian equivalents (`DEFAULT_BANNED_WORDS` in config.py).
  On our own website we describe the job plainly and honestly; on social platforms we say "streaming" / "online
  modeling".
- [HARD] No sexually suggestive imagery or wording. Glamour, fashion, beauty, lifestyle, studio, people, numbers.
- [HARD] No engagement bait ("tag a friend", "comment YES", "like if…"). Meta demotes it.
- [HARD] At most 5 hashtags (Instagram's limit since December 2025).
- Don't run paid ads for recruitment in this category without checking the platform's current policy first:
  TikTok's ad policy explicitly bans webcam/adult-streaming recruitment; Meta has an Adult Sexual Solicitation
  standard; Google restricts sexual content. Organic first.
- Post original content only. Instagram down-ranks reposted, watermarked or recycled content.
- Respond to every comment and DM within a day; never move a conversation to a channel that looks evasive.

---

## 5. Instagram

### What the algorithm rewards (2025–2026)
- **Sends (shares) per reach** — the strongest signal now. Make things people forward to a friend: a myth-buster,
  a clear "how it works", a surprising honest number.
- **Watch time** — for Reels, completion matters; Explore favours videos people finish. First 1–2 seconds decide.
- **Saves** — carousels with useful, practical content get saved.
- **Likes per reach**, comments, profile visits.
- **Originality** — original, non-watermarked content.
- Negative signals: quick skips, "Not interested".
- **Trial Reels** show a Reel to non-followers only: use them to test hooks before posting to followers.

### Formats and cadence (starting point)
- 5 feed posts a week (Mon–Fri), alternating carousel / single image; 1–2 Reels a week once filming is possible.
- Stories daily: behind the scenes, quick polls, FAQ answers, reposts of our own posts, "link in bio" reminders.
- Highlights: How it works · FAQ · Studio · Earnings · Apply. Keep them current — they are the profile's sales page.
- Post 18:00–20:00 Budapest time to start; test 12:00–13:00. Change only one thing at a time.

### Caption formula
1. **Hook line with a search keyword** in the first 125 characters (that's what shows before "… more"):
   e.g. "Online modell munka, rugalmas beosztással — így működik nálunk."
2. 2–5 short lines of value (Hungarian).
3. "—" then the English version, shorter.
4. One call to action: "Jelentkezés: link a bioban · 18+" / "Apply: link in bio · 18+".
5. 3–5 specific hashtags from the pool in `brand/seo.yaml`.

### Instagram search SEO
Instagram search reads the **Name field** (not just the username), bio, captions, alt text and hashtags; Google also
indexes public posts of professional accounts. So: keyword in the Name field ("Bloom Models | Online Modeling"),
keyword early in every caption, descriptive alt text in a full sentence on every image post, location context
(Budapest) often.

### Hooks that suit us (adapt, don't copy)
- "3 dolog, amit senki nem mond el az online modell munkáról"
- "Mennyit lehet *valójában* keresni? Őszintén."
- "Nem kell tapasztalat. Ez az első heted nálunk:"
- "Ha attól félsz, hogy valaki felismer — ezt nézd meg."
- "Nem budapesti vagy? Így oldjuk meg."
- "Myth: you need to be a model to do this."
- "What week one actually looks like."

### DMs and comments
Reply fast, warmly, briefly; answer the question; then point to the form ("A jelentkezés 2 perc: link a bioban").
Never ask for photos or personal details in DMs; the form exists for privacy reasons.

---

## 6. Website and Google (SEO)

The site is bloommodels.hu: Hungarian at `/`, English at `/en/`, with hreflang, structured data and a FAQ.

**On-page:** title ≤ 60 characters with the keyword first; description 110–160 characters with a reason to click;
one H1; a keyword in at least one H2; descriptive alt text; fast images (WebP); FAQ answering real questions.

**What to grow, in order:**
1. **Search Console data first.** Within weeks it shows the real searches. Queries at positions 4–20 are the
   cheapest wins: add the phrase to a heading, a FAQ answer, the title.
2. **FAQ depth.** Each real question applicants ask (DMs, forms, Search Console) becomes a FAQ entry, on the site
   in both languages. This also feeds AI assistants.
3. **New pages only when the data asks for them.** E.g. "How it works" (`/hogyan-mukodik/`), "For people outside
   Budapest", a fully honest earnings explainer. One page per real search intent, never thin pages.
4. **Links and mentions.** Hungarian business directories (with consistent name, city and website), partner
   sites, local press or blogs writing about flexible work. Never buy links.
5. **Google for Jobs** via JobPosting data: keep `valid_through` current; describe the role completely and
   honestly.
6. **Google Business Profile** only if the studio address can be public and meets Google's guidelines.

**Technical health** is checked daily by the agent (site up, indexable, HTTPS, redirects, sitemap, speed).
Netlify's free plan pauses the site if credits run out: keep deploys to about one a week (the agent batches its
website changes into one weekly pull request).

---

## 7. AI search (ChatGPT, Claude, Perplexity, Google AI answers)

People increasingly ask assistants "how can I earn money online from Budapest" or "what is a streaming model".
Assistants cite sources that are clear, consistent and easy to quote.

- Let AI search crawlers read the site (robots.txt allows them; the agent checks).
- Say the important facts in plain sentences, identically everywhere: site, llms.txt, Instagram bio, directories.
  Inconsistent facts make assistants skip you. `brand/facts.yaml` is the source.
- FAQ-style answers: question as a heading, a direct 1–3 sentence answer first, details after.
- Structured data: Organization (with sameAs → Instagram), WebSite, FAQPage.
- Get mentioned on other sites; assistants trust third-party mentions more than self-description.
- The agent monthly asks an assistant (with web search) the questions applicants ask, and records whether Bloom
  appears. Rising mentions are a lagging indicator; consistency and FAQ depth are the levers.

---

## 8. Other channels (in the order to try them)

1. **Referrals.** The form already asks "Who referred you?". A referral bonus for current models is usually the
   cheapest, highest-quality channel in this industry. Decide the terms, add them to `facts.yaml`, then promote.
2. **Speed to reply.** Replying to applications within hours instead of days lifts conversion more than any post.
   The site promises "usually within 72 hours"; aim for 24.
3. **Facebook Page + groups** (once there is a Facebook account): student and expat groups, following each group's
   rules. Never spam.
4. **Job boards** — only those whose policies allow this category; complete, honest listings.
5. **Partnerships** — photographers, make-up artists, studios: cross-promotion with consent.
6. Paid ads — see section 4. Not before organic works and policies have been checked.

---

## 9. Content system

- **Weekly mix (5 posts):** 1 earnings/how-it-pays (honest), 1 flexibility/lifestyle, 1 discretion/safety,
  1 how-it-works/training/FAQ, 1 apply/CTA. Plus Stories daily.
- **Series beat one-offs:** "Tévhitek" (myths), "GYIK" (FAQ), "Első heted" (your first week), "Stúdió".
- **Repurpose:** one idea → carousel + Reel script + 3 Stories + a FAQ entry on the site.
- **Visual identity:** rose → terracotta → gold gradient, Fraunces headlines, Plus Jakarta Sans text, white or deep
  plum backgrounds, the five-petal Bloom mark. Real photos of our AI characters only from the approved library.
  One message per image; readable on a phone in one second.

---

## 10. Hungarian copy

- Informal "te" form, warm and direct. Short sentences. No corporate tone.
- Hungarian quotation marks „így". Numbers: "2000 €", "80%", "heti 20 óra". Times: "18:30".
- Don't overuse English loanwords; "streaming modell" and "online modell" are fine (they are the search terms).
- Check suffixes after numbers and brand names: "Bloom Modelshez", "80%-a", "18 év felett".
- English versions are shorter and natural, not word-for-word.

---

## 11. Calendar (Hungary)

- Public holidays: Jan 1, Mar 15, Easter Monday, May 1, Whit Monday, Aug 20, Oct 23, Nov 1, Dec 24–26. Light posts.
- Universities: semesters start early September and early February; exams mid-December–January and mid-May–June
  (stress; "flexible income" lands well right after exams and before semesters start).
- January (new-year resolutions, post-holiday money worries) and late August/September (back to routine) are
  strong recruitment months. Summer: travel and "work from anywhere" angles.

---

## 12. Measuring and experimenting

- Review weekly; decide monthly. Small accounts are noisy: compare 2–4 week averages, not single posts.
- One experiment at a time per variable (time, format, hook, CTA); run at least 2 weeks; write the hypothesis,
  metric and result in `marketing/experiments.yaml`.
- Post score used in reports: profile visits ×3 + follows ×5 + shares ×3 + saves ×2 + comments ×2 + likes.
  It favours actions that lead toward an application.
- Track the bio link with UTM tags (`?utm_source=instagram&utm_medium=social&utm_campaign=bio`) once the site has
  privacy-friendly analytics. Until then, use the form's "Who referred you?" answers and Instagram link clicks.

---

## 13. What the agent may do alone, and what needs you

| The agent does on its own | Needs your OK (pull request or decision) |
|---|---|
| Check the site, speed, Google and Instagram SEO | Publishing any post (approve or merge the weekly pull request) |
| Write the weekly brief and SEO report | Any change to the website |
| Draft next week's posts and graphics | New numbers or promises (facts.yaml) |
| Propose experiments and site improvements | Referral programme, ads, new channels, keyword strategy |
| Alert you when something breaks | Anything that spends money |
