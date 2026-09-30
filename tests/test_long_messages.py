"""T23: LINE rejects a text message over 5,000 characters, and a reply or
push carrying one fails as a whole, so the user got nothing at all for a long
answer. Text is split into at most 5 messages (LINE's per-request cap), at a
newline where possible, and truncated with a note beyond that."""
import asyncio

import pytest
from linebot.v3.messaging import QuickReply

import app.line_handler as line
import app.reminders as rem

LIMIT = 5000  # LINE: text message at most 5,000 characters


def paragraphs(n, width=100):
    return "\n".join(f"{i:04d}" + "字" * (width - 4) for i in range(n))


def test_short_text_is_one_message():
    [m] = line._text_messages("你好")
    assert m.text == "你好" and m.quick_reply is None


def test_long_text_is_split_below_the_limit_without_losing_content():
    text = paragraphs(120)  # ~12,100 characters

    msgs = line._text_messages(text)

    assert 2 <= len(msgs) <= 5
    assert all(len(m.text) <= LIMIT for m in msgs)
    assert "".join(m.text for m in msgs).replace("\n", "") == text.replace("\n", "")


def test_splits_land_on_newlines_when_there_are_any():
    msgs = line._text_messages(paragraphs(120))

    # every piece starts with a paragraph number, i.e. no paragraph was cut in half
    assert all(m.text[:4].isdigit() for m in msgs)


def test_text_without_newlines_is_still_split():
    msgs = line._text_messages("字" * 12000)

    assert len(msgs) == 3 and all(len(m.text) <= LIMIT for m in msgs)
    assert sum(len(m.text) for m in msgs) == 12000


def test_beyond_five_messages_the_answer_is_truncated_with_a_note():
    msgs = line._text_messages(paragraphs(400))  # ~40,000 characters

    assert len(msgs) == 5
    assert all(len(m.text) <= LIMIT for m in msgs)
    assert msgs[-1].text.endswith(line.TRUNCATED_NOTE)


def test_quick_reply_rides_on_the_last_message_only():
    r = rem.Reminder(id="abc", user_id="U1", due_at=None, text="繳圖")
    qr = QuickReply(items=[line._cancel_button(r, "取消這個提醒")])

    msgs = line._text_messages(paragraphs(120), qr)

    assert msgs[-1].quick_reply == qr
    assert all(m.quick_reply is None for m in msgs[:-1])


class _Recorder:
    def __init__(self, configuration=None):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


@pytest.fixture
def api(monkeypatch):
    sent = []

    class FakeApi:
        def __init__(self, client):
            pass

        async def reply_message(self, request):
            sent.append(("reply", request))

        async def push_message(self, request):
            sent.append(("push", request))

    monkeypatch.setattr(line, "AsyncApiClient", _Recorder)
    monkeypatch.setattr(line, "AsyncMessagingApi", FakeApi)
    return sent


@pytest.mark.parametrize("send", ["reply", "push"])
def test_reply_and_push_both_split(api, send):
    text = paragraphs(120)

    if send == "reply":
        asyncio.run(line._reply("rt", text))
    else:
        asyncio.run(line._push("U1", text))

    [(kind, request)] = api
    assert kind == send
    assert len(request.messages) >= 2 and all(len(m.text) <= LIMIT for m in request.messages)
