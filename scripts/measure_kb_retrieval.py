"""How often does the production model actually use the HJPLUS KB on law
questions? (plan T19)

    python scripts/measure_kb_retrieval.py            # production model + store
    python scripts/measure_kb_retrieval.py --model gemini-3.5-flash-lite

Sends each question with the exact config query_with_text uses
(_text_query_config) and counts answers whose grounding metadata cites at least
one HJPLUS document. Uses GEMINI_API_KEY from .env: this is billed on a paid
key, about 10 short generations per run. The user id is a dummy, so only the
shared KB is visible.
"""
import argparse
import asyncio
import os
import sys

from dotenv import load_dotenv

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
load_dotenv(os.path.join(ROOT, ".env"))

PRODUCTION_STORE = "fileSearchStores/linebotmultimodalrag-c1v9232tcirj"
PRODUCTION_MODEL = "gemini-3.8-flash"
DUMMY_USER = "U" + "0" * 32

QUESTIONS = [
    "樓梯最小寬度？",
    "防火區劃面積上限是多少？",
    "住宅走廊最小寬度？",
    "無障礙坡道坡度規定？",
    "建蔽率怎麼算？",
    "屋頂欄杆高度至少多少？",
    "居室天花板淨高最低多少？",
    "直通樓梯步行距離規定？",
    "地下室什麼時候要設排煙設備？",
    "停車位尺寸規定？",
]


async def main(model: str) -> None:
    os.environ["GEMINI_STORE_NAME"] = PRODUCTION_STORE
    import app.gemini_service as gemini

    store = gemini.get_or_create_store()
    hits = 0
    for q in QUESTIONS:
        resp = await gemini._generate_with_retry(
            model=model, contents=q, config=gemini._text_query_config(store, DUMMY_USER)
        )
        kb = [t for t in gemini._retrieved_titles(resp) if t.startswith(f"{gemini.KB_SOURCE}/")]
        hits += bool(kb)
        print(f"{'KB ' if kb else '-- '} {q}  ({len(kb)} docs)", flush=True)
    print(f"model={model} kb_used={hits}/{len(QUESTIONS)}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default=PRODUCTION_MODEL)
    asyncio.run(main(ap.parse_args().model))
