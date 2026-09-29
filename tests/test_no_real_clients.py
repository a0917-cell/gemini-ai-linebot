"""No test may build a real Gemini, LINE or GCS client.

The sibling line-archiver-bot proved why: its "network-free" suite appended
rows to the production spreadsheet once .env held working credentials. Here
the same lazy constructors exist (gemini.get_client, AsyncApiClient, gcs.Client)
and .env holds a live key, so tests/conftest.py blocks all three. Each test
below fails if that guard is removed.
"""
import asyncio

import pytest

import app.gemini_service as gemini
import app.line_handler as line

BLOCKED = "real client blocked in tests"


def test_genai_client_is_never_built_for_real():
    gemini._client = None

    with pytest.raises(Exception, match=BLOCKED):
        gemini.get_client()


def test_line_api_client_is_never_built_for_real():
    with pytest.raises(Exception, match=BLOCKED):
        asyncio.run(line._reply("reply-token", "hi"))


def test_gcs_client_is_never_built_for_real(monkeypatch):
    monkeypatch.setattr(line, "GCS_BUCKET", "some-bucket")

    with pytest.raises(Exception, match=BLOCKED):
        asyncio.run(line._save_to_gcs(b"x", "a/b.txt", "text/plain"))
