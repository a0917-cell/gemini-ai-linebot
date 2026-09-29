"""T2 / AC2: users see a Chinese explanation, never the raw exception.

A user screenshot showed the bot replying with
"查詢失敗：503 UNAVAILABLE. {'error': {'code': 503, 'message': ...}}".
Every handler's failure path is driven with that kind of error here.
"""
import asyncio
from types import SimpleNamespace

import pytest

import app.gemini_service as gemini
import app.line_handler as line
from app.session import session_store

OVERLOAD = Exception("503 UNAVAILABLE. {'error': {'code': 503, 'message': 'This model is currently experiencing high demand.'}}")
BAD_REQUEST = Exception("400 INVALID_ARGUMENT. {'error': {'code': 400, 'message': 'Request contains an invalid argument.'}}")
RAW_MARKERS = ("503", "400", "{'error'", "UNAVAILABLE", "INVALID_ARGUMENT", "high demand")


@pytest.fixture
def sent(monkeypatch):
    out = []

    async def fake_reply(token, text, quick_reply=None):
        out.append(text)

    async def fake_push(user_id, text, quick_reply=None):
        out.append(text)

    monkeypatch.setattr(line, "_reply", fake_reply)
    monkeypatch.setattr(line, "_push", fake_push)
    return out


def _raiser(exc):
    async def boom(*args, **kwargs):
        raise exc
    return boom


def _event(message=None, data=None):
    return SimpleNamespace(
        reply_token="rt",
        source=SimpleNamespace(user_id="U1"),
        message=message,
        postback=SimpleNamespace(data=data),
    )


def _assert_clean(texts):
    assert texts, "handler sent nothing"
    for t in texts:
        for marker in RAW_MARKERS:
            assert marker not in t, f"raw error leaked: {t!r}"


@pytest.mark.parametrize("exc", [OVERLOAD, BAD_REQUEST])
def test_text_query_failure_is_explained_in_chinese(monkeypatch, sent, exc):
    monkeypatch.setattr(gemini, "query_with_text", _raiser(exc))

    asyncio.run(line.handle_text_message(_event(SimpleNamespace(text="你好"))))

    _assert_clean(sent)


def test_overload_tells_user_to_retry_shortly(monkeypatch, sent):
    monkeypatch.setattr(gemini, "query_with_text", _raiser(OVERLOAD))

    asyncio.run(line.handle_text_message(_event(SimpleNamespace(text="你好"))))

    assert "忙線" in sent[0] and "再傳一次" in sent[0]


@pytest.mark.parametrize("exc", [OVERLOAD, BAD_REQUEST])
def test_image_failure_is_explained_in_chinese(monkeypatch, sent, exc):
    monkeypatch.setattr(line, "_download_line_content", _raiser(exc))

    asyncio.run(line.handle_image_message(_event(SimpleNamespace(id="m1")), None))

    _assert_clean(sent)


@pytest.mark.parametrize("exc", [OVERLOAD, BAD_REQUEST])
def test_file_failure_is_explained_in_chinese(monkeypatch, sent, exc):
    monkeypatch.setattr(line, "_download_line_content", _raiser(exc))

    asyncio.run(line.handle_file_message(_event(SimpleNamespace(id="m1", file_name="a.pdf")), None))

    _assert_clean(sent)


@pytest.mark.parametrize("exc", [OVERLOAD, BAD_REQUEST])
def test_background_store_failure_is_explained_in_chinese(monkeypatch, sent, exc):
    monkeypatch.setattr(line, "_load_from_gcs", _raiser(exc))

    asyncio.run(line._bg_store_and_notify("U1", "uploads/U1/m1.pdf", "application/pdf", "a.pdf"))

    _assert_clean(sent)


@pytest.mark.parametrize("exc", [OVERLOAD, BAD_REQUEST])
def test_search_failure_is_explained_in_chinese(monkeypatch, sent, exc):
    session_store.set("U1", "gcs_path", "uploads/U1/m1.jpg")
    session_store.set("U1", "mime_type", "image/jpeg")
    session_store.set("U1", "display_name", "image_m1.jpg")
    session_store.set("U1", "content_type", "image")
    monkeypatch.setattr(line, "_load_from_gcs", _raiser(exc))

    asyncio.run(line.handle_postback(_event(data="action=search"), None))

    _assert_clean(sent)
