import asyncio
import os

from dotenv import load_dotenv

load_dotenv()

from fastapi import FastAPI, HTTPException, Request, BackgroundTasks
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


@app.get("/health")
async def health() -> dict:
    return {"status": "ok", "store": gemini._store_name or "not initialized"}


@app.get("/store/info")
async def store_info() -> dict:
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
        raise HTTPException(status_code=500, detail=str(e))


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
                    await line_handler.handle_text_message(event)
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
