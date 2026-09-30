"""T20: statute documents (source LAW) cite differently from HJPLUS notes:
official text, so no 待查證, but a snapshot date; and citing one counts as
using the KB, so LAW_NO_KB_NOTE stays away."""
from types import SimpleNamespace

import pytest

import app.gemini_service as gemini

FIRE = "LAW/建築技術規則建築設計施工編/第三章 建築物之防火.md"
FULL = "LAW/建築技術規則總則編/全文.md"
NOTE = "HJPLUS/建築法規/消防安全/排煙窗法規檢討/domain.md"


def _resp(text, titles=()):
    chunks = [SimpleNamespace(retrieved_context=SimpleNamespace(title=t)) for t in titles]
    cand = SimpleNamespace(grounding_metadata=SimpleNamespace(grounding_chunks=chunks))
    return SimpleNamespace(text=text, candidates=[cand])


@pytest.fixture
def kb(monkeypatch):
    monkeypatch.setattr(gemini, "_kb_status_map", lambda: {})


def test_statute_citation_names_law_chapter_and_snapshot(kb):
    out = gemini._with_sources(_resp("答案", [FIRE, FULL]), question="防火區劃面積上限？")

    assert out == ("答案\n\n📜 法規條文（全國法規資料庫，快照 2026-09-18）："
                   "建築技術規則建築設計施工編／第三章 建築物之防火、建築技術規則總則編／全文")


def test_statute_citation_is_not_marked_unverified(kb):
    out = gemini._with_sources(_resp("答案", [FIRE]), question="防火區劃面積上限？")

    assert gemini.KB_UNVERIFIED_NOTE not in out and gemini.LAW_NO_KB_NOTE not in out


def test_statutes_and_notes_are_listed_separately(kb):
    out = gemini._with_sources(_resp("答案", [NOTE, FIRE, "送審單.pdf"]), question="排煙設備規定？")

    lines = out.split("\n\n", 1)[1].splitlines()
    assert lines[0] == "📎 來源：送審單.pdf"
    assert lines[1].startswith("📜 法規條文") and "第三章 建築物之防火" in lines[1] and "排煙" not in lines[1]
    assert lines[2].startswith("📚 法規知識庫") and "排煙窗法規檢討" in lines[2]
    assert lines[3] == gemini.KB_UNVERIFIED_NOTE  # the HJPLUS note still carries no status


def test_statutes_are_capped_like_other_sources(kb):
    titles = [f"LAW/某法/第{i}章 x.md" for i in range(1, 7)]
    out = gemini._with_sources(_resp("答案", titles))

    assert out.count("某法／") == 3


def test_snapshot_constant_matches_the_footer():
    assert gemini.LAW_SNAPSHOT == "2026-09-18" and gemini.LAW_SOURCE == "LAW"
