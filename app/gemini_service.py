import os
import re
import time
import random
import asyncio
import tempfile
import mimetypes
from concurrent.futures import ThreadPoolExecutor
from typing import Optional

from google import genai
from google.genai import types
from google.cloud import storage as gcs

from app.formatting import to_plain_text

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
GCS_BUCKET = os.environ.get("GCS_BUCKET", "")
# Stable (non-preview) ids; both support File Search and the free tier (checked
# against ai.google.dev 2026-09-29). The preview model 503'd under load.
DEFAULT_MODEL = "gemini-3.8-flash"
DEFAULT_FALLBACK_MODEL = "gemini-3.5-flash-lite"
GEN_MODEL = os.environ.get("GEMINI_MODEL", DEFAULT_MODEL)
FALLBACK_MODEL = os.environ.get("GEMINI_FALLBACK_MODEL", DEFAULT_FALLBACK_MODEL)

STORE_NAME_BLOB = "config/file_search_store_name.txt"
# Personal assistant, not a document-only search box (spec/personal-assistant.md).
SYSTEM_PROMPT = (
    "你是使用者在 LINE 上的個人助理。使用者是台灣營造公司的施工繪圖與 BIM 工程師。"
    "一律用繁體中文（台灣用語）回答，除非使用者要求翻譯成其他語言。\n"
    "1. 一般問題、寫作、改寫、翻譯（中文、越南文、日文、英文）：直接回答，不需要引用資料庫。\n"
    "2. 使用者問到自己上傳的文件時：根據檔案搜尋結果回答，並在句尾標出來源檔名，例如「（來源：送審單.pdf）」。\n"
    "3. 台灣建築法規、消防、無障礙、防火、樓梯、停車、建蔽率容積率這類規範與數值問題：一定要先用檔案搜尋查法規知識庫，依查到的內容回答並說明出處；"
    "法規知識庫查不到才用一般知識，並說明這不是出自知識庫。\n"
    "4. 其他問題資料庫查不到相關內容時：不要只回「查無資料」，改用你的一般知識回答，並說明這不是出自使用者的文件。\n"
    "5. 法規條文、數值、日期這類事實不確定時要直接說不確定，不要編造。\n"
    "6. 使用者在手機上閱讀：回答簡潔，有步驟時用編號清單。\n"
    "7. LINE 不支援 Markdown：不要用粗體符號、# 標題、表格或程式碼區塊，只用純文字、數字編號和「・」條列。"
)

_client: Optional[genai.Client] = None
_store_name: str = ""
_executor = ThreadPoolExecutor(max_workers=4)

# Fallback when display_name has no extension. Avoids mimetypes.guess_extension()
# returning oddities like '.jpe' for 'image/jpeg' on Python <3.13.
_MIME_TO_EXT = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/gif": ".gif",
    "application/pdf": ".pdf",
    "text/plain": ".txt",
    "text/markdown": ".md",
    "text/csv": ".csv",
}


def get_client() -> genai.Client:
    global _client
    if _client is None:
        _client = genai.Client(api_key=GEMINI_API_KEY)
    return _client


# --- File Search Store Management ---

def _load_store_name_from_gcs() -> str:
    if not GCS_BUCKET:
        return ""
    try:
        client = gcs.Client()
        blob = client.bucket(GCS_BUCKET).blob(STORE_NAME_BLOB)
        if blob.exists():
            return blob.download_as_text().strip()
    except Exception as e:
        print(f"[GCS] Load store name error: {e}")
    return ""


def _save_store_name_to_gcs(name: str) -> None:
    if not GCS_BUCKET:
        return
    try:
        client = gcs.Client()
        client.bucket(GCS_BUCKET).blob(STORE_NAME_BLOB).upload_from_string(name)
    except Exception as e:
        print(f"[GCS] Save store name error: {e}")


