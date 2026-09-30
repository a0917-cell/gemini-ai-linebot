"""T20: pure helpers of scripts/ingest_laws.py. HJPLUS holds practice notes,
not statute text, so the core building laws are added from the local openlawtw
snapshot, one document per chapter so a source line can name the chapter."""
import pytest

import app.gemini_service as gemini
from scripts import ingest_laws as il

SNAP = "2026/9/18 上午 12:00:00"


def law(articles, name="建築技術規則建築設計施工編", snapshot=SNAP):
    return {"id": "D0070115", "name": name, "snapshot": snapshot,
            "url": "https://law.moj.gov.tw/LawClass/LawAll.aspx?pcode=D0070115",
            "articles": articles}


def art(no, text, *path):
    return {"no": no, "text": text, "path": list(path)}


@pytest.mark.parametrize("raw, clean", [
    ("第 三 章 建築物之防火", "第三章 建築物之防火"),
    ("第 十二 章 高層建築物", "第十二章 高層建築物"),
    ("第 二 編 消防設計", "第二編 消防設計"),
    ("第 三 節 防火區劃", "第三節 防火區劃"),
    ("第 二 章 A/B 類", "第二章 A／B 類"),
    # both spellings occur in the real data (設計施工編, 消防法, 職安設施規則)
    ("第 四 章 之 一 建築物安全維護設計", "第四章之一 建築物安全維護設計"),
    ("第 三 章 之一 消防人員安全衛生防護", "第三章之一 消防人員安全衛生防護"),
    ("第 十二 章 之 一 勞工身心健康保護措施", "第十二章之一 勞工身心健康保護措施"),
    # none of today's 262 headings has stray whitespace, but reruns skip by
    # display name, so a future pull with doubled spaces must map to the same name
    ("第 四 章 防火  避難　設施 ", "第四章 防火 避難 設施"),
])
def test_heading_spacing_is_normalised(raw, clean):
    assert il.clean_heading(raw) == clean


def test_one_document_per_chapter_in_order():
    docs = il.build_docs(law([
        art("第 1 條", "用語定義。", "第 一 章 用語定義"),
        art("第 79 條", "防火構造建築物總樓地板面積在一、五００平方公尺以上者，應按每一、五００平方公尺…",
            "第 三 章 建築物之防火", "第 三 節 防火區劃"),
        art("第 80 條", "非防火區劃分間牆…", "第 三 章 建築物之防火", "第 三 節 防火區劃"),
    ]))

    assert [name for name, _ in docs] == [
        "LAW/建築技術規則建築設計施工編/第一章 用語定義.md",
        "LAW/建築技術規則建築設計施工編/第三章 建築物之防火.md",
    ]
    fire = docs[1][1]
    assert "第三節 防火區劃" in fire and fire.count("第三節 防火區劃") == 1
    assert fire.index("第 79 條") < fire.index("第 80 條")
    assert "一、五００平方公尺" in fire


def test_document_header_names_law_chapter_source_and_snapshot():
    [(_, text)] = il.build_docs(law([art("第 1 條", "條文", "第 一 章 總則")]))

    first_lines = text.splitlines()[:3]
    assert first_lines[0] == "# 建築技術規則建築設計施工編 第一章 總則"
    assert "https://law.moj.gov.tw/LawClass/LawAll.aspx?pcode=D0070115" in first_lines[1]
    assert "2026-09-18" in first_lines[1] and "政府資料開放授權條款" in first_lines[1]


def test_law_without_chapters_becomes_one_full_text_document():
    docs = il.build_docs(law([art("第 1 條", "甲"), art("第 2 條", "乙")], name="建築技術規則總則編"))

    assert [name for name, _ in docs] == ["LAW/建築技術規則總則編/全文.md"]
    assert "第 1 條" in docs[0][1] and "第 2 條" in docs[0][1]


@pytest.mark.parametrize("deleted", ["（刪除）", "(刪除)", " （刪除） ", ""])
def test_deleted_and_empty_articles_are_left_out(deleted):
    [(_, text)] = il.build_docs(law([
        art("第 3 條", deleted, "第 二 章 通則"),
        art("第 4 條", "有效條文", "第 二 章 通則"),
    ]))

    assert "第 3 條" not in text and "第 4 條" in text


def test_chapter_with_only_deleted_articles_is_dropped():
    docs = il.build_docs(law([
        art("第 3 條", "（刪除）", "第 二 章 已刪"),
        art("第 4 條", "有效", "第 三 章 有效"),
    ]))

    assert [name for name, _ in docs] == ["LAW/建築技術規則建築設計施工編/第三章 有效.md"]


def test_snapshot_date_is_parsed():
    assert il.snapshot_date(law([])) == "2026-09-18"


def test_snapshot_must_match_what_the_bot_tells_users():
    # the footer shows gemini.LAW_SNAPSHOT; a newer openlawtw pull must not be
    # uploaded under an old date
    with pytest.raises(ValueError, match="LAW_SNAPSHOT"):
        il.build_docs(law([art("第 1 條", "甲", "第 一 章 總則")], snapshot="2026/12/1 上午 12:00:00"))


def test_metadata_marks_shared_law_documents():
    md = {m["key"]: m["string_value"] for m in il.metadata_for("D0070115")}

    assert md == {"user_id": gemini.KB_USER_ID, "source": gemini.LAW_SOURCE,
                  "law_id": "D0070115", "snapshot": gemini.LAW_SNAPSHOT}


def test_core_law_list_is_the_agreed_twelve():
    assert len(il.CORE_LAWS) == 12 and len(set(il.CORE_LAWS)) == 12
    assert {"D0070114", "D0070115", "D0070116", "D0070117", "D0070109", "D0120029"} <= set(il.CORE_LAWS)
