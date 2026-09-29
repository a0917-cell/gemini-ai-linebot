"""T10 / AC7: reminder times. Gemini only splits the sentence into fields; the
rules live in resolve() so they are deterministic and testable:
- no date and no hour -> ask for a time (never guess one)
- a past time -> refuse, nothing is created
- "9 點" without 早上/晚上 -> the nearest future of 09:00 / 21:00 (user choice 2026-09-29)
- a date without an hour -> 09:00 that day (user choice 2026-09-29)"""
import asyncio
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

import app.gemini_service as gemini
import app.reminder_parse as rp
from app.reminders import TAIPEI

NOW = datetime(2026, 9, 29, 15, 2, tzinfo=TAIPEI)  # a Tuesday
TODAY, TOMORROW, YESTERDAY = "2026-09-29", "2026-09-30", "2026-09-28"


def fields(date=None, hour=None, minute=None, period_given=False, text="繳圖"):
    return rp.ReminderFields(date=date, hour=hour, minute=minute, period_given=period_given, text=text)


def at(day, h, m=0):
    y, mo, d = map(int, day.split("-"))
    return datetime(y, mo, d, h, m, tzinfo=TAIPEI)


# --- normal ---

def test_explicit_date_and_time():
    r = rp.resolve(fields(TOMORROW, 9, period_given=True), NOW)
    assert r.problem is None and r.due_at == at(TOMORROW, 9) and r.text == "繳圖"


def test_minutes_are_kept():
    assert rp.resolve(fields(TOMORROW, 14, 30, True), NOW).due_at == at(TOMORROW, 14, 30)


def test_result_is_in_taipei_time_even_when_now_is_utc():
    r = rp.resolve(fields(TOMORROW, 9, period_given=True), NOW.astimezone(timezone.utc))
    assert r.due_at.utcoffset() == timedelta(hours=8)


def test_today_is_the_taipei_date_not_the_utc_date():
    # 07:30 on 9/30 in Taipei is still 9/29 in UTC; "凌晨 5 點" means 10/1 05:00,
    # which a UTC-dated today/tomorrow window (9/29, 9/30) would call past
    now_utc = at("2026-09-30", 7, 30).astimezone(timezone.utc)
    assert rp.resolve(fields(hour=5, period_given=True), now_utc).due_at == at("2026-10-01", 5)


# --- ambiguous hour: nearest future ---

@pytest.mark.parametrize("now, expected", [
    (NOW.replace(hour=15), at(TODAY, 21)),   # afternoon: tonight
    (NOW.replace(hour=8), at(TODAY, 9)),     # early morning: this morning
    (NOW.replace(hour=22), at(TOMORROW, 9)), # late night: tomorrow morning
])
def test_hour_without_period_takes_the_nearest_future(now, expected):
    assert rp.resolve(fields(hour=9), now).due_at == expected


def test_ambiguous_hour_on_a_given_date_takes_the_earliest_future():
    assert rp.resolve(fields(TOMORROW, 9), NOW).due_at == at(TOMORROW, 9)


def test_period_given_is_not_second_guessed():
    # "早上 9 點" said at 15:00 today is past, not silently moved to 21:00
    assert rp.resolve(fields(TODAY, 9, period_given=True), NOW).problem == rp.PAST


@pytest.mark.parametrize("hour", [0, 12, 13, 21])
def test_unambiguous_hours_have_one_reading(hour):
    r = rp.resolve(fields(TOMORROW, hour), NOW)
    assert r.due_at == at(TOMORROW, hour)


# --- date without time ---

def test_date_without_hour_defaults_to_nine():
    assert rp.resolve(fields(TOMORROW), NOW).due_at == at(TOMORROW, 9)


def test_today_without_hour_after_nine_is_past():
    assert rp.resolve(fields(TODAY), NOW).problem == rp.PAST


# --- AC7: never create for past or missing time ---

def test_yesterday_is_past():
    r = rp.resolve(fields(YESTERDAY, 9), NOW)
    assert r.problem == rp.PAST and r.due_at is None


def test_exactly_now_is_past():
    assert rp.resolve(fields(TODAY, 15, 2, True), NOW).problem == rp.PAST


def test_no_date_and_no_hour_asks_for_a_time():
    r = rp.resolve(fields(), NOW)
    assert r.problem == rp.MISSING_TIME and r.due_at is None


@pytest.mark.parametrize("bad", [
    dict(date="明天"), dict(date="2026-13-01"), dict(hour=24), dict(hour=-1),
])
def test_malformed_fields_count_as_missing(bad):
    assert rp.resolve(fields(**bad), NOW).problem == rp.MISSING_TIME


def test_bad_minute_falls_back_to_the_hour():
    assert rp.resolve(fields(TOMORROW, 9, 75, True), NOW).due_at == at(TOMORROW, 9)


@pytest.mark.parametrize("text", ["", "   ", None])
def test_missing_text_asks_what_to_remind(text):
    r = rp.resolve(fields(TOMORROW, 9, text=text), NOW)
    assert r.problem == rp.MISSING_TEXT and r.due_at is None


def test_every_problem_has_a_message():
    for p in (rp.PAST, rp.MISSING_TIME, rp.MISSING_TEXT):
        assert rp.MESSAGES[p]


# --- the Gemini call ---

@pytest.fixture
def model(monkeypatch):
    seen = {}

    def reply(payload):
        async def fake_generate(**kwargs):
            seen.update(kwargs)
            return SimpleNamespace(text=payload)
        monkeypatch.setattr(gemini, "_generate_with_retry", fake_generate)
    reply.seen = seen
    return reply


def test_parse_sends_the_current_taipei_time_and_asks_for_json(model):
    model(json.dumps({"date": TOMORROW, "hour": 9, "minute": 0, "period_given": True, "text": "繳圖"}))

    r = asyncio.run(rp.parse_reminder("明天早上 9 點提醒我繳圖", NOW))

    assert r.due_at == at(TOMORROW, 9) and r.text == "繳圖"
    cfg = model.seen["config"]
    assert "2026-09-29（二）15:02" in cfg.system_instruction
    assert cfg.response_mime_type == "application/json"
    assert cfg.response_schema is rp.ReminderFields
    assert model.seen["contents"] == "明天早上 9 點提醒我繳圖"


@pytest.mark.parametrize("payload", ["not json", "", "{\"hour\": \"九\"}"])
def test_unreadable_model_output_asks_instead_of_crashing(model, payload):
    model(payload)

    assert asyncio.run(rp.parse_reminder("提醒我", NOW)).problem == rp.MISSING_TIME
