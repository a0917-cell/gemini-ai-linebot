"""T9 / AC6: one-shot reminders kept in a dedicated Google Sheet so they survive
Render restarts and sleeps. Every contract test runs against both the in-memory
store and the Sheets store (backed by a fake Sheets service), so the two cannot
drift apart; the Sheets-only tests pin the row format, which is a one-way door."""
from datetime import datetime, timedelta, timezone

import pytest

import app.reminders as rem

TPE = rem.TAIPEI
A = "U" + "a" * 32
B = "U" + "b" * 32
T0 = datetime(2026, 9, 30, 9, 0, tzinfo=TPE)


class FakeSheets:
    """Just enough of spreadsheets().values() for the store: get/append/update
    over one tab, with the API's habit of trimming trailing empty cells."""

    def __init__(self, rows=None):
        self.rows = [list(rem.HEADER)] + [list(r) for r in (rows or [])]
        self.calls = []

    def spreadsheets(self):
        return self

    def values(self):
        return self

    def _run(self, result):
        return type("Req", (), {"execute": lambda _self: result})()

    def get(self, spreadsheetId, range):
        self.calls.append(("get", range))
        trimmed = []
        for r in self.rows:
            r = list(r)
            while r and r[-1] == "":
                r.pop()
            trimmed.append(r)
        return self._run({"values": trimmed})

    def append(self, spreadsheetId, range, valueInputOption, body, insertDataOption=None):
        self.calls.append(("append", range, valueInputOption))
        self.rows.extend(list(v) for v in body["values"])
        return self._run({})

    def update(self, spreadsheetId, range, valueInputOption, body):
        self.calls.append(("update", range, valueInputOption))
        row = int(range.split("!")[1].split(":")[0][1:])  # "reminders!A5:G5" -> 5
        self.rows[row - 1] = list(body["values"][0])
        return self._run({})


@pytest.fixture(params=["memory", "sheets"])
def store(request):
    if request.param == "memory":
        return rem.InMemoryReminderStore()
    return rem.SheetsReminderStore(FakeSheets(), "sheet-id")


# --- contract (both stores) ---

def test_added_reminder_is_listed_in_taipei_time(store):
    r = store.add(A, datetime(2026, 9, 30, 1, 0, tzinfo=timezone.utc), "繳圖")

    [got] = store.list_pending(A)
    assert got.id == r.id and got.text == "繳圖" and got.status == rem.PENDING
    assert got.due_at == T0 and got.due_at.utcoffset() == timedelta(hours=8)


def test_naive_time_is_rejected(store):
    with pytest.raises(ValueError):
        store.add(A, datetime(2026, 9, 30, 9, 0), "繳圖")


@pytest.mark.parametrize("user_id, text", [("", "繳圖"), (A, ""), (A, "   ")])
def test_missing_owner_or_text_is_rejected(store, user_id, text):
    with pytest.raises(ValueError):
        store.add(user_id, T0, text)


def test_pending_list_is_the_callers_own_sorted_by_time(store):
    store.add(A, T0 + timedelta(hours=2), "晚")
    store.add(B, T0, "別人的")
    store.add(A, T0, "早")

    assert [r.text for r in store.list_pending(A)] == ["早", "晚"]


def test_ids_are_unique(store):
    ids = {store.add(A, T0, f"第{i}則").id for i in range(20)}
    assert len(ids) == 20


def test_due_list_spans_users_and_stops_at_now(store):
    store.add(A, T0, "到了")
    store.add(B, T0 - timedelta(minutes=1), "也到了")
    store.add(A, T0 + timedelta(seconds=1), "還沒")

    assert [r.text for r in store.list_due(T0)] == ["也到了", "到了"]


def test_mark_sent_happens_once(store):
    r = store.add(A, T0, "繳圖")
    at = T0 + timedelta(minutes=2)

    assert store.mark_sent(r.id, at) is True
    assert store.mark_sent(r.id, at) is False
    assert store.list_due(T0 + timedelta(hours=1)) == []
    assert store.list_pending(A) == []


def test_mark_sent_unknown_id(store):
    assert store.mark_sent("nope", T0) is False


