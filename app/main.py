import asyncio
import hmac
import os
import time
from datetime import datetime

from dotenv import load_dotenv

load_dotenv()

from fastapi import FastAPI, Header, HTTPException, Request, BackgroundTasks
from linebot.v3 import WebhookParser
from linebot.v3.exceptions import InvalidSignatureError
from linebot.v3.webhooks import (
    FileMessageContent,
    ImageMessageContent,
    MessageEvent,
    PostbackEvent,
    TextMessageContent,
)

import app.gemini_service as gemini
import app.reminder_tick as reminder_tick
import app.reminders as reminders
from app import line_handler

app = FastAPI(title="LINE Bot Multimodal RAG")
parser = WebhookParser(os.environ.get("LINE_CHANNEL_SECRET", ""))


@app.on_event("startup")
async def startup() -> None:
    loop = asyncio.get_event_loop()
    try:
        store = await loop.run_in_executor(None, gemini.get_or_create_store)
        print(f"[Startup] File Search Store ready: {store}")
    except Exception as e:
        print(f"[Startup] Warning: Could not initialize store: {e}")


# LINE may deliver an event again (redelivery, retries). Seen webhookEventIds
# are kept for 6 hours in memory: Render runs one instance, and forgetting
# them on restart was accepted (plan T25).
EVENT_DEDUPE_SECONDS = 6 * 60 * 60
_seen_events: dict = {}  # webhookEventId -> time.monotonic() when first seen


def _already_seen(event_id: str) -> bool:
    now = time.monotonic()
    for eid in [e for e, t in _seen_events.items() if now - t > EVENT_DEDUPE_SECONDS]:
        del _seen_events[eid]
    if not event_id:
        return False
    if event_id in _seen_events:
        return True
    _seen_events[event_id] = now
    return False


def _require_admin(token: str) -> None:
    """403 unless the X-Admin-Token header matches ADMIN_TOKEN. Fails closed:
    with ADMIN_TOKEN unset, nothing gets in (an empty header must not match)."""
    expected = os.environ.get("ADMIN_TOKEN", "")
    if not expected or not hmac.compare_digest(token.encode(), expected.encode()):
        raise HTTPException(status_code=403, detail="forbidden")


@app.get("/health")
async def health() -> dict:
    # Public (keep-warm pings hit it): report liveness only, no resource names.
    return {"status": "ok"}


@app.get("/store/info")
async def store_info(x_admin_token: str = Header(default="")) -> dict:
    """Admin-only: lists every user's documents in the shared store."""
    _require_admin(x_admin_token)
    loop = asyncio.get_event_loop()
    try:
        store_name = await loop.run_in_executor(None, gemini.get_or_create_store)
        client = gemini.get_client()
        store = await loop.run_in_executor(
            None, lambda: client.file_search_stores.get(name=store_name)
        )
        documents = await loop.run_in_executor(
            None,
            lambda: list(client.file_search_stores.documents.list(parent=store_name)),
        )
        return {
            "store_name": store_name,
            "display_name": getattr(store, "display_name", ""),
            "embedding_model": getattr(store, "embedding_model", ""),
            "document_count": len(documents),
            "documents": [
                {
                    "name": getattr(d, "name", ""),
                    "display_name": getattr(d, "display_name", ""),
                    "state": str(getattr(d, "state", "")),
                }
                for d in documents
            ],
        }
    except Exception as e:
        print(f"[store/info] Error: {e}")
        raise HTTPException(status_code=500, detail="store info unavailable")


@app.api_route("/cron/tick", methods=["GET", "HEAD"])
async def cron_tick(key: str = "") -> dict:
    """UptimeRobot hits this every 5 minutes: sends due reminders and keeps the
    service warm. HEAD is accepted because UptimeRobot's HTTP monitor defaults
    to it. The key travels in the query string (custom headers are a paid
    UptimeRobot feature); it only lets a caller run a tick early, and a tick
    sends nothing that is not already due."""
    expected = os.environ.get("CRON_SECRET", "")
    if not expected or not hmac.compare_digest(key.encode(), expected.encode()):
        raise HTTPException(status_code=403, detail="forbidden")
    try:
        store = await asyncio.to_thread(reminders.get_store)
    except reminders.RemindersNotConfigured:
        return {"status": "ok", "reminders": "off"}
    try:
        result = await reminder_tick.run_tick(
            datetime.now(reminders.TAIPEI), store, line_handler._push
        )
    except Exception as e:  # a 503 makes the monitor alert; details stay in the log
        print(f"[cron/tick] Error: {e}")
        raise HTTPException(status_code=503, detail="reminder store unavailable")
    return {"status": "ok", **result}


@app.post("/webhook")
async def webhook(request: Request, background_tasks: BackgroundTasks) -> str:
    signature = request.headers.get("X-Line-Signature", "")
    body = await request.body()
    body_str = body.decode("utf-8")

    print(f"[Webhook] Received request, signature={signature[:10]}...")
    print(f"[Webhook] Body preview: {body_str[:200]}")

    try:
        events = parser.parse(body_str, signature)
    except InvalidSignatureError as e:
        print(f"[Webhook] ❌ InvalidSignatureError: {e}")
        raise HTTPException(status_code=400, detail="Invalid signature")

    print(f"[Webhook] Parsed {len(events)} events")

    for event in events:
        print(f"[Webhook] Processing event type: {type(event).__name__}")
        if _already_seen(getattr(event, "webhook_event_id", "") or ""):
            print(f"[Webhook] duplicate event {event.webhook_event_id}, skipped")
            continue
        try:
            if isinstance(event, MessageEvent):
                msg_type = type(event.message).__name__
                print(f"[Webhook] MessageEvent, message type: {msg_type}")
                if isinstance(event.message, TextMessageContent):
                    print(f"[Webhook] Text: {event.message.text[:80]}")
                    # Generation (retries + fallback) can outlast LINE's patience;
                    # answer the webhook now and reply from the background.
                    background_tasks.add_task(line_handler.handle_text_message, event, time.monotonic())
                elif isinstance(event.message, ImageMessageContent):
                    await line_handler.handle_image_message(event, background_tasks)
                elif isinstance(event.message, FileMessageContent):
                    await line_handler.handle_file_message(event, background_tasks)
                else:
                    print(f"[Webhook] ⚠️ Unhandled message type: {msg_type}")
            elif isinstance(event, PostbackEvent):
                print(f"[Webhook] PostbackEvent data: {event.postback.data}")
                await line_handler.handle_postback(event, background_tasks)
            else:
                print(f"[Webhook] ⚠️ Unhandled event type: {type(event).__name__}")
        except Exception as e:
            import traceback
            print(f"[Webhook] ❌ Error processing event: {e}")
            print(traceback.format_exc())

    return "OK"
