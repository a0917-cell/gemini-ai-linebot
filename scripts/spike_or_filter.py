"""Plan T5 spike: does File Search's metadata_filter accept OR?

The shared-KB design (plan T6/T7) tags regulation documents user_id="__kb__"
and queries with 'user_id="<caller>" OR user_id="__kb__"'. This script proves
or disproves that with a throwaway store:

  doc A  user_id="Uspikea"  -> must be retrieved by the OR filter
  doc KB user_id="__kb__"   -> must be retrieved by the OR filter
  doc C  user_id="Uspikec"  -> must NOT be retrieved by the OR filter
  control: filter user_id="Uspikec" alone -> only doc C

Evidence is the grounding metadata (which documents were actually retrieved),
not the model's wording. The store is deleted at the end, even on failure.

Usage: python scripts/spike_or_filter.py   (reads GEMINI_API_KEY from .env)
"""
import os
import sys
import tempfile
import time

from dotenv import load_dotenv
from google import genai
from google.genai import types

load_dotenv()
MODEL = "gemini-3.5-flash-lite"
DOCS = {
    "spike-A.txt": ("Uspikea", "文件 A：專案暗號是「藍色鯨魚」。"),
    "spike-KB.txt": ("__kb__", "文件 KB：共用法規的暗號是「紅色燈塔」。"),
    "spike-C.txt": ("Uspikec", "文件 C：另一位使用者的暗號是「綠色山羊」。"),
}
QUESTION = "列出你在文件中看到的每一個暗號，以及它出自哪一份文件。"


def retrieved_titles(resp) -> set:
    titles = set()
    for cand in resp.candidates or []:
        gm = getattr(cand, "grounding_metadata", None)
        for chunk in (getattr(gm, "grounding_chunks", None) or []):
            ctx = getattr(chunk, "retrieved_context", None)
            if ctx is not None and getattr(ctx, "title", None):
                titles.add(ctx.title)
    return titles


def query(client, store, flt):
    resp = client.models.generate_content(
        model=MODEL, contents=QUESTION,
        config=types.GenerateContentConfig(tools=[types.Tool(file_search=types.FileSearch(
            file_search_store_names=[store], metadata_filter=flt))]),
    )
    return retrieved_titles(resp), (resp.text or "")


def main() -> int:
    client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
    store = client.file_search_stores.create(config={
        "display_name": f"spike-or-filter-{int(time.time())}",
        "embedding_model": "models/gemini-embedding-2",
    }).name
    print("store:", store)
    try:
        with tempfile.TemporaryDirectory() as tmp:
            for name, (uid, body) in DOCS.items():
                path = os.path.join(tmp, name)
                with open(path, "w", encoding="utf-8") as f:
                    f.write(body)
                op = client.file_search_stores.upload_to_file_search_store(
                    file_search_store_name=store, file=path,
                    config={"display_name": name,
                            "custom_metadata": [{"key": "user_id", "string_value": uid}]},
                )
                while not op.done:
                    time.sleep(2)
                    op = client.operations.get(op)
                print("indexed:", name, "as", uid)

        results = {}
        for label, flt in [("OR", 'user_id="Uspikea" OR user_id="__kb__"'),
                           ("control C", 'user_id="Uspikec"')]:
            try:
                titles, text = query(client, store, flt)
                results[label] = titles
                print(f"[{label}] filter={flt}\n  retrieved: {sorted(titles)}\n  text: {text[:150]!r}")
            except Exception as e:
                results[label] = None
                print(f"[{label}] filter={flt}\n  ERROR {type(e).__name__}: {str(e)[:200]}")

        or_ok = results["OR"] == {"spike-A.txt", "spike-KB.txt"}
        control_ok = results["control C"] == {"spike-C.txt"}
        print("\nVERDICT:", "OR filter WORKS" if or_ok and control_ok else "OR filter NOT CONFIRMED",
              f"(OR={results['OR']}, control={results['control C']})")
        return 0 if or_ok and control_ok else 1
    finally:
        client.file_search_stores.delete(name=store, config={"force": True})
        print("deleted:", store)


if __name__ == "__main__":
    sys.exit(main())
