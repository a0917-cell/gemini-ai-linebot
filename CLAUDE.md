# Project Context for Claude Code

This file is read on every Claude Code session in this directory. It captures
project state, design decisions, and gotchas that aren't obvious from the code.

## What this project is

A personal LINE assistant ("Gemini AI" official account) for a Taiwan
construction drafting / BIM engineer, built on **Gemini File Search** RAG.
Forked from [kkdai/linebot-multimodal-rag](https://github.com/kkdai/linebot-multimodal-rag);
spec and task log in `spec/personal-assistant.md` and `spec/personal-assistant.plan.md`.

- General Q&A, writing, translation (zh / vi / ja / en)
- The user's own uploaded files and images (per-user, isolated)
- A shared knowledge base: HJPLUS practice notes + statute text of 12 core building laws
- One-shot reminders ("明天 9 點提醒我繳圖")

Deployment: **Render free web service**, auto-deployed from `main`. Not Cloud Run
(the upstream target); see `spec/deployment.md`.

## Important design decisions (don't undo without asking)

### 1. Gemini File Search Store, not a custom vector DB
The managed `client.file_search_stores` API does chunking, embedding and
indexing. Do NOT add ChromaDB / FAISS / pgvector.

### 2. One store; per-user isolation plus a shared KB, via metadata
- User uploads: `user_id=<LINE UID>`
- Shared KB: `user_id="__kb__"`, `source` = `HJPLUS` (notes, may carry `status`) or `LAW` (statutes, `snapshot`)
- Query filter: `user_id="<LINE UID>" OR user_id="__kb__"` (`_user_filter`; OR proved against the real API in `scripts/spike_or_filter.py`). Unsafe ids (quote, backslash, whitespace) raise.
- A personal upload can never be tagged `__kb__`, and extra metadata may not set `user_id` / `source`.
- The store is pinned with `GEMINI_STORE_NAME`; the project once held two stores with the same display name.

### 3. Sources come from grounding metadata, not from the model
`_with_sources` appends what was actually retrieved: 📎 own files, 📜 statutes
(with snapshot date), 📚 HJPLUS notes. HJPLUS citations get the 待查證 note unless
every cited note is `status: verified` (296 of 332 carry no status). A law
question (`is_law_question`, keyword list) answered with no HJPLUS or LAW
source gets `LAW_NO_KB_NOTE`: the model decides for itself whether to call File
Search and the SDK cannot force a built-in tool.

### 4. Reply within the token budget, then push
Text runs as a BackgroundTask; `_deliver` replies while the reply token is under
50 s old, otherwise pushes (pushes count against the LINE monthly quota: 200 on
the free plan). Model output is flattened to plain text (`app/formatting.py`):
LINE renders no Markdown.

### 5. Free Gemini tier, stable models with fallback
`gemini-3.8-flash`, falling back to `gemini-3.5-flash-lite` on transient errors
(429 / 503). Free-tier content may be used to improve Google's products, so file
prompts carry `PRIVACY_NOTE`. The key in `.env` has been the same free key
production uses: measuring on it eats the live quota (it did, 2026-09-30).

### 6. Reminders live in Google Sheets, triggered by UptimeRobot
`app/reminders.py` (row format is a one-way door, ISO +08:00 to the second,
RAW writes), `app/reminder_parse.py` (Gemini only splits fields; `resolve()`
owns the rules), `app/reminder_tick.py` (push first, mark after; failed pushes
retry next tick). UptimeRobot calls `/cron/tick?key=CRON_SECRET` every 5
minutes: it is both the reminder clock and the keep-warm. If the monitor stops,
reminders stop.

### 7. Session store is in-memory with 5-min TTL
Fine while Render runs a single instance; it only holds "user uploaded this file,
waiting for store-or-search".

## Tech stack snapshot

- Python 3.12 (local venv 3.11), FastAPI, uvicorn
- `line-bot-sdk` v3 async; `google-genai` (the new SDK); `google-api-python-client` for Sheets
- Embedding: `models/gemini-embedding-2`

## File map (where to look first)

| File | Purpose |
|------|---------|
| `app/main.py` | Webhook, `/health`, `/store/info` (needs `X-Admin-Token`), `/cron/tick` |
| `app/line_handler.py` | LINE events; reminder commands; reply/push delivery |
| `app/gemini_service.py` | File Search, model fallback, prompt, sources footer, law-question check |
| `app/formatting.py` | Markdown to LINE plain text |
| `app/reminders.py` / `reminder_parse.py` / `reminder_tick.py` | Reminder store, time parsing, sending |
| `app/session.py` | In-memory session w/ TTL |
| `scripts/ingest_kb.py` / `ingest_laws.py` | Upload HJPLUS notes / statute chapters (dry run by default) |
| `scripts/setup_reminder_sheet.py` | One-time Google sign-in + reminder sheet creation; `--smoke` |
| `scripts/measure_kb_retrieval.py` | How often law questions cite the shared KB (uses real quota) |
| `spec/architecture.md` / `spec/deployment.md` | Design and data flows / Render setup |

## Testing

`pytest -q` (tests/). `tests/conftest.py` blocks real Gemini, LINE, GCS and
Sheets clients, because `.env` holds live credentials. New guards get a mutation
probe: break the guarded line and confirm a test turns red.

## Not done yet

- AC8 check: reply within 30 s after an hour idle
- No deletion flow for a user's own documents via LINE
- No per-user quota or rate limiting
- Audio / video are not supported by File Search
- Whether uploads land in GCS or on Render's local disk depends on the value of `GCS_BUCKET` on Render, which has not been checked

## How to run locally

```bash
cp .env.example .env    # LINE secrets, GEMINI_API_KEY, GEMINI_STORE_NAME ...
uvicorn app.main:app --reload --port 8080
```
Expose the port with a tunnel (e.g. cloudflared) and set `https://<tunnel>/webhook` in the LINE console.

## GitHub remote

`https://github.com/a0917-cell/gemini-ai-linebot.git` (branch `main`, Render
deploys every push). Commits use the `tkgcc` identity.
