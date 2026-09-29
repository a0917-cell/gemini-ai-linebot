import os
import asyncio
import mimetypes
import time
import traceback
from datetime import datetime
from typing import Optional

from fastapi import BackgroundTasks
from linebot.v3.messaging import (
    AsyncApiClient,
    AsyncMessagingApi,
    AsyncMessagingApiBlob,
    Configuration,
    ReplyMessageRequest,
    PushMessageRequest,
    ShowLoadingAnimationRequest,
    TextMessage,
    QuickReply,
    QuickReplyItem,
    PostbackAction,
)
from linebot.v3.webhooks import (
    MessageEvent,
    PostbackEvent,
    TextMessageContent,
    ImageMessageContent,
    FileMessageContent,
)
from google.cloud import storage as gcs

from app.session import session_store
import app.gemini_service as gemini
import app.reminder_parse as rp
import app.reminders as rem

LINE_CHANNEL_ACCESS_TOKEN = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN", "")
GCS_BUCKET = os.environ.get("GCS_BUCKET", "")

configuration = Configuration(access_token=LINE_CHANNEL_ACCESS_TOKEN)

# Max file size LINE Bot supports: ~10MB for images, ~50MB for files
MAX_STORE_SIZE_BYTES = 100 * 1024 * 1024  # 100MB (Gemini limit)

# The bot runs on Gemini's free tier, whose content may be used to improve
# Google's products (ai.google.dev pricing, 2026-09-29). Shown whenever a file
# arrives, before the user chooses to store it.
PRIVACY_NOTE = "🔒 提醒：這個助手用的是免費版 AI，內容可能被 Google 用來改進產品，請不要上傳公司機密或客戶資料。"

# LINE: "Reply tokens must be used within one minute after receiving the webhook."
# Keep a margin; past this, answers go out by push instead.
REPLY_TOKEN_BUDGET_S = 50

# Reminder commands (plan T11). Anything else goes to the RAG answer.
REMINDER_TRIGGER = "提醒我"
LIST_COMMAND = "我的提醒"
CANCEL_PREFIX = "action=cancel_reminder&id="
NO_REMINDERS = "目前沒有待送的提醒。設定方式：「明天 9 點提醒我繳圖」。"
ALREADY_GONE = "這個提醒已經送出或取消了。"
REMINDERS_OFF = "⚠️ 提醒功能還沒設定好，暫時無法使用。"
MAX_QUICK_REPLY = 13  # LINE: at most 13 quick reply buttons
MAX_LABEL = 20  # LINE: action label at most 20 characters
_WEEKDAYS = "一二三四五六日"


# --- Helpers ---

def _choice_quick_reply() -> QuickReply:
    return QuickReply(
        items=[
            QuickReplyItem(
                action=PostbackAction(
                    label="📥 存入資料庫",
                    data="action=store",
                    display_text="存入資料庫",
                )
            ),
            QuickReplyItem(
                action=PostbackAction(
                    label="🔍 作為搜尋",
                    data="action=search",
                    display_text="作為搜尋",
                )
            ),
        ]
    )


async def _reply(
    reply_token: str,
    text: str,
    quick_reply: Optional[QuickReply] = None,
) -> None:
    msg = TextMessage(text=text, quick_reply=quick_reply)
    async with AsyncApiClient(configuration) as api_client:
        api = AsyncMessagingApi(api_client)
        await api.reply_message(
            ReplyMessageRequest(reply_token=reply_token, messages=[msg])
        )


async def _push(user_id: str, text: str, quick_reply: Optional[QuickReply] = None) -> None:
    async with AsyncApiClient(configuration) as api_client:
        api = AsyncMessagingApi(api_client)
        await api.push_message(
            PushMessageRequest(to=user_id, messages=[TextMessage(text=text, quick_reply=quick_reply)])
        )


async def _show_loading(user_id: str, seconds: int = 60) -> None:
    """LINE's typing indicator while the answer is being generated (1:1 chats)."""
    async with AsyncApiClient(configuration) as api_client:
        await AsyncMessagingApi(api_client).show_loading_animation(
            ShowLoadingAnimationRequest(chat_id=user_id, loading_seconds=seconds)
        )


async def _deliver(
    reply_token: str,
    user_id: str,
    text: str,
    received_at: float,
    quick_reply: Optional[QuickReply] = None,
) -> None:
    """Reply while the token is still fresh (replies are free); otherwise, or if
    LINE rejects the token, push (counts against the monthly message quota)."""
    if time.monotonic() - received_at < REPLY_TOKEN_BUDGET_S:
        try:
            await _reply(reply_token, text, quick_reply)
            return
        except Exception as e:
            print(f"[Reply] rejected, falling back to push: {e}")
    await _push(user_id, text, quick_reply)


