"""T24: anyone who added the bot could use it, and strangers would spend the
owner's 20 free requests a day on the primary model. Only LINE users listed in
ALLOWED_USER_IDS (comma-separated) get through; everyone else gets one free
reply and no handler runs. Unset means nobody (fails closed, like ADMIN_TOKEN
and CRON_SECRET), so the variable goes on Render before this deploys."""
import asyncio
import base64
import hashlib
import hmac
import json

import pytest
from fastapi import BackgroundTasks
from linebot.v3 import WebhookParser

import app.line_handler as line
import app.main as main

SECRET = "test-secret"
OWNER = "U" + "a" * 32
STRANGER = "U" + "b" * 32


def event(user, kind="text", eid=None):
    eid = eid or f"01{kind}{user[-4:]}"
    base = {"mode": "active", "timestamp": 1700000000000, "source": {"type": "user", "userId": user},
            "webhookEventId": eid, "deliveryContext": {"isRedelivery": False}, "replyToken": "rt-" + eid}
    if kind == "postback":
        return {**base, "type": "postback", "postback": {"data": "action=store"}}
    message = {"text": {"type": "text", "id": "m" + eid, "text": "你好", "quoteToken": "q"},
               "image": {"type": "image", "id": "m" + eid, "quoteToken": "q",
                         "contentProvider": {"type": "line"}},
               "file": {"type": "file", "id": "m" + eid, "fileName": "a.pdf", "fileSize": 10}}[kind]
    return {**base, "type": "message", "message": message}


class FakeRequest:
    def __init__(self, events):
        raw = json.dumps({"destination": "Uxxxx", "events": events})
        self._body = raw.encode()
        sig = base64.b64encode(hmac.new(SECRET.encode(), raw.encode(), hashlib.sha256).digest()).decode()
        self.headers = {"X-Line-Signature": sig}

    async def body(self):
        return self._body


@pytest.fixture
def hook(monkeypatch):
    seen = {"handled": [], "private": []}

    async def text(ev, received_at=None):
        seen["handled"].append(("text", ev.source.user_id))

    async def other(kind):
        async def handler(ev, bg):
            seen["handled"].append((kind, ev.source.user_id))
        return handler

    async def private(token):
        seen["private"].append(token)

    monkeypatch.setattr(main, "parser", WebhookParser(SECRET))
    monkeypatch.setattr(line, "handle_text_message", text)
    monkeypatch.setattr(line, "handle_image_message", asyncio.run(other("image")))
    monkeypatch.setattr(line, "handle_file_message", asyncio.run(other("file")))
    monkeypatch.setattr(line, "handle_postback", asyncio.run(other("postback")))
    monkeypatch.setattr(line, "reply_private", private)

    def post(*events):
        bg = BackgroundTasks()
        asyncio.run(main.webhook(FakeRequest(list(events)), bg))
        for task in bg.tasks:
            asyncio.run(task.func(*task.args, **task.kwargs))
    post.seen = seen
    return post


@pytest.mark.parametrize("kind", ["text", "image", "file", "postback"])
def test_listed_user_gets_through(hook, monkeypatch, kind):
    monkeypatch.setenv("ALLOWED_USER_IDS", OWNER)

    hook(event(OWNER, kind))

    assert hook.seen["handled"] == [(kind, OWNER)] and hook.seen["private"] == []


@pytest.mark.parametrize("kind", ["text", "image", "file", "postback"])
def test_stranger_gets_one_reply_and_no_handler(hook, monkeypatch, kind):
    monkeypatch.setenv("ALLOWED_USER_IDS", OWNER)

    hook(event(STRANGER, kind))

    assert hook.seen["handled"] == []
    assert hook.seen["private"] == [f"rt-01{kind}{STRANGER[-4:]}"]


def test_unset_allowlist_lets_nobody_in(hook, monkeypatch):
    monkeypatch.delenv("ALLOWED_USER_IDS", raising=False)

    hook(event(OWNER))

    assert hook.seen["handled"] == []


def test_list_takes_several_ids_with_spaces(hook, monkeypatch):
    monkeypatch.setenv("ALLOWED_USER_IDS", f" {STRANGER} , {OWNER} ,")

    hook(event(OWNER), event(STRANGER))

    assert sorted(u for _, u in hook.seen["handled"]) == sorted([OWNER, STRANGER])


def test_blank_entries_never_match_a_missing_user_id(monkeypatch):
    monkeypatch.setenv("ALLOWED_USER_IDS", f"{OWNER},,")

    assert line.is_allowed(OWNER)
    assert not line.is_allowed("") and not line.is_allowed(None)


def test_private_reply_text():
    assert "私人" in line.PRIVATE_NOTE
