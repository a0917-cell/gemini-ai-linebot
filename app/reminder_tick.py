"""Send due reminders (plan T12). /cron/tick calls run_tick every 5 minutes
via UptimeRobot. Decisions (user, 2026-09-29):
- push first, then mark sent: a failed mark means a duplicate next tick,
  never a lost reminder
- a failed push stays pending and is retried next tick
- a reminder over LATE_AFTER late says when it was due and how late it is
"""
import asyncio
from datetime import datetime, timedelta

from app.line_handler import _fmt_due

LATE_AFTER = timedelta(minutes=10)
# Overlapping ticks would both see the same pending rows and push them twice.
# Render runs one instance, so an in-process lock is enough.
_lock = asyncio.Lock()


def _how_late(delta: timedelta) -> str:
    minutes = int(delta.total_seconds() // 60)
    if minutes < 60:
        return f"{minutes} 分鐘"
    if minutes < 48 * 60:
        return f"{minutes // 60} 小時"
    return f"{minutes // (24 * 60)} 天"


def reminder_text(reminder, now: datetime) -> str:
    late = now - reminder.due_at
    if late <= LATE_AFTER:
        return f"⏰ 提醒：{reminder.text}"
    return f"⏰ 提醒：{reminder.text}（原定 {_fmt_due(reminder.due_at)}，晚了 {_how_late(late)}）"


async def run_tick(now: datetime, store, push) -> dict:
    """push(user_id, text) sends one LINE message. Store errors in list_due
    propagate (the endpoint turns them into a 503 so the monitor alerts)."""
    if _lock.locked():
        return {"busy": True}
    async with _lock:
        due = await asyncio.to_thread(store.list_due, now)
        sent = failed = 0
        for r in due:
            try:
                await push(r.user_id, reminder_text(r, now))
            except Exception as e:
                failed += 1
                print(f"[Tick] push failed for {r.id}, retrying next tick: {e}")
                continue
            try:
                if await asyncio.to_thread(store.mark_sent, r.id, now):
                    sent += 1
            except Exception as e:
                failed += 1
                print(f"[Tick] {r.id} was pushed but not marked; the next tick may send it again: {e}")
        return {"due": len(due), "sent": sent, "failed": failed}
