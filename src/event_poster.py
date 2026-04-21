"""
event_poster.py — Claude stream-json 이벤트를 Slack에 포스팅.

EventPoster는 Claude CLI의 실시간 이벤트를 수집하고, Slack rate limit을
준수하면서 단일 "진행 상황" 메시지를 갱신합니다.
"""

import logging
import re
import time
from typing import Any

logger = logging.getLogger(__name__)

_POST_INTERVAL = 3.0


class EventPoster:
    """Accumulates Claude stream-json events and posts formatted progress to a Slack thread.

    Batches events to stay within Slack rate limits (~1 msg/sec) and updates
    a single "progress" message instead of spamming many messages.
    """

    def __init__(self, slack_client: Any, channel: str, thread_ts: str) -> None:
        self._client = slack_client
        self._channel = channel
        self._thread_ts = thread_ts
        self._progress_ts: str | None = None
        self._lines: list[str] = []
        self._last_post: float = 0.0
        self._dirty = False

    async def handle_event(self, event: dict[str, Any]) -> None:
        line = self._format_event(event)
        if line is None:
            return

        self._lines.append(line)
        self._dirty = True

        now = time.monotonic()
        if now - self._last_post >= _POST_INTERVAL:
            await self._post_or_update()

    async def flush(self) -> str | None:
        """Post any remaining buffered progress and return the progress message ts."""
        if self._dirty:
            await self._post_or_update()
        return self._progress_ts

    @staticmethod
    def _format_event(event: dict[str, Any]) -> str | None:
        etype = event.get("type")

        if etype == "assistant":
            content = event.get("message", {}).get("content", [])
            parts: list[str] = []
            for block in content:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "tool_use":
                    parts.append(_format_tool_use(block))
            return "\n".join(parts) if parts else None

        if etype == "system" and event.get("subtype") == "init":
            session_id = event.get("session_id", "")[:8]
            return f":rocket:  세션 시작 (`{session_id}…`)"

        return None

    async def _post_or_update(self) -> None:
        visible = self._lines[-30:]
        text = "\n".join(visible)
        if not text:
            return

        try:
            if self._progress_ts:
                await self._client.chat_update(
                    channel=self._channel, ts=self._progress_ts, text=text, mrkdwn=True,
                )
            else:
                resp = await self._client.chat_postMessage(
                    channel=self._channel, thread_ts=self._thread_ts, text=text, mrkdwn=True,
                )
                self._progress_ts = resp["ts"]
        except Exception as exc:
            logger.warning("EventPoster post/update failed: %s", exc)

        self._last_post = time.monotonic()
        self._dirty = False


def get_model_label(requested: str, model_usage: dict[str, Any]) -> str:
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
        version = f"{parts[-2]}.{parts[-1]}"
        return f"{family} {version}"
    if len(parts) >= 2 and parts[-1].isdigit():
        family = " ".join(p.capitalize() for p in parts[:-1])
        return f"{family} {parts[-1]}"
    return name.replace("-", " ").title()


def _format_tool_use(block: dict[str, Any]) -> str:
    name = block.get("name", "unknown")
    inp = block.get("input", {})

    if name == "Bash":
        cmd = inp.get("command", "")
        display = cmd if len(cmd) <= 120 else cmd[:117] + "…"
        return f":terminal:  `$ {display}`"

    if name in ("Read", "Glob"):
        path = inp.get("file_path") or inp.get("pattern", "")
        return f":page_facing_up:  *{name}* `{path}`"

    if name in ("Edit", "Write"):
        path = inp.get("file_path", "")
        return f":pencil2:  *{name}* `{path}`"

    if name == "Grep":
        pattern = inp.get("pattern", "")
        return f":mag:  *Grep* `{pattern}`"

    if name in ("Agent", "agent"):
        desc = inp.get("description") or inp.get("prompt", "")[:60]
        return f":robot_face:  *Agent* {desc}"

    summary = str(inp)[:80]
    return f":hammer_and_wrench:  *{name}* {summary}"
