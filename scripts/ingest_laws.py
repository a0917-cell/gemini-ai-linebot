"""Upload statute text for the core building laws into the File Search store
(plan T20). HJPLUS holds practice notes, not articles: fire compartment areas,
walking distances and parking sizes were simply not in the KB.

    python scripts/ingest_laws.py                    # dry run: what would upload
    python scripts/ingest_laws.py --apply --store fileSearchStores/...

Source: the local openlawtw clone (central laws frozen at the MOJ XML batch of
2026-09-18, Government Open Data License v1; statute text itself is not a
copyright subject under Copyright Act art. 9). One markdown document per
chapter (編/章), named LAW/<law>/<chapter>.md so a source line can name the
chapter; laws without chapters become LAW/<law>/全文.md. Metadata: user_id
__kb__ (shared, visible to everyone) and source LAW. Already uploaded names
are skipped, so a rerun only fills gaps.
"""
import argparse
import os
import re
import shutil
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.gemini_service import KB_USER_ID, LAW_SNAPSHOT, LAW_SOURCE  # noqa: E402
from scripts.ingest_kb import _upload_with_retry  # noqa: E402

DEFAULT_LAWS_JSON = Path(r"F:\01  CHOU\09_AI相關資料\openlawtw\repo\public\data\laws.json")
# Agreed with the user 2026-09-30.
CORE_LAWS = [
    "D0070114",  # 建築技術規則總則編
    "D0070115",  # 建築技術規則建築設計施工編
    "D0070116",  # 建築技術規則建築構造編
    "D0070117",  # 建築技術規則建築設備編
    "D0070109",  # 建築法
    "D0120029",  # 各類場所消防安全設備設置標準
    "D0120001",  # 消防法
    "N0060014",  # 營造安全衛生設施標準
    "N0060009",  # 職業安全衛生設施規則
    "D0070110",  # 營造業法
    "D0070148",  # 建築物室內裝修管理辦法
    "D0070001",  # 都市計畫法
]
_DELETED = {"", "（刪除）", "(刪除)"}
_HEADING = re.compile(r"^第\s*(\S+?)\s*([編章節款目])(?:\s*之\s*(\S+?)(?=\s))?\s*")


def clean_heading(raw: str) -> str:
    """'第 三 章 建築物之防火' -> '第三章 建築物之防火'; '第 四 章 之 一 X' and
    '第 三 章 之一 X' -> '第四章之一 X'; '/' would split the display name."""
    s = " ".join(raw.split())

    def tight(m):
        suffix = f"之{m.group(3)}" if m.group(3) else ""
        return f"第{m.group(1)}{m.group(2)}{suffix} "
    s = _HEADING.sub(tight, s).strip()
    return s.replace("/", "／")


def snapshot_date(law: dict) -> str:
    """'2026/9/18 上午 12:00:00' -> '2026-09-18'"""
    y, m, d = re.match(r"(\d{4})/(\d{1,2})/(\d{1,2})", law["snapshot"]).groups()
    return f"{y}-{int(m):02d}-{int(d):02d}"


def build_docs(law: dict) -> list:
    """[(display_name, markdown)] in chapter order; deleted articles and chapters
    left empty by them are dropped."""
    snap = snapshot_date(law)
    if snap != LAW_SNAPSHOT:
        raise ValueError(f"{law['name']} is a {snap} snapshot but the bot says LAW_SNAPSHOT={LAW_SNAPSHOT}; "
                         f"update app.gemini_service.LAW_SNAPSHOT together with the upload")
    chapters = {}  # chapter -> [(section, no, text)]
    for a in law.get("articles", []):
        text = (a.get("text") or "").strip()
        if text in _DELETED:
            continue
        path = [clean_heading(p) for p in (a.get("path") or [])]
        chapter = path[0] if path else "全文"
        section = path[1] if len(path) > 1 else ""
        chapters.setdefault(chapter, []).append((section, a["no"], text))

    docs = []
    for chapter, rows in chapters.items():
        title = law["name"] if chapter == "全文" else f"{law['name']} {chapter}"
        lines = [f"# {title}",
                 f"來源：全國法規資料庫 {law['url']}（條文快照 {snap}，政府資料開放授權條款第 1 版）。現行條文請以全國法規資料庫為準。",
                 ""]
        current = None
        for section, no, text in rows:
            if section and section != current:
                lines += [f"## {section}", ""]
                current = section
            lines += [no, text, ""]
        docs.append((f"{LAW_SOURCE}/{law['name']}/{chapter}.md", "\n".join(lines)))
    return docs


def metadata_for(law_id: str) -> list:
    return [{"key": "user_id", "string_value": KB_USER_ID},
            {"key": "source", "string_value": LAW_SOURCE},
            {"key": "law_id", "string_value": law_id},
            {"key": "snapshot", "string_value": LAW_SNAPSHOT}]


def _existing_law_names(client, store: str) -> set:
    names = set()
    for d in client.file_search_stores.documents.list(parent=store):
        meta = {m.key: m.string_value for m in (d.custom_metadata or [])}
        if meta.get("source") == LAW_SOURCE:
            names.add(d.display_name)
    return names


def _upload_one(client, store: str, name: str, text: str, law_id: str) -> str:
    # The SDK sends the file's basename in an HTTP header, which must be ASCII
    # (same reason as ingest_kb._upload_one).
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "law-document.md"
        path.write_text(text, encoding="utf-8")
        return _upload_with_retry(client, store, path, name, metadata_for(law_id))


def main() -> int:
    import json

    ap = argparse.ArgumentParser()
    ap.add_argument("--laws-json", type=Path, default=DEFAULT_LAWS_JSON)
    ap.add_argument("--store", default="")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--workers", type=int, default=3)
    args = ap.parse_args()

    laws = json.loads(args.laws_json.read_text(encoding="utf-8"))
    plan = []  # (name, text, law_id)
    for law_id in CORE_LAWS:
        docs = build_docs(laws[law_id])
        print(f"{law_id} {laws[law_id]['name']}: {len(docs)} documents")
        plan += [(name, text, law_id) for name, text in docs]
    total_chars = sum(len(t) for _, t, _ in plan)
    print(f"documents: {len(plan)} ({total_chars / 1000:.0f}k characters), snapshot {LAW_SNAPSHOT}")

    from dotenv import dotenv_values
    from google import genai
    env = dotenv_values(ROOT / ".env")
    store = args.store or os.environ.get("GEMINI_STORE_NAME") or env.get("GEMINI_STORE_NAME", "")
    if not store:
        print("ERROR: pass --store or set GEMINI_STORE_NAME")
        return 2
    client = genai.Client(api_key=env["GEMINI_API_KEY"])
    existing = _existing_law_names(client, store)
    todo = [p for p in plan if p[0] not in existing]
    print(f"store: {store}\nalready uploaded: {len(existing)}\nto upload: {len(todo)}")
    if not args.apply:
        print("dry run - add --apply to upload")
        return 0

    done, failed = 0, []
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(_upload_one, client, store, n, t, i): n for n, t, i in todo}
        for fut in as_completed(futures):
            try:
                fut.result()
                done += 1
                if done % 20 == 0:
                    print(f"  uploaded {done}/{len(todo)}")
            except Exception as e:
                failed.append((futures[fut], str(e)[:160]))
    final = len(_existing_law_names(client, store))
    print(f"uploaded: {done}, failed: {len(failed)}, LAW documents now in store: {final} (planned {len(plan)})")
    for name, err in failed[:10]:
        print("  FAILED", name, "|", err)
    return 0 if not failed and final == len(plan) else 1


if __name__ == "__main__":
    sys.exit(main())
