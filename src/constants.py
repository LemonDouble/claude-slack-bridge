"""constants.py — 공유 상수.

.env를 여기서 로드해 모든 모듈(데몬, tools_mcp)이 동일한 환경을 보게 한다.
"""

import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

PROJECTS_ROOT = Path(os.environ.get("PROJECTS_DIR", str(Path.home() / "claude-projects")))
CLAUDE_PROJECTS_DIR = Path.home() / ".claude" / "projects"
VALID_MODELS = ("sonnet", "opus", "haiku")
VALID_EFFORTS = ("low", "medium", "high", "xhigh", "max")
VALID_PERMISSION_MODES = ("auto", "acceptEdits", "bypassPermissions")
# 설정 종류(kind)별 전역 기본값 — ClaudeHandler의 설정 레지스트리 키와 일치
DEFAULT_SETTINGS = {"model": "sonnet", "effort": "high", "perm": "auto"}
# Slack 승인 요청에 응답이 없으면 자동 거부까지 기다리는 시간
APPROVAL_TIMEOUT_SECONDS = 600
STATE_FILE = Path.home() / ".claude" / "slack-bridge-state.json"
SLACK_MAX_MESSAGE_LENGTH = 40000
