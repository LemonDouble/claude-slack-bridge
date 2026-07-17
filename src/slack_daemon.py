"""
slack_daemon.py — Slack Socket Mode listener.

The daemon holds one Socket Mode WebSocket connection to Slack and forwards
human messages to the Claude Code CLI, posting responses back as thread
replies.

Project selection: when a user mentions the bot, a folder-browser Block Kit
UI is shown. The user can navigate the PROJECTS_DIR tree at any depth, start
a session in any folder, or create a new folder by path. After choosing a
folder, existing Claude CLI sessions in that directory (including sessions
started from the terminal) can be resumed.
"""

import asyncio
import json
import logging
import re
import uuid
from collections import deque
from pathlib import Path
from typing import Any

from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler
from slack_bolt.async_app import AsyncApp

from claude_handler import ClaudeHandler, ClaudeResult
from constants import (
    PROJECTS_ROOT, SLACK_MAX_MESSAGE_LENGTH,
    VALID_MODELS, VALID_EFFORTS, VALID_PERMISSION_MODES,
    APPROVAL_TIMEOUT_SECONDS,
)
from event_poster import EventPoster, get_model_label
from file_downloader import format_file_metadata
from session_catalog import format_age, list_sessions

logger = logging.getLogger(__name__)

_MAX_FOLDER_BUTTONS = 40
_SESSION_LIST_LIMIT = 5


