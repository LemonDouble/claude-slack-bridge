"""브릿지가 깨지기 쉬운 지점만 확인하는 최소 테스트.

    uv run pytest

확인 대상:
  - 트랜스크립트 파싱 — 부모 체인 추적과 되돌리기 분기 처리
  - 턴 추적 — Slack 메시지 ts → 되돌릴 턴 매핑
  - CLI 숨은 플래그 — Claude CLI 업데이트로 사라졌는지 (API 호출 없음)
  - 완료 알림 — 최종 응답이 새 메시지 + 멘션으로 나가는지
"""

import asyncio
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

# config가 필수 토큰을 검증하므로 .env 없이도 임포트되게 더미를 넣는다.
os.environ.setdefault("SLACK_BOT_TOKEN", "xoxb-test")
os.environ.setdefault("SLACK_APP_TOKEN", "xapp-test")

import claude_handler  # noqa: E402
import session_catalog  # noqa: E402
from claude_handler import ClaudeHandler  # noqa: E402
from slack_daemon import SlackDaemon  # noqa: E402

PROJECT = "/tmp/demo-project"
SESSION = "11111111-2222-3333-4444-555555555555"


@pytest.fixture
def transcript(tmp_path, monkeypatch):
    """되돌리기가 한 번 일어난 세션을 흉내 낸 트랜스크립트.

    u1 → a1 → s1 → u2 → a2   (되돌리기로 버려진 분기)
          └──→ u3 → a3        (실제로 이어지는 분기)

    u2의 부모가 assistant가 아니라 system인 것이 핵심이다. 실제 트랜스크립트에서
    가장 흔한 형태이고, 예전에 여기서 부모 추적이 끊겼다.
    """
    monkeypatch.setattr(session_catalog, "CLAUDE_PROJECTS_DIR", tmp_path / "projects")

    def msg(uuid, typ, parent, text, ts, prompt=False):
        e = {
            "uuid": uuid, "type": typ, "parentUuid": parent,
            "timestamp": f"2026-01-01T00:0{ts}:00.000Z",
            "message": {"role": typ, "content": [{"type": "text", "text": text}]},
        }
        if prompt:
            e["promptSource"] = "typed"
        return e

    rows = [
        msg("u1", "user", None, "첫 요청", 1, prompt=True),
        msg("a1", "assistant", "u1", "첫 응답", 2),
        {"uuid": "s1", "type": "system", "parentUuid": "a1",
         "timestamp": "2026-01-01T00:03:00.000Z"},
        msg("u2", "user", "s1", "버려질 요청", 4, prompt=True),
        msg("a2", "assistant", "u2", "버려질 응답", 5),
        msg("u3", "user", "a1", "되돌린 뒤 요청", 6, prompt=True),
        msg("a3", "assistant", "u3", "최종 응답", 7),
    ]
    d = session_catalog.CLAUDE_PROJECTS_DIR / session_catalog.encode_project_path(PROJECT)
    d.mkdir(parents=True)
    (d / f"{SESSION}.jsonl").write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in rows), encoding="utf-8",
    )


@pytest.fixture
def handler(tmp_path, monkeypatch):
    """실제 상태 파일을 건드리지 않는 ClaudeHandler."""
    monkeypatch.setattr(claude_handler, "STATE_FILE", tmp_path / "state.json")
    return ClaudeHandler(object(), idle_timeout_minutes=1)


# ----------------------------------------------------------------------
# 트랜스크립트 파싱
# ----------------------------------------------------------------------

@pytest.mark.parametrize(("user_uuid", "expected"), [
    ("u2", "a1"),   # 부모가 system — 건너뛰고 그 위 assistant를 찾아야 한다
    ("u3", "a1"),   # 부모가 바로 assistant
    ("u1", None),   # 세션 첫 턴이라 기준점 없음
])
def test_find_resume_point(transcript, user_uuid, expected):
    assert session_catalog.find_resume_point(PROJECT, SESSION, user_uuid) == expected


