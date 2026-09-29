"""One-time setup for the reminder store (plan T9).

    python scripts/setup_reminder_sheet.py --client-env ..\\line-archiver-bot\\.env
    python scripts/setup_reminder_sheet.py --smoke

Default run: signs in with Google (browser) asking for the drive.file scope
only, creates the spreadsheet "LINE 助手提醒" with a "reminders" tab and the
header row, and writes GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET /
GOOGLE_REFRESH_TOKEN / REMINDER_SHEET_ID into this repo's .env. Nothing secret
is printed. drive.file means the token reaches this spreadsheet and nothing
else in the Drive.

The OAuth client (id/secret) is the one line-archiver-bot already uses; pass
its .env with --client-env if this repo's .env does not have them yet.

--smoke: using .env only, add a reminder, read it back, cancel it.
"""
import argparse
import os
import sys
from datetime import datetime, timedelta

from dotenv import dotenv_values

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

ENV_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env")
TITLE = "LINE 助手提醒"


def update_env(path, values):
    """Set KEY=value lines in .env in place (same as line-archiver-bot's
    get_refresh_token.py): other lines and line endings are kept."""
    try:
        with open(path, encoding="utf-8", newline="") as f:
            text = f.read()
    except FileNotFoundError:
        text = ""
    eol = "\r\n" if "\r\n" in text else "\n"
    lines = text.splitlines(keepends=True)
    pending = dict(values)
    for i, line in enumerate(lines):
        key = line.split("=", 1)[0].strip()
        if "=" in line and key in pending:
            ending = line[len(line.rstrip("\r\n")):] or eol
            lines[i] = f"{key}={pending.pop(key)}{ending}"
    if lines and not lines[-1].endswith(("\n", "\r")):
        lines[-1] += eol
    lines += [f"{k}={v}{eol}" for k, v in pending.items()]
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8", newline="") as f:
        f.write("".join(lines))
    os.replace(tmp, path)


def setup(client_env):
    from google_auth_oauthlib.flow import InstalledAppFlow
    from googleapiclient.discovery import build

    import app.reminders as rem

    env = dotenv_values(ENV_PATH)
    if env.get("REMINDER_SHEET_ID"):
        sys.exit(f"❌ .env 已經有 REMINDER_SHEET_ID，不重複建立試算表。要重建請先把那行刪掉。")
    source = env if env.get("GOOGLE_CLIENT_ID") else dotenv_values(client_env) if client_env else {}
    cid, secret = (source.get("GOOGLE_CLIENT_ID") or "").strip(), (source.get("GOOGLE_CLIENT_SECRET") or "").strip()
    if not cid or not secret:
        sys.exit("❌ 找不到 GOOGLE_CLIENT_ID／GOOGLE_CLIENT_SECRET。請加上 --client-env 指向 line-archiver-bot 的 .env。")

    flow = InstalledAppFlow.from_client_config({"installed": {
        "client_id": cid,
        "client_secret": secret,
        "auth_uri": "https://accounts.google.com/o/oauth2/auth",
        "token_uri": "https://oauth2.googleapis.com/token",
        "redirect_uris": ["http://localhost"],
    }}, rem.SCOPES)
    print("🚀 瀏覽器會開啟 Google 登入，權限只有「這個程式建立的檔案」。")
    print("   出現「Google 尚未驗證這個應用程式」→ 進階 → 前往（不安全）。")
    creds = flow.run_local_server(port=0, prompt="consent")

    sheets = build("sheets", "v4", credentials=creds, cache_discovery=False)
    created = sheets.spreadsheets().create(body={
        "properties": {"title": TITLE, "timeZone": "Asia/Taipei"},
        "sheets": [{"properties": {"title": rem.TAB, "gridProperties": {"frozenRowCount": 1}}}],
    }).execute()
    sheet_id = created["spreadsheetId"]
    sheets.spreadsheets().values().update(
        spreadsheetId=sheet_id, range=f"{rem.TAB}!A1:G1",
        valueInputOption="RAW", body={"values": [rem.HEADER]},
    ).execute()

    update_env(ENV_PATH, {
        "GOOGLE_CLIENT_ID": cid,
        "GOOGLE_CLIENT_SECRET": secret,
        "GOOGLE_REFRESH_TOKEN": creds.refresh_token,
        "REMINDER_SHEET_ID": sheet_id,
    })
    print(f"🎉 已建立試算表「{TITLE}」，四個值已寫進 {ENV_PATH}（不顯示內容）。")
    print(f"   試算表網址：https://docs.google.com/spreadsheets/d/{sheet_id}")


def smoke():
    import app.reminders as rem

    for k, v in dotenv_values(ENV_PATH).items():
        os.environ.setdefault(k, v or "")
    store = rem.get_store()
    due = datetime.now(rem.TAIPEI).replace(microsecond=0) + timedelta(days=1)
    r = store.add("U" + "0" * 32, due, "T9 smoke test（會自動取消）")
    found = [x.id for x in store.list_pending(r.user_id)]
    cancelled = store.cancel(r.id, r.user_id)
    print(f"add={r.id} due={r.due_at.isoformat()} read_back={r.id in found} cancelled={cancelled}")
    if r.id not in found or not cancelled:
        sys.exit(1)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--client-env", help="a .env holding GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET")
    ap.add_argument("--smoke", action="store_true", help="write, read back and cancel one reminder")
    args = ap.parse_args()
    smoke() if args.smoke else setup(args.client_env)
