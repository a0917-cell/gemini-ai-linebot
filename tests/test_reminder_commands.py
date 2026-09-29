"""T11: reminder commands in the LINE chat.
- a message containing 提醒我 creates a reminder (or asks / refuses, AC7)
- 我的提醒 lists the caller's pending reminders with cancel buttons
- a cancel button (postback) cancels, and needs no upload session
Everything else still goes to the RAG answer."""
import asyncio
import time
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi import BackgroundTasks
from linebot.v3.messaging import QuickReply

import app.gemini_service as gemini
import app.line_handler as line
import app.reminder_parse as rp
import app.reminders as rem

TPE = rem.TAIPEI
A = "U" + "a" * 32
B = "U" + "b" * 32
DUE = datetime(2026, 9, 30, 9, 0, tzinfo=TPE)  # a Wednesday


@pytest.fixture
def chat(monkeypatch):
    store = rem.InMemoryReminderStore()
    sent = []

    async def fake_reply(reply_token, text, quick_reply=None):
        sent.append(SimpleNamespace(via="reply", text=text, quick_reply=quick_reply))

    async def fake_push(user_id, text, quick_reply=None):
        sent.append(SimpleNamespace(via="push", text=text, quick_reply=quick_reply))

    async def no_loading(user_id, seconds=60):
        pass

    async def rag_answer(text, user_id):
        return f"RAG:{text}"

    parsed = {}

    async def fake_parse(message, now):
        parsed["message"], parsed["now"] = message, now
        return parsed.get("result", rp.ParsedReminder(DUE, "繳圖"))

    monkeypatch.setattr(line, "_reply", fake_reply)
    monkeypatch.setattr(line, "_push", fake_push)
    monkeypatch.setattr(line, "_show_loading", no_loading)
    monkeypatch.setattr(gemini, "query_with_text", rag_answer)
    monkeypatch.setattr(rp, "parse_reminder", fake_parse)
    monkeypatch.setattr(rem, "get_store", lambda: store)
    return SimpleNamespace(store=store, sent=sent, parsed=parsed)


def say(text, user=A):
    event = SimpleNamespace(message=SimpleNamespace(text=text), source=SimpleNamespace(user_id=user),
                            reply_token="rt")
    asyncio.run(line.handle_text_message(event))


def press(data, user=A):
    event = SimpleNamespace(postback=SimpleNamespace(data=data), source=SimpleNamespace(user_id=user),
                            reply_token="rt")
    asyncio.run(line.handle_postback(event, BackgroundTasks()))


def buttons(msg):
    return [(i.action.label, i.action.data) for i in msg.quick_reply.items] if msg.quick_reply else []


# --- create ---

def test_reminder_is_created_and_confirmed(chat):
    say("明天 9 點提醒我繳圖")

    [r] = chat.store.list_pending(A)
    assert r.due_at == DUE and r.text == "繳圖"
    assert chat.sent[-1].text == "⏰ 已設定：9/30（三）09:00 繳圖"
    assert chat.parsed["message"] == "明天 9 點提醒我繳圖"
    assert chat.parsed["now"].utcoffset() == timedelta(hours=8)


def test_confirmation_offers_to_cancel_that_reminder(chat):
    say("明天 9 點提醒我繳圖")

    [r] = chat.store.list_pending(A)
    assert [d for _, d in buttons(chat.sent[-1])] == [f"action=cancel_reminder&id={r.id}"]


@pytest.mark.parametrize("problem", [rp.PAST, rp.MISSING_TIME, rp.MISSING_TEXT])
def test_problems_are_explained_and_nothing_is_created(chat, problem):
    chat.parsed["result"] = rp.ParsedReminder(None, "繳圖", problem)

    say("昨天 9 點提醒我繳圖")

    assert chat.store.list_pending(A) == []
    assert chat.sent[-1].text == rp.MESSAGES[problem]


def test_ordinary_messages_still_go_to_rag(chat):
    say("樓梯最小寬度？")

    assert chat.sent[-1].text == "RAG:樓梯最小寬度？"
    assert "message" not in chat.parsed and chat.store.list_pending(A) == []


# --- list ---

def test_list_shows_only_my_pending_reminders_in_order(chat):
    late = chat.store.add(A, DUE + timedelta(days=1), "送審")
    early = chat.store.add(A, DUE, "繳圖")
    chat.store.add(B, DUE, "別人的")
    done = chat.store.add(A, DUE, "已送出")
    chat.store.mark_sent(done.id, DUE)

    say("我的提醒")

    msg = chat.sent[-1]
    assert msg.text == "⏰ 你的提醒（2 筆）：\n1. 9/30（三）09:00 繳圖\n2. 10/1（四）09:00 送審"
    assert [d for _, d in buttons(msg)] == [
        f"action=cancel_reminder&id={early.id}", f"action=cancel_reminder&id={late.id}"]
    assert "message" not in chat.parsed