class SlackDaemon:
    """
    Handles Human→Claude messages via the Claude Code CLI, with a
    folder-browser UI for project selection and session resume.

    Args:
        bot_token: Slack bot OAuth token (xoxb-...).
        app_token: Slack app-level token for Socket Mode (xapp-...).
    """

    def __init__(self, bot_token: str, app_token: str, idle_timeout_minutes: int = 30) -> None:
        self._app = AsyncApp(token=bot_token)
        self._handler = AsyncSocketModeHandler(self._app, app_token)
        self._claude = ClaudeHandler(
            slack_client=self._app.client,
            idle_timeout_minutes=idle_timeout_minutes,
        )
        self._active_threads: set[str] = set()
        self._thread_queues: dict[str, deque] = {}
        self._pending_approvals: dict[str, asyncio.Future] = {}
        self._bot_user_id: str = ""

        # Register event/action/view handlers
        self._app.event("message")(self._handle_slack_message)
        self._app.event("reaction_added")(self._handle_reaction_added)
        self._app.action("perm_allow")(self._handle_perm_decision)
        self._app.action("perm_deny")(self._handle_perm_decision)
        self._app.action(re.compile(r"^answer:\d+$"))(self._handle_question_answer)
        self._app.action(re.compile(r"^nav:\d+$"))(self._handle_nav)
        self._app.action("nav_up")(self._handle_nav_up)
        self._app.action("back_to_browser")(self._handle_nav)
        self._app.action("start_here")(self._handle_start_here)
        self._app.action("new_session")(self._handle_new_session)
        self._app.action(re.compile(r"^pick_session:\d+$"))(self._handle_pick_session)
        self._app.action("new_folder")(self._handle_new_folder)
        self._app.view("new_folder_modal")(self._handle_new_folder_modal)

    # ------------------------------------------------------------------
    # Folder browser (Block Kit builders + path helpers)
    # ------------------------------------------------------------------

    @staticmethod
    def _abs_path(rel: str) -> Path:
        """rel 경로를 PROJECTS_ROOT 기준 절대 경로로 변환한다 (탈출 방지 검증 포함)."""
        root = PROJECTS_ROOT.resolve()
        path = (root / rel).resolve() if rel else root
        path.relative_to(root)  # PROJECTS_ROOT 밖이면 ValueError
        return path

    # Slack 버튼 value는 빈 문자열을 허용하지 않으므로 루트("")를 "."로 인코딩한다.
    @staticmethod
    def _encode_rel(rel: str) -> str:
        return rel or "."

    @staticmethod
    def _decode_rel(value: str) -> str:
        return "" if value == "." else value

    @staticmethod
    def _display_path(rel: str) -> str:
        return f"{PROJECTS_ROOT.name}/{rel}" if rel else PROJECTS_ROOT.name

    def _build_browser_blocks(self, rel: str) -> list[dict]:
        """폴더 브라우저 화면: 하위 폴더 버튼 + 탐색/시작/새 폴더 컨트롤."""
        folder = self._abs_path(rel)
        subdirs = sorted(
            d.name for d in folder.iterdir()
            if d.is_dir() and not d.name.startswith(".")
        )
        truncated = len(subdirs) > _MAX_FOLDER_BUTTONS
        subdirs = subdirs[:_MAX_FOLDER_BUTTONS]

        header = f":open_file_folder: *{self._display_path(rel)}*"
        if truncated:
            header += f"  _(하위 폴더 {_MAX_FOLDER_BUTTONS}개까지만 표시)_"
        blocks: list[dict] = [
            {"type": "section", "text": {"type": "mrkdwn", "text": header}},
        ]

        for i in range(0, len(subdirs), 5):
            elements = [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": name, "emoji": True},
                    "action_id": f"nav:{i + j}",
                    "value": f"{rel}/{name}" if rel else name,
                }
                for j, name in enumerate(subdirs[i : i + 5])
            ]
            blocks.append({"type": "actions", "elements": elements})

        controls: list[dict] = []
        if rel:
            controls.append({
                "type": "button",
                "text": {"type": "plain_text", "text": "⬆ 상위로", "emoji": True},
                "action_id": "nav_up",
                "value": rel,
            })
        controls.append({
            "type": "button",
            "text": {"type": "plain_text", "text": "▶ 여기서 시작", "emoji": True},
            "action_id": "start_here",
            "value": self._encode_rel(rel),
            "style": "primary",
        })
        controls.append({
            "type": "button",
            "text": {"type": "plain_text", "text": "➕ 새 폴더", "emoji": True},
            "action_id": "new_folder",
            "value": self._encode_rel(rel),
        })
        blocks.append({"type": "actions", "elements": controls})

        return blocks

    def _build_session_blocks(self, rel: str) -> list[dict]:
        """세션 선택 화면: 새 세션 + 해당 폴더의 최근 Claude CLI 세션 목록."""
        sessions = list_sessions(str(self._abs_path(rel)), limit=_SESSION_LIST_LIMIT)

        blocks: list[dict] = [
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f":open_file_folder: *{self._display_path(rel)}* — 세션을 선택하세요:",
                },
            },
            {
                "type": "actions",
                "elements": [
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "🆕 새 세션", "emoji": True},
                        "action_id": "new_session",
                        "value": self._encode_rel(rel),
                        "style": "primary",
                    },
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "⬅ 폴더 목록", "emoji": True},
                        "action_id": "back_to_browser",
                        "value": self._encode_rel(rel),
                    },
                ],
            },
        ]

        for i, s in enumerate(sessions):
            blocks.append({
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"*{s.title}*\n`{s.session_id[:8]}` · {format_age(s.mtime)}",
                },
                "accessory": {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "이어가기", "emoji": True},
                    "action_id": f"pick_session:{i}",
                    "value": json.dumps({"rel": rel, "sid": s.session_id, "title": s.title}),
                },
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

        # Case 1: Threaded reply — continue the Claude conversation for that thread.
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

        # Case 2: Top-level message — only respond if the bot is mentioned.
        mention_tag = f"<@{self._bot_user_id}>"
        if mention_tag not in text:
            return

        # Show the folder browser starting at PROJECTS_ROOT
        await self._app.client.chat_postMessage(
            channel=channel,
            text="프로젝트 폴더를 선택하세요:",
            blocks=self._build_browser_blocks(""),
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
    # Action handlers (folder browser + session picker)
    # ------------------------------------------------------------------

    async def _update_browser(self, channel: str, ts: str, rel: str) -> None:
        """브라우저 메시지를 rel 폴더 화면으로 갱신한다 (경로 오류 시 루트로)."""
        try:
            blocks = self._build_browser_blocks(rel)
        except (ValueError, FileNotFoundError, NotADirectoryError):
            logger.warning("Invalid browse path %r, falling back to root.", rel)
            blocks = self._build_browser_blocks("")
        await self._app.client.chat_update(
            channel=channel, ts=ts,
            text="프로젝트 폴더를 선택하세요:", blocks=blocks,
        )

    async def _handle_nav(self, ack: Any, body: dict[str, Any]) -> None:
        """폴더 버튼 클릭 / 세션 화면에서 폴더 목록으로 복귀."""
        await ack()
        rel = self._decode_rel(body["actions"][0]["value"])
        await self._update_browser(body["channel"]["id"], body["message"]["ts"], rel)

    async def _handle_nav_up(self, ack: Any, body: dict[str, Any]) -> None:
        """상위 폴더로 이동."""
        await ack()
        rel = self._decode_rel(body["actions"][0]["value"])
        parent = rel.rsplit("/", 1)[0] if "/" in rel else ""
        await self._update_browser(body["channel"]["id"], body["message"]["ts"], parent)

    async def _handle_start_here(self, ack: Any, body: dict[str, Any]) -> None:
        """현재 폴더에서 시작 — 기존 세션이 있으면 세션 선택 화면을 먼저 보여준다."""
        await ack()
        rel = self._decode_rel(body["actions"][0]["value"])
        channel = body["channel"]["id"]
        ts = body["message"]["ts"]

        blocks = self._build_session_blocks(rel)
        if len(blocks) <= 2:  # 헤더 + 버튼 행뿐 = 기존 세션 없음 → 바로 새 세션
            await self._finalize_selection(channel, ts, rel)
            return
        await self._app.client.chat_update(
            channel=channel, ts=ts, text="세션을 선택하세요:", blocks=blocks,
        )

    async def _handle_new_session(self, ack: Any, body: dict[str, Any]) -> None:
        await ack()
        rel = self._decode_rel(body["actions"][0]["value"])
        await self._finalize_selection(body["channel"]["id"], body["message"]["ts"], rel)

    async def _handle_pick_session(self, ack: Any, body: dict[str, Any]) -> None:
        await ack()
        data = json.loads(body["actions"][0]["value"])
        await self._finalize_selection(
            body["channel"]["id"], body["message"]["ts"], data["rel"],
            session_id=data["sid"], session_title=data.get("title", ""),
        )

    async def _finalize_selection(
        self, channel: str, ts: str, rel: str,
        session_id: str | None = None, session_title: str = "",
    ) -> None:
        """폴더(+세션) 선택 확정 — 스레드를 시작할 수 있는 상태로 만든다."""
        project_dir = self._claude.set_thread_project(ts, rel)
        if session_id:
            self._claude.set_thread_session(ts, session_id)
        display = self._display_path(rel)
        logger.info(
            "Project %s selected for thread %s (session=%s)",
            project_dir, ts, session_id or "new",
        )

        summary = f"*프로젝트: {display}*"
        if session_id:
            summary += f"\n:leftwards_arrow_with_hook: 세션 이어가기: _{session_title}_ (`{session_id[:8]}`)"
        await self._app.client.chat_update(
            channel=channel, ts=ts,
            text=f"프로젝트: {display}",
            blocks=[{"type": "section", "text": {"type": "mrkdwn", "text": summary}}],
        )

        model = self._claude.get_model(ts)
        effort = self._claude.get_effort(ts)
        perm = self._claude.get_permission_mode(ts)
        intro = "이어서 무엇을 할까요?" if session_id else "무엇을 도와드릴까요?"
        await self._app.client.chat_postMessage(
            channel=channel,
            thread_ts=ts,
            text=(
                f"`{display}` 폴더에서 시작합니다. {intro}\n"
                f"> :gear: *{model}* · effort *{effort}* · 권한 *{perm}*"
                f"  |  `!model`, `!effort`, `!perm`, `!settings` 로 변경 가능"
            ),
            mrkdwn=True,
        )

    # ------------------------------------------------------------------
    # New folder creation (path-based)
    # ------------------------------------------------------------------

    async def _handle_new_folder(self, ack: Any, body: dict[str, Any]) -> None:
        """'새 폴더' 버튼 — 경로 입력 모달을 연다."""
        await ack()
        rel = self._decode_rel(body["actions"][0]["value"])
        metadata = json.dumps({
            "channel": body["channel"]["id"],
            "ts": body["message"]["ts"],
            "rel": rel,
        })
        await self._app.client.views_open(
            trigger_id=body["trigger_id"],
            view={
                "type": "modal",
                "callback_id": "new_folder_modal",
                "private_metadata": metadata,
                "title": {"type": "plain_text", "text": "새 폴더"},
                "submit": {"type": "plain_text", "text": "생성 후 시작"},
                "close": {"type": "plain_text", "text": "취소"},
                "blocks": [
                    {
                        "type": "section",
                        "text": {
                            "type": "mrkdwn",
                            "text": f"현재 위치: `{self._display_path(rel)}/`",
                        },
                    },
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
            },
        )

    async def _handle_new_folder_modal(self, ack: Any, body: dict[str, Any], view: dict[str, Any]) -> None:
        """모달 제출 — 폴더를 만들고 바로 새 세션으로 시작한다."""
        raw = view["state"]["values"]["folder_path_block"]["folder_path_input"]["value"]
        raw = raw.strip().strip("/")
        segments = raw.split("/")
        if (
            not re.match(r"^[A-Za-z0-9][A-Za-z0-9._/-]*$", raw)
            or any(seg in ("", ".", "..") for seg in segments)
        ):
            await ack({
                "response_action": "errors",
                "errors": {
                    "folder_path_block": "영문자/숫자로 시작하고 영문자, 숫자, . _ - / 만 사용할 수 있습니다.",
                },
            })
            return

        meta = json.loads(view["private_metadata"])
        rel = f"{meta['rel']}/{raw}" if meta["rel"] else raw
        try:
            self._abs_path(rel)
        except ValueError:
            await ack({
                "response_action": "errors",
                "errors": {"folder_path_block": "PROJECTS_DIR 밖의 경로는 사용할 수 없습니다."},
            })
            return

        await ack()
        self._claude.create_project(rel)
        await self._finalize_selection(meta["channel"], meta["ts"], rel)

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
        if cmd == "!perm":
            return await self._cmd_change_setting(
                channel, thread_ts, message_ts, arg,
                name="권한 모드", cmd="perm", valid=VALID_PERMISSION_MODES,
                get_current="get_permission_mode", set_value="set_thread_permission_mode",
                get_default="default_permission_mode",
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
        # arg는 소문자로 정규화되어 들어오므로 대소문자 무시로 매칭 (예: acceptedits → acceptEdits)
        canon = next((v for v in valid if v.lower() == arg), None)
        if canon is None:
            await self._app.client.chat_postMessage(
                channel=channel, thread_ts=thread_ts,
                text=f":warning: 지원하지 않는 {name}입니다. 선택 가능: `{options}`",
                mrkdwn=True,
            )
            return True
        setter(thread_ts, canon)
        await self._add_reaction(channel, message_ts, "white_check_mark")
        await self._app.client.chat_postMessage(
            channel=channel, thread_ts=thread_ts,
            text=f":gear: {name}이(가) *{canon}*(으)로 변경되었습니다.",
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
        perm = self._claude.get_permission_mode(thread_ts)
        default_model = self._claude.default_model
        default_effort = self._claude.default_effort
        default_perm = self._claude.default_permission_mode
        project_dir = self._claude.get_thread_project(thread_ts) or "(미지정)"
        session_id = self._claude.get_thread_session(thread_ts)
        text = (
            f":gear: *현재 스레드 설정*\n"
            f"> 모델: *{model}*  |  effort: *{effort}*  |  권한: *{perm}*\n"
            f"> 기본값: *{default_model}* / *{default_effort}* / *{default_perm}*\n"
            f"> 프로젝트: `{project_dir}`\n"
            f"> 세션: `{session_id or '(아직 없음)'}`\n"
        )
        if session_id:
            text += (
                f"\n:house: *터미널에서 이어가기:*\n"
                f"`cd {project_dir} && claude --resume {session_id}`\n"
            )
        text += (
            f"\n*명령어:*\n"
            f"• `!model sonnet|opus|haiku` — 이 스레드 모델 변경\n"
            f"• `!effort low|medium|high|xhigh|max` — 이 스레드 effort 변경\n"
            f"• `!perm auto|acceptEdits|bypassPermissions` — 이 스레드 권한 모드 변경\n"
            f"• `!default model|effort|perm <값>` — 기본값 변경 (전체 적용)\n"
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
        if len(parts) != 2 or parts[0] not in ("model", "effort", "perm"):
            await self._app.client.chat_postMessage(
                channel=channel, thread_ts=thread_ts,
                text=":warning: 사용법: `!default model sonnet` / `!default effort high` / `!default perm auto`",
                mrkdwn=True,
            )
            return True

        kind, value = parts[0], parts[1].strip()
        valid, setter, label = {
            "model":  (VALID_MODELS,  self._claude.set_default_model,  "모델"),
            "effort": (VALID_EFFORTS, self._claude.set_default_effort, "effort"),
            "perm":   (VALID_PERMISSION_MODES, self._claude.set_default_permission_mode, "권한 모드"),
        }[kind]

        canon = next((v for v in valid if v.lower() == value.lower()), None)
        if canon is None:
            options = ", ".join(f"`{v}`" for v in valid)
            await self._app.client.chat_postMessage(
                channel=channel, thread_ts=thread_ts,
                text=f":warning: 선택 가능: {options}", mrkdwn=True,
            )
            return True

        setter(canon)
        await self._add_reaction(channel, message_ts, "white_check_mark")
        await self._app.client.chat_postMessage(
            channel=channel, thread_ts=thread_ts,
            text=f":gear: 기본 {label}이(가) *{canon}*(으)로 변경되었습니다.", mrkdwn=True,
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
    # Permission approval flow (can_use_tool → Slack buttons)
    # ------------------------------------------------------------------

    @staticmethod
    def _format_permission_summary(tool_name: str, tool_input: dict) -> str:
        """승인 메시지에 보여줄 도구 호출 요약."""
        if tool_name == "Bash":
            summary = tool_input.get("command", "")
        elif tool_name in ("Edit", "Write", "Read"):
            summary = tool_input.get("file_path", "")
        else:
            summary = json.dumps(tool_input, ensure_ascii=False)
        if len(summary) > 500:
            summary = summary[:497] + "…"
        return summary or "(입력 없음)"

    async def _finish_approval_message(
        self, channel: str, ts: str, tool_name: str, summary: str, outcome: str,
    ) -> None:
        """승인 메시지의 버튼을 제거하고 결과를 표시한다."""
        text = f":lock: *{tool_name}*\n```{summary}```\n{outcome}"
        try:
            await self._app.client.chat_update(
                channel=channel, ts=ts, text=text,
                blocks=[{"type": "section", "text": {"type": "mrkdwn", "text": text}}],
            )
        except Exception as exc:
            logger.warning("Failed to update approval message: %s", exc)

    async def _request_permission(
        self, channel: str, thread_ts: str, request: dict[str, Any],
    ) -> dict[str, Any]:
        """권한 요청을 Slack 스레드에 게시하고 사용자의 승인/거부를 기다린다.

        타임아웃(APPROVAL_TIMEOUT_SECONDS) 내에 응답이 없으면 거부한다.
        AskUserQuestion은 승인 대신 선택지 버튼으로 답변을 받아 주입한다.
        """
        tool_name = request.get("tool_name", "unknown")
        if tool_name == "AskUserQuestion":
            return await self._request_user_answers(channel, thread_ts, request)
        summary = self._format_permission_summary(tool_name, request.get("input", {}))

        approval_id = uuid.uuid4().hex[:12]
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending_approvals[approval_id] = future

        resp = await self._app.client.chat_postMessage(
            channel=channel, thread_ts=thread_ts,
            text=f"승인 필요: {tool_name} — {summary[:100]}",
            blocks=[
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": f":lock: *승인이 필요합니다* — *{tool_name}*\n```{summary}```",
                    },
                },
                {
                    "type": "actions",
                    "elements": [
                        {
                            "type": "button",
                            "text": {"type": "plain_text", "text": "✅ 승인", "emoji": True},
                            "action_id": "perm_allow",
                            "value": approval_id,
                            "style": "primary",
                        },
                        {
                            "type": "button",
                            "text": {"type": "plain_text", "text": "🚫 거부", "emoji": True},
                            "action_id": "perm_deny",
                            "value": approval_id,
                            "style": "danger",
                        },
                    ],
                },
            ],
        )
        msg_ts = resp["ts"]
        logger.info("Permission request %s posted for thread %s: %s %s",
                    approval_id, thread_ts, tool_name, summary[:100])

        try:
            try:
                allowed, user_id = await asyncio.wait_for(
                    future, timeout=APPROVAL_TIMEOUT_SECONDS,
                )
            except asyncio.TimeoutError:
                await self._finish_approval_message(
                    channel, msg_ts, tool_name, summary,
                    f":hourglass: {APPROVAL_TIMEOUT_SECONDS // 60}분 내 응답이 없어 자동 거부되었습니다.",
                )
                return {
                    "behavior": "deny",
                    "message": "Slack에서 제한 시간 내 승인을 받지 못했습니다. "
                               "다른 방법으로 진행하거나 사용자에게 확인하세요.",
                }
            who = f"<@{user_id}>" if user_id else "사용자"
            if allowed:
                await self._finish_approval_message(
                    channel, msg_ts, tool_name, summary,
                    f":white_check_mark: {who} 승인",
                )
                return {"behavior": "allow"}
            await self._finish_approval_message(
                channel, msg_ts, tool_name, summary,
                f":no_entry_sign: {who} 거부",
            )
            return {
                "behavior": "deny",
                "message": "사용자가 Slack에서 이 작업을 거부했습니다.",
            }
        except asyncio.CancelledError:
            # 작업 중단/프로세스 종료로 승인 대기가 취소된 경우
            try:
                await asyncio.shield(self._finish_approval_message(
                    channel, msg_ts, tool_name, summary,
                    ":heavy_minus_sign: 작업이 종료되어 무효화되었습니다.",
                ))
            except Exception:
                pass
            raise
        finally:
            self._pending_approvals.pop(approval_id, None)

    async def _handle_perm_decision(self, ack: Any, body: dict[str, Any]) -> None:
        """승인/거부 버튼 클릭 — 대기 중인 승인 future를 resolve한다."""
        await ack()
        action = body["actions"][0]
        approval_id = action["value"]
        allowed = action["action_id"] == "perm_allow"
        user_id = body.get("user", {}).get("id", "")

        future = self._pending_approvals.get(approval_id)
        if future is None or future.done():
            logger.info("Approval %s already resolved or expired.", approval_id)
            return
        future.set_result((allowed, user_id))

    # ------------------------------------------------------------------
    # AskUserQuestion flow (질문 → Slack 선택지 버튼 → 답변 주입)
    # ------------------------------------------------------------------

    async def _request_user_answers(
        self, channel: str, thread_ts: str, request: dict[str, Any],
    ) -> dict[str, Any]:
        """AskUserQuestion의 질문들을 Slack 버튼으로 묻고 답변을 주입한다.

        시간 내 응답이 없으면 답변 없이 allow하여 Claude가 질문을
        텍스트로 남기고 턴을 마무리하게 한다.
        """
        tool_input = request.get("input", {})
        questions = tool_input.get("questions", [])
        answers: dict[str, str] = {}

        for q in questions:
            answer = await self._ask_single_question(channel, thread_ts, q)
            if answer is None:  # 타임아웃 — 부분 답변으로는 진행하지 않는다
                return {"behavior": "allow"}
            answers[q.get("question", "")] = answer

        updated = dict(tool_input)
        updated["answers"] = answers
        return {"behavior": "allow", "updatedInput": updated}

    async def _ask_single_question(
        self, channel: str, thread_ts: str, q: dict[str, Any],
    ) -> str | None:
        """질문 하나를 게시하고 선택된 옵션 label을 반환한다 (타임아웃 시 None)."""
        question = q.get("question", "")
        header = q.get("header", "")
        options = q.get("options", [])

        approval_id = uuid.uuid4().hex[:12]
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending_approvals[approval_id] = future

        title = f":speech_balloon: *{question}*"
        if header:
            title = f":speech_balloon: *[{header}]* {question}"
        blocks: list[dict] = [
            {"type": "section", "text": {"type": "mrkdwn", "text": title}},
        ]
        desc_lines = [
            f"• *{o.get('label', '')}* — {o.get('description', '')}"
            for o in options if o.get("description")
        ]
        if desc_lines:
            blocks.append({
                "type": "section",
                "text": {"type": "mrkdwn", "text": "\n".join(desc_lines)},
            })
        if q.get("multiSelect"):
            blocks.append({
                "type": "context",
                "elements": [{"type": "mrkdwn", "text": "복수 선택 질문이지만 Slack에서는 하나만 선택할 수 있습니다."}],
            })
        blocks.append({
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": o.get("label", "?")[:75], "emoji": True},
                    "action_id": f"answer:{i}",
                    "value": json.dumps({"aid": approval_id, "label": o.get("label", "")}),
                }
                for i, o in enumerate(options[:5])
            ],
        })

        resp = await self._app.client.chat_postMessage(
            channel=channel, thread_ts=thread_ts,
            text=f"질문: {question}", blocks=blocks,
        )
        msg_ts = resp["ts"]
        logger.info("Question %s posted for thread %s: %s", approval_id, thread_ts, question[:80])

        try:
            try:
                label, user_id = await asyncio.wait_for(
                    future, timeout=APPROVAL_TIMEOUT_SECONDS,
                )
            except asyncio.TimeoutError:
                await self._finish_question_message(
                    channel, msg_ts, title,
                    f":hourglass: {APPROVAL_TIMEOUT_SECONDS // 60}분 내 응답이 없었습니다. 스레드에 답장으로 답변해 주세요.",
                )
                return None
            who = f"<@{user_id}>" if user_id else "사용자"
            await self._finish_question_message(
                channel, msg_ts, title, f":white_check_mark: {who} 답변: *{label}*",
            )
            return label
        except asyncio.CancelledError:
            try:
                await asyncio.shield(self._finish_question_message(
                    channel, msg_ts, title, ":heavy_minus_sign: 작업이 종료되어 무효화되었습니다.",
                ))
            except Exception:
                pass
            raise
        finally:
            self._pending_approvals.pop(approval_id, None)

    async def _finish_question_message(
        self, channel: str, ts: str, title: str, outcome: str,
    ) -> None:
        """질문 메시지의 버튼을 제거하고 결과를 표시한다."""
        text = f"{title}\n{outcome}"
        try:
            await self._app.client.chat_update(
                channel=channel, ts=ts, text=text,
                blocks=[{"type": "section", "text": {"type": "mrkdwn", "text": text}}],
            )
        except Exception as exc:
            logger.warning("Failed to update question message: %s", exc)

    async def _handle_question_answer(self, ack: Any, body: dict[str, Any]) -> None:
        """질문 옵션 버튼 클릭 — 선택된 label로 future를 resolve한다."""
        await ack()
        data = json.loads(body["actions"][0]["value"])
        user_id = body.get("user", {}).get("id", "")
        future = self._pending_approvals.get(data["aid"])
        if future is None or future.done():
            logger.info("Question %s already answered or expired.", data["aid"])
            return
        future.set_result((data["label"], user_id))

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

        # 승인/질문 메시지가 진행 메시지보다 아래에 게시되므로, 인터랙션이
        # 있었던 턴은 최종 응답을 in-place로 바꾸면 시간 순서가 뒤집힌다.
        interacted = False

        async def on_permission(request: dict[str, Any]) -> dict[str, Any]:
            nonlocal interacted
            interacted = True
            return await self._request_permission(channel, thread_ts, request)

        try:
            result = await self._claude.handle_thread_reply(
                channel, thread_ts, text,
                on_event=poster.handle_event, on_permission=on_permission,
            )
            progress_ts = await poster.flush()
            usage_footer = self._format_usage_footer(result)
            await self._post_response(
                channel, thread_ts, result.text,
                progress_ts=progress_ts, usage_footer=usage_footer,
                in_place=not interacted,
            )
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

    @staticmethod
    def _format_usage_footer(result: ClaudeResult) -> str:
        """Format a usage/cost summary line. Returns empty string if no usage."""
        if result.total_cost_usd == 0 and result.input_tokens == 0:
            return ""

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

        return "".join(parts)

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
        self, channel: str, thread_ts: str, text: str, *,
        progress_ts: str | None = None, usage_footer: str = "",
        in_place: bool = True,
    ) -> None:
        """Post a response to Slack, splitting if it exceeds the message length limit.

        If *progress_ts* is provided and the response fits in a single message,
        the progress message is updated in-place for a seamless transition.
        Pass ``in_place=False`` to always post at the bottom instead (used when
        approval/question messages were posted mid-run — updating in place would
        put the final response above them, breaking chronological order).
        For multi-chunk or file responses, the progress message is deleted first.
        """
        text = self._markdown_to_slack(text)

        if not text or not text.strip():
            await self._delete_progress(channel, progress_ts)
            return

        footer_suffix = "\n\n" + usage_footer if usage_footer else ""

        chunks = self._split_message(text, SLACK_MAX_MESSAGE_LENGTH)

        # Single chunk — update progress message in-place if available
        if len(chunks) == 1 and progress_ts and in_place:
            try:
                await self._app.client.chat_update(
                    channel=channel, ts=progress_ts, text=chunks[0] + footer_suffix, mrkdwn=True,
                )
                return
            except Exception:
                pass  # Fall through to normal post

        await self._delete_progress(channel, progress_ts)

        # If it's too many chunks, upload as a file instead
        if len(chunks) > 3:
            await self._app.client.chat_postMessage(
                channel=channel, thread_ts=thread_ts,
                text=text[:3000] + "\n\n_(전체 응답은 파일로 첨부되었습니다)_" + footer_suffix,
                mrkdwn=True,
            )
            await self._app.client.files_upload_v2(
                channel=channel, thread_ts=thread_ts,
                content=text, filename="response.md",
                title="전체 응답",
            )
            return

        for i, chunk in enumerate(chunks):
            if i == len(chunks) - 1:
                chunk += footer_suffix
            await self._app.client.chat_postMessage(
                channel=channel, thread_ts=thread_ts, text=chunk, mrkdwn=True,
            )

    # ------------------------------------------------------------------
    # Startup
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Start the Slack Socket Mode handler."""
        await self._claude.initialize()
        self._bot_user_id = self._claude._bot_user_id
        await self._handler.start_async()
