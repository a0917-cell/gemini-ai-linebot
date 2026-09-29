"""LINE text messages render no Markdown: `**粗體**` and `## 標題` show up as
literal symbols. Flatten model output to plain text, keeping list structure."""
import re

_FENCE = re.compile(r"^\s*```")
_HEADING = re.compile(r"^\s{0,3}#{1,6}\s+")
_QUOTE = re.compile(r"^\s*>\s?")
_RULE = re.compile(r"^\s*([-*_])(\s*\1){2,}\s*$")
_BULLET = re.compile(r"^(\s*)[*+-]\s+")
_TABLE_SEP = re.compile(r"^\s*\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)*\|?\s*$")
_TABLE_ROW = re.compile(r"^\s*\|(.*)\|\s*$")
_BOLD = re.compile(r"(\*\*|__)(?=\S)(.+?)(?<=\S)\1")
_ITALIC = re.compile(r"(?<![*\w])\*(?=\S)([^*\n]+?)(?<=\S)\*(?![*\w])")
_CODE = re.compile(r"`([^`\n]+)`")
_LINK = re.compile(r"\[([^\]\n]+)\]\((https?://[^)\s]+)\)")
_BLANKS = re.compile(r"\n{3,}")


def _inline(line: str) -> str:
    line = _CODE.sub(r"\1", line)
    line = _LINK.sub(r"\1 (\2)", line)
    line = _BOLD.sub(r"\2", line)
    return _ITALIC.sub(r"\1", line)


def to_plain_text(md: str) -> str:
    out = []
    for line in (md or "").split("\n"):
        if _RULE.match(line):  # before _TABLE_SEP, which a bare --- also matches
            out.append("")
            continue
        if _FENCE.match(line) or _TABLE_SEP.match(line):
            continue
        row = _TABLE_ROW.match(line)
        if row:
            line = "｜".join(cell.strip() for cell in row.group(1).split("|"))
        line = _HEADING.sub("", line)
        line = _QUOTE.sub("", line)
        line = _BULLET.sub(r"\1・", line)
        out.append(_inline(line))
    return _BLANKS.sub("\n\n", "\n".join(out)).strip()
