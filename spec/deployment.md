# Deployment (Render)

This fork runs on a **Render free web service**, not on Cloud Run (the upstream
target; its guide and `cloudbuild.yaml` are in git history before 2026-09-30).

| Item | Value |
|------|-------|
| Service | `gemini-ai-linebot-a0917` |
| URL | `https://gemini-ai-linebot-a0917.onrender.com` (LINE webhook: `/webhook`) |
| Source | `a0917-cell/gemini-ai-linebot`, branch `main`; every push deploys |
| Build | Not checked from the dashboard whether Render uses the repo's `Dockerfile` or a native Python build |
| Plan | Free: sleeps when idle; 750 instance hours per month **per workspace**, shared with `line-archiver-bot`; over the limit, all free services are suspended until the next month (no card on file, so no charge) |

## Environment variables (Render → Environment)

Secret values are pasted by the owner, never typed by an assistant. The
"Import from .env" option pastes several at once; delete an old row before
importing the same key, or the form holds it twice.

| Key | Value / where it comes from |
|-----|-----------------------------|
| `LINE_CHANNEL_SECRET` | LINE Developers Console → Messaging API |
| `LINE_CHANNEL_ACCESS_TOKEN` | LINE Developers Console → Messaging API |
| `GEMINI_API_KEY` | Google AI Studio, **free** project key (last four `WlPM`) |
| `GEMINI_MODEL` | `gemini-3.8-flash` |
| `GEMINI_FALLBACK_MODEL` | `gemini-3.5-flash-lite` |
| `GEMINI_STORE_NAME` | `fileSearchStores/linebotmultimodalrag-c1v9232tcirj` |
| `CRON_SECRET` | random, generated into local `.env`; also in the UptimeRobot URL |
| `REMINDER_SHEET_ID` | sheet 「LINE 助手提醒」, created by `scripts/setup_reminder_sheet.py` |
| `GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET` | OAuth client shared with line-archiver-bot ("LINE Archiver", published) |
| `GOOGLE_REFRESH_TOKEN` | drive.file scope only, from `setup_reminder_sheet.py` |
| `GCS_BUCKET` | left over from upstream; value not checked. Unset means uploads use the instance's local disk, which is wiped on redeploy. Uploads work end to end either way (verified 2026-09-30) |
| `ADMIN_TOKEN` | not set: `/store/info` then always answers 403 |

## Keep-warm and reminder clock (UptimeRobot)

Monitor 「LINE assistant reminder tick」: HTTP(s), `https://…onrender.com/cron/tick?key=<CRON_SECRET>`,
every 5 minutes, timeout 60 s (a cold start takes about 42 s).

- The free plan sends **HEAD** and cannot change the method; `/cron/tick` accepts GET and HEAD.
- Custom headers are a paid feature, so the secret travels in the query string and appears in access logs. It only lets a caller run a tick early, and a tick sends only reminders already due.
- UptimeRobot names a new monitor after its full URL, secret included: rename it.
- A store outage answers 503, so the monitor emails the owner.
- The account's other monitor, `line-archiver-bot.onrender.com/health`, was **paused on 2026-09-30, on purpose**. It had shown Down for three months only because that `/health` is GET-only and the free plan sends HEAD (GET 200, HEAD 405). Fixing it would keep the archiver awake around the clock too, and the two services together (~1,488 h) would exhaust the shared 750 h mid-month and suspend both. The archiver wakes on LINE webhooks instead (cold start ~30 s).

## One-time setup

1. **Reminder sheet**: `python scripts/setup_reminder_sheet.py --client-env <line-archiver-bot .env>` (browser sign-in), then `--smoke` to write, read back and cancel one reminder.
2. **Shared KB**: `python scripts/ingest_kb.py --apply --store <store>` (HJPLUS notes) and `python scripts/ingest_laws.py --apply --store <store>` (statutes). Both skip documents already uploaded; run without `--apply` first.
3. Copy the new values into Render, save; Render redeploys.
4. Check: `GET /cron/tick` without key → 403; with key → `{"status": "ok", "due": …}` (with `"reminders": "off"` the sheet is not configured).

## Updating the statute snapshot

`scripts/ingest_laws.py` refuses a snapshot date different from
`app.gemini_service.LAW_SNAPSHOT`. After pulling a newer openlawtw, change the
constant, delete the old `LAW/…` documents, re-upload, and deploy together.

## Local run

```bash
cp .env.example .env
uvicorn app.main:app --reload --port 8080
```

Tunnel the port (cloudflared quick tunnel or ngrok) and point the LINE webhook
at `https://<tunnel>/webhook`. Local `.env` has held the production free key:
anything run locally spends the live quota.
