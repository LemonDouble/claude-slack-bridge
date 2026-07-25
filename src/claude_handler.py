"""
claude_handler.py — Spawns Claude Code CLI subprocesses for Human→Claude tasks.

When a human posts a message in Slack, this handler runs ``claude -p`` to
generate a response.  Thread continuations use ``--resume`` so Claude retains
full context (tool use, reasoning) across messages in the same thread.

If the session ID is lost (e.g. process restart), falls back to a one-shot
``claude -p`` with the formatted thread history as the prompt.

The project (working directory) for each thread is selected via the Slack
folder-browser UI and tracked per thread_ts.  Any folder under PROJECTS_DIR
— at any depth — can be a project.
"""

import asyncio
import json
import logging
import os
import signal
from collections import deque
from collections.abc import Callable, Coroutine
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from constants import (
    CLI_ENTRYPOINT,
    DEFAULT_SETTINGS,
    MAX_TRACKED_FILES,
    MAX_TRACKED_THREADS,
    MAX_TRACKED_TURNS,
    PROJECTS_ROOT,
    REWIND_TIMEOUT_SECONDS,
    STATE_FILE,
)
from file_downloader import format_file_metadata
from session_catalog import find_resume_point

# 파일 되돌리기 대상으로 표시할 도구 → 경로가 담긴 입력 키
_EDIT_TOOLS = {"Edit": "file_path", "Write": "file_path", "NotebookEdit": "notebook_path"}


