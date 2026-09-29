"""Upload the HJPLUS Taiwan architect KB into the bot's File Search store (plan T6).

Every Markdown file under raw/ becomes one document tagged
  user_id="__kb__"  (shared: the query filter is 'user_id="<caller>" OR user_id="__kb__"')
  source="HJPLUS"
  status=<metadata.status or top-level status>  (only when the file declares one)

Dry run by default: lists what would be uploaded. --apply uploads; already
uploaded display names are skipped, so an interrupted run can simply be
re-run. The key is read from .env and passed explicitly, so a GOOGLE_API_KEY
in the environment (which the SDK would otherwise prefer) cannot redirect
the upload to another project. KB content is CC BY-SA 4.0 (HJPLUS).

Usage:
  python scripts/ingest_kb.py                       # dry run
  python scripts/ingest_kb.py --apply --store fileSearchStores/...
"""
import argparse
import os
import re
import shutil
import sys
import tempfile
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

DEFAULT_SRC = Path(r"C:\Users\tkgcc\.gemini\kb\HJPLUS_Taiwan_Architect_KB\raw")
PREFIX = "HJPLUS"
KB_USER_ID = "__kb__"
_TRANSIENT = ("429", "503", "500", "unavailable", "resource_exhausted", "deadline", "overloaded")


def collect_files(root: Path) -> list:
    """(path, display_name) for every .md under root, sorted, display names in POSIX form."""
    root = Path(root)
    out = [(p, f"{PREFIX}/{p.relative_to(root).as_posix()}") for p in root.rglob("*.md") if p.is_file()]
    return sorted(out, key=lambda t: t[1])


def parse_status(text: str) -> str:
    """metadata.status if present, else top-level status, else "". Frontmatter only."""
    m = re.match(r"---\r?\n(.*?)\r?\n---", text, re.S)
    if not m:
        return ""
    top, nested, in_meta = "", "", False
    for line in m.group(1).splitlines():
        if re.match(r"\S", line):              # a top-level key starts (or continues) here
            in_meta = line.startswith("metadata:")
            hit = re.match(r"status:\s*(\S+)", line)
            if hit:
                top = hit.group(1).strip("'\"")
        elif in_meta:
            hit = re.match(r"\s+status:\s*(\S+)", line)
            if hit:
                nested = hit.group(1).strip("'\"")
    return nested or top


def plan_uploads(files: list, existing: set) -> list:
    return [f for f in files if f[1] not in existing]


def metadata_for(status: str) -> list:
    md = [{"key": "user_id", "string_value": KB_USER_ID},
          {"key": "source", "string_value": PREFIX}]
    if status:
        md.append({"key": "status", "string_value": status})
    return md


def _existing_kb_names(client, store: str) -> set:
    names = set()
    for d in client.file_search_stores.documents.list(parent=store):
        meta = {m.key: m.string_value for m in (d.custom_metadata or [])}
        if meta.get("source") == PREFIX:
            names.add(d.display_name)
    return names


def _upload_one(client, store: str, path: Path, name: str) -> str:
    status = parse_status(path.read_text(encoding="utf-8", errors="ignore"))
    # The SDK sends the file's basename in an HTTP header, which must be ASCII:
    # "安裝與分享說明.md" failed with "'ascii' codec can't encode". Upload an
    # ASCII-named copy; the Chinese path survives as display_name.
    with tempfile.TemporaryDirectory() as tmp:
        ascii_copy = Path(tmp) / "kb-document.md"
        shutil.copyfile(path, ascii_copy)
        return _upload_with_retry(client, store, ascii_copy, name, status)


def _upload_with_retry(client, store: str, path: Path, name: str, status: str) -> str:
    for attempt in range(5):
        try:
            op = client.file_search_stores.upload_to_file_search_store(
                file_search_store_name=store, file=str(path),
                config={"display_name": name, "mime_type": "text/markdown",
                        "custom_metadata": metadata_for(status)},
            )
            while not op.done:
                time.sleep(2)
                op = client.operations.get(op)
            if getattr(op, "error", None):
                raise RuntimeError(f"indexing failed: {op.error}")
            return name
        except Exception as e:
            if attempt == 4 or not any(t in str(e).lower() for t in _TRANSIENT):
                raise
            time.sleep(min(2 ** attempt * 2, 30))
    return name


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", type=Path, default=DEFAULT_SRC)
    ap.add_argument("--store", default="")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--workers", type=int, default=3)
    args = ap.parse_args()

    files = collect_files(args.src)
    statuses = Counter(parse_status(p.read_text(encoding="utf-8", errors="ignore")) or "(none)" for p, _ in files)
    total_bytes = sum(p.stat().st_size for p, _ in files)
    print(f"source: {args.src}\nmarkdown files: {len(files)} ({total_bytes / 1e6:.1f} MB)")
    print("status:", dict(statuses))
    for _, name in files[:5]:
        print("  e.g.", name)

    from dotenv import dotenv_values
    from google import genai
    env = dotenv_values(Path(__file__).resolve().parent.parent / ".env")
    store = args.store or os.environ.get("GEMINI_STORE_NAME") or env.get("GEMINI_STORE_NAME", "")
    if not store:
        print("ERROR: pass --store or set GEMINI_STORE_NAME")
        return 2
    client = genai.Client(api_key=env["GEMINI_API_KEY"])
    existing = _existing_kb_names(client, store)
    todo = plan_uploads(files, existing)
    print(f"store: {store}\nalready uploaded: {len(existing)}\nto upload: {len(todo)}")
    if not args.apply:
        print("dry run - add --apply to upload")
        return 0

    done, failed = 0, []
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(_upload_one, client, store, p, n): n for p, n in todo}
        for fut in as_completed(futures):
            try:
                fut.result()
                done += 1
                if done % 20 == 0:
                    print(f"  uploaded {done}/{len(todo)}")
            except Exception as e:
                failed.append((futures[fut], str(e)[:160]))
    final = len(_existing_kb_names(client, store))
    print(f"uploaded: {done}, failed: {len(failed)}, KB documents now in store: {final}")
    for name, err in failed[:10]:
        print("  FAILED", name, "|", err)
    return 0 if not failed and final == len(files) else 1


if __name__ == "__main__":
    sys.exit(main())
