"""T18: LINE renders no Markdown, so `**粗體**` and `## 標題` from the model
arrive as literal symbols. Answers are flattened to plain text before sending;
list structure is kept because it is what makes a phone-length answer readable."""
from types import SimpleNamespace

import pytest

import app.gemini_service as gemini
from app.formatting import to_plain_text


@pytest.mark.parametrize("md, plain", [
    ("樓梯寬度 **至少 120 公分**。", "樓梯寬度 至少 120 公分。"),
    ("__重點__ 在這", "重點 在這"),
    ("這是 *斜體* 字", "這是 斜體 字"),
    ("## 結論\n內容", "結論\n內容"),
    ("###### 六級標題", "六級標題"),
    ("用 `GEMINI_MODEL` 設定", "用 GEMINI_MODEL 設定"),
    ("見 [全國法規資料庫](https://law.moj.gov.tw)", "見 全國法規資料庫 (https://law.moj.gov.tw)"),
    ("> 引用一句", "引用一句"),
    ("上面\n---\n下面", "上面\n\n下面"),
    ("```python\nprint(1)\n```", "print(1)"),
])
def test_markdown_syntax_is_removed(md, plain):
    assert to_plain_text(md) == plain


def test_bullets_become_dots_and_keep_their_indent():
    md = "* 第一點\n- 第二點\n  + 子項"
    assert to_plain_text(md) == "・第一點\n・第二點\n  ・子項"


def test_numbered_lists_are_kept_as_is():
    md = "1. 放樣\n2. 綁筋\n3. 灌漿"
    assert to_plain_text(md) == md


def test_bold_inside_a_list_item():
    assert to_plain_text("* **淨寬**：120 cm") == "・淨寬：120 cm"


def test_tables_become_rows_separated_by_full_width_bars():
    md = "| 用途 | 寬度 |\n|---|:---:|\n| 住宅 | 120 cm |"
    assert to_plain_text(md) == "用途｜寬度\n住宅｜120 cm"


@pytest.mark.parametrize("text", [
    "面積 = 3 * 4 * 5 m²",
    "檔名 floor_plan_v2_final.pdf",
    "C#、Python 都可以",
    "第 #3 號送審單",
    "溫度 -5 度",
])
def test_ordinary_symbols_are_left_alone(text):
    assert to_plain_text(text) == text


def test_blank_line_runs_collapse():
    assert to_plain_text("甲\n\n\n\n乙") == "甲\n\n乙"


def test_empty_input():
    assert to_plain_text("") == ""


# --- wiring: every answer path goes through _with_sources ---

def _resp(text, titles=()):
    chunks = [SimpleNamespace(retrieved_context=SimpleNamespace(title=t)) for t in titles]
    cand = SimpleNamespace(grounding_metadata=SimpleNamespace(grounding_chunks=chunks))
    return SimpleNamespace(text=text, candidates=[cand])


def test_answer_without_sources_is_flattened():
    assert gemini._with_sources(_resp("## 答案\n**重點**")) == "答案\n重點"


def test_answer_with_sources_is_flattened_and_filenames_survive():
    out = gemini._with_sources(_resp("**答案**", ["plan_v2__final.pdf"]))

    assert out.startswith("答案\n\n")
    assert "📎 來源：plan_v2__final.pdf" in out


def test_system_prompt_forbids_markdown():
    assert "Markdown" in gemini.SYSTEM_PROMPT
