"""Turn "明天 9 點提醒我繳圖" into a due time (spec AC7, plan T10).

Gemini only splits the sentence into fields (structured output); the rules
that decide the time live in resolve(), so they are deterministic and tested:
- no date and no hour -> ask for a time, never guess one
- a time at or before now -> refuse; the caller creates nothing
- an hour without 早上/晚上 -> the nearest future of h:00 and (h+12):00
- a date without an hour -> DEFAULT_HOUR that day
"""
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Optional

from google.genai import types
from pydantic import BaseModel, ValidationError

import app.gemini_service as gemini
from app.reminders import TAIPEI

DEFAULT_HOUR = 9
PAST, MISSING_TIME, MISSING_TEXT = "past", "missing_time", "missing_text"
MESSAGES = {
    PAST: "這個時間已經過了，請給一個之後的時間。",
    MISSING_TIME: "要在什麼時間提醒？例如「明天 9 點提醒我繳圖」。",
    MISSING_TEXT: "要提醒你什麼事？例如「明天 9 點提醒我繳圖」。",
}
_WEEKDAYS = "一二三四五六日"


class ReminderFields(BaseModel):
    date: Optional[str] = None  # YYYY-MM-DD
    hour: Optional[int] = None  # 0-23
    minute: Optional[int] = None
    period_given: bool = False  # 早上/下午/晚上… or a 24-hour time was said
    text: Optional[str] = ""


@dataclass(frozen=True)
class ParsedReminder:
    due_at: Optional[datetime]
    text: str
    problem: Optional[str] = None  # PAST / MISSING_TIME / MISSING_TEXT


def _prompt(now: datetime) -> str:
    stamp = f"{now:%Y-%m-%d}（{_WEEKDAYS[now.weekday()]}）{now:%H:%M}"
    return (
        f"現在時間：{stamp}，台灣時間。從使用者設定提醒的句子擷取欄位：\n"
        "- date：YYYY-MM-DD。把「今天」「明天」「下週一」「月底」換算成日期；句子沒提到日期就填 null。\n"
        "- hour：0–23。有說下午、晚上就換成 24 小時制（下午 3 點 = 15）；沒提到幾點就填 null。\n"
        "- minute：0–59；「半」= 30；沒說就填 null。\n"
        "- period_given：有說早上、上午、中午、下午、傍晚、晚上、凌晨，或用 24 小時制（15 點、21:00），填 true；只說「9 點」填 false。\n"
        "- text：要提醒的事，去掉時間和「提醒我」這類字。\n"
        "句子沒提到的欄位一律填 null，不要自己補。"
    )


def _day(value: Optional[str]) -> Optional[date]:
    try:
        return date.fromisoformat(value) if value else None
    except ValueError:
        return None


def resolve(f: ReminderFields, now: datetime) -> ParsedReminder:
    now = now.astimezone(TAIPEI)
    text = (f.text or "").strip()
    day = _day(f.date)
    hour = f.hour if f.hour is not None and 0 <= f.hour <= 23 else None
    minute = f.minute if f.minute is not None and 0 <= f.minute <= 59 else 0

    if day is None and hour is None:
        return ParsedReminder(None, text, MISSING_TIME)
    if not text:
        return ParsedReminder(None, "", MISSING_TEXT)

    if hour is None:
        hours, minute = [DEFAULT_HOUR], 0
    elif f.period_given or hour == 0 or hour >= 12:
        hours = [hour]
    else:
        hours = [hour, hour + 12]
    days = [day] if day else [now.date(), now.date() + timedelta(days=1)]
    candidates = sorted(
        datetime(d.year, d.month, d.day, h, minute, tzinfo=TAIPEI) for d in days for h in hours
    )
    future = [c for c in candidates if c > now]
    if not future:
        return ParsedReminder(None, text, PAST)
    return ParsedReminder(future[0], text)


async def parse_reminder(message: str, now: datetime) -> ParsedReminder:
    now = now.astimezone(TAIPEI)
    response = await gemini._generate_with_retry(
        model=gemini.GEN_MODEL,
        contents=message,
        config=types.GenerateContentConfig(
            system_instruction=_prompt(now),
            response_mime_type="application/json",
            response_schema=ReminderFields,
        ),
    )
    try:
        fields = ReminderFields.model_validate_json(response.text or "")
    except ValidationError as e:  # unreadable output: ask rather than guess
        print(f"[Reminder] unparseable model output: {e}")
        fields = ReminderFields()
    return resolve(fields, now)