def test_only_the_owner_can_cancel(store):
    r = store.add(A, T0, "繳圖")

    assert store.cancel(r.id, B) is False
    assert [x.id for x in store.list_pending(A)] == [r.id]
    assert store.cancel(r.id, A) is True
    assert store.list_pending(A) == [] and store.list_due(T0) == []


def test_sent_reminder_cannot_be_cancelled(store):
    r = store.add(A, T0, "繳圖")
    store.mark_sent(r.id, T0)

    assert store.cancel(r.id, A) is False


# --- Sheets store: row format (one-way door) ---

def test_row_format_is_pinned():
    fake = FakeSheets()
    store = rem.SheetsReminderStore(fake, "sheet-id")
    r = store.add(A, T0, "繳圖")

    assert rem.HEADER == ["id", "user_id", "due_at", "text", "status", "created_at", "sent_at"]
    row = fake.rows[1]
    assert row[:5] == [r.id, A, "2026-09-30T09:00:00+08:00", "繳圖", "pending"]
    assert row[5].endswith("+08:00") and row[6] == ""

    store.mark_sent(r.id, T0 + timedelta(minutes=3))
    assert fake.rows[1][4] == "sent" and fake.rows[1][6] == "2026-09-30T09:03:00+08:00"


def test_times_are_written_to_the_second():
    # /cron/tick passes datetime.now(), which carries microseconds; the sheet
    # showed sent_at 08:06:49.829263+08:00 on the first live reminder (2026-09-30)
    fake = FakeSheets()
    store = rem.SheetsReminderStore(fake, "sheet-id")
    r = store.add(A, T0.replace(microsecond=123456), "繳圖")

    store.mark_sent(r.id, T0.replace(second=49, microsecond=829263))

    row = fake.rows[1]
    assert row[2] == "2026-09-30T09:00:00+08:00"
    assert row[6] == "2026-09-30T09:00:49+08:00"


def test_writes_are_raw_so_text_never_becomes_a_formula():
    fake = FakeSheets()
    store = rem.SheetsReminderStore(fake, "sheet-id")
    r = store.add(A, T0, "=HYPERLINK(\"http://x\")")
    store.cancel(r.id, A)

    writes = [c for c in fake.calls if c[0] in ("append", "update")]
    assert writes and all(c[2] == "RAW" for c in writes)


def test_reminders_survive_a_restart():
    fake = FakeSheets()
    rem.SheetsReminderStore(fake, "sheet-id").add(A, T0, "繳圖")

    [got] = rem.SheetsReminderStore(fake, "sheet-id").list_pending(A)
    assert got.text == "繳圖"


def test_hand_edited_rows_do_not_break_the_store():
    fake = FakeSheets(rows=[
        ["r1", A, "2026-09-30T09:00:00+08:00", "正常", "pending"],  # trimmed tail
        ["r2", A, "明天早上", "壞日期", "pending", "", ""],
        [],
        ["r3", A, "2026-09-30T08:00:00", "沒時區", "pending", "", ""],
        ["r4", A, "", "沒時間", "pending"],
        ["", A, "2026-09-30T09:00:00+08:00", "沒 id", "pending"],
    ])
    store = rem.SheetsReminderStore(fake, "sheet-id")

    assert [r.id for r in store.list_pending(A)] == ["r1"]
    assert store.mark_sent("r1", T0) is True
    assert fake.rows[1][0] == "r1" and fake.rows[1][4] == "sent"


def test_update_targets_the_row_holding_the_id():
    fake = FakeSheets()
    store = rem.SheetsReminderStore(fake, "sheet-id")
    first = store.add(A, T0, "一")
    second = store.add(A, T0, "二")

    store.cancel(second.id, A)

    assert fake.rows[1][0] == first.id and fake.rows[1][4] == "pending"
    assert fake.rows[2][0] == second.id and fake.rows[2][4] == "cancelled"


# --- factory ---

def test_store_is_not_configured_without_a_sheet_id(monkeypatch):
    monkeypatch.delenv("REMINDER_SHEET_ID", raising=False)

    with pytest.raises(rem.RemindersNotConfigured):
        rem.get_store()
