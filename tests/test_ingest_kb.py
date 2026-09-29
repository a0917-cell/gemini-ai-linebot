"""T6: pure helpers of scripts/ingest_kb.py (collect, status, skip, metadata).

The trust field in the HJPLUS KB is metadata.status (nested), with a few
files using a top-level status; only 34 of 90 SKILL.md carry the nested one,
so "no status" must stay distinguishable from "verified"."""
from scripts import ingest_kb as kb


def _write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_collect_files_takes_markdown_only_with_stable_display_names(tmp_path):
    _write(tmp_path / "index.md", "# idx")
    _write(tmp_path / "建築法規" / "防火" / "SKILL.md", "# a")
    _write(tmp_path / "建築法規" / "note.pdf", "x")

    files = kb.collect_files(tmp_path)

    assert [name for _, name in files] == ["HJPLUS/index.md", "HJPLUS/建築法規/防火/SKILL.md"]


def test_nested_metadata_status_is_read():
    text = "---\nname: x\nmetadata:\n  region: taiwan\n  status: unverified\n---\n# body\n"
    assert kb.parse_status(text) == "unverified"


def test_top_level_status_is_used_when_no_nested_one():
    assert kb.parse_status("---\nstatus: draft\n---\n# body\n") == "draft"


def test_nested_status_wins_over_top_level():
    text = "---\nstatus: draft\nmetadata:\n  status: verified\n---\n"
    assert kb.parse_status(text) == "verified"


def test_status_in_body_or_missing_frontmatter_counts_as_none():
    assert kb.parse_status("# title\nstatus: verified\n") == ""
    assert kb.parse_status("---\nname: x\n---\nstatus: verified\n") == ""


def test_already_uploaded_documents_are_skipped():
    files = [("a", "HJPLUS/a.md"), ("b", "HJPLUS/b.md")]

    assert kb.plan_uploads(files, existing={"HJPLUS/a.md"}) == [("b", "HJPLUS/b.md")]


def test_metadata_marks_kb_source_and_status():
    md = kb.metadata_for("unverified")

    assert {"key": "user_id", "string_value": "__kb__"} in md
    assert {"key": "source", "string_value": "HJPLUS"} in md
    assert {"key": "status", "string_value": "unverified"} in md


def test_non_ascii_file_names_are_uploaded_under_an_ascii_name(tmp_path):
    # The SDK puts the file's basename in an HTTP header; "安裝與分享說明.md"
    # failed with "'ascii' codec can't encode" on the 2026-09-29 run.
    src = tmp_path / "安裝與分享說明.md"
    src.write_text("---\nstatus: draft\n---\n# x\n", encoding="utf-8")
    seen = {}

    def upload(file_search_store_name, file, config):
        seen["basename"] = file.replace("\\", "/").rsplit("/", 1)[-1]
        seen["content"] = open(file, encoding="utf-8").read()
        seen["display_name"] = config["display_name"]
        from types import SimpleNamespace
        return SimpleNamespace(done=True, error=None)

    from types import SimpleNamespace
    client = SimpleNamespace(file_search_stores=SimpleNamespace(upload_to_file_search_store=upload))

    kb._upload_one(client, "fileSearchStores/s", src, "HJPLUS/安裝與分享說明.md")

    assert seen["basename"].isascii()
    assert seen["content"].startswith("---\nstatus: draft")
    assert seen["display_name"] == "HJPLUS/安裝與分享說明.md"


def test_metadata_omits_status_when_file_has_none():
    assert all(m["key"] != "status" for m in kb.metadata_for(""))
