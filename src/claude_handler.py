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
from collections.abc import Callable, Coroutine
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from constants import (
    STATE_FILE, PROJECTS_ROOT,
    VALID_MODELS, VALID_EFFORTS, DEFAULT_MODEL, DEFAULT_EFFORT,
    DEFAULT_PERMISSION_MODE,
)

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
    permission_denials: list[dict[str, Any]] = field(default_factory=list)


class ClaudeHandler:
    """
    Manages Claude Code CLI invocations for Slack messages.

    Args:
        slack_client: An async Slack WebClient (``self._app.client``).
    """

    def __init__(self, slack_client: Any, idle_timeout_minutes: int = 720) -> None:
        self._slack_client = slack_client
        self._bot_user_id: str = ""
        self._idle_timeout = idle_timeout_minutes * 60
        self._sessions: dict[str, str] = {}  # thread_ts → session UUID
        self._thread_projects: dict[str, str] = {}  # thread_ts → project dir
        self._active_processes: dict[str, asyncio.subprocess.Process] = {}
        self._default_model: str = DEFAULT_MODEL
        self._default_effort: str = DEFAULT_EFFORT
        self._default_perm: str = DEFAULT_PERMISSION_MODE
        self._thread_models: dict[str, str] = {}   # thread_ts → model
        self._thread_efforts: dict[str, str] = {}  # thread_ts → effort
        self._thread_perms: dict[str, str] = {}    # thread_ts → permission mode
        self._load_state()

    async def initialize(self) -> None:
        """Cache the bot's own user ID."""
        resp = await self._slack_client.auth_test()
        self._bot_user_id = resp["user_id"]
        logger.info("ClaudeHandler initialized, bot_user_id=%s", self._bot_user_id)

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
            self._default_model = data.get("default_model", DEFAULT_MODEL)
            self._default_effort = data.get("default_effort", DEFAULT_EFFORT)
            self._default_perm = data.get("default_permission_mode", DEFAULT_PERMISSION_MODE)
            self._thread_models = data.get("thread_models", {})
            self._thread_efforts = data.get("thread_efforts", {})
            self._thread_perms = data.get("thread_permission_modes", {})
            logger.info(
                "Restored state: %d threads, %d sessions, default=%s/%s.",
                len(self._thread_projects), len(self._sessions),
                self._default_model, self._default_effort,
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
                "default_model": self._default_model,
                "default_effort": self._default_effort,
                "default_permission_mode": self._default_perm,
                "thread_models": self._thread_models,
                "thread_efforts": self._thread_efforts,
                "thread_permission_modes": self._thread_perms,
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
    # Model / effort settings
    # ------------------------------------------------------------------

    def get_model(self, thread_ts: str) -> str:
        """Return the model for a thread (falls back to global default)."""
        return self._thread_models.get(thread_ts, self._default_model)

    def get_effort(self, thread_ts: str) -> str:
        """Return the effort level for a thread (falls back to global default)."""
        return self._thread_efforts.get(thread_ts, self._default_effort)

    def set_thread_model(self, thread_ts: str, model: str) -> None:
        """Set the model for a specific thread."""
        self._thread_models[thread_ts] = model
        self._save_state()

    def set_thread_effort(self, thread_ts: str, effort: str) -> None:
        """Set the effort level for a specific thread."""
        self._thread_efforts[thread_ts] = effort
        self._save_state()

    def get_permission_mode(self, thread_ts: str) -> str:
        """Return the permission mode for a thread (falls back to global default)."""
        return self._thread_perms.get(thread_ts, self._default_perm)

    def set_thread_permission_mode(self, thread_ts: str, mode: str) -> None:
        """Set the permission mode for a specific thread."""
        self._thread_perms[thread_ts] = mode
        self._save_state()

    def set_default_model(self, model: str) -> None:
        """Set the global default model (persisted)."""
        self._default_model = model
        self._save_state()

    def set_default_effort(self, effort: str) -> None:
        """Set the global default effort level (persisted)."""
        self._default_effort = effort
        self._save_state()

    def set_default_permission_mode(self, mode: str) -> None:
        """Set the global default permission mode (persisted)."""
        self._default_perm = mode
        self._save_state()

    @property
    def default_model(self) -> str:
        return self._default_model

    @property
    def default_effort(self) -> str:
        return self._default_effort

    @property
    def default_permission_mode(self) -> str:
        return self._default_perm

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def handle_thread_reply(
        self, channel: str, thread_ts: str, text: str,
        on_event: OnEventFn | None = None,
        on_permission: OnPermissionFn | None = None,
    ) -> ClaudeResult:
        """Handle a threaded reply (resume existing session or fallback)."""
        session_id = self._sessions.get(thread_ts)
        project_dir = self._thread_projects.get(thread_ts)
        model = self.get_model(thread_ts)
        effort = self.get_effort(thread_ts)
        perm = self.get_permission_mode(thread_ts)

        if session_id:
            logger.info("Resuming session %s for thread %s", session_id, thread_ts)
            cmd = self._build_cmd(
                resume=session_id, model=model, effort=effort, permission_mode=perm,
            )
            result = await self._run_claude(
                cmd, text, cwd=project_dir, on_event=on_event,
                slack_channel=channel, slack_thread_ts=thread_ts,
                thread_ts=thread_ts, on_permission=on_permission,
            )
            result.requested_model = model
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
            thread_ts=thread_ts, on_permission=on_permission,
        )
        result.requested_model = model
        return result

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
        resume: str | None = None,
        model: str = DEFAULT_MODEL,
        effort: str = DEFAULT_EFFORT,
        name: str | None = None,
        permission_mode: str = DEFAULT_PERMISSION_MODE,
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
        if self._sessions.pop(thread_ts, None):
            self._save_state()

    async def _run_claude(
        self, cmd: list[str], prompt: str, cwd: str | None = None,
        on_event: OnEventFn | None = None,
        slack_channel: str = "", slack_thread_ts: str = "",
        thread_ts: str = "",
        on_permission: OnPermissionFn | None = None,
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

        # Stream stdout line-by-line with an idle timeout.
        lines: list[str] = []
        result_seen = False
        assert process.stdout is not None
        try:
            while True:
                try:
                    line_bytes = await asyncio.wait_for(
                        process.stdout.readline(), timeout=self._idle_timeout
                    )
                except asyncio.TimeoutError:
                    process.kill()
                    await process.wait()
                    logger.error(
                        "Claude subprocess idle-timed out after %ds", self._idle_timeout
                    )
                    return ClaudeResult(text="죄송합니다. 요청 시간이 초과되었습니다. 다시 시도해주세요.")

                if not line_bytes:  # EOF
                    break

                line_str = line_bytes.decode("utf-8", errors="replace")
                lines.append(line_str)

                # Parse event and capture session ID.
                stripped = line_str.strip()
                if not stripped:
                    continue
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
                if on_event:
                    try:
                        await on_event(event)
                    except Exception as exc:
                        logger.debug("on_event error: %s", exc)

                # stream-json 입력 모드에서는 result 후에도 프로세스가 다음
                # 입력을 기다리므로, result를 받으면 턴을 종료시킨다.
                if event.get("type") == "result":
                    result_seen = True
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
        except asyncio.TimeoutError:
            logger.warning("Claude subprocess did not exit after stdin close, killing.")
            process.kill()
            await process.wait()

        if not result_seen and process.returncode != 0:
            stderr_bytes = await process.stderr.read() if process.stderr else b""
            stderr_text = stderr_bytes.decode("utf-8", errors="replace").strip()
            stdout_text = "".join(lines).strip()
            logger.error(
                "Claude CLI failed (rc=%d) stderr: %s | stdout: %s | cmd: %s | prompt: %r",
                process.returncode, stderr_text, stdout_text, cmd, prompt[:200],
            )
            return ClaudeResult(text="죄송합니다. 요청 처리 중 오류가 발생했습니다.")

        return self._parse_stream_response(lines)

    @staticmethod
    def _parse_stream_response(lines: list[str]) -> ClaudeResult:
        """Extract the final result text and usage stats from stream-json output.

        ``stream-json`` emits one JSON object per line.  The final message
        with ``"type": "result"`` contains the ``"result"`` field we need.
        Falls back to collecting all ``assistant`` message text blocks.
        """
        result_text: str | None = None
        result_event: dict[str, Any] | None = None
        text_parts: list[str] = []

        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue

            # The final "result" event carries the complete answer.
            if event.get("type") == "result":
                result_text = event.get("result", "")
                result_event = event
                break

            # Accumulate assistant text blocks as fallback.
            if event.get("type") == "assistant" and "message" in event:
                for block in event["message"].get("content", []):
                    if isinstance(block, dict) and block.get("type") == "text":
                        text_parts.append(block["text"])

        if result_text is not None:
            text = result_text
        elif text_parts:
            text = "\n\n".join(text_parts)
        else:
            text = "".join(lines).strip()

        # Extract usage stats from the result event.
        cr = ClaudeResult(text=text)
        if result_event:
            cr.total_cost_usd = result_event.get("total_cost_usd", 0.0)
            cr.duration_ms = result_event.get("duration_ms", 0)
            cr.model_usage = result_event.get("modelUsage", {})
            cr.permission_denials = result_event.get("permission_denials", []) or []
            usage = result_event.get("usage", {})
            cr.input_tokens = usage.get("input_tokens", 0)
            cr.output_tokens = usage.get("output_tokens", 0)
            cr.cache_read_tokens = usage.get("cache_read_input_tokens", 0)
            cr.cache_creation_tokens = usage.get("cache_creation_input_tokens", 0)
        return cr

    async def _build_thread_prompt(self, channel: str, thread_ts: str) -> str:
        """Fetch Slack thread history and format as a conversation prompt."""
        from file_downloader import format_file_metadata

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
