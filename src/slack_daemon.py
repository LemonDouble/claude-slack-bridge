"""
slack_daemon.py — Slack Socket Mode listener + Unix domain socket server.

The daemon holds exactly one Socket Mode WebSocket connection to Slack and
accepts local connections from session processes.

Each session connects, sends ``REGISTER {thread_ts}\n``, and blocks. When a
Slack reply arrives for that thread_ts the daemon forwards it over the socket,
unblocking the waiting session with zero polling.

Additionally, the daemon handles Human→Claude messages: top-level Slack
messages (and threaded replies with no pending MCP session) are forwarded to
the Claude Code CLI, and the response is posted back as a thread reply.

Project selection: when a user mentions the bot, a Block Kit UI is shown
with available projects (scanned from PROJECTS_DIR) and a "New Project" button.
Selecting a project starts a Claude thread in that project directory.
"""

import asyncio
import logging
import os
import re
from collections import deque
from typing import Any

from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler
from slack_bolt.async_app import AsyncApp

from claude_handler import ClaudeHandler, ClaudeResult
from constants import (
    SOCKET_PATH, SLACK_MAX_MESSAGE_LENGTH,
    VALID_MODELS, VALID_EFFORTS,
)
from event_poster import EventPoster, get_model_label
from file_downloader import format_file_metadata

logger = logging.getLogger(__name__)