def test_recap_follows_active_branch(transcript):
    recap = session_catalog.build_recap(PROJECT, SESSION)
    assert recap.prompts == ["첫 요청", "되돌린 뒤 요청"]  # "버려질 요청"은 빠진다
    assert recap.last_response == "최종 응답"


def test_recap_missing_session(transcript):
    assert session_catalog.build_recap(PROJECT, "no-such-session") is None


# ----------------------------------------------------------------------
# 턴 추적 → 되돌리기 대상 매핑
# ----------------------------------------------------------------------

@pytest.fixture
def two_turns(handler):
    handler._begin_turn("T", "100.0", "첫 요청")
    handler._turns["T"][-1]["last_assistant_uuid"] = "a1"
    handler._begin_turn("T", "200.0", "둘째 요청")
    return handler


def test_turn_chains_to_previous_response(two_turns):
    assert two_turns._turns["T"][1]["prev_uuid"] == "a1"


@pytest.mark.parametrize(("message_ts", "expected"), [
    ("250.0", 200.0),   # 봇이 턴 도중 올린 메시지도 그 턴에 귀속
    ("200.0", 200.0),
    ("150.0", 100.0),
    ("50.0", None),     # 첫 턴보다 이전
    ("쓰레기", None),    # 타임스탬프가 아님
])
def test_find_turn(two_turns, message_ts, expected):
    turn = two_turns.find_turn("T", message_ts)
    assert (turn["slack_ts"] if turn else None) == expected


def test_rewind_truncates_and_schedules(two_turns):
    two_turns.rewind_conversation("T", two_turns.find_turn("T", "250.0"), "a1")
    assert len(two_turns._turns["T"]) == 1
    assert two_turns._pending_resume_at["T"] == "a1"


# ----------------------------------------------------------------------
# CLI 숨은 플래그 계약
# ----------------------------------------------------------------------

# --help에 나오지 않지만 브릿지가 의존하는 플래그들. 사라지면 조용히 깨진다.
HIDDEN_FLAGS = [
    ["--resume-session-at", "00000000-0000-0000-0000-000000000000"],
    ["--rewind-files", "00000000-0000-0000-0000-000000000000"],
    ["--replay-user-messages"],
    ["--permission-prompt-tool", "stdio"],
]


@pytest.mark.skipif(shutil.which("claude") is None, reason="claude CLI 없음")
@pytest.mark.parametrize("flag", HIDDEN_FLAGS, ids=[f[0] for f in HIDDEN_FLAGS])
def test_hidden_cli_flag_still_exists(flag):
    # 세션 ID를 일부러 틀리게 줘서 인자 파싱만 통과시킨다 (모델 호출 없음).
    result = subprocess.run(
        ["claude", "-p", "--resume", "00000000-0000-0000-0000-000000000000", *flag],
        capture_output=True, text=True, timeout=90,
    )
    assert "unknown option" not in result.stdout + result.stderr


# ----------------------------------------------------------------------
# 완료 알림 (chat.update는 알림을 보내지 않는다)
# ----------------------------------------------------------------------

class FakeClient:
    def __init__(self):
        self.calls: list[tuple[str, dict]] = []

    async def chat_postMessage(self, **kw):
        self.calls.append(("POST", kw))
        return {"ts": "999.0"}

    async def chat_update(self, **kw):
        self.calls.append(("UPDATE", kw))
        return {"ts": kw["ts"]}

    async def chat_delete(self, **kw):
        self.calls.append(("DELETE", kw))
        return {}


def test_response_is_a_new_message_with_mention():
    daemon = SlackDaemon.__new__(SlackDaemon)
    daemon._app = type("App", (), {"client": FakeClient()})()
    daemon._thread_users = {"T": "U_ME"}

    asyncio.run(daemon._post_response("C", "T", "다 했습니다.", progress_ts="1.0"))

    kinds = [kind for kind, _ in daemon._app.client.calls]
    assert kinds == ["DELETE", "POST"]          # 진행 메시지를 지우고 새로 게시
    assert "UPDATE" not in kinds                # chat.update는 알림을 안 보낸다
    assert daemon._app.client.calls[-1][1]["text"].startswith("<@U_ME> ")