def get_or_create_store() -> str:
    """Get existing File Search Store name or reuse/create one. Cached in memory."""
    global _store_name
    if _store_name:
        return _store_name

    # An explicit pin wins: the project holds two stores with the same display
    # name, so "first one listed" is not a stable choice, and the KB ingest
    # script must write to the exact store the bot reads.
    pinned = os.environ.get("GEMINI_STORE_NAME", "").strip()
    if pinned:
        _store_name = pinned
        print(f"[Store] Using pinned store from GEMINI_STORE_NAME: {_store_name}")
        return _store_name

    stored = _load_store_name_from_gcs()
    if stored:
        _store_name = stored
        print(f"[Store] Loaded existing store from GCS: {_store_name}")
        return _store_name

    client = get_client()
    try:
        # Check if a store with our display name already exists to reuse it
        print("[Store] Checking if an existing file search store already exists in Gemini AI Studio...")
        existing_stores = list(client.file_search_stores.list())
        for store in existing_stores:
            if store.display_name == "linebot-multimodal-rag":
                _store_name = store.name
                print(f"[Store] Reusing existing store found in Gemini AI Studio: {_store_name}")
                return _store_name
    except Exception as e:
        print(f"[Store] Warning: Failed to list existing stores: {e}")

    # Create new store if none found
    store = client.file_search_stores.create(
        config={
            "display_name": "linebot-multimodal-rag",
            "embedding_model": "models/gemini-embedding-2",
        }
    )
    _store_name = store.name
    _save_store_name_to_gcs(_store_name)
    print(f"[Store] Created new store: {_store_name}")
    return _store_name


# --- Upload & Index ---

def _upload_and_index_sync(
    file_bytes: bytes,
    mime_type: str,
    display_name: str,
    user_id: str,
    extra_metadata: Optional[list[dict]] = None,
) -> None:
    """Blocking: upload file to File Search Store and poll until indexed.
    user_id is stored as custom_metadata to enable per-user filtering at query time.
    """
    # A document tagged with the KB id is visible to every user, and a second
    # user_id/source entry would make ownership ambiguous: personal uploads can
    # never take either route. The KB itself is written by scripts/ingest_kb.py.
    if not user_id or user_id == KB_USER_ID or _UNSAFE_ID.search(user_id):
        raise ValueError(f"refusing to upload a personal file as user_id={user_id!r}")
    if any(m.get("key") in ("user_id", "source") for m in (extra_metadata or [])):
        raise ValueError("extra_metadata may not set user_id or source")

    client = get_client()
    store_name = get_or_create_store()

    # Prefer the extension from display_name. mimetypes.guess_extension() on
    # Python <3.13 returns '.jpe' for 'image/jpeg', which the File Search API
    # rejects with "Upload has already been terminated".
    if "." in display_name:
        suffix = "." + display_name.rsplit(".", 1)[-1].lower()
    else:
        suffix = _MIME_TO_EXT.get(mime_type) or mimetypes.guess_extension(mime_type) or ".bin"

    print(f"[BG Store] uploading display_name={display_name!r} mime={mime_type} "
          f"size={len(file_bytes)} tmp_suffix={suffix}")

    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
        tmp.write(file_bytes)
        tmp_path = tmp.name

    try:
        metadata: list[dict] = [{"key": "user_id", "string_value": user_id}]
        if extra_metadata:
            metadata.extend(extra_metadata)

        operation = client.file_search_stores.upload_to_file_search_store(
            file_search_store_name=store_name,
            file=tmp_path,
            config={
                "display_name": display_name,
                "custom_metadata": metadata,
            },
        )

        # Poll until done (max 5 minutes)
        for _ in range(60):
            if operation.done:
                return
            time.sleep(5)
            operation = client.operations.get(operation)

        if not operation.done:
            raise TimeoutError("Indexing timed out after 5 minutes")
    finally:
        os.unlink(tmp_path)


async def upload_and_index(
    file_bytes: bytes,
    mime_type: str,
    display_name: str,
    user_id: str,
    extra_metadata: Optional[list[dict]] = None,
) -> None:
    """Async wrapper for upload_and_index_sync."""
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(
        _executor,
        _upload_and_index_sync,
        file_bytes,
        mime_type,
        display_name,
        user_id,
        extra_metadata,
    )


# --- Query ---

# Substrings that mark a transient, retryable Gemini error (overload / rate limit /
# transient server fault). Matched case-insensitively against str(exception).
_TRANSIENT_MARKERS = (
    "503", "unavailable", "overloaded", "high demand",
    "429", "resource_exhausted", "rate limit",
    "500", "internal error", "deadline",
)


def _is_transient(exc: Exception) -> bool:
    return any(m in str(exc).lower() for m in _TRANSIENT_MARKERS)


