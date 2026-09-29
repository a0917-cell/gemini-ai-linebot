"""T12 / AC6: /cron/tick pushes due reminders. UptimeRobot calls it every 5
minutes (it also keeps the Render service warm), so it must:
- refuse without the CRON_SECRET key (fails closed when unset)
- accept HEAD as well as GET (UptimeRobot's HTTP monitor defaults to HEAD)
- push first, then mark sent (user choice 2026-09-29: a duplicate beats a loss)
- leave a failed push pending for the next tick (user choice)
- say how late a reminder is when it is over 10 minutes late (user choice)
- never send one reminder twice from overlapping ticks"""
import asyncio
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

import app.line_handler as line
import app.main as main
import app.reminder_tick as tick
import app.reminders as rem

TPE = rem.TAIPEI
A = "U" + "a" * 32
B = "U" + "b" * 32
DUE = datetime(2026, 9, 30, 9, 0, tzinfo=TPE)  # a Wednesday
KEY = "correct-cron-secret"
client = TestClient(main.app)


class Pusher:
    def __init__(self, fail_for=()):
        self.sent, self.fail_for = [], set(fail_for)

    async def __call__(self, user_id, text, quick_reply=None):
        await asyncio.sleep(0)
        if user_id in self.fail_for:
            raise RuntimeError("429 monthly limit")
        self.sent.append((user_id, text))


def run(store, push, now=DUE):
    return asyncio.run(tick.run_tick(now, store, push))


# --- run_tick ---

def test_only_due_reminders_are_pushed_to_their_owner():
    store, push = rem.InMemoryReminderStore(), Pusher()
    store.add(A, DUE, "繳圖")
    store.add(B, DUE - timedelta(minutes=3), "送審")
    store.add(A, DUE + timedelta(minutes=1), "還沒到")

    result = run(store, push)

    assert push.sent == [(B, "⏰ 提醒：送審"), (A, "⏰ 提醒：繳圖")]
    assert result == {"due": 2, "sent": 2, "failed": 0}
    assert [r.text for r in store.list_pending(A)] == ["還沒到"]


def test_a_reminder_is_sent_once():
    store, push = rem.InMemoryReminderStore(), Pusher()
    store.add(A, DUE, "繳圖")

    run(store, push)
    run(store, push, DUE + timedelta(minutes=5))

    assert len(push.sent) == 1


@pytest.mark.parametrize("late, note", [
    (timedelta(minutes=10), None),
    (timedelta(minutes=11), "晚了 11 分鐘"),
    (timedelta(hours=3, minutes=20), "晚了 3 小時"),
    (timedelta(days=2, hours=5), "晚了 2 天"),
])
def test_late_reminders_say_how_late(late, note):
    store, push = rem.InMemoryReminderStore(), Pusher()
    store.add(A, DUE, "繳圖")

    run(store, push, DUE + late)

    [(_, text)] = push.sent
    if note is None:
        assert text == "⏰ 提醒：繳圖"
    else:
        assert text == f"⏰ 提醒：繳圖（原定 9/30（三）09:00，{note}）"


def test_failed_push_stays_pending_and_does_not_block_others():
    store, push = rem.InMemoryReminderStore(), Pusher(fail_for={A})
    store.add(A, DUE, "繳圖")
    store.add(B, DUE, "送審")

    result = run(store, push)

    assert push.sent == [(B, "⏰ 提醒：送審")]
    assert result == {"due": 2, "sent": 1, "failed": 1}
    assert [r.text for r in store.list_due(DUE)] == ["繳圖"]

    push.fail_for.clear()
    run(store, push, DUE + timedelta(minutes=5))
    assert push.sent[-1] == (A, "⏰ 提醒：繳圖")


def test_mark_failure_after_a_push_is_reported_and_retried(monkeypatch):
    store, push = rem.InMemoryReminderStore(), Pusher()
    store.add(A, DUE, "繳圖")

    def broken(*args):
        raise RuntimeError("sheets 503")
    monkeypatch.setattr(store, "mark_sent", broken)

    result = run(store, push)

    assert push.sent == [(A, "⏰ 提醒：繳圖")]
    assert result == {"due": 1, "sent": 0, "failed": 1}
    assert store.list_due(DUE)  # still pending: the next tick sends it again


def test_reminder_cancelled_during_the_tick_is_not_counted_as_sent(monkeypatch):
    store, push = rem.InMemoryReminderStore(), Pusher()
    r = store.add(A, DUE, "繳圖")
    real_mark = store.mark_sent

    def cancel_then_mark(reminder_id, sent_at):
        store.cancel(r.id, A)  # the user pressed cancel while the push was in flight
        return real_mark(reminder_id, sent_at)
    monkeypatch.setattr(store, "mark_sent", cancel_then_mark)

    result = run(store, push)

    assert result == {"due": 1, "sent": 0, "failed": 0}


def test_overlapping_ticks_send_each_reminder_once():
    store, push = rem.InMemoryReminderStore(), Pusher()
    for i in range(3):
        store.add(A, DUE, f"第{i}則")

    async def both():
        return await asyncio.gather(tick.run_tick(DUE, store, push), tick.run_tick(DUE, store, push))

    results = asyncio.run(both())

    assert len(push.sent) == 3
    assert {"busy": True} in results


# --- /cron/tick endpoint ---

@pytest.fixture
def wired(monkeypatch):
    store, push = rem.InMemoryReminderStore(), Pusher()
    monkeypatch.setenv("CRON_SECRET", KEY)
    monkeypatch.setattr(rem, "get_store", lambda: store)
    monkeypatch.setattr(line, "_push", push)
    store.add(A, datetime.now(TPE) - timedelta(minutes=1), "繳圖")
    return store, push


def test_wrong_or_missing_key_is_forbidden(wired):
    store, push = wired
    for url in ("/cron/tick", "/cron/tick?key=wrong", "/cron/tick?key="):
        assert client.get(url).status_code == 403
    assert push.sent == []


def test_unset_secret_fails_closed(wired, monkeypatch):
    monkeypatch.delenv("CRON_SECRET")

    assert client.get("/cron/tick?key=").status_code == 403
    assert client.get(f"/cron/tick?key={KEY}").status_code == 403


def test_get_with_key_sends_due_reminders(wired):
    store, push = wired

    resp = client.get(f"/cron/tick?key={KEY}")

    assert resp.status_code == 200
    assert resp.json() == {"status": "ok", "due": 1, "sent": 1, "failed": 0}
    assert push.sent == [(A, "⏰ 提醒：繳圖")]


def test_head_is_accepted_and_runs_the_tick(wired):
    store, push = wired

    resp = client.head(f"/cron/tick?key={KEY}")

    assert resp.status_code == 200 and len(push.sent) == 1


def test_unconfigured_reminders_still_answer_ok(wired, monkeypatch):
    def not_configured():
        raise rem.RemindersNotConfigured("REMINDER_SHEET_ID is not set")
    monkeypatch.setattr(rem, "get_store", not_configured)

    resp = client.get(f"/cron/tick?key={KEY}")

    assert resp.status_code == 200 and resp.json() == {"status": "ok", "reminders": "off"}


def test_store_outage_is_a_503_without_details(wired, monkeypatch):
    store, push = wired

    def broken(now):
        raise RuntimeError("HttpError 500 {secret json}")
    monkeypatch.setattr(store, "list_due", broken)

    resp = client.get(f"/cron/tick?key={KEY}")

    assert resp.status_code == 503 and "HttpError" not in resp.text