def _friendly_error(action: str, exc: Exception) -> str:
    """Log the real error and return only a Chinese explanation for the user.
    Raw exception text (status codes, JSON bodies) must never reach LINE.
    Call from inside the except block so the traceback is available."""
    print(f"[{action}] Error: {exc}\n{traceback.format_exc()}")
    if gemini._is_transient(exc):
        return "⚠️ AI 忙線中，請 1 分鐘後再傳一次 🙏"
    return f"❌ {action}失敗，請稍後再試一次。"


def _fmt_due(dt: datetime) -> str:
    """2026-09-30 09:00 -> 9/30（三）09:00"""
    dt = dt.astimezone(rem.TAIPEI)
    return f"{dt.month}/{dt.day}（{_WEEKDAYS[dt.weekday()]}）{dt:%H:%M}"


def _cancel_button(reminder, label: str) -> QuickReplyItem:
    return QuickReplyItem(
        action=PostbackAction(
            label=label[:MAX_LABEL],
            data=f"{CANCEL_PREFIX}{reminder.id}",
            display_text=f"取消提醒：{reminder.text}"[:300],
        )
    )


async def _reminder_command(text: str, user_id: str):
    """(reply text, quick reply) for 我的提醒 / …提醒我…, or None for other text.
    Sheets calls are blocking, so they run in a worker thread."""
    is_list = text == LIST_COMMAND
    if not is_list and REMINDER_TRIGGER not in text:
        return None
    try:
        store = await asyncio.to_thread(rem.get_store)
        if is_list:
            pending = await asyncio.to_thread(store.list_pending, user_id)
            if not pending:
                return NO_REMINDERS, None
            lines = [f"{i}. {_fmt_due(r.due_at)} {r.text}" for i, r in enumerate(pending, start=1)]
            buttons = [_cancel_button(r, f"取消 {i}. {r.text}")
                       for i, r in enumerate(pending[:MAX_QUICK_REPLY], start=1)]
            return f"⏰ 你的提醒（{len(pending)} 筆）：\n" + "\n".join(lines), QuickReply(items=buttons)

        parsed = await rp.parse_reminder(text, datetime.now(rem.TAIPEI))
        if parsed.problem:
            return rp.MESSAGES[parsed.problem], None
        r = await asyncio.to_thread(store.add, user_id, parsed.due_at, parsed.text)
        return f"⏰ 已設定：{_fmt_due(r.due_at)} {r.text}", QuickReply(items=[_cancel_button(r, "取消這個提醒")])
    except rem.RemindersNotConfigured:
        return REMINDERS_OFF, None
    except Exception as e:
        return _friendly_error("讀取提醒" if is_list else "設定提醒", e), None


async def _cancel_reminder(reminder_id: str, user_id: str) -> str:
    try:
        store = await asyncio.to_thread(rem.get_store)
        pending = await asyncio.to_thread(store.list_pending, user_id)  # the caller's own only
        r = next((x for x in pending if x.id == reminder_id), None)
        if r is None or not await asyncio.to_thread(store.cancel, r.id, user_id):
            return ALREADY_GONE
        return f"🗑️ 已取消提醒：{_fmt_due(r.due_at)} {r.text}"
    except rem.RemindersNotConfigured:
        return REMINDERS_OFF
    except Exception as e:
        return _friendly_error("取消提醒", e)


async def _download_line_content(message_id: str) -> bytes:
    async with AsyncApiClient(configuration) as api_client:
        blob_api = AsyncMessagingApiBlob(api_client)
        content = await blob_api.get_message_content(message_id=message_id)
        return bytes(content)


async def _save_to_gcs(data: bytes, path: str, content_type: str) -> str:
    if not GCS_BUCKET:
        local_path = os.path.join("tmp_uploads", path.replace("/", os.sep))
        os.makedirs(os.path.dirname(local_path), exist_ok=True)
        with open(local_path, "wb") as f:
            f.write(data)
        return local_path

    client = gcs.Client()
    blob = client.bucket(GCS_BUCKET).blob(path)
    blob.upload_from_string(data, content_type=content_type)
    return f"gs://{GCS_BUCKET}/{path}"


async def _load_from_gcs(path: str) -> bytes:
    if not GCS_BUCKET:
        local_path = os.path.join("tmp_uploads", path.replace("/", os.sep))
        with open(local_path, "rb") as f:
            return f.read()

    client = gcs.Client()
    return client.bucket(GCS_BUCKET).blob(path).download_as_bytes()


# --- Background task ---

async def _bg_store_and_notify(
    user_id: str,
    gcs_path: str,
    mime_type: str,
    display_name: str,
) -> None:
    try:
        file_bytes = await _load_from_gcs(gcs_path)
        await gemini.upload_and_index(file_bytes, mime_type, display_name, user_id)
        await _push(user_id, f"✅ 已成功存入您的資料庫！\n📄 {display_name}")
    except Exception as e:
        await _push(user_id, _friendly_error("存入", e))


# --- Event Handlers ---

