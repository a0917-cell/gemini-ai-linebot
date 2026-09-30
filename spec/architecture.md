# Architecture

## System Overview

```
User (LINE App)
    │  HTTPS webhook
    ▼
LINE Platform ──────────────► POST /webhook
                                   │
FastAPI on Render (free, 1 instance)
    ├── text ──► BackgroundTask ──┬── 我的提醒 / …提醒我… ─► reminders (Sheets) ─► reply
    │                             └── anything else ─────► Gemini File Search ─► reply or push
    ├── image / file ─► save (GCS or local disk) ─► quick reply: store / search
    └── postback
          ├── action=store ──────► BackgroundTask ─► File Search index ─► push "✅"
          ├── action=search ─────► Gemini File Search (image or filename) ─► reply
          └── action=cancel_reminder&id=… ─► reminders (Sheets) ─► reply

UptimeRobot ── HEAD /cron/tick?key=… every 5 min ─► due reminders ─► LINE push
                (also keeps the free instance awake)
```

## Components

| Module | Role |
|--------|------|
| `app/main.py` | Webhook signature check and routing; `/health` (status only); `/store/info` (admin token, fails closed); `/cron/tick` (GET or HEAD, `CRON_SECRET`) |
| `app/line_handler.py` | LINE events, reminder commands, `_deliver` (reply under 50 s, else push), friendly errors |
| `app/gemini_service.py` | Store pinning, upload/index, text and image queries, model fallback, system prompt, sources footer |
| `app/formatting.py` | Markdown → LINE plain text |
| `app/reminders.py` | Reminder store: Sheets implementation + in-memory double, one contract |
| `app/reminder_parse.py` | Gemini structured output → fields; `resolve()` applies the time rules |
| `app/reminder_tick.py` | Sends due reminders; lock against overlapping ticks |
| `app/session.py` | In-memory upload session, 5-minute TTL |

## One store, three kinds of documents

| Kind | `user_id` | `source` | Other metadata | Display name |
|------|-----------|----------|----------------|--------------|
| User upload | LINE UID | – | – | original filename |
| HJPLUS note | `__kb__` | `HJPLUS` | `status` (often absent) | `HJPLUS/<path>.md` |
| Statute chapter | `__kb__` | `LAW` | `law_id`, `snapshot` | `LAW/<law>/<chapter>.md` |

Query filter: `user_id="<LINE UID>" OR user_id="__kb__"`, applied server-side, so
a user sees their own files plus the shared KB and never another user's files.
User ids that could rewrite the filter (quote, backslash, whitespace) are
rejected, and uploads refuse `__kb__` or caller-supplied `user_id` / `source`.

As of 2026-09-30: 332 HJPLUS notes (CC BY-SA 4.0), 104 statute chapters (1,949
articles from 12 core building laws, MOJ snapshot 2026-09-18, Government Open
Data License v1), plus user uploads.

## Answer footer

Built from grounding metadata (`_with_sources`), not from the model:

```
📎 來源：送審單.pdf                                   own files
📜 法規條文（全國法規資料庫，快照 2026-09-18）：…     statutes, no 待查證
📚 法規知識庫（HJPLUS，CC BY-SA 4.0）：…              notes
⚠️ …（待查證）…                                        unless every cited note is verified
⚠️ 這個回答沒有引用法規知識庫…                         law question with no HJPLUS/LAW source
```

The model decides whether to call File Search at all. On 10 law questions it
cited the shared KB once before the prompt change and three times after
(2026-09-30); most misses were topics the notes never covered, which is why
statute text was added.

## Data flow: text question

```
1. Webhook answers LINE at once; handle_text_message runs in the background
2. Loading animation (best effort)
3. Reminder command? → reminders path (see below)
4. generate_content(model, system_instruction, FileSearch(store, filter))
   └── 429/503 after retries → same call on the fallback model
5. to_plain_text + sources footer
6. Reply if the token is < 50 s old, otherwise push
```

## Data flow: reminder

```
set:    "明天 9 點提醒我繳圖" → Gemini fields {date, hour, minute, period_given, text}
        → resolve(): no time → ask; past → refuse; "9 點" → nearest future 09:00/21:00;
          date only → 09:00 → append row → "⏰ 已設定：9/30（三）09:00 繳圖" + cancel button
list:   "我的提醒" → caller's pending rows, cancel buttons (≤ 13, labels ≤ 20 chars)
send:   /cron/tick → list_due(now) → push → mark_sent (only if still pending)
        push failed → stays pending, retried next tick; > 10 min late → says how late
```

Sheet row (one-way door): `id | user_id | due_at | text | status | created_at | sent_at`,
ISO 8601 with +08:00 to the second, written RAW.

## Data flow: store an upload

```
1. Image/file → download from LINE → save (GCS if GCS_BUCKET, else tmp_uploads/)
2. Session holds the path for 5 minutes; reply with privacy note + buttons
3. "📥 存入資料庫" → background upload_to_file_search_store(user_id=UID) → poll → push "✅"
```

## Limits that shape the design

| Limit | Consequence |
|-------|-------------|
| LINE reply token: 1 minute | Background work; push after 50 s |
| LINE free plan: 200 pushes/month | Reminders and slow answers share it; replies are free |
| Render free: sleeps when idle, 750 h/month per workspace (shared with line-archiver-bot) | UptimeRobot every 5 min keeps it awake (744 h in a 31-day month) |
| Gemini free tier: **20 requests/day per project on gemini-3.8-flash** (reported by the API, 2026-09-30); flash-lite reportedly ~500 (third-party figure); content may improve Google products | A per-day 429 goes straight to the fallback model; privacy note on uploads; never measure on the production key |
| File Search: no way to force a built-in tool | Law-question warning instead of a guarantee |
| Single Render instance | In-memory session and tick lock are enough |
