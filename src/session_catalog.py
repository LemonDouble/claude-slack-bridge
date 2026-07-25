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


@dataclass
class SessionRecap:
    """세션을 이어가기 전에 보여줄 직전 진행 상황."""
    turn_count: int
    prompts: list[str]       # 최근 사용자 요청 (오래된 순)
    last_response: str       # 마지막 assistant 텍스트


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


def _session_file(project_dir: str, session_id: str) -> Path:
    return CLAUDE_PROJECTS_DIR / encode_project_path(project_dir) / f"{session_id}.jsonl"


def _active_chain(
    nodes: dict[str, tuple[str, str | None, str]], leaves: set[str],
) -> list[str]:
    """트랜스크립트의 활성 분기를 root→leaf 순 uuid 목록으로 돌려준다.

    되돌리기(rewind)를 하면 한 세션 파일 안에 버려진 분기가 함께 남는다.
    CLI가 resume할 때 고르는 것과 같은 기준(타임스탬프가 가장 늦은 메시지)을
    leaf로 잡고 parentUuid를 거슬러 올라가, 실제로 이어질 대화만 추린다.

    *nodes*에는 uuid를 가진 모든 엔트리가 들어 있어야 한다. 부모 체인이
    attachment 같은 중간 엔트리를 거쳐 가므로, 이들을 빼면 추적이 끊긴다.
    *leaves*는 leaf 후보(대화 메시지)의 uuid 집합이다.
    """
    if not leaves:
        return []
    leaf = max(leaves, key=lambda u: nodes[u][2])
    chain: list[str] = []
    seen: set[str] = set()
    cursor: str | None = leaf
    while cursor and cursor in nodes and cursor not in seen:
        seen.add(cursor)
        chain.append(cursor)
        cursor = nodes[cursor][1]
    chain.reverse()
    return chain


def build_recap(
    project_dir: str, session_id: str,
    prompt_limit: int = 3, prompt_chars: int = 100, response_chars: int = 300,
) -> SessionRecap | None:
    """세션의 최근 사용자 요청과 마지막 응답을 발췌한다 (없으면 None).

    이어가기를 누른 시점에 "어디까지 진행했는지" 보여주기 위한 것으로,
    모델을 호출하지 않고 트랜스크립트에서 그대로 뽑는다.
    """
    session_file = _session_file(project_dir, session_id)
    if not session_file.is_file():
        return None

    nodes: dict[str, tuple[str, str | None, str]] = {}  # uuid → (type, parent, timestamp)
    messages: set[str] = set()   # leaf 후보 (user/assistant 메시지)
    texts: dict[str, str] = {}
    with session_file.open(encoding="utf-8", errors="replace") as fp:
        for line in fp:
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            uuid, etype = event.get("uuid"), event.get("type")
            if not uuid:
                continue
            # 부모 체인이 attachment 등을 거쳐 가므로 모든 엔트리를 인덱싱한다.
            nodes[uuid] = (etype or "", event.get("parentUuid"), event.get("timestamp", ""))
            if etype not in ("user", "assistant") or event.get("isSidechain"):
                continue
            messages.add(uuid)
            # 사람이 입력한 프롬프트와 assistant의 텍스트만 본문을 보관한다.
            if etype == "user":
                if not event.get("promptSource"):
                    continue
                text = _clean_prompt(_message_text(event))
            else:
                text = _message_text(event)
            if text:
                texts[uuid] = text[: response_chars * 2]

    prompts: list[str] = []
    last_response = ""
    for uuid in _active_chain(nodes, messages):
        text = texts.get(uuid)
        if not text:
            continue
        if nodes[uuid][0] == "user":
            prompts.append(text)
        else:
            last_response = text

    if not prompts and not last_response:
        return None
    return SessionRecap(
        turn_count=len(prompts),
        prompts=[_shorten(p, prompt_chars) for p in prompts[-prompt_limit:]],
        last_response=_shorten(last_response, response_chars),
    )


def _message_text(event: dict) -> str:
    """user/assistant 이벤트에서 텍스트 블록만 이어붙인다."""
    content = event.get("message", {}).get("content", "")
    if isinstance(content, str):
        return " ".join(content.split())
    if not isinstance(content, list):
        return ""
    parts = [
        block.get("text", "") for block in content
        if isinstance(block, dict) and block.get("type") == "text"
    ]
    return " ".join(" ".join(parts).split())


def _clean_prompt(text: str) -> str:
    """사용자 프롬프트에서 표시할 부분만 남긴다 (없으면 빈 문자열).

    브릿지는 세션이 없을 때 스레드 전체를 프롬프트로 넘기므로, 그 경우엔
    마지막 ``[Human]:`` 발화만 뽑는다. 시스템/커맨드 래퍼는 건너뛴다.
    """
    if text.startswith("The following is a Slack conversation"):
        _, sep, tail = text.rpartition("[Human]:")
        text = tail.strip() if sep else ""
    if text.startswith("<") or text.startswith("Caveat:"):
        return ""
    return text


def _shorten(text: str, limit: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def find_resume_point(project_dir: str, session_id: str, user_uuid: str) -> str | None:
    """*user_uuid* 사용자 메시지 직전의 assistant 메시지 uuid를 찾는다.

    ``--resume-session-at``은 "지정한 assistant 메시지까지만 유지"하므로, 어떤
    턴을 통째로 취소하려면 그 턴의 사용자 메시지 바로 앞 assistant 메시지를
    기준점으로 넘겨야 한다. 트랜스크립트의 parentUuid 체인을 거슬러 올라가
    처음 만나는 assistant 메시지를 반환한다.

    브릿지가 추적하기 전에 시작된 세션(터미널 세션 이어가기 등)의 턴에 대해
    기준점을 복원하기 위한 폴백이다. 첫 턴이라 기준점이 없으면 None.
    """
    session_file = _session_file(project_dir, session_id)
    if not session_file.is_file():
        return None

    # uuid → (type, parentUuid) 인덱스만 만든다 (전체 메시지를 들고 있지 않기 위함).
    parents: dict[str, tuple[str, str | None]] = {}
    with session_file.open(encoding="utf-8", errors="replace") as fp:
        for line in fp:
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            uuid = event.get("uuid")
            if uuid and event.get("type") in ("user", "assistant"):
                parents[uuid] = (event["type"], event.get("parentUuid"))

    node = parents.get(user_uuid)
    seen: set[str] = {user_uuid}
    while node:
        _etype, parent_uuid = node
        if not parent_uuid or parent_uuid in seen:
            return None
        seen.add(parent_uuid)
        parent = parents.get(parent_uuid)
        if parent is None:
            return None
        if parent[0] == "assistant":
            return parent_uuid
        node = parent
    return None


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
                first_user_text = _clean_prompt(_message_text(event))

    return _shorten(custom_title or summary or first_user_text, _TITLE_MAX_LENGTH)