async def handle_text_message(event: MessageEvent, received_at: Optional[float] = None) -> None:
    """Runs as a background task: the webhook has already answered LINE.
    received_at is time.monotonic() when the webhook arrived (reply-token clock)."""
    if received_at is None:
        received_at = time.monotonic()
    text = event.message.text.strip()
    if not text:
        return

    user_id = event.source.user_id

    try:
        await _show_loading(user_id)
    except Exception as e:  # cosmetic only; never block the answer
        print(f"[Loading] could not show animation: {e}")

    command = await _reminder_command(text, user_id)
    if command is not None:
        answer, quick_reply = command
    else:
        quick_reply = None
        try:
            answer = await gemini.query_with_text(text, user_id)
        except Exception as e:
            answer = _friendly_error("查詢", e)

    await _deliver(event.reply_token, user_id, answer, received_at, quick_reply)


async def handle_image_message(
    event: MessageEvent, background_tasks: BackgroundTasks
) -> None:
    user_id = event.source.user_id
    message_id = event.message.id

    try:
        image_bytes = await _download_line_content(message_id)
        gcs_path = f"uploads/{user_id}/{message_id}.jpg"
        await _save_to_gcs(image_bytes, gcs_path, "image/jpeg")

        session_store.set(user_id, "gcs_path", gcs_path)
        session_store.set(user_id, "mime_type", "image/jpeg")
        session_store.set(user_id, "display_name", f"image_{message_id}.jpg")
        session_store.set(user_id, "content_type", "image")

        await _reply(
            event.reply_token,
            f"🖼️ 收到圖片！請問要：\n\n{PRIVACY_NOTE}",
            _choice_quick_reply(),
        )
    except Exception as e:
        await _reply(event.reply_token, _friendly_error("圖片處理", e))


async def handle_file_message(
    event: MessageEvent, background_tasks: BackgroundTasks
) -> None:
    user_id = event.source.user_id
    message_id = event.message.id
    filename = getattr(event.message, "file_name", None) or f"file_{message_id}"

    mime_type, _ = mimetypes.guess_type(filename)
    mime_type = mime_type or "application/octet-stream"
    ext = filename.rsplit(".", 1)[-1] if "." in filename else "bin"

    # Reject unsupported types (Gemini File Search limitation)
    unsupported = ("audio/", "video/")
    if any(mime_type.startswith(u) for u in unsupported):
        await _reply(
            event.reply_token,
            f"⚠️ 目前不支援音訊/影片檔案。\n"
            f"支援格式：PDF、圖片、TXT、CSV 等文字/文件類型。",
        )
        return

    try:
        file_bytes = await _download_line_content(message_id)
        gcs_path = f"uploads/{user_id}/{message_id}.{ext}"
        await _save_to_gcs(file_bytes, gcs_path, mime_type)

        session_store.set(user_id, "gcs_path", gcs_path)
        session_store.set(user_id, "mime_type", mime_type)
        session_store.set(user_id, "display_name", filename)
        session_store.set(user_id, "content_type", "file")

        await _reply(
            event.reply_token,
            f"📄 收到檔案：{filename}\n請問要：\n\n{PRIVACY_NOTE}",
            _choice_quick_reply(),
        )
    except Exception as e:
        await _reply(event.reply_token, _friendly_error("檔案處理", e))


async def handle_postback(
    event: PostbackEvent, background_tasks: BackgroundTasks
) -> None:
    user_id = event.source.user_id
    action = event.postback.data

    # Reminder cancel buttons carry their own id; they are not part of an upload session.
    if action.startswith(CANCEL_PREFIX):
        await _reply(event.reply_token, await _cancel_reminder(action[len(CANCEL_PREFIX):], user_id))
        return

    gcs_path = session_store.get(user_id, "gcs_path")
    mime_type = session_store.get(user_id, "mime_type")
    display_name = session_store.get(user_id, "display_name")
    content_type = session_store.get(user_id, "content_type")

    if not gcs_path:
        await _reply(
            event.reply_token,
            "⚠️ 工作階段已過期（5 分鐘），請重新上傳檔案。",
        )
        return

    session_store.clear(user_id)

    if action == "action=store":
        await _reply(
            event.reply_token,
            f"⏳ 正在建立索引，完成後會通知您...\n📄 {display_name}",
        )
        background_tasks.add_task(
            _bg_store_and_notify, user_id, gcs_path, mime_type, display_name
        )

    elif action == "action=search":
        try:
            file_bytes = await _load_from_gcs(gcs_path)

            if content_type == "image":
                answer = await gemini.query_with_image(file_bytes, mime_type, user_id)
            else:
                # Non-image file: query by filename as hint
                answer = await gemini.query_with_text(
                    f"請從資料庫中找到與《{display_name}》相關的資訊並說明。",
                    user_id,
                )

            await _reply(event.reply_token, answer)
        except Exception as e:
            await _reply(event.reply_token, _friendly_error("搜尋", e))
