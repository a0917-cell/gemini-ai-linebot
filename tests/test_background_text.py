"""T4: text replies are produced in the background.

The webhook used to await the whole Gemini call (retries + fallback can take
well over a minute) before answering LINE. Now it schedules the work and
returns at once; the background task shows a loading animation, then replies
with the reply token while it is still valid (LINE: "must be used within one
minute after receiving the webhook"), and falls back to push after that or if
the reply is rejected.
"""
import asyncio
import base64
import hashlib
import hmac
import json
from types import SimpleNamespace

import pytest
from fastapi import BackgroundTasks
from linebot.v3 import WebhookParser

import app.gemini_service as gemini
import app.line_handler as line
import app.main as main

SECRET = "test-secret"
BODY = json.dumps({
    "destination": "Uxxxx",
    "events": [{
        "type": "message", "mode": "active", "timestamp": 1700000000000,
        "source": {"type": "user", "userId": "U1"},
        "webhookEventId": "01EVENT", "deliveryContext": {"isRedelivery": False},
        "replyToken": "rt",
        "message": {"type": "text", "id": "m1", "text": "你好", "quoteToken": "q"},
    }],
})


class FakeRequest:
    def __init__(self, body, signature):
        self._body = body.encode()
        self.headers = {"X-Line-Signature": signature}

    async def body(self):
        return self._body


def _sign(body):
    return base64.b64encode(hmac.new(SECRET.encode(), body.encode(), hashlib.sha256).digest()).decode()


def test_webhook_schedules_text_handling_instead_of_awaiting_it(monkeypatch):
    monkeypatch.setattr(main, "parser", WebhookParser(SECRET))
    ran = []

    async def handler(event, received_at=None):
        ran.append(event.message.text)
    monkeypatch.setattr(line, "handle_text_message", handler)
    bg = BackgroundTasks()

    result = asyncio.run(main.webhook(FakeRequest(BODY, _sign(BODY)), bg))

    assert result == "OK"
    assert ran == []                      # not run inside the request
    assert len(bg.tasks) == 1 and bg.tasks[0].func is handler


# --- the background task itself ---

@pytest.fixture
def io(monkeypatch):
    """Capture reply / push / loading calls; control the clock."""
    calls = SimpleNamespace(reply=[], push=[], loading=[], now=100.0, reply_error=None)

    async def fake_reply(token, text, quick_reply=None):
        if calls.reply_error:
            raise calls.reply_error
        calls.reply.append(text)

    async def fake_push(user_id, text):
        calls.push.append(text)

    async def fake_loading(user_id, seconds=60):
        calls.loading.append(user_id)

    async def answer(text, user_id):
        return "回答"

    monkeypatch.setattr(line, "_reply", fake_reply)
    monkeypatch.setattr(line, "_push", fake_push)
    monkeypatch.setattr(line, "_show_loading", fake_loading)
    monkeypatch.setattr(line.time, "monotonic", lambda: calls.now)
    monkeypatch.setattr(gemini, "query_with_text", answer)
    return calls


def _event(text="你好"):
    return SimpleNamespace(reply_token="rt", source=SimpleNamespace(user_id="U1"),
                           message=SimpleNamespace(text=text))


def test_fast_answer_uses_the_free_reply_token(io):
    asyncio.run(line.handle_text_message(_event(), received_at=90.0))   # 10 s old

    assert io.reply == ["回答"] and io.push == []


def test_slow_answer_falls_back_to_push(io):
    asyncio.run(line.handle_text_message(_event(), received_at=40.0))   # 60 s old

    assert io.push == ["回答"] and io.reply == []


def test_rejected_reply_token_falls_back_to_push(io):
    io.reply_error = Exception("400 Invalid reply token")

    asyncio.run(line.handle_text_message(_event(), received_at=95.0))

    assert io.push == ["回答"]


def test_loading_animation_is_shown_for_the_sender(io):
    asyncio.run(line.handle_text_message(_event(), received_at=99.0))

    assert io.loading == ["U1"]


def test_failed_loading_animation_does_not_block_the_answer(io, monkeypatch):
    async def broken(user_id, seconds=60):
        raise Exception("429 Too Many Requests")
    monkeypatch.setattr(line, "_show_loading", broken)

    asyncio.run(line.handle_text_message(_event(), received_at=99.0))

    assert io.reply == ["回答"]
