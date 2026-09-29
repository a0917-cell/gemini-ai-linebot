import asyncio
import hmac
import os
import time

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