def test_list_command_tolerates_surrounding_spaces(chat):
    say("  我的提醒 ")
    assert chat.sent[-1].text == line.NO_REMINDERS


def test_empty_list(chat):
    say("我的提醒")

    assert chat.sent[-1].text == line.NO_REMINDERS and chat.sent[-1].quick_reply is None


def test_buttons_respect_line_limits(chat):
    for i in range(15):
        chat.store.add(A, DUE + timedelta(minutes=i), "一個非常非常非常長的提醒內容需要被截斷才行")

    say("我的提醒")

    items = buttons(chat.sent[-1])
    assert len(items) == 13  # LINE: at most 13 quick reply buttons
    assert all(len(label) <= 20 for label, _ in items)  # LINE: label at most 20 characters
    assert chat.sent[-1].text.count("\n") == 15  # the list itself is complete


# --- cancel ---

def test_cancel_button_works_without_an_upload_session(chat):
    r = chat.store.add(A, DUE, "繳圖")

    press(f"action=cancel_reminder&id={r.id}")

    assert chat.store.list_pending(A) == []
    assert chat.sent[-1].text == "🗑️ 已取消提醒：9/30（三）09:00 繳圖"


def test_cannot_cancel_someone_elses_reminder(chat):
    r = chat.store.add(B, DUE, "別人的")

    press(f"action=cancel_reminder&id={r.id}", user=A)

    assert [x.id for x in chat.store.list_pending(B)] == [r.id]
    assert chat.sent[-1].text == line.ALREADY_GONE


def test_cancelling_twice_says_it_is_gone(chat):
    r = chat.store.add(A, DUE, "繳圖")
    press(f"action=cancel_reminder&id={r.id}")

    press(f"action=cancel_reminder&id={r.id}")

    assert chat.sent[-1].text == line.ALREADY_GONE


def test_create_list_cancel_round_trip(chat):
    say("明天 9 點提醒我繳圖")
    say("我的提醒")
    [(_, data)] = buttons(chat.sent[-1])

    press(data)
    say("我的提醒")

    assert chat.sent[-1].text == line.NO_REMINDERS


# --- a slow answer goes out by push and keeps its buttons ---

def test_stale_reply_token_pushes_with_the_cancel_buttons(chat):
    chat.store.add(A, DUE, "繳圖")
    event = SimpleNamespace(message=SimpleNamespace(text="我的提醒"), source=SimpleNamespace(user_id=A),
                            reply_token="rt")

    asyncio.run(line.handle_text_message(event, received_at=time.monotonic() - 120))

    assert chat.sent[-1].via == "push" and len(buttons(chat.sent[-1])) == 1


def test_push_request_carries_the_quick_reply(monkeypatch):
    captured = []

    class FakeClient:
        def __init__(self, configuration):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    class FakeApi:
        def __init__(self, client):
            pass

        async def push_message(self, request):
            captured.append(request)

    monkeypatch.setattr(line, "AsyncApiClient", FakeClient)
    monkeypatch.setattr(line, "AsyncMessagingApi", FakeApi)
    r = rem.Reminder(id="abc", user_id=A, due_at=DUE, text="繳圖")
    qr = QuickReply(items=[line._cancel_button(r, "取消這個提醒")])

    asyncio.run(line._push(A, "⏰ 已設定", qr))

    assert captured[0].to == A and captured[0].messages[0].quick_reply == qr


# --- failures stay friendly ---

def test_unconfigured_store_is_explained(chat, monkeypatch):
    def not_configured():
        raise rem.RemindersNotConfigured("REMINDER_SHEET_ID is not set")
    monkeypatch.setattr(rem, "get_store", not_configured)

    say("明天 9 點提醒我繳圖")
    say("我的提醒")

    assert [m.text for m in chat.sent] == [line.REMINDERS_OFF, line.REMINDERS_OFF]


def test_store_errors_never_leak_to_the_chat(chat, monkeypatch):
    def broken(*args, **kwargs):
        raise RuntimeError("HttpError 403 quota {json}")
    monkeypatch.setattr(chat.store, "add", broken)

    say("明天 9 點提醒我繳圖")

    assert "HttpError" not in chat.sent[-1].text and "失敗" in chat.sent[-1].text