def _to_ts(value: str) -> float:
    """Slack 타임스탬프 문자열 → float (형식이 아니면 0.0)."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


OnEventFn = Callable[[dict[str, Any]], Coroutine[Any, Any, None]]
# 권한 요청(can_use_tool request dict)을 받아 결정을 반환하는 콜백.
# 반환값: {"behavior": "allow", "updatedInput": {...}} 또는 {"behavior": "deny", "message": "..."}
OnPermissionFn = Callable[[dict[str, Any]], Coroutine[Any, Any, dict[str, Any]]]

logger = logging.getLogger(__name__)


@dataclass
class ClaudeResult:
    """Result from a Claude CLI invocation."""
    text: str
    total_cost_usd: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0
    duration_ms: int = 0
    model_usage: dict[str, Any] = field(default_factory=dict)
    requested_model: str = ""


class ClaudeHandler:
    """
    Manages Claude Code CLI invocations for Slack messages.

    Args:
        slack_client: An async Slack WebClient (``self._app.client``).
    """

    def __init__(self, slack_client: Any, idle_timeout_minutes: int) -> None:
        self._slack_client = slack_client
        self._bot_user_id: str = ""
        self._idle_timeout = idle_timeout_minutes * 60
        self._sessions: dict[str, str] = {}  # thread_ts → session UUID
        self._thread_projects: dict[str, str] = {}  # thread_ts → project dir
        self._active_processes: dict[str, asyncio.subprocess.Process] = {}
        self._defaults: dict[str, str] = dict(DEFAULT_SETTINGS)  # kind → 전역 기본값
        self._thread_settings: dict[str, dict[str, str]] = {  # kind → {thread_ts → 값}
            kind: {} for kind in DEFAULT_SETTINGS
        }
        # 되돌리기(rewind)용: thread_ts → 턴 기록 목록 (오래된 순).
        self._turns: dict[str, list[dict[str, Any]]] = {}
        # thread_ts → 다음 실행에 적용할 --resume-session-at 기준점.
        self._pending_resume_at: dict[str, str] = {}
        self._load_state()

    async def initialize(self) -> str:
        """Cache the bot's own user ID and return it."""
        resp = await self._slack_client.auth_test()
        self._bot_user_id = resp["user_id"]
        logger.info("ClaudeHandler initialized, bot_user_id=%s", self._bot_user_id)
        return self._bot_user_id

    # ------------------------------------------------------------------
    # State persistence
    # ------------------------------------------------------------------

    def _load_state(self) -> None:
        """Load persisted thread→project and thread→session mappings."""
        if not STATE_FILE.exists():
            return
        try:
            data = json.loads(STATE_FILE.read_text())
            self._thread_projects = data.get("thread_projects", {})
            self._sessions = data.get("sessions", {})
            self._turns = data.get("turns", {})
            self._pending_resume_at = data.get("pending_resume_at", {})
            for kind in DEFAULT_SETTINGS:
                self._defaults[kind] = data.get("defaults", {}).get(kind) or DEFAULT_SETTINGS[kind]
                self._thread_settings[kind] = data.get("thread_settings", {}).get(kind) or {}
            logger.info(
                "Restored state: %d threads, %d sessions, defaults=%s.",
                len(self._thread_projects), len(self._sessions), self._defaults,
            )
        except Exception as exc:
            logger.warning("Failed to load state: %s", exc)

    def _save_state(self) -> None:
        """Persist current mappings to disk."""
        try:
            STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
            STATE_FILE.write_text(json.dumps({
                "thread_projects": self._thread_projects,
                "sessions": self._sessions,
                "defaults": self._defaults,
                "thread_settings": self._thread_settings,
                "turns": self._turns,
                "pending_resume_at": self._pending_resume_at,
            }))
        except Exception as exc:
            logger.warning("Failed to save state: %s", exc)

    # ------------------------------------------------------------------
    # Project (working directory) mapping
    # ------------------------------------------------------------------

    def set_thread_project(self, thread_ts: str, rel_path: str) -> str:
        """Associate a thread with a folder under PROJECTS_ROOT (any depth).

        Returns the full project path.
        """
        project_dir = str(PROJECTS_ROOT / rel_path) if rel_path else str(PROJECTS_ROOT)
        self._thread_projects[thread_ts] = project_dir
        self._save_state()
        return project_dir

    def get_thread_project(self, thread_ts: str) -> str | None:
        """Get the project directory for a thread."""
        return self._thread_projects.get(thread_ts)

    def set_thread_session(self, thread_ts: str, session_id: str) -> None:
        """스레드에 기존 Claude CLI 세션을 연결한다 (다음 메시지부터 --resume)."""
        self._sessions[thread_ts] = session_id
        self._save_state()

    def get_thread_session(self, thread_ts: str) -> str | None:
        """스레드에 연결된 세션 ID를 반환한다."""
        return self._sessions.get(thread_ts)

    @staticmethod
    def create_project(name: str) -> str:
        """Create a new project directory. Returns the full path."""
        project_dir = PROJECTS_ROOT / name
        project_dir.mkdir(parents=True, exist_ok=True)
        logger.info("Created project directory: %s", project_dir)
        return str(project_dir)

    # ------------------------------------------------------------------
    # Settings (kind: "model" | "effort" | "perm")
    # ------------------------------------------------------------------

    def get_setting(self, thread_ts: str, kind: str) -> str:
        """스레드 설정 값을 반환한다 (없으면 전역 기본값)."""
        return self._thread_settings[kind].get(thread_ts, self._defaults[kind])

    def set_setting(self, thread_ts: str, kind: str, value: str) -> None:
        """스레드 설정 값을 저장한다 (영속)."""
        self._thread_settings[kind][thread_ts] = value
        self._save_state()

    def get_default(self, kind: str) -> str:
        """전역 기본값을 반환한다."""
        return self._defaults[kind]

    def set_default(self, kind: str, value: str) -> None:
        """전역 기본값을 저장한다 (영속)."""
        self._defaults[kind] = value
        self._save_state()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def handle_thread_reply(
        self, channel: str, thread_ts: str, text: str,
        on_event: OnEventFn | None = None,
        on_permission: OnPermissionFn | None = None,
        message_ts: str = "",
    ) -> ClaudeResult:
        """Handle a threaded reply (resume existing session or fallback)."""
        session_id = self._sessions.get(thread_ts)
        project_dir = self._thread_projects.get(thread_ts)
        model = self.get_setting(thread_ts, "model")
        effort = self.get_setting(thread_ts, "effort")
        perm = self.get_setting(thread_ts, "perm")
        if not session_id:
            # 새 세션이 시작되면 이전 세션의 메시지 uuid는 모두 무효하다.
            self._turns.pop(thread_ts, None)
            self._pending_resume_at.pop(thread_ts, None)
        turn = self._begin_turn(thread_ts, message_ts, text)

        if session_id:
            resume_at = self._pending_resume_at.get(thread_ts)
            logger.info(
                "Resuming session %s for thread %s (resume_at=%s)",
                session_id, thread_ts, resume_at or "-",
            )
            cmd = self._build_cmd(
                resume=session_id, model=model, effort=effort, permission_mode=perm,
                resume_at=resume_at,
            )
            result = await self._run_claude(
                cmd, text, cwd=project_dir, on_event=on_event,
                slack_channel=channel, slack_thread_ts=thread_ts,
                thread_ts=thread_ts, on_permission=on_permission, turn=turn,
            )
            # 실행이 끝난 뒤에 소비한다 — 도중에 실패하면 예약을 남겨 다시 시도한다.
            self._pending_resume_at.pop(thread_ts, None)
            result.requested_model = model
            self._save_state()
            return result

        # Fallback: session lost (process restart) — use thread history as context.
        logger.info("No session for thread %s, falling back to thread history.", thread_ts)
        prompt = await self._build_thread_prompt(channel, thread_ts)
        cmd = self._build_cmd(
            model=model, effort=effort, name=self._session_name(text),
            permission_mode=perm,
        )
        result = await self._run_claude(
            cmd, prompt, cwd=project_dir, on_event=on_event,
            slack_channel=channel, slack_thread_ts=thread_ts,
            thread_ts=thread_ts, on_permission=on_permission, turn=turn,
        )
        result.requested_model = model
        self._save_state()
        return result

    # ------------------------------------------------------------------
    # Turn tracking + rewind
    # ------------------------------------------------------------------

    def _begin_turn(self, thread_ts: str, message_ts: str, text: str) -> dict[str, Any]:
        """새 턴 기록을 만들어 스레드 기록에 추가하고 반환한다.

        기록은 ``_run_claude``가 스트림을 읽으면서 채운다(메시지 uuid, 수정 파일).
        되돌리기 시 Slack 메시지 ts로 어느 턴인지 역추적하는 데 쓰인다.
        """
        turns = self._turns.setdefault(thread_ts, [])
        turn: dict[str, Any] = {
            "slack_ts": _to_ts(message_ts or thread_ts),
            "prev_uuid": turns[-1].get("last_assistant_uuid") if turns else None,
            "user_uuid": None,
            "last_assistant_uuid": None,
            "text": " ".join(text.split())[:120],
            "files": [],
        }
        turns.append(turn)
        del turns[:-MAX_TRACKED_TURNS]
        self._prune_turns(thread_ts)
        return turn

    def _prune_turns(self, keep: str) -> None:
        """오래된 스레드의 턴 기록을 버린다 (상태 파일 크기 제한).

        thread_ts는 고정폭 Slack 타임스탬프 문자열이라 사전순 = 시간순이다.
        """
        for thread_ts in sorted(self._turns)[:-MAX_TRACKED_THREADS]:
            if thread_ts != keep:
                del self._turns[thread_ts]

    def find_turn(self, thread_ts: str, message_ts: str) -> dict[str, Any] | None:
        """Slack 메시지 ts가 속한 턴을 찾는다.

        어떤 메시지든(사용자 요청, 봇의 진행/응답 메시지) 그 턴이 시작된 이후에
        게시되므로, ``slack_ts <= message_ts``인 마지막 턴이 해당 턴이다.
        """
        target = _to_ts(message_ts)
        match = None
        for turn in self._turns.get(thread_ts, []):
            if turn["slack_ts"] <= target:
                match = turn
            else:
                break
        return match

    def resolve_resume_point(self, thread_ts: str, turn: dict[str, Any]) -> str | None:
        """턴의 대화 되돌리기 기준점을 반환한다 (없으면 트랜스크립트에서 복원)."""
        if turn.get("prev_uuid"):
            return turn["prev_uuid"]
        session_id = self._sessions.get(thread_ts)
        project_dir = self._thread_projects.get(thread_ts)
        user_uuid = turn.get("user_uuid")
        if not (session_id and project_dir and user_uuid):
            return None
        found = find_resume_point(project_dir, session_id, user_uuid)
        if found:
            turn["prev_uuid"] = found
            self._save_state()
        return found

    def rewind_conversation(self, thread_ts: str, turn: dict[str, Any], resume_at: str) -> None:
        """대화를 *turn* 직전으로 되돌린다.

        ``--resume-session-at``은 프롬프트와 함께여야 동작하므로 기준점만 예약해
        두고, 다음 메시지를 보낼 때 적용한다. 취소된 턴 기록도 함께 버린다.
        """
        self._pending_resume_at[thread_ts] = resume_at
        turns = self._turns.get(thread_ts, [])
        if turn in turns:
            del turns[turns.index(turn):]
        self._save_state()

    async def rewind_files(self, thread_ts: str, turn: dict[str, Any]) -> tuple[bool, str]:
        """*turn* 시작 시점의 파일 상태로 되돌린다. (성공 여부, 메시지)를 반환."""
        session_id = self._sessions.get(thread_ts)
        project_dir = self._thread_projects.get(thread_ts)
        user_uuid = turn.get("user_uuid")
        if not session_id:
            return False, "이 스레드에 연결된 세션이 없습니다."
        if not user_uuid:
            return False, "이 턴의 메시지 ID를 추적하지 못해 파일을 되돌릴 수 없습니다."

        # --rewind-files는 프롬프트와 함께 쓸 수 없는 단독 동작이다.
        cmd = ["claude", "-p", "--resume", session_id, "--rewind-files", user_uuid]
        env = os.environ.copy()
        env.pop("CLAUDECODE", None)
        env["CLAUDE_CODE_ENTRYPOINT"] = CLI_ENTRYPOINT
        env["CLAUDE_CODE_ENABLE_SDK_FILE_CHECKPOINTING"] = "1"
        try:
            process = await asyncio.create_subprocess_exec(
                *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                env=env, cwd=project_dir,
            )
            stdout, stderr = await asyncio.wait_for(
                process.communicate(), timeout=REWIND_TIMEOUT_SECONDS
            )
        except TimeoutError:
            return False, "파일 되돌리기가 시간 내에 끝나지 않았습니다."
        except FileNotFoundError:
            return False, "Claude CLI를 찾을 수 없습니다."

        out = (stdout or b"").decode("utf-8", errors="replace").strip()
        err = (stderr or b"").decode("utf-8", errors="replace").strip()
        if process.returncode != 0:
            logger.warning("rewind-files failed (rc=%s): %s | %s", process.returncode, err, out)
            return False, (err or out or "알 수 없는 오류")[:300]
        logger.info("Rewound files for thread %s to %s: %s", thread_ts, user_uuid, out)
        return True, out or "파일을 되돌렸습니다."

    @staticmethod
    def _session_name(text: str) -> str:
        """새 세션의 표시 이름 — 터미널 /resume 목록에서 Slack 세션을 알아보기 위함."""
        snippet = " ".join(text.split())[:40]
        return f"Slack: {snippet}" if snippet else "Slack thread"

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    @staticmethod
    def _build_cmd(
        model: str,
        effort: str,
        permission_mode: str,
        resume: str | None = None,
        name: str | None = None,
        resume_at: str | None = None,
    ) -> list[str]:
        project_root = str(Path(__file__).resolve().parent.parent)
        tools_mcp_path = str(Path(__file__).resolve().parent / "tools_mcp.py")
        mcp_config = json.dumps({
            "mcpServers": {
                "slack-tools": {
                    "command": "uv",
                    "args": ["run", "--project", project_root, "python", tools_mcp_path],
                }
            }
        })
        cmd = [
            "claude", "-p",
            "--verbose",
            "--input-format", "stream-json",
            "--output-format", "stream-json",
            # 사용자 메시지를 uuid와 함께 되돌려받아 되돌리기 기준점으로 쓴다.
            "--replay-user-messages",
            "--model", model,
            "--effort", effort,
            "--mcp-config", mcp_config,
        ]
        if permission_mode == "bypassPermissions":
            cmd.append("--dangerously-skip-permissions")
        else:
            # 승인이 필요한 작업은 stdio control 프로토콜(can_use_tool)로 전달받아
            # Slack 승인 플로우로 처리한다.
            cmd.extend([
                "--permission-mode", permission_mode,
                "--permission-prompt-tool", "stdio",
            ])
        if resume:
            cmd.extend(["--resume", resume])
            # 지정한 assistant 메시지 이후의 대화를 잘라내고 이어간다 (rewind).
            if resume_at:
                cmd.extend(["--resume-session-at", resume_at])
        if name:
            cmd.extend(["--name", name])
        return cmd

    async def cancel_thread(self, thread_ts: str) -> bool:
        """Cancel the active Claude process for a thread (SIGINT + fallback SIGKILL).

        Sends SIGINT and returns immediately. The running _run_claude will
        detect EOF and clean up. A background task sends SIGKILL after 10s
        if the process hasn't exited yet.
        """
        process = self._active_processes.get(thread_ts)
        if not process or process.returncode is not None:
            return False
        logger.info("Cancelling Claude process for thread %s (pid=%d)", thread_ts, process.pid)
        process.send_signal(signal.SIGINT)

        async def _ensure_killed() -> None:
            await asyncio.sleep(10)
            if process.returncode is None:
                logger.warning("Process %d did not exit after SIGINT, sending SIGKILL", process.pid)
                process.kill()

        asyncio.create_task(_ensure_killed())
        return True

    def clear_session(self, thread_ts: str) -> None:
        """Remove the stored session ID so the next run starts fresh."""
        # 새 세션에서는 기존 턴 기록과 예약된 되돌리기가 모두 무의미하다.
        removed = self._sessions.pop(thread_ts, None)
        self._turns.pop(thread_ts, None)
        self._pending_resume_at.pop(thread_ts, None)
        if removed:
            self._save_state()

    async def _run_claude(
        self, cmd: list[str], prompt: str, cwd: str | None = None,
        on_event: OnEventFn | None = None,
        slack_channel: str = "", slack_thread_ts: str = "",
        thread_ts: str = "",
        on_permission: OnPermissionFn | None = None,
        turn: dict[str, Any] | None = None,
    ) -> ClaudeResult:
        """Spawn a ``claude -p`` subprocess and return the response text.

        Uses ``--output-format stream-json`` and reads stdout line-by-line so
        that long-running tasks (hours) are never killed as long as Claude is
        still producing output.  Only an *idle* timeout (no new output for
        the configured seconds) will terminate the process.

        stdin is kept open (``--input-format stream-json``) so that
        ``can_use_tool`` permission requests from the CLI can be answered
        mid-run via *on_permission* — this powers the Slack approval flow.

        If *on_event* is provided, each parsed JSON event is forwarded to it
        so callers can post real-time progress to Slack.
        """
        env = os.environ.copy()
        env.pop("CLAUDECODE", None)
        # 터미널 /resume 목록에서 필터링되지 않도록 entrypoint를 명시하고,
        # print 모드에서 기본 비활성인 파일 체크포인트를 켠다 (코드 되돌리기용).
        env["CLAUDE_CODE_ENTRYPOINT"] = CLI_ENTRYPOINT
        env["CLAUDE_CODE_ENABLE_SDK_FILE_CHECKPOINTING"] = "1"
        if slack_channel:
            env["SLACK_CHANNEL"] = slack_channel
        if slack_thread_ts:
            env["SLACK_THREAD_TS"] = slack_thread_ts

        try:
            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
                cwd=cwd,
                limit=10 * 1024 * 1024,  # 10 MB readline buffer
            )
        except FileNotFoundError:
            logger.error("claude CLI not found — is it installed and in PATH?")
            return ClaudeResult(text="죄송합니다. Claude CLI를 사용할 수 없습니다.")

        # Track process for cancellation support.
        if thread_ts:
            self._active_processes[thread_ts] = process

        # Feed prompt as a stream-json user message; stdin stays open for
        # control_response messages (permission approvals).
        assert process.stdin is not None
        user_msg = json.dumps({
            "type": "user",
            "message": {"role": "user", "content": [{"type": "text", "text": prompt}]},
        })
        process.stdin.write(user_msg.encode("utf-8") + b"\n")
        await process.stdin.drain()

        stdin_lock = asyncio.Lock()
        approval_tasks: set[asyncio.Task] = set()

        async def _answer_permission_request(event: dict[str, Any]) -> None:
            """can_use_tool 요청을 on_permission에 위임하고 control_response를 보낸다."""
            request = event.get("request", {})
            decision: dict[str, Any] = {
                "behavior": "deny",
                "message": "No approval handler configured.",
            }
            if on_permission is not None:
                try:
                    decision = await on_permission(request)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    logger.warning("on_permission handler error: %s", exc)
                    decision = {"behavior": "deny", "message": f"승인 처리 오류: {exc}"}
            if decision.get("behavior") == "allow" and "updatedInput" not in decision:
                decision["updatedInput"] = request.get("input", {})
            payload = json.dumps({
                "type": "control_response",
                "response": {
                    "subtype": "success",
                    "request_id": event.get("request_id", ""),
                    "response": decision,
                },
            })
            try:
                async with stdin_lock:
                    process.stdin.write(payload.encode("utf-8") + b"\n")
                    await process.stdin.drain()
            except Exception as exc:
                logger.warning("Failed to send control_response: %s", exc)

        # Stream stdout line-by-line with an idle timeout. 이벤트는 이 자리에서
        # 바로 해석하고, 에러 로깅용으로는 마지막 일부 라인만 남긴다 (몇 시간짜리
        # 작업의 전체 stdout을 메모리에 들고 있지 않기 위함).
        tail: deque[str] = deque(maxlen=50)
        result_event: dict[str, Any] | None = None
        text_parts: list[str] = []  # result 이벤트가 없을 때의 폴백용 assistant 텍스트
        assert process.stdout is not None
        try:
            while True:
                try:
                    line_bytes = await asyncio.wait_for(
                        process.stdout.readline(), timeout=self._idle_timeout
                    )
                except TimeoutError:
                    process.kill()
                    await process.wait()
                    logger.error(
                        "Claude subprocess idle-timed out after %ds", self._idle_timeout
                    )
                    return ClaudeResult(text="죄송합니다. 요청 시간이 초과되었습니다. 다시 시도해주세요.")

                if not line_bytes:  # EOF
                    break

                stripped = line_bytes.decode("utf-8", errors="replace").strip()
                if not stripped:
                    continue
                tail.append(stripped)
                try:
                    event = json.loads(stripped)
                except json.JSONDecodeError:
                    continue

                # Permission request — answer asynchronously so the read
                # loop keeps consuming events while the user decides.
                if event.get("type") == "control_request":
                    if event.get("request", {}).get("subtype") == "can_use_tool":
                        task = asyncio.create_task(_answer_permission_request(event))
                        approval_tasks.add(task)
                        task.add_done_callback(approval_tasks.discard)
                    continue

                # Capture session_id from the init event.
                if (
                    thread_ts
                    and event.get("type") == "system"
                    and event.get("subtype") == "init"
                    and event.get("session_id")
                ):
                    self._sessions[thread_ts] = event["session_id"]
                    self._save_state()
                    logger.info("Captured session %s for thread %s", event["session_id"], thread_ts)

                # 되돌리기용 턴 정보 수집. --replay-user-messages 덕분에 첫 user
                # 이벤트가 이번 턴의 프롬프트이며, 그 uuid가 파일 되돌리기 기준점이다.
                if turn is not None:
                    if (
                        event.get("type") == "user"
                        and turn.get("user_uuid") is None
                        and event.get("uuid")
                    ):
                        turn["user_uuid"] = event["uuid"]
                    elif event.get("type") == "assistant" and event.get("uuid"):
                        turn["last_assistant_uuid"] = event["uuid"]

                if event.get("type") == "assistant":
                    for block in event.get("message", {}).get("content", []):
                        if not isinstance(block, dict):
                            continue
                        if block.get("type") == "text":
                            text_parts.append(block["text"])
                        elif turn is not None and block.get("type") == "tool_use":
                            key = _EDIT_TOOLS.get(block.get("name", ""))
                            path = block.get("input", {}).get(key) if key else None
                            if (
                                isinstance(path, str) and path
                                and path not in turn["files"]
                                and len(turn["files"]) < MAX_TRACKED_FILES
                            ):
                                turn["files"].append(path)

                if on_event:
                    try:
                        await on_event(event)
                    except Exception as exc:
                        logger.debug("on_event error: %s", exc)

                # stream-json 입력 모드에서는 result 후에도 프로세스가 다음
                # 입력을 기다리므로, result를 받으면 턴을 종료시킨다.
                if event.get("type") == "result":
                    result_event = event
                    break

        except Exception:
            process.kill()
            await process.wait()
            raise
        finally:
            for task in approval_tasks:
                task.cancel()
            if thread_ts:
                self._active_processes.pop(thread_ts, None)

        try:
            process.stdin.close()
        except Exception:
            pass
        try:
            await asyncio.wait_for(process.wait(), timeout=15)
        except TimeoutError:
            logger.warning("Claude subprocess did not exit after stdin close, killing.")
            process.kill()
            await process.wait()

        if result_event is None and process.returncode != 0:
            stderr_bytes = await process.stderr.read() if process.stderr else b""
            stderr_text = stderr_bytes.decode("utf-8", errors="replace").strip()
            logger.error(
                "Claude CLI failed (rc=%d) stderr: %s | stdout tail: %s | cmd: %s | prompt: %r",
                process.returncode, stderr_text, "\n".join(tail), cmd, prompt[:200],
            )
            return ClaudeResult(text="죄송합니다. 요청 처리 중 오류가 발생했습니다.")

        return self._make_result(result_event, text_parts)

    @staticmethod
    def _make_result(result_event: dict[str, Any] | None, text_parts: list[str]) -> ClaudeResult:
        """result 이벤트(없으면 assistant 텍스트 폴백)로 ClaudeResult를 만든다."""
        result_text = result_event.get("result", "") if result_event else None
        if result_text is not None:
            text = result_text
        elif text_parts:
            text = "\n\n".join(text_parts)
        else:
            text = ""

        cr = ClaudeResult(text=text)
        if result_event:
            cr.total_cost_usd = result_event.get("total_cost_usd", 0.0)
            cr.duration_ms = result_event.get("duration_ms", 0)
            cr.model_usage = result_event.get("modelUsage", {})
            usage = result_event.get("usage", {})
            cr.input_tokens = usage.get("input_tokens", 0)
            cr.output_tokens = usage.get("output_tokens", 0)
            cr.cache_read_tokens = usage.get("cache_read_input_tokens", 0)
            cr.cache_creation_tokens = usage.get("cache_creation_input_tokens", 0)
        return cr

    async def _build_thread_prompt(self, channel: str, thread_ts: str) -> str:
        """Fetch Slack thread history and format as a conversation prompt."""
        resp = await self._slack_client.conversations_replies(
            channel=channel, ts=thread_ts
        )
        messages = resp.get("messages", [])

        lines = ["The following is a Slack conversation. Continue assisting the user.\n"]
        for msg in messages:
            is_bot = (
                msg.get("user") == self._bot_user_id
                or msg.get("bot_id")
            )
            label = "[Assistant]" if is_bot else "[Human]"
            text = msg.get("text", "")
            files = msg.get("files", [])
            if files:
                text += format_file_metadata(files)
            lines.append(f"{label}: {text}")

        return "\n".join(lines)
