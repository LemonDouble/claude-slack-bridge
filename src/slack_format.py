"""slack_format.py — Slack 표시용 텍스트 변환.

Slack API를 호출하지 않는 순수 함수만 모은다. 마크다운 → mrkdwn 변환,
길이 제한에 맞춘 분할, 발췌문 평문화, 사용량 요약 등 "무엇을 보여줄지"가
아니라 "어떻게 보이게 할지"에 해당하는 것들이다.
"""

import json
import re
from typing import Any

from claude_handler import ClaudeResult

# 승인 메시지의 도구 요약에서 경로/명령을 뽑을 키 (그 외는 입력 전체를 JSON으로)
_SUMMARY_KEYS = {"Bash": "command", "Edit": "file_path", "Write": "file_path", "Read": "file_path"}
_SUMMARY_MAX = 500


def markdown_to_slack(text: str) -> str:
    """Convert standard Markdown to Slack mrkdwn format."""
    # Headers: ## Header → *Header*
    text = re.sub(r"^#{1,6}\s+(.+)$", r"*\1*", text, flags=re.MULTILINE)
    # Bold: **text** → *text*
    text = re.sub(r"\*\*(.+?)\*\*", r"*\1*", text)
    # Italic: _text_ stays the same, but *text* (single) that isn't bold needs care
    # Strikethrough: ~~text~~ → ~text~
    text = re.sub(r"~~(.+?)~~", r"~\1~", text)
    # Links: [text](url) → <url|text>
    text = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r"<\2|\1>", text)
    # Images: ![alt](url) → <url|alt> (best effort in Slack)
    text = re.sub(r"!\[([^\]]*)\]\(([^)]+)\)", r"<\2|\1>", text)
    return text


def plain(text: str) -> str:
    """발췌문에서 서식 문자를 제거한다.

    잘린 코드블록이나 짝이 맞지 않는 강조 기호가 Slack 렌더링을 깨뜨리지
    않도록, 미리보기는 서식 없는 평문으로 보여준다. 발췌문은 한 줄로 접힌
    상태라 헤딩 마커가 줄 중간에 나타날 수 있다.
    """
    text = re.sub(r"(?:^|\s)#{1,6}\s+", " ", text)
    text = text.translate(str.maketrans("", "", "`*_~"))
    # 본문의 <...>가 Slack 링크 문법으로 해석되지 않도록 이스케이프한다.
    text = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return text.strip()


def split_message(text: str, max_length: int) -> list[str]:
    """Split text into chunks at line boundaries, preserving code blocks."""
    if len(text) <= max_length:
        return [text]

    chunks: list[str] = []
    current = ""
    in_code_block = False

    for line in text.split("\n"):
        line_with_newline = line + "\n"

        # Track code block state
        if line.startswith("```"):
            in_code_block = not in_code_block

        # If adding this line would exceed the limit, flush current chunk
        if current and len(current) + len(line_with_newline) > max_length:
            # If we're inside a code block, close it in the current chunk
            if in_code_block:
                current += "```\n"
            chunks.append(current.rstrip("\n"))
            # Re-open code block in the next chunk
            current = "```\n" + line_with_newline if in_code_block else line_with_newline
        else:
            current += line_with_newline

    if current.strip():
        chunks.append(current.rstrip("\n"))

    return chunks


def permission_summary(tool_name: str, tool_input: dict) -> str:
    """승인 메시지에 보여줄 도구 호출 요약."""
    key = _SUMMARY_KEYS.get(tool_name)
    summary = tool_input.get(key, "") if key else json.dumps(tool_input, ensure_ascii=False)
    if len(summary) > _SUMMARY_MAX:
        summary = summary[: _SUMMARY_MAX - 3] + "…"
    return summary or "(입력 없음)"


def usage_footer(result: ClaudeResult) -> str:
    """Format a usage/cost summary line. Returns empty string if no usage."""
    if result.total_cost_usd == 0 and result.input_tokens == 0:
        return ""

    total_input = result.input_tokens + result.cache_read_tokens + result.cache_creation_tokens
    parts = [
        f":bar_chart: *{model_label(result.requested_model, result.model_usage)}* | ",
        f"Tokens In: `{total_input:,}` Out: `{result.output_tokens:,}`",
    ]
    if result.cache_read_tokens:
        cache_pct = result.cache_read_tokens / total_input * 100 if total_input else 0
        parts.append(f" (cache hit `{cache_pct:.0f}%`)")
    parts.append(f" | Cost: `${result.total_cost_usd:.4f}`")
    parts.append(f" | Time: `{result.duration_ms / 1000:.1f}s`")
    return "".join(parts)


def model_label(requested: str, model_usage: dict[str, Any]) -> str:
    """Return a display label using the requested model name."""
    for model_id in model_usage:
        if requested and requested in model_id:
            return _format_model_name(model_id)
    return requested.capitalize() if requested else "Unknown"


def _format_model_name(model_id: str) -> str:
    """Convert a model ID like 'claude-opus-4-6' to 'Opus 4.6'."""
    name = model_id.removeprefix("claude-")
    name = re.sub(r"-\d{8,}$", "", name)
    parts = name.split("-")
    if len(parts) >= 3 and parts[-2].isdigit() and parts[-1].isdigit():
        family = " ".join(p.capitalize() for p in parts[:-2])
        return f"{family} {parts[-2]}.{parts[-1]}"
    if len(parts) >= 2 and parts[-1].isdigit():
        family = " ".join(p.capitalize() for p in parts[:-1])
        return f"{family} {parts[-1]}"
    return name.replace("-", " ").title()
