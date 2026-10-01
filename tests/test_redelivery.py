"""T25: LINE may deliver the same webhook event again (redelivery, or a retry
after a slow answer). Each event carries a webhookEventId; one already seen in
the last 6 hours is dropped, so "…提醒我…" cannot create the same reminder
twice. Kept in memory: Render runs a single instance, and a restart forgetting
the ids was accepted (user choice 2026-10-01)."""
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


def body(*event_ids, text="明天 9 點提醒我繳圖"):
    return json.dumps({"destination": "Uxxxx", "events": [{
        "type": "message", "mode": "active", "timestamp": 1700000000000,
        "source": {"type": "user", "userId": "U1"},
        "webhookEventId": eid, "deliveryContext": {"isRedelivery": False},
        "replyToken": "rt-" + eid,
        "message": {"type": "text", "id": "m-" + eid, "text": text, "quoteToken": "q"},
    } for eid in event_ids]})


class FakeRequest:
    def __init__(self, raw):
        self._body = raw.encode()
        sig = base64.b64encode(hmac.new(SECRET.encode(), raw.encode(), hashlib.sha256).digest()).decode()
        self.headers = {"X-Line-Signature": sig}

    async def body(self):
        return self._body


@pytest.fixture
def hook(monkeypatch):
    handled = []

    async def handler(event, received_at=None):
        handled.append(event.webhook_event_id)
    monkeypatch.setattr(main, "parser", WebhookParser(SECRET))
    monkeypatch.setattr(line, "handle_text_message", handler)
    monkeypatch.setattr(main, "_seen_events", {})
    monkeypatch.setenv("ALLOWED_USER_IDS", "U1")  # T24: the webhook serves listed users only
    clock = {"now": 1000.0}
    monkeypatch.setattr(main.time, "monotonic", lambda: clock["now"])

    def post(raw):
        bg = BackgroundTasks()
        asyncio.run(main.webhook(FakeRequest(raw), bg))
        for task in bg.tasks:
            asyncio.run(task.func(*task.args, **task.kwargs))
    post.handled, post.clock = handled, clock
    return post


def test_a_redelivered_event_is_handled_once(hook):
    hook(body("01EVENT"))
    hook(body("01EVENT"))

    assert hook.handled == ["01EVENT"]


def test_different_events_are_all_handled(hook):
    hook(body("01A", "01B"))
    hook(body("01C"))

    assert hook.handled == ["01A", "01B", "01C"]


def test_the_same_event_twice_in_one_delivery_is_handled_once(hook):
    hook(body("01A", "01A"))

    assert hook.handled == ["01A"]


def test_events_without_an_id_are_never_treated_as_duplicates(hook):
    # an empty id must not be remembered, or every id-less event after the
    # first would be dropped
    assert main._already_seen("") is False
    assert main._already_seen("") is False
    assert "" not in main._seen_events


def test_ids_expire_after_six_hours_and_are_pruned(hook):
    hook(body("01OLD"))
    hook.clock["now"] += main.EVENT_DEDUPE_SECONDS + 1

    hook(body("01NEW"))

    assert "01OLD" not in main._seen_events and "01NEW" in main._seen_events
    hook(body("01OLD"))
    assert hook.handled == ["01OLD", "01NEW", "01OLD"]