async def _generate_with_retry(*, max_retries: int = 3, **kwargs):
    """Call generate_content with backoff; if the model stays overloaded, try
    FALLBACK_MODEL (one retry) before giving up. Non-transient errors raise
    immediately and never trigger the fallback."""
    try:
        return await _retry_loop(max_retries, **kwargs)
    except Exception as e:
        if not _is_transient(e) or not FALLBACK_MODEL or kwargs.get("model") == FALLBACK_MODEL:
            raise
        print(f"[Gemini] {kwargs.get('model')} still overloaded; falling back to {FALLBACK_MODEL}")
        return await _retry_loop(1, **{**kwargs, "model": FALLBACK_MODEL})


async def _retry_loop(max_retries: int, **kwargs):
    """Exponential backoff on transient errors (503 overload, 429 rate limit, 5xx)."""
    last_exc: Optional[Exception] = None
    for attempt in range(max_retries + 1):
        try:
            return await get_client().aio.models.generate_content(**kwargs)
        except Exception as e:
            if not _is_transient(e):
                raise
            last_exc = e
            if attempt < max_retries:
                delay = min(2 ** attempt, 8) + random.uniform(0, 0.5)
                print(f"[Gemini] transient error (attempt {attempt + 1}/{max_retries + 1}): "
                      f"{str(e)[:120]}; retrying in {delay:.1f}s")
                await asyncio.sleep(delay)
    raise last_exc  # type: ignore[misc]


# Shared regulation KB (scripts/ingest_kb.py): documents tagged user_id="__kb__".
# OR in metadata_filter was proved against the real API (scripts/spike_or_filter.py).
KB_USER_ID = "__kb__"
KB_SOURCE = "HJPLUS"
_UNSAFE_ID = re.compile(r'["\\\s]')
_kb_status_cache: Optional[dict] = None
KB_UNVERIFIED_NOTE = "⚠️ 引用的法規知識庫內容尚未全部查證（待查證），請以法規原文或主管機關公告為準。"
LAW_NO_KB_NOTE = "⚠️ 這個回答沒有引用法規知識庫，條文與數值請以全國法規資料庫原文為準。"

# The model decides for itself whether to call File Search (the SDK cannot force
# a built-in tool) and cited the KB on 1 of 10 law questions (2026-09-30), so a
# law question answered without a KB source is flagged. Domain words and
# regulation words only: generic ones like 至少 / 最小 alone would flag writing
# requests too.
_LAW_WORDS = re.compile(
    r"法規|法令|規則|規定|規範|條文|建築法|建築技術|消防|防火|耐燃|避難|逃生|"
    r"樓梯|坡道|欄杆|扶手|走廊|淨高|淨寬|天花板|建蔽率|容積率?|退縮|停車位|"
    r"無障礙|排煙|昇降機|採光|通風|步行距離|"
    r"第\s*[0-9０-９一二三四五六七八九十百]+\s*條"
)


def is_law_question(text: str) -> bool:
    return bool(text) and bool(_LAW_WORDS.search(text))


def _user_filter(user_id: str) -> str:
    """The caller's own documents plus the shared KB, never anyone else's.
    LINE user IDs are 'U' + 32 hex chars; anything that could close the quoted
    string and add a clause (quote, backslash, whitespace) is rejected."""
    if not user_id or _UNSAFE_ID.search(user_id):
        raise ValueError(f"unsafe user id for metadata filter: {user_id!r}")
    return f'user_id="{user_id}" OR user_id="{KB_USER_ID}"'


def _kb_status_map() -> dict:
    """display_name -> status for KB documents, fetched once per process."""
    global _kb_status_cache
    if _kb_status_cache is None:
        store = get_or_create_store()
        statuses = {}
        for d in get_client().file_search_stores.documents.list(parent=store):
            meta = {m.key: m.string_value for m in (d.custom_metadata or [])}
            if meta.get("source") == KB_SOURCE:
                statuses[d.display_name] = meta.get("status", "")
        _kb_status_cache = statuses
    return _kb_status_cache


def _retrieved_titles(response) -> list:
    """Titles of documents actually retrieved (grounding metadata), in order, unique."""
    seen = []
    for cand in getattr(response, "candidates", None) or []:
        gm = getattr(cand, "grounding_metadata", None)
        for chunk in (getattr(gm, "grounding_chunks", None) or []):
            ctx = getattr(chunk, "retrieved_context", None)
            title = getattr(ctx, "title", None) if ctx is not None else None
            if title and title not in seen:
                seen.append(title)
    return seen


