"""
claude_handler.py — Spawns Claude Code CLI subprocesses for Human→Claude tasks.

When a human posts a message in Slack, this handler runs ``claude -p`` to
generate a response.  Thread continuations use ``--resume`` so Claude retains
full context (tool use, reasoning) across messages in the same thread.

If the session ID is lost (e.g. container restart), falls back to a one-shot
``claude -p`` with the formatted thread history as the prompt.

Project detection: scans ``/projects/`` for 1-depth subdirectories.  Each
subdirectory is treated as a separate project.  The project for each thread
is selected via Slack Block Kit interactions and tracked per thread_ts.
"""

import asyncio
import json
import logging
import os
import uuid
from collections.abc import Callable, Coroutine
from pathlib import Path
from typing import Any

_STATE_FILE = Path.home() / ".claude" / "slack-bridge-state.json"

OnEventFn = Callable[[dict[str, Any]], Coroutine[Any, Any, None]]

logger = logging.getLogger(__name__)

DEFAULT_IDLE_TIMEOUT = 43200  # 12 hours
PROJECTS_ROOT = Path("/projects")


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
        if not _STATE_FILE.exists():
            return
        try:
            data = json.loads(_STATE_FILE.read_text())
            self._thread_projects = data.get("thread_projects", {})
            self._sessions = data.get("sessions", {})
            logger.info(
                "Restored state: %d threads, %d sessions.",
                len(self._thread_projects), len(self._sessions),
            )
        except Exception as exc:
            logger.warning("Failed to load state: %s", exc)

    def _save_state(self) -> None:
        """Persist current mappings to disk."""
        try:
            _STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
            _STATE_FILE.write_text(json.dumps({
                "thread_projects": self._thread_projects,
                "sessions": self._sessions,
            }))
        except Exception as exc:
            logger.warning("Failed to save state: %s", exc)

    # ------------------------------------------------------------------
    # Project scanning
    # ------------------------------------------------------------------

    @staticmethod
    def scan_projects() -> list[str]:
        """Return sorted list of project directory names under /projects."""
        if not PROJECTS_ROOT.exists():
            logger.warning("Projects root %s does not exist.", PROJECTS_ROOT)
            return []
        return sorted(
            d.name for d in PROJECTS_ROOT.iterdir()
            if d.is_dir() and not d.name.startswith(".")
        )

    def set_thread_project(self, thread_ts: str, project_name: str) -> str:
        """Associate a thread with a project. Returns the full project path."""
        project_dir = str(PROJECTS_ROOT / project_name)
        self._thread_projects[thread_ts] = project_dir
        self._save_state()
        return project_dir

    def get_thread_project(self, thread_ts: str) -> str | None:
        """Get the project directory for a thread."""
        return self._thread_projects.get(thread_ts)

    @staticmethod
    def create_project(name: str) -> str:
        """Create a new project directory. Returns the full path."""
        project_dir = PROJECTS_ROOT / name
        project_dir.mkdir(parents=True, exist_ok=True)
        logger.info("Created project directory: %s", project_dir)
        return str(project_dir)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def handle_message(
        self, channel: str, message_ts: str, text: str,
        on_event: OnEventFn | None = None,
    ) -> str:
        """Handle a new top-level Slack message (start a new Claude session)."""
        session_id = str(uuid.uuid4())
        self._sessions[message_ts] = session_id
        self._save_state()
        logger.info("New Claude session %s for thread %s", session_id, message_ts)

        project_dir = self._thread_projects.get(message_ts)
        cmd = self._build_cmd(session_id=session_id)
        return await self._run_claude(cmd, text, cwd=project_dir, on_event=on_event)

    async def handle_thread_reply(
        self, channel: str, thread_ts: str, text: str,
        on_event: OnEventFn | None = None,
    ) -> str:
        """Handle a threaded reply (resume existing session or fallback)."""
        session_id = self._sessions.get(thread_ts)
        project_dir = self._thread_projects.get(thread_ts)

        if session_id:
            logger.info("Resuming session %s for thread %s", session_id, thread_ts)
            cmd = self._build_cmd(resume=session_id)
            return await self._run_claude(cmd, text, cwd=project_dir, on_event=on_event)

        # Fallback: session lost (container restart) — use thread history as context.
        logger.info("No session for thread %s, falling back to thread history.", thread_ts)
        prompt = await self._build_thread_prompt(channel, thread_ts)
        cmd = self._build_cmd()
        return await self._run_claude(cmd, prompt, cwd=project_dir, on_event=on_event)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    @staticmethod
    def _build_cmd(
        session_id: str | None = None,
        resume: str | None = None,
    ) -> list[str]:
        cmd = ["claude", "-p", "--dangerously-skip-permissions", "--verbose", "--output-format", "stream-json"]
        if session_id:
            cmd.extend(["--session-id", session_id])
        if resume:
            cmd.extend(["--resume", resume])
        return cmd

    async def _run_claude(
        self, cmd: list[str], prompt: str, cwd: str | None = None,
        on_event: OnEventFn | None = None,
    ) -> str:
        """Spawn a ``claude -p`` subprocess and return the response text.

        Uses ``--output-format stream-json`` and reads stdout line-by-line so
        that long-running tasks (hours) are never killed as long as Claude is
        still producing output.  Only an *idle* timeout (no new output for
        the configured seconds) will terminate the process.

        If *on_event* is provided, each parsed JSON event is forwarded to it
        so callers can post real-time progress to Slack.
        """
        env = os.environ.copy()
        env.pop("CLAUDECODE", None)

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
            return "죄송합니다. Claude CLI를 사용할 수 없습니다."

        # Feed prompt and close stdin so Claude starts processing.
        assert process.stdin is not None
        process.stdin.write(prompt.encode("utf-8"))
        await process.stdin.drain()
        process.stdin.close()

        # Stream stdout line-by-line with an idle timeout.
        lines: list[str] = []
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
                    return "죄송합니다. 요청 시간이 초과되었습니다. 다시 시도해주세요."

                if not line_bytes:  # EOF
                    break

                line_str = line_bytes.decode("utf-8", errors="replace")
                lines.append(line_str)

                # Forward parsed event to callback.
                if on_event:
                    stripped = line_str.strip()
                    if stripped:
                        try:
                            event = json.loads(stripped)
                            await on_event(event)
                        except (json.JSONDecodeError, Exception) as exc:
                            logger.debug("on_event error: %s", exc)

        except Exception:
            process.kill()
            await process.wait()
            raise

        await process.wait()

        if process.returncode != 0:
            stderr_bytes = await process.stderr.read() if process.stderr else b""
            stderr_text = stderr_bytes.decode("utf-8", errors="replace").strip()
            stdout_text = "".join(lines).strip()
            logger.error(
                "Claude CLI failed (rc=%d) stderr: %s | stdout: %s | cmd: %s | prompt: %r",
                process.returncode, stderr_text, stdout_text, cmd, prompt[:200],
            )
            return "죄송합니다. 요청 처리 중 오류가 발생했습니다."

        return self._parse_stream_response(lines)

    @staticmethod
    def _parse_stream_response(lines: list[str]) -> str:
        """Extract the final result text from stream-json output.

        ``stream-json`` emits one JSON object per line.  The final message
        with ``"type": "result"`` contains the ``"result"`` field we need.
        Falls back to collecting all ``assistant`` message text blocks.
        """
        result_text: str | None = None
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
                break

            # Accumulate assistant text blocks as fallback.
            if event.get("type") == "assistant" and "message" in event:
                for block in event["message"].get("content", []):
                    if isinstance(block, dict) and block.get("type") == "text":
                        text_parts.append(block["text"])

        if result_text is not None:
            return result_text
        if text_parts:
            return "\n\n".join(text_parts)
        # Last resort: return raw concatenation.
        return "".join(lines).strip()

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
            lines.append(f"{label}: {text}")

        return "\n".join(lines)
