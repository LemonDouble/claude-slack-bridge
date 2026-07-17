"""session_catalog.py — Claude CLI 세션 파일 조회.

Claude CLI는 세션을 ``~/.claude/projects/<인코딩된 cwd>/<session-id>.jsonl``에
저장한다 (경로 인코딩: 영숫자 외 문자를 ``-``로 치환). 이 모듈은 특정 프로젝트
디렉토리의 세션 목록을 스캔해 Slack UI에 보여줄 제목/수정 시각을 추출한다.

터미널에서 시작한 세션과 Slack에서 시작한 세션이 같은 곳에 저장되므로,
양쪽 어디서든 서로의 세션을 이어갈 수 있다.
"""

import json
import logging
import re
import time
from dataclasses import dataclass
from pathlib import Path

from constants import CLAUDE_PROJECTS_DIR

logger = logging.getLogger(__name__)

_TITLE_SCAN_LINES = 200
_TITLE_MAX_LENGTH = 60


@dataclass
class SessionInfo:
    session_id: str
    title: str
    mtime: float


def encode_project_path(project_dir: str) -> str:
    """Claude CLI의 세션 디렉토리 인코딩 규칙 (영숫자 외 → '-')."""
    return re.sub(r"[^a-zA-Z0-9]", "-", project_dir)


def list_sessions(project_dir: str, limit: int = 5) -> list[SessionInfo]:
    """프로젝트 디렉토리의 Claude CLI 세션을 최신순으로 반환한다."""
    sessions_dir = CLAUDE_PROJECTS_DIR / encode_project_path(project_dir)
    if not sessions_dir.is_dir():
        return []

    files = sorted(
        (f for f in sessions_dir.glob("*.jsonl") if f.is_file()),
        key=lambda f: f.stat().st_mtime,
        reverse=True,
    )

    sessions: list[SessionInfo] = []
    for f in files[:limit]:
        try:
            title = _extract_title(f)
        except Exception as exc:
            logger.debug("Failed to extract title from %s: %s", f, exc)
            title = ""
        sessions.append(SessionInfo(
            session_id=f.stem,
            title=title or "(제목 없음)",
            mtime=f.stat().st_mtime,
        ))
    return sessions


def format_age(mtime: float) -> str:
    """수정 시각을 '3시간 전' 형태의 상대 시간으로 포맷한다."""
    delta = max(0, time.time() - mtime)
    if delta < 60:
        return "방금 전"
    if delta < 3600:
        return f"{int(delta // 60)}분 전"
    if delta < 86400:
        return f"{int(delta // 3600)}시간 전"
    return f"{int(delta // 86400)}일 전"


def _extract_title(session_file: Path) -> str:
    """세션 jsonl에서 표시용 제목을 추출한다.

    우선순위: custom-title(--name) > summary > 첫 사용자 메시지.
    """
    custom_title = ""
    summary = ""
    first_user_text = ""

    with session_file.open(encoding="utf-8", errors="replace") as fp:
        for i, line in enumerate(fp):
            if i >= _TITLE_SCAN_LINES:
                break
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue

            etype = event.get("type")
            if etype == "custom-title" and event.get("customTitle"):
                custom_title = event["customTitle"]
                break
            if etype == "summary" and not summary and event.get("summary"):
                summary = event["summary"]
            if etype == "user" and not first_user_text and not event.get("isMeta"):
                first_user_text = _user_message_text(event)

    title = custom_title or summary or first_user_text
    title = " ".join(title.split())
    if len(title) > _TITLE_MAX_LENGTH:
        title = title[: _TITLE_MAX_LENGTH - 1] + "…"
    return title


def _user_message_text(event: dict) -> str:
    """user 이벤트에서 사람이 입력한 텍스트를 추출한다 (커맨드/캐비앳 제외)."""
    content = event.get("message", {}).get("content", "")
    if isinstance(content, list):
        content = " ".join(
            block.get("text", "") for block in content
            if isinstance(block, dict) and block.get("type") == "text"
        )
    if not isinstance(content, str):
        return ""
    text = content.strip()
    if text.startswith("<") or text.startswith("Caveat:"):
        return ""
    return text
