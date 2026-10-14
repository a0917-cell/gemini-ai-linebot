"""T26: the model cannot set reminders, so it must not claim it did.
2026-10-06 a message without 提醒我 reached the RAG answer, Gemini replied
「已幫您設定好提醒」and nothing was stored. The prompt forbids the claim; when
the model makes it anyway, the code appends a warning."""
import asyncio
from types import SimpleNamespace

import pytest

import app.gemini_service as gemini
import app.line_handler as line
import app.reminders as rem

A = "U" + "a" * 32
# The 2026-10-06 reply, from the user's screenshot.
FAKE = (
    "已幫您設定好提醒：\n\n・時間：10月11日（日）上午 10:00 跳通知\n・內容：\n"
    "提醒下周二下午1330德運開會\n1.檢討植棲施工進度\n2.植生牆細節討論"
)


@pytest.fixture
def chat(monkeypatch):
    sent = []
    answer = {"text": ""}

    async def fake_reply(reply_token, text, quick_reply=None):
        sent.append(text)

    async def no_loading(user_id, seconds=60):
        pass

    async def rag_answer(text, user_id):
        return answer["text"]

    monkeypatch.setattr(line, "_reply", fake_reply)
    monkeypatch.setattr(line, "_show_loading", no_loading)
    monkeypatch.setattr(gemini, "query_with_text", rag_answer)
    monkeypatch.setattr(rem, "get_store", lambda: rem.InMemoryReminderStore())
    return SimpleNamespace(sent=sent, answer=answer)


def ask(text):
    event = SimpleNamespace(message=SimpleNamespace(text=text), source=SimpleNamespace(user_id=A),
                            reply_token="rt")
    asyncio.run(line.handle_text_message(event))


def test_prompt_says_reminders_cannot_be_set_here():
    assert "你不能設定提醒" in gemini.SYSTEM_PROMPT
    assert "不要說已經設定好" in gemini.SYSTEM_PROMPT


@pytest.mark.parametrize("claim", [
    FAKE,
    "好的，已為你設定提醒，明天 9 點會通知你。",
    "我幫您設好提醒了。",
    "已經安排好通知。",
    "已經成功設定提醒。",  # words between 已 and the verb
])
def test_claimed_reminder_gets_a_warning(chat, claim):
    chat.answer["text"] = claim
    ask("下週二開會")

    assert chat.sent[-1].startswith(claim)
    assert chat.sent[-1].endswith(line.FAKE_REMINDER_NOTE)


@pytest.mark.parametrize("honest", [
    "樓梯最小寬度依建築技術規則第 33 條。",
    "提醒您：法規數值請以原文為準。",
    "我沒辦法設定提醒，請用「明天 9 點提醒我繳圖」這種寫法。",
])
def test_other_answers_are_untouched(chat, honest):
    chat.answer["text"] = honest
    ask("樓梯寬度？")

    assert chat.sent[-1] == honest
