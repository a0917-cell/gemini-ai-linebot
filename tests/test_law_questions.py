"""T19: gemini-3.8-flash decides for itself whether to call File Search, and
on 2026-09-30 it cited the HJPLUS KB for 1 of 10 law questions. The SDK has no
way to force a built-in tool, so (user decision) the prompt asks for it and a
keyword check makes the gap visible: a law question answered without any KB
source gets a fixed warning."""
import asyncio
from types import SimpleNamespace

import pytest

import app.gemini_service as gemini

UID = "U" + "a" * 32

# the ten questions scripts/measure_kb_retrieval.py sends
LAW = [
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
    "建築技術規則第 33 條在講什麼？",
    "第三十三條的內容？",
]
GENERAL = [
    "幫我寫一封請假信",
    "翻譯成越南文：明天停工",
    "Revit 怎麼匯出 IFC？",
    "幫我改寫這段會議紀錄，至少列三點",
    "今天天氣如何",
    "送審單.pdf 裡的審查意見是什麼？",
]


@pytest.mark.parametrize("q", LAW)
def test_law_questions_are_recognised(q):
    assert gemini.is_law_question(q)


@pytest.mark.parametrize("q", GENERAL)
def test_general_questions_are_not(q):
    assert not gemini.is_law_question(q)


def _resp(text, titles=()):
    chunks = [SimpleNamespace(retrieved_context=SimpleNamespace(title=t)) for t in titles]
    cand = SimpleNamespace(grounding_metadata=SimpleNamespace(grounding_chunks=chunks))
    return SimpleNamespace(text=text, candidates=[cand])


@pytest.fixture
def kb(monkeypatch):
    monkeypatch.setattr(gemini, "_kb_status_map", lambda: {})


def test_law_answer_without_any_source_is_flagged(kb):
    out = gemini._with_sources(_resp("**淨寬** 75 公分"), question="樓梯最小寬度？")

    assert out == "淨寬 75 公分\n\n" + gemini.LAW_NO_KB_NOTE


def test_law_answer_citing_only_user_documents_is_flagged(kb):
    out = gemini._with_sources(_resp("答案", ["送審單.pdf"]), question="樓梯最小寬度？")

    assert "📎 來源：送審單.pdf" in out and gemini.LAW_NO_KB_NOTE in out


def test_law_answer_citing_the_kb_is_not_flagged(kb):
    out = gemini._with_sources(_resp("答案", ["HJPLUS/建築法規/樓梯/stair/SKILL.md"]), question="樓梯最小寬度？")

    assert gemini.LAW_NO_KB_NOTE not in out and "📚 法規知識庫" in out


def test_general_answer_without_sources_is_left_alone(kb):
    assert gemini._with_sources(_resp("好的"), question="幫我寫一封請假信") == "好的"


def test_no_question_means_no_flag(kb):
    # the image path has no question text to classify
    assert gemini._with_sources(_resp("圖片裡是樓梯")) == "圖片裡是樓梯"


def test_query_with_text_flags_an_ungrounded_law_answer(monkeypatch, kb):
    async def fake_generate(**kwargs):
        return _resp("75 公分")
    monkeypatch.setattr(gemini, "_generate_with_retry", fake_generate)
    monkeypatch.setattr(gemini, "_store_name", "fileSearchStores/s")

    out = asyncio.run(gemini.query_with_text("樓梯最小寬度？", UID))

    assert out.endswith(gemini.LAW_NO_KB_NOTE)


def test_prompt_tells_the_model_to_search_the_kb_for_law_questions():
    # the words alone also appear in other rules; pin the instruction itself
    # (whether the model obeys it is measured by scripts/measure_kb_retrieval.py)
    assert "一定要先用檔案搜尋查法規知識庫" in gemini.SYSTEM_PROMPT
