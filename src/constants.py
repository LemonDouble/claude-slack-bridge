"""constants.py — 공유 상수.

.env를 여기서 로드해 모든 모듈(데몬, tools_mcp)이 동일한 환경을 보게 한다.
"""

import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

PROJECTS_ROOT = Path(os.environ.get("PROJECTS_DIR", str(Path.home() / "claude-projects")))
CLAUDE_PROJECTS_DIR = Path.home() / ".claude" / "projects"

# Claude CLI가 세션에 기록하는 entrypoint. 기본값(sdk-cli)은 터미널 /resume
# 목록에서 필터링되므로("filtered from /resume: entrypoint=sdk-cli"), 필터
# 대상이 아닌 값을 명시해 Slack 세션도 터미널에서 이어갈 수 있게 한다.
CLI_ENTRYPOINT = "claude-in-slack"

VALID_MODELS = (
    "opus", "opus[1m]", "sonnet", "sonnet[1m]", "haiku",
    "fable", "fable[1m]", "opusplan", "best",
)
VALID_EFFORTS = ("low", "medium", "high", "xhigh", "max")
VALID_PERMISSION_MODES = ("auto", "acceptEdits", "bypassPermissions")

# 설정 선택 UI(드롭다운)에 함께 보여줄 짧은 설명.
SETTING_DESCRIPTIONS: dict[str, dict[str, str]] = {
    "model": {
        "opus": "가장 강력함 — 복잡한 작업",
        "opus[1m]": "Opus + 1M 컨텍스트",
        "sonnet": "성능/속도 균형",
        "sonnet[1m]": "Sonnet + 1M 컨텍스트",
        "haiku": "가장 빠르고 저렴함",
        "fable": "최신 실험 모델",
        "fable[1m]": "Fable + 1M 컨텍스트",
        "opusplan": "계획은 Opus, 실행은 Sonnet",
        "best": "작업에 맞춰 자동 선택",
    },
    "effort": {
        "low": "최소한의 추론 — 가장 빠름",
        "medium": "보통 수준의 추론",
        "high": "충분한 추론 (기본값)",
        "xhigh": "깊은 추론 — 느림",
        "max": "최대 추론 — 가장 느림",
    },
    "perm": {
        "auto": "안전한 작업만 자동 승인 (기본값)",
        "acceptEdits": "파일 수정까지 자동 승인",
        "bypassPermissions": "전부 자동 승인 (위험)",
    },
}

# 설정 종류(kind)별 전역 기본값 — ClaudeHandler의 설정 레지스트리 키와 일치
DEFAULT_SETTINGS = {"model": "sonnet", "effort": "high", "perm": "auto"}
# Slack 승인 요청에 응답이 없으면 자동 거부까지 기다리는 시간
APPROVAL_TIMEOUT_SECONDS = 600
STATE_FILE = Path.home() / ".claude" / "slack-bridge-state.json"
SLACK_MAX_MESSAGE_LENGTH = 40000

# 리액션 트리거
CANCEL_REACTION = "x"
REWIND_REACTION = "rewind"
# 되돌리기용 턴 기록 보관 한도 (상태 파일이 무한정 커지지 않도록 제한)
MAX_TRACKED_TURNS = 30      # 스레드당 턴 수
MAX_TRACKED_THREADS = 30    # 기록을 보관할 스레드 수 (최근 것 우선)
MAX_TRACKED_FILES = 20      # 턴당 기록할 수정 파일 수
# rewind-files 서브프로세스 타임아웃(초)
REWIND_TIMEOUT_SECONDS = 120