def _short_kb_title(title: str) -> str:
    """HJPLUS/建築法規/防火區劃/fire-compartment/SKILL.md -> 防火區劃/fire-compartment"""
    parts = title.split("/")[1:-1]
    return "/".join(parts[-2:]) if parts else title.split("/")[-1]


def _with_sources(response, question: Optional[str] = None) -> str:
    """Append sources computed from what was actually retrieved, plus a warning
    unless every KB source is marked verified: most KB docs carry no status,
    and unmarked is not checked. Deterministic, unlike asking the model to
    cite, and the status lives in metadata the model rarely sees. A law
    question (when the question is known) answered without any KB source gets
    LAW_NO_KB_NOTE instead."""
    text = to_plain_text(response.text or "")
    titles = _retrieved_titles(response)
    kb = [t for t in titles if t.startswith(f"{KB_SOURCE}/")]
    own = [t for t in titles if t not in kb]
    lines = []
    if not kb and question and is_law_question(question):
        lines.append(LAW_NO_KB_NOTE)
    if own:
        lines.append("📎 來源：" + "、".join(own[:5]))
    if kb:
        lines.append(f"📚 法規知識庫（{KB_SOURCE}，CC BY-SA 4.0）：" + "、".join(_short_kb_title(t) for t in kb[:3]))
        try:
            statuses = _kb_status_map()
        except Exception as e:  # a missing warning must not cost the answer
            print(f"[KB] status lookup failed: {e}")
            statuses = {}
        if any(statuses.get(t) != "verified" for t in kb):
            lines.append(KB_UNVERIFIED_NOTE)
    return f"{text}\n\n" + "\n".join(lines) if lines else text


def _text_query_config(store_name: str, user_id: str) -> types.GenerateContentConfig:
    """Shared by query_with_text and scripts/measure_kb_retrieval.py, so the
    measurement exercises exactly what production sends."""
    return types.GenerateContentConfig(
        system_instruction=SYSTEM_PROMPT,
        tools=[
            types.Tool(
                file_search=types.FileSearch(
                    file_search_store_names=[store_name],
                    metadata_filter=_user_filter(user_id),
                )
            )
        ],
    )


async def query_with_text(text: str, user_id: str) -> str:
    """RAG query using text input, restricted to caller's documents."""
    store_name = get_or_create_store()

    response = await _generate_with_retry(
        model=GEN_MODEL,
        contents=text,
        config=_text_query_config(store_name, user_id),
    )
    return _with_sources(response, question=text)


async def query_with_image(image_bytes: bytes, mime_type: str, user_id: str) -> str:
    """RAG query using image input, restricted to caller's documents.
    Primary: pass image directly with file_search tool.
    Fallback: describe image with vision, then text search.
    """
    store_name = get_or_create_store()
    filter_expr = _user_filter(user_id)

    try:
        response = await _generate_with_retry(
            model=GEN_MODEL,
            contents=types.Content(
                parts=[
                    types.Part(
                        inline_data=types.Blob(mime_type=mime_type, data=image_bytes)
                    ),
                    types.Part(
                        text=(
                            "請根據這張圖片的內容，從資料庫中找出相關資訊，"
                            "並提供詳細的分析與說明。請用繁體中文回答。"
                        )
                    ),
                ]
            ),
            config=types.GenerateContentConfig(
                tools=[
                    types.Tool(
                        file_search=types.FileSearch(
                            file_search_store_names=[store_name],
                            metadata_filter=filter_expr,
                        )
                    )
                ],
            ),
        )
        return _with_sources(response)
    except Exception:
        # Fallback: describe image first, then text search
        desc_response = await _generate_with_retry(
            model=GEN_MODEL,
            contents=types.Content(
                parts=[
                    types.Part(
                        inline_data=types.Blob(mime_type=mime_type, data=image_bytes)
                    ),
                    types.Part(
                        text="請詳細描述這張圖片的所有重要內容，包括文字、圖形、物件等。"
                    ),
                ]
            ),
        )
        description = desc_response.text
        return await query_with_text(
            f"根據以下圖片描述，請從資料庫中找到相關資訊：\n\n{description}",
            user_id,
        )
