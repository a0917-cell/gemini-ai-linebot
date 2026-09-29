"""T7 / AC4 / AC5: queries see the caller's documents plus the shared HJPLUS KB,
and answers carry their sources, computed from the grounding metadata rather
than left to the model's discretion. Any KB source not marked verified adds a
"待查證" warning: 296 of 332 KB docs carry no status at all, and an unmarked
entry is not a checked one (retrieved chunks rarely include the frontmatter
that holds the status, so the model cannot be relied on to notice it)."""
import asyncio
from types import SimpleNamespace

import pytest

import app.gemini_service as gemini

UID = "U" + "a" * 32


# --- filter (AC4 isolation, AC5 KB) ---

def test_filter_matches_caller_or_shared_kb():
    assert gemini._user_filter(UID) == f'user_id="{UID}" OR user_id="__kb__"'


def test_filter_always_names_the_caller():
    flt = gemini._user_filter(UID)
    assert f'user_id="{UID}"' in flt and flt.startswith(f'user_id="{UID}"')


@pytest.mark.parametrize("bad", ['U1" OR user_id="Uvictim', "U1\\", ""])
def test_filter_rejects_ids_that_could_rewrite_the_expression(bad):
    with pytest.raises(ValueError):
        gemini._user_filter(bad)


# --- sources footer ---

def _resp(text, titles):
    chunks = [SimpleNamespace(retrieved_context=SimpleNamespace(title=t)) for t in titles]
    cand = SimpleNamespace(grounding_metadata=SimpleNamespace(grounding_chunks=chunks))
    return SimpleNamespace(text=text, candidates=[cand])


KB_STATUS = {
    "HJPLUS/建築法規/樓梯欄杆坡道/taiwan-stair-railing-ramp/SKILL.md": "unverified",
    "HJPLUS/建築法規/防火區劃/fire-compartment/SKILL.md": "verified",
    "HJPLUS/設計理論/spatial-planning/SKILL.md": "",
}


@pytest.fixture
def kb(monkeypatch):
    monkeypatch.setattr(gemini, "_kb_status_map", lambda: KB_STATUS)


def test_answer_without_retrieval_is_left_alone(kb):
    assert gemini._with_sources(_resp("直接回答", [])) == "直接回答"


def test_user_documents_are_listed_as_sources(kb):
    out = gemini._with_sources(_resp("回答", ["送審單.pdf", "送審單.pdf", "會議紀錄.txt"]))

    assert "📎 來源：送審單.pdf、會議紀錄.txt" in out


def test_kb_sources_are_attributed_and_shortened(kb):
    out = gemini._with_sources(_resp("回答", ["HJPLUS/建築法規/防火區劃/fire-compartment/SKILL.md"]))

    assert "HJPLUS" in out and "CC BY-SA 4.0" in out
    assert "防火區劃/fire-compartment" in out
    assert "SKILL.md" not in out
    assert gemini.KB_UNVERIFIED_NOTE not in out


@pytest.mark.parametrize("title", [
    "HJPLUS/建築法規/樓梯欄杆坡道/taiwan-stair-railing-ramp/SKILL.md",  # unverified
    "HJPLUS/設計理論/spatial-planning/SKILL.md",  # no status
    "HJPLUS/新主題/not-in-map/SKILL.md",  # unknown to the status map
])
def test_kb_source_not_marked_verified_adds_a_warning(kb, title):
    out = gemini._with_sources(_resp("回答", [title]))

    assert gemini.KB_UNVERIFIED_NOTE in out and "待查證" in out


def test_one_unverified_source_among_verified_ones_still_warns(kb):
    out = gemini._with_sources(_resp("回答", [
        "HJPLUS/建築法規/防火區劃/fire-compartment/SKILL.md",
        "HJPLUS/設計理論/spatial-planning/SKILL.md"]))

    assert gemini.KB_UNVERIFIED_NOTE in out


def test_status_lookup_failure_warns_rather_than_vouching(monkeypatch):
    def boom():
        raise RuntimeError("store unreachable")
    monkeypatch.setattr(gemini, "_kb_status_map", boom)

    out = gemini._with_sources(_resp("回答", ["HJPLUS/建築法規/防火區劃/fire-compartment/SKILL.md"]))

    assert out.startswith("回答") and gemini.KB_UNVERIFIED_NOTE in out


def test_user_documents_alone_never_warn(kb):
    out = gemini._with_sources(_resp("回答", ["送審單.pdf"]))

    assert gemini.KB_UNVERIFIED_NOTE not in out


def test_kb_status_map_is_fetched_once(monkeypatch):
    calls = []

    def docs(parent):
        calls.append(parent)
        return [SimpleNamespace(display_name="HJPLUS/a.md", custom_metadata=[
            SimpleNamespace(key="source", string_value="HJPLUS"),
            SimpleNamespace(key="status", string_value="draft")])]
    monkeypatch.setattr(gemini, "_client", SimpleNamespace(file_search_stores=SimpleNamespace(
        documents=SimpleNamespace(list=docs))))
    monkeypatch.setattr(gemini, "_store_name", "fileSearchStores/s")
    monkeypatch.setattr(gemini, "_kb_status_cache", None)

    assert gemini._kb_status_map() == {"HJPLUS/a.md": "draft"}
    gemini._kb_status_map()
    assert len(calls) == 1


# --- wiring: query_with_text returns the footer ---

def test_query_with_text_appends_sources(monkeypatch, kb):
    async def fake_generate(**kwargs):
        assert kwargs["config"].tools[0].file_search.metadata_filter == gemini._user_filter(UID)
        return _resp("答案", ["送審單.pdf"])
    monkeypatch.setattr(gemini, "_generate_with_retry", fake_generate)
    monkeypatch.setattr(gemini, "_store_name", "fileSearchStores/s")

    out = asyncio.run(gemini.query_with_text("問題", UID))

    assert out.startswith("答案") and "送審單.pdf" in out
