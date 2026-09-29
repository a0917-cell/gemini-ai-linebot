"""T8 / AC4: a user's documents are visible to that user only.

Read side: every query path filters on the caller's own LINE id (plus the
shared KB). Write side: a document tagged user_id="__kb__" is visible to
EVERY user, so no upload path may tag a personal file that way, directly or
through extra metadata."""
import asyncio
from types import SimpleNamespace

import pytest

import app.gemini_service as gemini
import app.line_handler as line

ALICE = "U" + "a" * 32
BOB = "U" + "b" * 32


def _capture_filters(monkeypatch):
    seen = []

    async def fake_generate(**kwargs):
        config = kwargs.get("config")
        if config and config.tools:
            seen.append(config.tools[0].file_search.metadata_filter)
        return SimpleNamespace(text="ok", candidates=[])
    monkeypatch.setattr(gemini, "_generate_with_retry", fake_generate)
    monkeypatch.setattr(gemini, "_store_name", "fileSearchStores/s")
    return seen


def test_text_query_filters_on_the_caller_only(monkeypatch):
    seen = _capture_filters(monkeypatch)

    asyncio.run(gemini.query_with_text("q", ALICE))

    assert seen == [f'user_id="{ALICE}" OR user_id="__kb__"']
    assert BOB not in seen[0]


def test_image_query_filters_on_the_caller_only(monkeypatch):
    seen = _capture_filters(monkeypatch)

    asyncio.run(gemini.query_with_image(b"img", "image/jpeg", ALICE))

    assert seen == [f'user_id="{ALICE}" OR user_id="__kb__"']


def test_text_handler_queries_with_the_senders_line_id(monkeypatch):
    asked = []

    async def fake_query(text, user_id):
        asked.append(user_id)
        return "ok"

    async def noop(*a, **k):
        return None
    monkeypatch.setattr(gemini, "query_with_text", fake_query)
    monkeypatch.setattr(line, "_show_loading", noop)
    monkeypatch.setattr(line, "_reply", noop)
    event = SimpleNamespace(reply_token="rt", source=SimpleNamespace(user_id=BOB),
                            message=SimpleNamespace(text="hi"))

    asyncio.run(line.handle_text_message(event))

    assert asked == [BOB]


# --- write side ---

@pytest.fixture
def uploads(monkeypatch):
    calls = []

    def upload(file_search_store_name, file, config):
        calls.append(config["custom_metadata"])
        return SimpleNamespace(done=True)
    monkeypatch.setattr(gemini, "_client", SimpleNamespace(
        file_search_stores=SimpleNamespace(upload_to_file_search_store=upload)))
    monkeypatch.setattr(gemini, "_store_name", "fileSearchStores/s")
    return calls


def test_upload_is_tagged_with_the_uploader(uploads):
    gemini._upload_and_index_sync(b"x", "text/plain", "a.txt", ALICE)

    assert uploads == [[{"key": "user_id", "string_value": ALICE}]]


def test_personal_upload_can_never_be_tagged_as_shared_kb(uploads):
    with pytest.raises(ValueError):
        gemini._upload_and_index_sync(b"x", "text/plain", "a.txt", gemini.KB_USER_ID)
    assert uploads == []


@pytest.mark.parametrize("key", ["user_id", "source"])
def test_extra_metadata_cannot_override_ownership(uploads, key):
    with pytest.raises(ValueError):
        gemini._upload_and_index_sync(b"x", "text/plain", "a.txt", ALICE,
                                      extra_metadata=[{"key": key, "string_value": "__kb__"}])
    assert uploads == []