class SlackDaemon:
    """
    Bridges Slack Socket Mode events to waiting session processes via a
    Unix domain socket, and handles Human→Claude messages via the Claude
    Code CLI.

    Args:
        bot_token: Slack bot OAuth token (xoxb-...).
        app_token: Slack app-level token for Socket Mode (xapp-...).
    """

    def __init__(self, bot_token: str, app_token: str, idle_timeout_minutes: int = 30) -> None:
        self._app = AsyncApp(token=bot_token)
        self._handler = AsyncSocketModeHandler(self._app, app_token)
        self._pending: dict[str, asyncio.StreamWriter] = {}
        self._lock = asyncio.Lock()
        self._claude = ClaudeHandler(
            slack_client=self._app.client,
            idle_timeout_minutes=idle_timeout_minutes,
        )
        self._active_threads: set[str] = set()
        self._thread_queues: dict[str, deque] = {}
        self._bot_user_id: str = ""

        # Register event/action/view handlers
        self._app.event("message")(self._handle_slack_message)
        self._app.event("reaction_added")(self._handle_reaction_added)
        self._app.action(re.compile(r"^select_project:.+$"))(self._handle_project_select)
        self._app.action("create_project")(self._handle_create_project)
        self._app.view("create_project_modal")(self._handle_create_project_modal)

    # ------------------------------------------------------------------
    # Block Kit builders
    # ------------------------------------------------------------------

    def _build_project_blocks(self) -> list[dict]:
        """Build Block Kit blocks with project buttons + create new button."""
        projects = self._claude.scan_projects()

        blocks: list[dict] = [
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": "*프로젝트를 선택하세요:*",
                },
            },
        ]

        if projects:
            # Chunk projects into groups of 5 (Slack actions block limit)
            for i in range(0, len(projects), 5):
                chunk = projects[i : i + 5]
                elements = [
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": name, "emoji": True},
                        "action_id": f"select_project:{name}",
                        "value": name,
                    }
                    for name in chunk
                ]
                blocks.append({"type": "actions", "elements": elements})
        else:
            blocks.append(
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": "_프로젝트가 없습니다. 새로 만들어 시작하세요._",
                    },
                }
            )

        # Always add "New Project" button at the end
        blocks.append({
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "+ 새 프로젝트", "emoji": True},
                    "action_id": "create_project",
                    "style": "primary",
                },
            ],
        })

        return blocks

    # ------------------------------------------------------------------
    # Slack event handlers
    # ------------------------------------------------------------------

    async def _handle_slack_message(self, event: dict[str, Any]) -> None:
        # Filter: Ignore bot messages (prevents self-echo loops).
        if event.get("bot_id"):
            return

        thread_ts: str | None = event.get("thread_ts")
        text: str = event.get("text", "")
        channel: str = event.get("channel", "")
        files: list[dict] = event.get("files", [])
        logger.info("Message event keys: %s, has files: %d, subtype: %s, text: %r, bot_id: %s, display_as_bot: %s, thread_ts: %s",
                     list(event.keys()), len(files), event.get("subtype"), text[:100],
                     event.get("bot_id"), event.get("display_as_bot"), thread_ts)

        # Case 1: Threaded reply WITH a pending MCP session — forward to session.
        if thread_ts:
            async with self._lock:
                writer = self._pending.pop(thread_ts, None)

            if writer is not None:
                logger.info("Slack reply in thread %s: %r", thread_ts, text)
                if files:
                    text += format_file_metadata(files)
                try:
                    writer.write(text.encode() + b"\n")
                    await writer.drain()
                    logger.info("Reply forwarded to session for thread %s.", thread_ts)
                except Exception as exc:
                    logger.warning("Failed to forward reply for %s: %s", thread_ts, exc)
                finally:
                    writer.close()
                return

        # Case 2: Threaded reply with NO pending session — continue Claude conversation.
        if thread_ts:
            project = self._claude.get_thread_project(thread_ts)
            logger.info("Thread %s project lookup: %s (known projects: %s)",
                        thread_ts, project, list(self._claude._thread_projects.keys()))
            if not project:
                return
            message_ts = event.get("ts", thread_ts)

            # Handle slash commands before forwarding to Claude.
            if await self._handle_thread_command(channel, thread_ts, message_ts, text):
                return

            if files:
                text += format_file_metadata(files)
            if thread_ts in self._active_threads:
                queue = self._thread_queues.setdefault(thread_ts, deque())
                position = len(queue) + 1
                logger.info("Thread %s is active, queuing message (#%d).", thread_ts, position)
                await self._add_reaction(channel, message_ts, "eyes")
                try:
                    resp = await self._app.client.chat_postMessage(
                        channel=channel, thread_ts=thread_ts,
                        text=f":hourglass: 대기 중… (#{position})",
                        mrkdwn=True,
                    )
                    status_ts = resp["ts"]
                except Exception:
                    status_ts = None
                queue.append((channel, thread_ts, text, message_ts, status_ts))
                return
            asyncio.create_task(self._handle_claude_thread_reply(channel, thread_ts, text, message_ts))
            return

        # Case 3: Top-level message — only respond if the bot is mentioned.
        mention_tag = f"<@{self._bot_user_id}>"
        if mention_tag not in text:
            return

        # Show project selection UI
        await self._app.client.chat_postMessage(
            channel=channel,
            text="프로젝트를 선택하세요:",
            blocks=self._build_project_blocks(),
        )

    async def _handle_reaction_added(self, event: dict, say: Any) -> None:  # noqa: ARG002
        """Handle reaction_added events — :x: cancels an active Claude thread."""
        if event.get("reaction") != "x":
            return
        item = event.get("item", {})
        if item.get("type") != "message":
            return
        channel = item.get("channel", "")
        message_ts = item.get("ts", "")

        # message_ts could be the thread root or a reply inside the thread.
        # Check both: direct match, or look up the thread root via Slack API.
        thread_ts: str | None = None
        if message_ts in self._active_threads:
            thread_ts = message_ts
        else:
            # Fetch the message to find its thread_ts (root of the thread).
            try:
                resp = await self._app.client.conversations_replies(
                    channel=channel, ts=message_ts, limit=1,
                )
                msgs = resp.get("messages", [])
                if msgs:
                    root_ts = msgs[0].get("thread_ts", message_ts)
                    if root_ts in self._active_threads:
                        thread_ts = root_ts
            except Exception as exc:
                logger.debug("Failed to resolve thread for reaction: %s", exc)

        if not thread_ts:
            return

        logger.info("Cancel requested via :x: reaction for thread %s", thread_ts)
        cancelled = await self._claude.cancel_thread(thread_ts)
        if cancelled:
            await self._app.client.chat_postMessage(
                channel=channel, thread_ts=thread_ts,
                text=":no_entry_sign: 작업이 중단되었습니다.",
                mrkdwn=True,
            )

    # ------------------------------------------------------------------
    # Action handlers (Block Kit interactions)
    # ------------------------------------------------------------------

    async def _handle_project_select(self, ack: Any, body: dict[str, Any]) -> None:
        """Handle project button click — start a Claude thread."""
        await ack()

        action = body["actions"][0]
        project_name = action["value"]
        channel = body["channel"]["id"]
        original_ts = body["message"]["ts"]

        # Update the original message to show selected project
        await self._app.client.chat_update(
            channel=channel,
            ts=original_ts,
            text=f"프로젝트: *{project_name}*",
            blocks=[
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": f"*프로젝트: {project_name}*",
                    },
                },
            ],
        )

        # Associate this thread with the project
        self._claude.set_thread_project(original_ts, project_name)
        logger.info("Project %s selected for thread %s", project_name, original_ts)

        # Post initial message in thread with current settings
        model = self._claude.get_model(original_ts)
        effort = self._claude.get_effort(original_ts)
        await self._app.client.chat_postMessage(
            channel=channel,
            thread_ts=original_ts,
            text=(
                f"`{project_name}` 프로젝트가 선택되었습니다. 무엇을 도와드릴까요?\n"
                f"> :gear: *{model}* · effort *{effort}*"
                f"  |  `!model`, `!effort`, `!settings` 로 변경 가능"
            ),
            mrkdwn=True,
        )

    async def _handle_create_project(self, ack: Any, body: dict[str, Any]) -> None:
        """Handle 'New Project' button click — open modal."""
        await ack()

        trigger_id = body["trigger_id"]
        channel = body["channel"]["id"]
        original_ts = body["message"]["ts"]

        await self._app.client.views_open(
            trigger_id=trigger_id,
            view={
                "type": "modal",
                "callback_id": "create_project_modal",
                "private_metadata": f"{channel}:{original_ts}",
                "title": {"type": "plain_text", "text": "새 프로젝트"},
                "submit": {"type": "plain_text", "text": "생성"},
                "close": {"type": "plain_text", "text": "취소"},
                "blocks": [
                    {
                        "type": "input",
                        "block_id": "project_name_block",
                        "element": {
                            "type": "plain_text_input",
                            "action_id": "project_name_input",
                            "placeholder": {
                                "type": "plain_text",
                                "text": "예: my-new-project",
                            },
                        },
                        "label": {"type": "plain_text", "text": "프로젝트 이름"},
                    },
                ],
            },
        )

    async def _handle_create_project_modal(self, ack: Any, body: dict[str, Any], view: dict[str, Any]) -> None:
        """Handle modal submission — create project directory and start thread."""
        values = view["state"]["values"]
        project_name = values["project_name_block"]["project_name_input"]["value"].strip()

        # Validate: only allow alphanumeric, hyphens, underscores
        if not re.match(r"^[a-zA-Z0-9][a-zA-Z0-9_-]*$", project_name):
            await ack({
                "response_action": "errors",
                "errors": {
                    "project_name_block": "프로젝트 이름은 영문자 또는 숫자로 시작하며, 영문자, 숫자, 하이픈(-), 밑줄(_)만 사용할 수 있습니다.",
                },
            })
            return

        # Check if project already exists
        existing = self._claude.scan_projects()
        if project_name in existing:
            await ack({
                "response_action": "errors",
                "errors": {
                    "project_name_block": f"'{project_name}' 프로젝트가 이미 존재합니다.",
                },
            })
            return

        await ack()

        # Create the project directory
        self._claude.create_project(project_name)

        # Parse channel and original_ts from private_metadata
        private_metadata = view.get("private_metadata", "")
        channel, original_ts = private_metadata.split(":", 1)

        # Update the original message
        await self._app.client.chat_update(
            channel=channel,
            ts=original_ts,
            text=f"프로젝트: *{project_name}* (신규)",
            blocks=[
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": f"*프로젝트: {project_name}* _(신규)_",
                    },
                },
            ],
        )

        # Associate thread with the new project
        self._claude.set_thread_project(original_ts, project_name)
        logger.info("New project %s created for thread %s", project_name, original_ts)

        # Post initial message in thread with current settings
        model = self._claude.get_model(original_ts)
        effort = self._claude.get_effort(original_ts)
        await self._app.client.chat_postMessage(
            channel=channel,
            thread_ts=original_ts,
            text=(
                f"`{project_name}` 프로젝트가 생성되었습니다. 무엇을 도와드릴까요?\n"
                f"> :gear: *{model}* · effort *{effort}*"
                f"  |  `!model`, `!effort`, `!settings` 로 변경 가능"
            ),
            mrkdwn=True,
        )

    # ------------------------------------------------------------------
    # Thread commands (!model, !effort, !settings, !default)
    # ------------------------------------------------------------------

    async def _handle_thread_command(
        self, channel: str, thread_ts: str, message_ts: str, text: str,
    ) -> bool:
        """Handle ! commands in thread messages. Returns True if handled."""
        stripped = text.strip()
        if not stripped.startswith("!"):
            return False

        parts = stripped.split(None, 1)
        cmd = parts[0].lower()
        arg = parts[1].strip().lower() if len(parts) > 1 else ""

        if cmd == "!model":
            return await self._cmd_change_setting(
                channel, thread_ts, message_ts, arg,
                name="모델", cmd="model", valid=VALID_MODELS,
                get_current="get_model", set_value="set_thread_model",
                get_default="default_model",
            )
        if cmd == "!effort":
            return await self._cmd_change_setting(
                channel, thread_ts, message_ts, arg,
                name="effort", cmd="effort", valid=VALID_EFFORTS,
                get_current="get_effort", set_value="set_thread_effort",
                get_default="default_effort",
            )
        if cmd in ("!settings", "!help"):
            return await self._cmd_settings(channel, thread_ts)
        if cmd == "!default":
            return await self._cmd_default(channel, thread_ts, message_ts, arg)
        if cmd == "!restart":
            return await self._cmd_restart(channel, thread_ts, message_ts, arg)

        return False

    async def _cmd_change_setting(
        self, channel: str, thread_ts: str, message_ts: str, arg: str,
        *, name: str, cmd: str, valid: tuple[str, ...],
        get_current: str, set_value: str, get_default: str,
    ) -> bool:
        """!model / !effort 공통 핸들러. get_current/set_value/get_default는 ClaudeHandler 메서드명."""
        getter = getattr(self._claude, get_current)
        setter = getattr(self._claude, set_value)
        default = getattr(self._claude, get_default)
        options = " | ".join(valid)

        if not arg:
            await self._app.client.chat_postMessage(
                channel=channel, thread_ts=thread_ts,
                text=(
                    f":gear: 현재 {name}: *{getter(thread_ts)}* (기본값: *{default}*)\n"
                    f"사용법: `!{cmd} {options}`\n"
                    f"기본값 변경: `!default {cmd} {options}`"
                ),
                mrkdwn=True,
            )
            return True
        if arg not in valid:
            await self._app.client.chat_postMessage(
                channel=channel, thread_ts=thread_ts,
                text=f":warning: 지원하지 않는 {name}입니다. 선택 가능: `{options}`",
                mrkdwn=True,
            )
            return True
        setter(thread_ts, arg)
        await self._add_reaction(channel, message_ts, "white_check_mark")
        await self._app.client.chat_postMessage(
            channel=channel, thread_ts=thread_ts,
            text=f":gear: {name}이(가) *{arg}*(으)로 변경되었습니다.",
            mrkdwn=True,
        )
        return True

    async def _cmd_restart(
        self, channel: str, thread_ts: str, message_ts: str, arg: str,
    ) -> bool:
        """Kill current Claude process and spawn a fresh session."""
        self._claude.clear_session(thread_ts)

        if thread_ts in self._active_threads:
            await self._claude.cancel_thread(thread_ts)
            self._thread_queues.pop(thread_ts, None)
            for _ in range(150):
                if thread_ts not in self._active_threads:
                    break
                await asyncio.sleep(0.1)

        await self._add_reaction(channel, message_ts, "arrows_counterclockwise")
        await self._app.client.chat_postMessage(
            channel=channel, thread_ts=thread_ts,
            text=":arrows_counterclockwise: 세션을 재시작합니다...",
            mrkdwn=True,
        )

        restart_prompt = arg if arg else "이전 대화 내용을 참고해서, 이어서 작업을 계속 진행해줘."
        asyncio.create_task(
            self._handle_claude_thread_reply(channel, thread_ts, restart_prompt, message_ts)
        )
        return True

    async def _cmd_settings(self, channel: str, thread_ts: str) -> bool:
        model = self._claude.get_model(thread_ts)
        effort = self._claude.get_effort(thread_ts)
        default_model = self._claude.default_model
        default_effort = self._claude.default_effort
        text = (
            f":gear: *현재 스레드 설정*\n"
            f"> 모델: *{model}*  |  effort: *{effort}*\n"
            f"> 기본값: *{default_model}* / *{default_effort}*\n\n"
            f"*명령어:*\n"
            f"• `!model sonnet|opus|haiku` — 이 스레드 모델 변경\n"
            f"• `!effort low|medium|high|xhigh|max` — 이 스레드 effort 변경\n"
            f"• `!default model sonnet` — 기본 모델 변경 (전체 적용)\n"
            f"• `!default effort high` — 기본 effort 변경 (전체 적용)\n"
            f"• `!restart` — 세션 재시작 (현재 작업 중단 후 새 세션으로 이어서 진행)"
        )
        await self._app.client.chat_postMessage(
            channel=channel, thread_ts=thread_ts, text=text, mrkdwn=True,
        )
        return True

    async def _cmd_default(
        self, channel: str, thread_ts: str, message_ts: str, arg: str,
    ) -> bool:
        """Handle !default model <val> or !default effort <val>."""
        parts = arg.split(None, 1)
        if len(parts) != 2 or parts[0] not in ("model", "effort"):
            await self._app.client.chat_postMessage(
                channel=channel, thread_ts=thread_ts,
                text=":warning: 사용법: `!default model sonnet` 또는 `!default effort high`",
                mrkdwn=True,
            )
            return True

        kind, value = parts[0], parts[1].strip()
        valid, setter, label = {
            "model":  (VALID_MODELS,  self._claude.set_default_model,  "모델"),
            "effort": (VALID_EFFORTS, self._claude.set_default_effort, "effort"),
        }[kind]

        if value not in valid:
            options = ", ".join(f"`{v}`" for v in valid)
            await self._app.client.chat_postMessage(
                channel=channel, thread_ts=thread_ts,
                text=f":warning: 선택 가능: {options}", mrkdwn=True,
            )
            return True

        setter(value)
        await self._add_reaction(channel, message_ts, "white_check_mark")
        await self._app.client.chat_postMessage(
            channel=channel, thread_ts=thread_ts,
            text=f":gear: 기본 {label}이(가) *{value}*(으)로 변경되었습니다.", mrkdwn=True,
        )
        return True

    # ------------------------------------------------------------------
    # Claude conversation handlers
    # ------------------------------------------------------------------

    async def _add_reaction(self, channel: str, timestamp: str, name: str) -> None:
        """Add an emoji reaction to a message, ignoring errors."""
        try:
            await self._app.client.reactions_add(channel=channel, timestamp=timestamp, name=name)
        except Exception as exc:
            logger.warning("Failed to add reaction %s: %s", name, exc)

    async def _remove_reaction(self, channel: str, timestamp: str, name: str) -> None:
        """Remove an emoji reaction from a message, ignoring errors."""
        try:
            await self._app.client.reactions_remove(channel=channel, timestamp=timestamp, name=name)
        except Exception as exc:
            logger.warning("Failed to remove reaction %s: %s", name, exc)

    # ------------------------------------------------------------------
    # Stream event formatting
    # ------------------------------------------------------------------

    def _make_event_poster(self, channel: str, thread_ts: str) -> "EventPoster":
        """Create an EventPoster that formats and posts Claude stream events."""
        return EventPoster(self._app.client, channel, thread_ts)

    async def _handle_claude_thread_reply(self, channel: str, thread_ts: str, text: str, message_ts: str | None = None) -> None:
        """Spawn Claude for a thread reply and post the response."""
        react_ts = message_ts or thread_ts
        logger.info("Handling thread reply: thread=%s, react_ts=%s, channel=%s", thread_ts, react_ts, channel)
        self._active_threads.add(thread_ts)
        await self._add_reaction(channel, react_ts, "hourglass_flowing_sand")
        poster = self._make_event_poster(channel, thread_ts)
        try:
            result = await self._claude.handle_thread_reply(
                channel, thread_ts, text, on_event=poster.handle_event,
            )
            progress_ts = await poster.flush()
            await self._post_response(channel, thread_ts, result.text, progress_ts=progress_ts)
            await self._post_usage_footer(channel, thread_ts, result)
            await self._remove_reaction(channel, react_ts, "hourglass_flowing_sand")
            await self._add_reaction(channel, react_ts, "white_check_mark")
        except Exception as exc:
            logger.error("Error in thread continuation %s: %s", thread_ts, exc)
            await self._remove_reaction(channel, react_ts, "hourglass_flowing_sand")
            await self._add_reaction(channel, react_ts, "x")
            await self._post_error(channel, thread_ts, exc)
        finally:
            self._active_threads.discard(thread_ts)
            await self._process_thread_queue(thread_ts)

    async def _process_thread_queue(self, thread_ts: str) -> None:
        """Merge and process all queued messages for a thread."""
        queue = self._thread_queues.pop(thread_ts, None)
        if not queue:
            return
        channel = queue[0][0]
        texts: list[str] = []
        last_message_ts: str | None = None
        for _ch, _ts, text, msg_ts, status_ts in queue:
            texts.append(text)
            last_message_ts = msg_ts
            await self._remove_reaction(_ch, msg_ts, "eyes")
            if status_ts:
                try:
                    await self._app.client.chat_delete(channel=_ch, ts=status_ts)
                except Exception:
                    pass
        merged_text = "\n\n".join(texts)
        logger.info("Processing %d merged queued messages for thread %s", len(texts), thread_ts)
        asyncio.create_task(self._handle_claude_thread_reply(channel, thread_ts, merged_text, last_message_ts))

    async def _post_error(self, channel: str, thread_ts: str, exc: Exception) -> None:
        """Post an error summary to the Slack thread so the user knows what went wrong."""
        error_type = type(exc).__name__
        error_msg = str(exc)
        if len(error_msg) > 500:
            error_msg = error_msg[:497] + "…"
        text = f":warning: *오류가 발생했습니다*\n`{error_type}: {error_msg}`"
        try:
            await self._app.client.chat_postMessage(
                channel=channel, thread_ts=thread_ts, text=text, mrkdwn=True,
            )
        except Exception as post_exc:
            logger.warning("Failed to post error message: %s", post_exc)

    async def _post_usage_footer(self, channel: str, thread_ts: str, result: ClaudeResult) -> None:
        """Post a small usage/cost summary as a thread reply."""
        if result.total_cost_usd == 0 and result.input_tokens == 0:
            return

        duration_s = result.duration_ms / 1000
        total_input = result.input_tokens + result.cache_read_tokens + result.cache_creation_tokens

        model_label = get_model_label(result.requested_model, result.model_usage)

        parts = [f":bar_chart: *{model_label}* | "]
        parts.append(f"Tokens In: `{total_input:,}` Out: `{result.output_tokens:,}`")
        if result.cache_read_tokens:
            cache_pct = result.cache_read_tokens / total_input * 100 if total_input else 0
            parts.append(f" (cache hit `{cache_pct:.0f}%`)")
        parts.append(f" | Cost: `${result.total_cost_usd:.4f}`")
        parts.append(f" | Time: `{duration_s:.1f}s`")

        text = "".join(parts)
        try:
            await self._app.client.chat_postMessage(
                channel=channel, thread_ts=thread_ts, text=text, mrkdwn=True,
            )
        except Exception as exc:
            logger.warning("Failed to post usage footer: %s", exc)

    @staticmethod
    def _markdown_to_slack(text: str) -> str:
        """Convert standard Markdown to Slack mrkdwn format."""
        # Headers: ## Header → *Header*
        text = re.sub(r"^#{1,6}\s+(.+)$", r"*\1*", text, flags=re.MULTILINE)
        # Bold: **text** → *text*
        text = re.sub(r"\*\*(.+?)\*\*", r"*\1*", text)
        # Italic: _text_ stays the same, but *text* (single) that isn't bold needs care
        # Strikethrough: ~~text~~ → ~text~
        text = re.sub(r"~~(.+?)~~", r"~\1~", text)
        # Links: [text](url) → <url|text>
        text = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r"<\2|\1>", text)
        # Images: ![alt](url) → <url|alt> (best effort in Slack)
        text = re.sub(r"!\[([^\]]*)\]\(([^)]+)\)", r"<\2|\1>", text)
        return text

    @staticmethod
    def _split_message(text: str, max_length: int) -> list[str]:
        """Split text into chunks at line boundaries, preserving code blocks."""
        if len(text) <= max_length:
            return [text]

        chunks: list[str] = []
        current = ""
        in_code_block = False

        for line in text.split("\n"):
            line_with_newline = line + "\n"

            # Track code block state
            if line.startswith("```"):
                in_code_block = not in_code_block

            # If adding this line would exceed the limit, flush current chunk
            if current and len(current) + len(line_with_newline) > max_length:
                # If we're inside a code block, close it in the current chunk
                if in_code_block:
                    current += "```\n"
                chunks.append(current.rstrip("\n"))
                # Re-open code block in the next chunk
                current = "```\n" + line_with_newline if in_code_block else line_with_newline
            else:
                current += line_with_newline

        if current.strip():
            chunks.append(current.rstrip("\n"))

        return chunks

    async def _delete_progress(self, channel: str, progress_ts: str | None) -> None:
        """Delete a progress message if it exists."""
        if not progress_ts:
            return
        try:
            await self._app.client.chat_delete(channel=channel, ts=progress_ts)
        except Exception:
            pass

    async def _post_response(
        self, channel: str, thread_ts: str, text: str, *, progress_ts: str | None = None,
    ) -> None:
        """Post a response to Slack, splitting if it exceeds the message length limit.

        If *progress_ts* is provided and the response fits in a single message,
        the progress message is updated in-place for a seamless transition.
        For multi-chunk or file responses, the progress message is deleted first.
        """
        text = self._markdown_to_slack(text)

        if not text or not text.strip():
            await self._delete_progress(channel, progress_ts)
            return

        chunks = self._split_message(text, SLACK_MAX_MESSAGE_LENGTH)

        # Single chunk — update progress message in-place if available
        if len(chunks) == 1 and progress_ts:
            try:
                await self._app.client.chat_update(
                    channel=channel, ts=progress_ts, text=chunks[0], mrkdwn=True,
                )
                return
            except Exception:
                pass  # Fall through to normal post

        await self._delete_progress(channel, progress_ts)

        # If it's too many chunks, upload as a file instead
        if len(chunks) > 3:
            await self._app.client.chat_postMessage(
                channel=channel, thread_ts=thread_ts,
                text=text[:3000] + "\n\n_(전체 응답은 파일로 첨부되었습니다)_",
                mrkdwn=True,
            )
            await self._app.client.files_upload_v2(
                channel=channel, thread_ts=thread_ts,
                content=text, filename="response.md",
                title="전체 응답",
            )
            return

        for chunk in chunks:
            await self._app.client.chat_postMessage(
                channel=channel, thread_ts=thread_ts, text=chunk, mrkdwn=True,
            )

    # ------------------------------------------------------------------
    # Unix socket server (MCP session relay)
    # ------------------------------------------------------------------

    async def _handle_session_connection(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        thread_ts: str | None = None
        try:
            line = await asyncio.wait_for(reader.readline(), timeout=10.0)
            parts = line.decode().strip().split(" ", 1)

            if len(parts) != 2 or parts[0] != "REGISTER":
                logger.warning("Bad session registration: %r", line)
                return

            thread_ts = parts[1]
            async with self._lock:
                self._pending[thread_ts] = writer

            logger.info("Session registered for thread %s.", thread_ts)

            # Block until the session disconnects (reader.read returns b"" on close).
            await reader.read(1)

        except Exception as exc:
            logger.error("Session connection error: %s", exc)
        finally:
            if thread_ts:
                async with self._lock:
                    self._pending.pop(thread_ts, None)
            if not writer.is_closing():
                writer.close()

    async def start(self) -> None:
        """Start the Unix socket server and Slack Socket Mode handler concurrently."""
        await self._claude.initialize()
        self._bot_user_id = self._claude._bot_user_id

        if os.path.exists(SOCKET_PATH):
            os.unlink(SOCKET_PATH)

        server = await asyncio.start_unix_server(
            self._handle_session_connection, path=SOCKET_PATH
        )
        logger.info("Unix socket server listening at %s.", SOCKET_PATH)

        async with server:
            await asyncio.gather(
                server.serve_forever(),
                self._handler.start_async(),
            )
