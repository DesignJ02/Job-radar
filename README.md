# Job Radar

Checks for new Product Designer / UX / UI-UX jobs every 15 minutes and:

- sends each new job to **Telegram** straight away
- sends one **email digest** every morning (9 AM IST)
- keeps a live **job list page** on GitHub Pages

Sources: company career pages on Greenhouse, Lever and Ashby (reliable), plus LinkedIn's public job search (best effort, it often blocks bots).

Filters: Delhi NCR, Bangalore, Hyderabad, Remote (India-friendly), 3–5 years of experience. Change anything in `config.yaml`.

---

## Setup (about 20 minutes)

### 1. Put the code on GitHub
1. Create a new repo on GitHub called `job-radar`. **Public** is best (free, unlimited runs). Private works too, but change the schedule to every 30 minutes (see the note in `.github/workflows/radar.yml`).
2. Upload all these files, keeping the folders (`.github/workflows/...`).

### 2. Make a Telegram bot
1. In Telegram, message **@BotFather** → send `/newbot` → pick a name. Copy the **token** it gives you.
2. Open your new bot and send it any message, like "hi".
3. Open `https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates` in a browser. Find `"chat":{"id": 123456789` — that number is your **chat ID**.

### 3. Make a Gmail app password (for email)
1. Your Google account needs 2-step verification turned on.
2. Go to Google Account → Security → **App passwords** → create one called "Job Radar". Copy the 16-character password.

### 4. Add secrets to the repo
Repo → **Settings → Secrets and variables → Actions → New repository secret**. Add:

| Name | Value |
|---|---|
| `TELEGRAM_BOT_TOKEN` | token from BotFather |
| `TELEGRAM_CHAT_ID` | your chat ID |
| `SMTP_USER` | your Gmail address |
| `SMTP_PASS` | the app password |
| `EMAIL_TO` | where to send the digest (can be the same Gmail) |

Secrets stay hidden even in a public repo.

### 5. Check the company list
**Actions → Check sources → Run workflow.** Open the run and read the summary table. Delete or fix every company marked **FAILED** in `config.yaml`.

The starter list is a guess and is **not verified**. Add the companies you actually want: open their careers page, click a job, and look at the address:
- `boards.greenhouse.io/<slug>` or `job-boards.greenhouse.io/<slug>` → `ats: greenhouse`
- `jobs.lever.co/<slug>` → `ats: lever`
- `jobs.ashbyhq.com/<slug>` → `ats: ashby`

Companies on Workday, Darwinbox or their own sites aren't supported yet.

### 6. Switch it on
1. **Actions → Job Radar → Run workflow.** The first run sends one "Job Radar is live" message and does **not** send every open job (to avoid flooding you). After that you only get new ones.
2. Turn on the page: **Settings → Pages → Deploy from a branch → `main` / `docs`**. Your list will be at `https://<your-username>.github.io/job-radar/`. Paste that into `page_url` in `config.yaml`.

---

## Things to know

- **No salary filter.** Most Indian job posts don't show pay, so the tool can't filter by 10–20 LPA. It filters by experience level instead. Your company list is the real salary filter: only add companies that pay in your range.
- **LinkedIn will fail sometimes.** You'll see "LinkedIn blocked" in the run log. That's expected; the next run tries again. Never add your LinkedIn login.
- **GitHub timing isn't exact.** Scheduled runs can be 5–15 minutes late when GitHub is busy.
- **Too many alerts?** Add words to `title_exclude`, or remove LinkedIn (`enabled: false`).
- **GitHub may pause the schedule** if a public repo has no activity for 60 days. It emails you first; one click turns it back on.

Test on your own computer:
```
pip install -r requirements.txt
python radar.py --check      # which sources work
python radar.py --dry-run    # print matches, send nothing
```
