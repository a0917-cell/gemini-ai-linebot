"""T3 / plan decision (a): the bot stays on Gemini's free tier, whose content
may be used to improve Google's products. The user chose to keep uploading
but only non-sensitive files, so the warning must appear at the moment of
choice: when an image or file arrives, before "存入資料庫" is tapped."""
import asyncio
from types import SimpleNamespace

import pytest

import app.line_handler as line


@pytest.fixture
def sent(monkeypatch):
    out = []

    async def fake_reply(token, text, quick_reply=None):
        out.append(text)

    async def fake_download(message_id):
        return b"data"

    async def fake_save(data, path, content_type):
        return path

    monkeypatch.setattr(line, "_reply", fake_reply)
    monkeypatch.setattr(line, "_download_line_content", fake_download)
    monkeypatch.setattr(line, "_save_to_gcs", fake_save)
    return out


def _event(message):
    return SimpleNamespace(reply_token="rt", source=SimpleNamespace(user_id="U1"), message=message)


def test_received_image_prompt_carries_the_privacy_note(sent):
    asyncio.run(line.handle_image_message(_event(SimpleNamespace(id="m1")), None))

    assert line.PRIVACY_NOTE in sent[0]


def test_received_file_prompt_carries_the_privacy_note(sent):
    asyncio.run(line.handle_file_message(_event(SimpleNamespace(id="m1", file_name="a.pdf")), None))

    assert line.PRIVACY_NOTE in sent[0]


def test_privacy_note_names_the_actual_risk():
    assert "Google" in line.PRIVACY_NOTE and "機密" in line.PRIVACY_NOTE
