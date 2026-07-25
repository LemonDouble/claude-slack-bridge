"""slack_blocks.py — Block Kit 구성 요소.

Slack API를 호출하지 않고 화면(blocks) 구조만 만드는 순수 함수 모음이다.
데몬은 데이터를 모아 여기에 넘기고, 결과를 그대로 게시한다.
"""

import json
from typing import Any

from constants import SETTING_DESCRIPTIONS
from session_catalog import SessionInfo, format_age


def section(text: str, accessory: dict | None = None) -> dict:
    """mrkdwn 텍스트 섹션 (선택적으로 우측 액세서리)."""
    block: dict = {"type": "section", "text": {"type": "mrkdwn", "text": text}}
    if accessory:
        block["accessory"] = accessory
    return block


def actions(*elements: dict) -> dict:
    """버튼 등을 담는 액션 행."""
    return {"type": "actions", "elements": list(elements)}


def button(text: str, action_id: str, value: str, style: str | None = None) -> dict:
    """버튼 요소."""
    element: dict = {
        "type": "button",
        "text": {"type": "plain_text", "text": text, "emoji": True},
        "action_id": action_id,
        "value": value,
    }
    if style:
        element["style"] = style
    return element


# Slack 버튼 value는 빈 문자열을 허용하지 않으므로 루트("")를 "."로 인코딩한다.
def encode_rel(rel: str) -> str:
    return rel or "."


def decode_rel(value: str) -> str:
    return "" if value == "." else value


def browser_blocks(display: str, rel: str, subdirs: list[str], truncated: bool) -> list[dict]:
    """폴더 브라우저 화면: 하위 폴더 버튼 + 탐색/시작/새 폴더 컨트롤."""
    header = f":open_file_folder: *{display}*"
    if truncated:
        header += f"  _(하위 폴더 {len(subdirs)}개까지만 표시)_"
    blocks = [section(header)]

    for i in range(0, len(subdirs), 5):
        blocks.append(actions(*(
            button(name, f"nav:{i + j}", f"{rel}/{name}" if rel else name)
            for j, name in enumerate(subdirs[i : i + 5])
        )))

    controls = []
    if rel:
        controls.append(button("⬆ 상위로", "nav_up", rel))
    controls.append(button("▶ 여기서 시작", "start_here", encode_rel(rel), style="primary"))
    controls.append(button("➕ 새 폴더", "new_folder", encode_rel(rel)))
    blocks.append(actions(*controls))
    return blocks


def session_blocks(display: str, rel: str, sessions: list[SessionInfo]) -> list[dict]:
    """세션 선택 화면: 새 세션 + 해당 폴더의 최근 Claude CLI 세션 목록."""
    blocks = [
        section(f":open_file_folder: *{display}* — 세션을 선택하세요:"),
        actions(
            button("🆕 새 세션", "new_session", encode_rel(rel), style="primary"),
            button("⬅ 폴더 목록", "back_to_browser", encode_rel(rel)),
        ),
    ]
    for i, s in enumerate(sessions):
        blocks.append(section(
            f"*{s.title}*\n`{s.session_id[:8]}` · {format_age(s.mtime)}",
            accessory=button(
                "이어가기", f"pick_session:{i}",
                json.dumps({"rel": rel, "sid": s.session_id, "title": s.title}),
            ),
        ))
    return blocks


def setting_select(kind: str, name: str, valid: tuple[str, ...], current: str, label: str) -> dict:
    """설정 값을 고를 수 있는 드롭다운 블록."""
    descriptions = SETTING_DESCRIPTIONS.get(kind, {})

    def option(value: str) -> dict:
        opt: dict = {
            "text": {"type": "plain_text", "text": value, "emoji": False},
            "value": value,
        }
        if descriptions.get(value):
            opt["description"] = {
                "type": "plain_text", "text": descriptions[value], "emoji": False,
            }
        return opt

    element: dict = {
        "type": "static_select",
        "action_id": f"set:{kind}",
        "placeholder": {"type": "plain_text", "text": f"{name} 선택"},
        "options": [option(v) for v in valid],
    }
    # initial_option은 options의 항목과 정확히 일치해야 한다.
    if current in valid:
        element["initial_option"] = option(current)
    return section(label, accessory=element)


def rewind_blocks(text: str, value: str, can_conv: bool, has_files: bool) -> list[dict]:
    """되돌리기 범위 선택 버튼. 가능한 범위만 버튼으로 노출한다."""
    elements = []
    if can_conv:
        elements.append(button("💬 대화만", "rewind:conv", value))
    if has_files:
        elements.append(button("📁 코드만", "rewind:files", value))
    if can_conv and has_files:
        elements.append(button("🔄 둘 다", "rewind:both", value, style="primary"))
    elements.append(button("취소", "rewind:cancel", value))
    return [section(text), actions(*elements)]


def new_folder_view(display: str, metadata: str) -> dict[str, Any]:
    """'새 폴더' 경로 입력 모달."""
    return {
        "type": "modal",
        "callback_id": "new_folder_modal",
        "private_metadata": metadata,
        "title": {"type": "plain_text", "text": "새 폴더"},
        "submit": {"type": "plain_text", "text": "생성 후 시작"},
        "close": {"type": "plain_text", "text": "취소"},
        "blocks": [
            section(f"현재 위치: `{display}/`"),
            {
                "type": "input",
                "block_id": "folder_path_block",
                "element": {
                    "type": "plain_text_input",
                    "action_id": "folder_path_input",
                    "placeholder": {
                        "type": "plain_text",
                        "text": "예: my-project 또는 tools/my-project",
                    },
                },
                "label": {"type": "plain_text", "text": "폴더 경로 (하위 경로 가능)"},
            },
        ],
    }
