# Website Uptime Monitor (runs in the cloud, free)

Checks your website **every 5 minutes from GitHub's servers** — not your laptop, not your hosting server — and alerts you on Telegram / Email / Slack the moment it is down.

## What it catches

| Problem | Alert |
|---|---|
| Domain / DNS not resolving | 🔴 DOWN |
| Server not responding, timeout, connection refused | 🔴 DOWN |
| SSL certificate expired, wrong domain, untrusted, self-signed ("Not Secure") | 🔴 DOWN |
| Site redirects from https to non-secure http | 🔴 DOWN |
| HTTP errors (500, 502, 503, 404, 403…) | 🔴 DOWN |
| Page loads but expected text missing (blank page, error page, defaced) | 🔴 DOWN |
| SSL certificate expiring within 14 days | ⚠️ WARNING |
| Page takes more than 10 s to load | ⚠️ WARNING |

Every failure is **re-checked after 30 seconds** before alerting, so you don't get false alarms from a one-off blip.
You get: a DOWN alert → a reminder every 30 min while still down → a RECOVERED alert with total downtime → and a **daily status message at 9 AM IST** so you know the monitor itself is alive.

---

## Setup (about 15 minutes)

### 1. Create a GitHub repository
1. Sign in at github.com (free account is fine) → **New repository** → name it `site-monitor`.
2. Choose **Private** (recommended — gives 2,000 free Actions minutes/month; this monitor uses roughly that at a 5-min interval, see note below) or **Public** (unlimited free minutes).
3. Upload all files from this folder, keeping the `.github/workflows/uptime.yml` path.

### 2. Put your website in `sites.json`
```json
[
  { "name": "Main website", "url": "https://www.yourcompany.com", "keyword": "Your Company" },
  { "name": "Investor page", "url": "https://www.yourcompany.com/investors" }
]
```
`keyword` is optional but recommended: a word that always appears on the real page (company name, footer text). It catches cases where the server returns a blank or error page with status 200.

### 3. Set up alerts (pick one or more)

Go to repo **Settings → Secrets and variables → Actions → New repository secret**.

**Telegram (recommended — instant phone notification, free)**
1. In Telegram, open **@BotFather** → `/newbot` → copy the token.
2. Send any message to your new bot (or add it to a team group).
3. Open `https://api.telegram.org/bot<TOKEN>/getUpdates` in a browser and find `"chat":{"id": ...}`.
4. Add secrets: `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` (multiple IDs comma-separated).

**Email (Gmail example)**
1. Turn on 2-Step Verification on the Gmail account, then create an **App Password** (Google Account → Security → App passwords).
2. Add secrets: `SMTP_HOST` = `smtp.gmail.com`, `SMTP_PORT` = `587`, `SMTP_USER` = the Gmail address, `SMTP_PASS` = the app password, `ALERT_EMAIL_TO` = recipients, comma-separated.

**Slack**
Create an Incoming Webhook in Slack and add secret `SLACK_WEBHOOK_URL`.

### 4. Allow the workflow to save its memory
Repo **Settings → Actions → General → Workflow permissions → Read and write permissions → Save**.

### 5. Test it
**Actions** tab → *Website uptime monitor* → **Run workflow**.
To test the DOWN alert, temporarily add a fake site such as `https://expired.badssl.com` to `sites.json`, run it, confirm you get the alert, then remove it.

---

## Good to know

- **Speed:** GitHub's schedule is every 5 minutes at best and can run late during GitHub's busy periods. So expect to hear within roughly 5–15 minutes.
- **Monitoring the monitor:** the daily 9 AM message is your proof it's running. If it doesn't arrive one morning, check the Actions tab.
- **Private repo minutes:** at every-5-minutes, a private repo can use close to the 2,000 free minutes/month. If GitHub warns you, either make the repo public (no secrets are stored in files — they live in GitHub Secrets) or change the cron in `uptime.yml` to `*/10 * * * *`.
- **Settings you can change** (add as `env:` in `uptime.yml`): `SSL_WARN_DAYS`, `SLOW_SECONDS`, `REMINDER_MINUTES`, `RETRY_DELAY`, `HEARTBEAT_HOUR_IST`.

## Recommended: add a second, independent monitor
For an investor-facing site, use two monitors from different providers so one outage never goes unnoticed. Pair this with a hosted service (UptimeRobot, Better Stack, StatusCake etc.) for 1-minute checks and SMS/voice-call alerts; their paid entry plans are a few dollars a month. Check each one's free-plan terms — some free tiers are non-commercial only or leave out SSL monitoring.
