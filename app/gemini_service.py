import os
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

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
GCS_BUCKET = os.environ.get("GCS_BUCKET", "")
# Stable (non-preview) ids; both support File Search and the free tier (checked
# against ai.google.dev 2026-09-29). The preview model 503'd under load.
DEFAULT_MODEL = "gemini-3.8-flash"
DEFAULT_FALLBACK_MODEL = "gemini-3.5-flash-lite"
GEN_MODEL = os.environ.get("GEMINI_MODEL", DEFAULT_MODEL)
FALLBACK_MODEL = os.environ.get("GEMINI_FALLBACK_MODEL", DEFAULT_FALLBACK_MODEL)

STORE_NAME_BLOB = "config/file_search_store_name.txt"
SYSTEM_PROMPT = (
    "你是一個智慧助理，根據資料庫內容回答問題。"
    "請用繁體中文回答，並盡量引用具體資料內容。"
    "若資料庫中沒有足夠資訊，請如實告知並給出最佳建議。"
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


def _user_filter(user_id: str) -> str:
    """Metadata filter expression — restricts results to documents owned by user_id."""
    # LINE user IDs are 'U' + 32 hex chars, safe in google.aip.dev/160 filter syntax
    return f'user_id="{user_id}"'


async def query_with_text(text: str, user_id: str) -> str:
    """RAG query using text input, restricted to caller's documents."""
    store_name = get_or_create_store()

    response = await _generate_with_retry(
        model=GEN_MODEL,
        contents=text,
        config=types.GenerateContentConfig(
            system_instruction=SYSTEM_PROMPT,
            tools=[
                types.Tool(
                    file_search=types.FileSearch(
                        file_search_store_names=[store_name],
                        metadata_filter=_user_filter(user_id),
                    )
                )
            ],
        ),
    )
    return response.text


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
        return response.text
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
