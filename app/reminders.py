"""One-shot reminders (spec/personal-assistant.md AC6/AC7, plan T9).

Kept in a dedicated Google Sheet so they survive Render restarts and sleeps;
/cron/tick (T12) sends whatever is due. The row format is a one-way door
(changing it means starting a new sheet):

    id | user_id | due_at | text | status | created_at | sent_at

Times are ISO 8601 with the +08:00 offset: readable in the sheet, still
unambiguous to compare. Every write uses valueInputOption=RAW, so a reminder
text starting with "=" stays text instead of becoming a formula.

The credential is a refresh token with the drive.file scope only, which
reaches just the spreadsheet scripts/setup_reminder_sheet.py created.
"""
import os
import uuid
from dataclasses import dataclass, replace
from datetime import datetime
from typing import List, Optional
from zoneinfo import ZoneInfo

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

TAIPEI = ZoneInfo("Asia/Taipei")
HEADER = ["id", "user_id", "due_at", "text", "status", "created_at", "sent_at"]
PENDING, SENT, CANCELLED = "pending", "sent", "cancelled"
TAB = "reminders"
SCOPES = ["https://www.googleapis.com/auth/drive.file"]


class RemindersNotConfigured(RuntimeError):
    pass


@dataclass(frozen=True)
class Reminder:
    id: str
    user_id: str
    due_at: datetime
    text: str
    status: str = PENDING
    created_at: Optional[datetime] = None
    sent_at: Optional[datetime] = None


def _taipei(dt: datetime, name: str) -> datetime:
    if dt.tzinfo is None:
        raise ValueError(f"{name} must be timezone-aware, got {dt!r}")
    return dt.astimezone(TAIPEI)


class ReminderStore:
    """Contract shared by every backend. Subclasses supply three primitives:
    _load() -> all reminders, _append(r), _update(r)."""

    def add(self, user_id: str, due_at: datetime, text: str) -> Reminder:
        if not user_id:
            raise ValueError("reminder needs an owner")
        text = (text or "").strip()
        if not text:
            raise ValueError("reminder needs text")
        r = Reminder(
            id=uuid.uuid4().hex[:12],
            user_id=user_id,
            due_at=_taipei(due_at, "due_at"),
            text=text,
            created_at=datetime.now(TAIPEI).replace(microsecond=0),
        )
        self._append(r)
        return r

    def list_pending(self, user_id: str) -> List[Reminder]:
        mine = [r for r in self._load() if r.user_id == user_id and r.status == PENDING]
        return sorted(mine, key=lambda r: r.due_at)

    def list_due(self, now: datetime) -> List[Reminder]:
        now = _taipei(now, "now")
        due = [r for r in self._load() if r.status == PENDING and r.due_at <= now]
        return sorted(due, key=lambda r: r.due_at)

    def mark_sent(self, reminder_id: str, sent_at: datetime) -> bool:
        """False when the reminder is unknown or no longer pending, so a second
        tick cannot send the same reminder twice."""
        r = self._find(reminder_id)
        if r is None or r.status != PENDING:
            return False
        self._update(replace(r, status=SENT, sent_at=_taipei(sent_at, "sent_at")))
        return True

    def cancel(self, reminder_id: str, user_id: str) -> bool:
        r = self._find(reminder_id)
        if r is None or r.user_id != user_id or r.status != PENDING:
            return False
        self._update(replace(r, status=CANCELLED))
        return True

    def _find(self, reminder_id: str) -> Optional[Reminder]:
        return next((r for r in self._load() if r.id == reminder_id), None)

    def _load(self) -> List[Reminder]:
        raise NotImplementedError

    def _append(self, r: Reminder) -> None:
        raise NotImplementedError

    def _update(self, r: Reminder) -> None:
        raise NotImplementedError


class InMemoryReminderStore(ReminderStore):
    """Test double; also documents the contract without any Sheets detail."""

    def __init__(self):
        self._rows: List[Reminder] = []

    def _load(self):
        return list(self._rows)

    def _append(self, r):
        self._rows.append(r)

    def _update(self, r):
        self._rows = [r if x.id == r.id else x for x in self._rows]


def _fmt(dt: Optional[datetime]) -> str:
    return dt.isoformat() if dt else ""


def _parse_time(value: str) -> Optional[datetime]:
    if not value:
        return None
    dt = datetime.fromisoformat(value)
    return _taipei(dt, "sheet time")  # a naive value in the sheet is rejected, not guessed


def _to_row(r: Reminder) -> list:
    return [r.id, r.user_id, _fmt(r.due_at), r.text, r.status, _fmt(r.created_at), _fmt(r.sent_at)]


def _from_row(row: list) -> Reminder:
    row = list(row) + [""] * (len(HEADER) - len(row))  # the API trims trailing empty cells
    rid, user_id, due_at, text, status, created_at, sent_at = row[:len(HEADER)]
    if not rid or not user_id or not due_at:
        raise ValueError("row without id, owner or due time")
    return Reminder(
        id=rid,
        user_id=user_id,
        due_at=_parse_time(due_at),
        text=text,
        status=status,
        created_at=_parse_time(created_at),
        sent_at=_parse_time(sent_at),
    )


class SheetsReminderStore(ReminderStore):
    """Reads the whole tab on every call: a personal bot holds tens of rows,
    and re-reading is what keeps row numbers right after a hand edit."""

    def __init__(self, sheets, spreadsheet_id: str):
        self._values = sheets.spreadsheets().values()
        self._sheet_id = spreadsheet_id

    def _rows(self) -> list:
        resp = self._values.get(spreadsheetId=self._sheet_id, range=f"{TAB}!A:G").execute()
        return resp.get("values", [])

    def _load(self):
        out = []
        for i, row in enumerate(self._rows()[1:], start=2):
            if not any(row):
                continue
            try:
                out.append(_from_row(row))
            except ValueError as e:  # a hand-edited row must not stop every other reminder
                print(f"[Reminders] skipping sheet row {i}: {e}")
        return out

    def _append(self, r):
        self._values.append(
            spreadsheetId=self._sheet_id,
            range=f"{TAB}!A:G",
            valueInputOption="RAW",
            insertDataOption="INSERT_ROWS",
            body={"values": [_to_row(r)]},
        ).execute()

    def _update(self, r):
        for i, row in enumerate(self._rows(), start=1):
            if i > 1 and row and row[0] == r.id:
                self._values.update(
                    spreadsheetId=self._sheet_id,
                    range=f"{TAB}!A{i}:G{i}",
                    valueInputOption="RAW",
                    body={"values": [_to_row(r)]},
                ).execute()
                return
        raise KeyError(f"reminder {r.id} vanished from the sheet")


_store: Optional[ReminderStore] = None


def get_store() -> ReminderStore:
    global _store
    if _store is None:
        sheet_id = os.environ.get("REMINDER_SHEET_ID", "").strip()
        if not sheet_id:
            raise RemindersNotConfigured("REMINDER_SHEET_ID is not set")
        creds = Credentials(
            token=None,
            refresh_token=os.environ.get("GOOGLE_REFRESH_TOKEN", "").strip(),
            token_uri="https://oauth2.googleapis.com/token",
            client_id=os.environ.get("GOOGLE_CLIENT_ID", "").strip(),
            client_secret=os.environ.get("GOOGLE_CLIENT_SECRET", "").strip(),
            scopes=SCOPES,
        )
        sheets = build("sheets", "v4", credentials=creds, cache_discovery=False)
        _store = SheetsReminderStore(sheets, sheet_id)
    return _store
