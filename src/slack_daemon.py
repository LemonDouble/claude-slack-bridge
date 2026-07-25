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

화면(blocks) 구성은 ``slack_blocks``, 텍스트 변환은 ``slack_format``에 있다.
여기에는 Slack 이벤트 라우팅과 상태를 다루는 코드만 둔다.
"""

import asyncio
import json
import logging
import re
import uuid
from collections import deque
from collections.abc import Callable
from pathlib import Path
from typing import Any

from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler
from slack_bolt.async_app import AsyncApp

import slack_blocks as blocks
import slack_format as fmt
from claude_handler import ClaudeHandler
from constants import (
    APPROVAL_TIMEOUT_SECONDS,
    CANCEL_REACTION,
    PROJECTS_ROOT,
    REWIND_REACTION,
    SLACK_MAX_MESSAGE_LENGTH,
    VALID_EFFORTS,
    VALID_MODELS,
    VALID_PERMISSION_MODES,
)
from event_poster import EventPoster
from file_downloader import format_file_metadata
from session_catalog import build_recap, list_sessions

logger = logging.getLogger(__name__)

_MAX_FOLDER_BUTTONS = 40
_SESSION_LIST_LIMIT = 5
_MAX_RESPONSE_CHUNKS = 3      # 이보다 길면 파일로 첨부한다
_REWIND_FILES_SHOWN = 8       # 되돌리기 안내에 나열할 파일 수

# !model / !effort / !perm / !default 처리용: kind → (표시 이름, 허용 값)
_SETTINGS: dict[str, tuple[str, tuple[str, ...]]] = {
    "model": ("모델", VALID_MODELS),
    "effort": ("effort", VALID_EFFORTS),
    "perm": ("권한 모드", VALID_PERMISSION_MODES),
}


class SlackDaemon:
    """
    Handles Human→Claude messages via the Claude Code CLI, with a
    folder-browser UI for project selection and session resume.

    Args:
        bot_token: Slack bot OAuth token (xoxb-...).
        app_token: Slack app-level token for Socket Mode (xapp-...).
    """

    def __init__(self, bot_token: str, app_token: str, idle_timeout_minutes: int) -> None:
        self._app = AsyncApp(token=bot_token)
        self._handler = AsyncSocketModeHandler(self._app, app_token)
        self._claude = ClaudeHandler(
            slack_client=self._app.client,
            idle_timeout_minutes=idle_timeout_minutes,
        )
        self._active_threads: set[str] = set()
        self._thread_queues: dict[str, deque] = {}
        self._pending_approvals: dict[str, asyncio.Future] = {}
        # thread_ts → 마지막으로 요청한 사용자. 완료/승인 알림 멘션에 쓴다.
        self._thread_users: dict[str, str] = {}
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
        self._app.action(re.compile(r"^set:(model|effort|perm)$"))(self._handle_setting_select)
        self._app.action(re.compile(r"^rewind:(conv|files|both|cancel)$"))(self._handle_rewind_choice)

    # ------------------------------------------------------------------
    # Slack 기본 동작 (게시 / 수정 / 리액션 / 멘션)
    # ------------------------------------------------------------------

    async def _say(
        self, channel: str, thread_ts: str, text: str, blocks_: list[dict] | None = None,
    ) -> str | None:
        """스레드에 메시지를 게시하고 ts를 반환한다 (실패해도 흐름을 막지 않는다)."""
        try:
            resp = await self._app.client.chat_postMessage(
                channel=channel, thread_ts=thread_ts, text=text,
                blocks=blocks_, mrkdwn=True,
            )
            return resp["ts"]
        except Exception as exc:
            logger.warning("Failed to post message: %s", exc)
            return None

    async def _replace_message(self, channel: str, ts: str, text: str) -> None:
        """메시지의 블록(버튼)을 제거하고 텍스트로 교체한다."""
        try:
            await self._app.client.chat_update(
                channel=channel, ts=ts, text=text, blocks=[blocks.section(text)],
            )
        except Exception as exc:
            logger.warning("Failed to update interactive message: %s", exc)

    async def _delete_message(self, channel: str, ts: str | None) -> None:
        """메시지를 삭제한다 (없거나 실패해도 무시)."""
        if not ts:
            return
        try:
            await self._app.client.chat_delete(channel=channel, ts=ts)
        except Exception:
            pass

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

    def _mention(self, thread_ts: str) -> str:
        """스레드에서 마지막으로 요청한 사용자의 멘션 (없으면 빈 문자열).

        Slack은 ``chat.update``로 수정된 메시지에는 알림을 보내지 않으므로,
        작업 완료·승인 요청은 멘션이 포함된 *새* 메시지로 게시해야 사용자가
        알림을 받는다.
        """
        user_id = self._thread_users.get(thread_ts)
        return f"<@{user_id}> " if user_id else ""

    # ------------------------------------------------------------------
    # 폴더 브라우저 + 세션 선택
    # ------------------------------------------------------------------

    @staticmethod
    def _abs_path(rel: str) -> Path:
        """rel 경로를 PROJECTS_ROOT 기준 절대 경로로 변환한다 (탈출 방지 검증 포함)."""
        root = PROJECTS_ROOT.resolve()
        path = (root / rel).resolve() if rel else root
        path.relative_to(root)  # PROJECTS_ROOT 밖이면 ValueError
        return path

    @staticmethod
    def _display_path(rel: str) -> str:
        return f"{PROJECTS_ROOT.name}/{rel}" if rel else PROJECTS_ROOT.name

    def _browser_blocks(self, rel: str) -> list[dict]:
        """rel 폴더의 하위 디렉토리를 읽어 브라우저 화면을 만든다."""
        subdirs = sorted(
            d.name for d in self._abs_path(rel).iterdir()
            if d.is_dir() and not d.name.startswith(".")
        )
        truncated = len(subdirs) > _MAX_FOLDER_BUTTONS
        return blocks.browser_blocks(
            self._display_path(rel), rel, subdirs[:_MAX_FOLDER_BUTTONS], truncated,
        )

    async def _update_browser(self, channel: str, ts: str, rel: str) -> None:
        """브라우저 메시지를 rel 폴더 화면으로 갱신한다 (경로 오류 시 루트로)."""
        try:
            view = self._browser_blocks(rel)
        except (ValueError, FileNotFoundError, NotADirectoryError):
            logger.warning("Invalid browse path %r, falling back to root.", rel)
            view = self._browser_blocks("")
        await self._app.client.chat_update(
            channel=channel, ts=ts, text="프로젝트 폴더를 선택하세요:", blocks=view,
        )

    async def _handle_nav(self, ack: Any, body: dict[str, Any]) -> None:
        """폴더 버튼 클릭 / 세션 화면에서 폴더 목록으로 복귀."""
        await ack()
        rel = blocks.decode_rel(body["actions"][0]["value"])
        await self._update_browser(body["channel"]["id"], body["message"]["ts"], rel)

    async def _handle_nav_up(self, ack: Any, body: dict[str, Any]) -> None:
        """상위 폴더로 이동."""
        await ack()
        rel = blocks.decode_rel(body["actions"][0]["value"])
        parent = rel.rsplit("/", 1)[0] if "/" in rel else ""
        await self._update_browser(body["channel"]["id"], body["message"]["ts"], parent)

    async def _handle_start_here(self, ack: Any, body: dict[str, Any]) -> None:
        """현재 폴더에서 시작 — 기존 세션이 있으면 세션 선택 화면을 먼저 보여준다."""
        await ack()
        rel = blocks.decode_rel(body["actions"][0]["value"])
        channel, ts = body["channel"]["id"], body["message"]["ts"]

        sessions = list_sessions(str(self._abs_path(rel)), limit=_SESSION_LIST_LIMIT)
        if not sessions:
            await self._finalize_selection(channel, ts, rel)
            return
        await self._app.client.chat_update(
            channel=channel, ts=ts, text="세션을 선택하세요:",
            blocks=blocks.session_blocks(self._display_path(rel), rel, sessions),
        )

    async def _handle_new_session(self, ack: Any, body: dict[str, Any]) -> None:
        await ack()
        rel = blocks.decode_rel(body["actions"][0]["value"])
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
            channel=channel, ts=ts, text=f"프로젝트: {display}",
            blocks=[blocks.section(summary)],
        )

        model = self._claude.get_setting(ts, "model")
        effort = self._claude.get_setting(ts, "effort")
        perm = self._claude.get_setting(ts, "perm")
        intro = "이어서 무엇을 할까요?" if session_id else "무엇을 도와드릴까요?"
        await self._say(channel, ts, (
            f"{await self._recap_text(project_dir, session_id)}"
            f"`{display}` 폴더에서 시작합니다. {intro}\n"
            f"> :gear: *{model}* · effort *{effort}* · 권한 *{perm}*"
            f"  |  `!settings` 로 변경 · :{REWIND_REACTION}: 리액션으로 되돌리기"
        ))

    async def _recap_text(self, project_dir: str, session_id: str | None) -> str:
        """이어가는 세션의 직전 진행 상황 발췌 (없으면 빈 문자열).

        트랜스크립트 파싱은 블로킹이므로 별도 스레드에서 수행한다.
        """
        if not session_id or not project_dir:
            return ""
        try:
            recap = await asyncio.to_thread(build_recap, project_dir, session_id)
        except Exception as exc:
            logger.warning("Failed to build session recap: %s", exc)
            return ""
        if recap is None:
            return ""

        lines: list[str] = []
        if recap.prompts:
            shown, total = len(recap.prompts), recap.turn_count
            scope = f"{total}턴 중 최근 {shown}개" if total > shown else f"{total}턴"
            lines.append(f":clipboard: *지금까지의 요청* ({scope})")
            lines += [f"> • {fmt.plain(p)}" for p in recap.prompts]
        if recap.last_response:
            if lines:
                lines.append("")
            lines.append(":speech_balloon: *마지막 응답*")
            lines.append(f"> {fmt.plain(recap.last_response)}")
        return "\n".join(lines) + "\n\n" if lines else ""

    # ------------------------------------------------------------------
    # New folder creation (path-based)
    # ------------------------------------------------------------------

    async def _handle_new_folder(self, ack: Any, body: dict[str, Any]) -> None:
        """'새 폴더' 버튼 — 경로 입력 모달을 연다."""
        await ack()
        rel = blocks.decode_rel(body["actions"][0]["value"])
        await self._app.client.views_open(
            trigger_id=body["trigger_id"],
            view=blocks.new_folder_view(
                self._display_path(rel),
                json.dumps({
                    "channel": body["channel"]["id"],
                    "ts": body["message"]["ts"],
                    "rel": rel,
                }),
            ),
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
    # Thread commands (!model, !effort, !settings, !default, !restart)
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

        if cmd[1:] in _SETTINGS:
            return await self._cmd_change_setting(channel, thread_ts, message_ts, cmd[1:], arg)
        if cmd in ("!settings", "!help"):
            return await self._cmd_settings(channel, thread_ts)
        if cmd == "!default":
            return await self._cmd_default(channel, thread_ts, message_ts, arg)
        if cmd == "!restart":
            return await self._cmd_restart(channel, thread_ts, message_ts, arg)

        return False

    @staticmethod
    def _canonical(kind: str, value: str) -> str | None:
        """입력값을 허용 값 중 하나로 정규화한다 (대소문자 무시, 없으면 None)."""
        _name, valid = _SETTINGS[kind]
        return next((v for v in valid if v.lower() == value.lower()), None)

    def _setting_block(self, thread_ts: str, kind: str, label: str) -> dict:
        """해당 설정의 드롭다운 블록."""
        name, valid = _SETTINGS[kind]
        return blocks.setting_select(
            kind, name, valid, self._claude.get_setting(thread_ts, kind), label,
        )

    async def _cmd_change_setting(
        self, channel: str, thread_ts: str, message_ts: str, kind: str, arg: str,
    ) -> bool:
        """!model / !effort / !perm 공통 핸들러."""
        name, valid = _SETTINGS[kind]

        if not arg:
            # 값을 외울 필요 없도록 선택 가능한 옵션을 드롭다운으로 보여준다.
            text = f":gear: {name}을(를) 선택하세요 (기본값: *{self._claude.get_default(kind)}*)"
            await self._say(channel, thread_ts, text, [self._setting_block(thread_ts, kind, text)])
            return True

        canon = self._canonical(kind, arg)
        if canon is None:
            await self._say(
                channel, thread_ts,
                f":warning: 지원하지 않는 {name}입니다. 선택 가능: `{' | '.join(valid)}`",
            )
            return True
        self._claude.set_setting(thread_ts, kind, canon)
        await self._add_reaction(channel, message_ts, "white_check_mark")
        await self._say(channel, thread_ts, f":gear: {name}이(가) *{canon}*(으)로 변경되었습니다.")
        return True

    async def _handle_setting_select(self, ack: Any, body: dict[str, Any]) -> None:
        """드롭다운 선택 — 해당 스레드의 설정을 변경한다."""
        await ack()
        action = body["actions"][0]
        kind = action["action_id"].split(":", 1)[1]
        value = action["selected_option"]["value"]
        thread_ts = body["message"].get("thread_ts") or body["message"]["ts"]
        name, _valid = _SETTINGS[kind]

        self._claude.set_setting(thread_ts, kind, value)
        logger.info("Setting %s=%s for thread %s via dropdown", kind, value, thread_ts)
        who = body.get("user", {}).get("id", "")
        await self._say(
            body["channel"]["id"], thread_ts,
            f":gear: <@{who}> {name}을(를) *{value}*(으)로 변경했습니다.",
        )

    async def _cmd_settings(self, channel: str, thread_ts: str) -> bool:
        """현재 설정 + 드롭다운 + 명령어 안내."""
        project_dir = self._claude.get_thread_project(thread_ts) or "(미지정)"
        session_id = self._claude.get_thread_session(thread_ts)
        defaults = " / ".join(f"*{self._claude.get_default(k)}*" for k in _SETTINGS)
        header = (
            f":gear: *현재 스레드 설정* — 아래에서 바로 바꿀 수 있습니다\n"
            f"> 기본값: {defaults}\n"
            f"> 프로젝트: `{project_dir}`\n"
            f"> 세션: `{session_id or '(아직 없음)'}`"
        )
        view: list[dict] = [blocks.section(header)]
        for kind, (name, _valid) in _SETTINGS.items():
            current = self._claude.get_setting(thread_ts, kind)
            view.append(self._setting_block(thread_ts, kind, f"*{name}* — 현재 `{current}`"))

        footer = ""
        if session_id:
            footer += (
                f":house: *터미널에서 이어가기*\n"
                f"`cd {project_dir} && claude --resume {session_id}`\n"
                f"_(프로젝트 폴더에서 `claude --resume` 만 실행해 목록에서 골라도 됩니다)_\n\n"
            )
        footer += (
            "*명령어*\n"
            "• `!model` / `!effort` / `!perm` — 위 드롭다운을 개별로 표시 (인자를 주면 바로 변경)\n"
            "• `!default model|effort|perm <값>` — 기본값 변경 (전체 적용)\n"
            "• `!restart` — 세션 재시작 (현재 작업 중단 후 새 세션으로 이어서 진행)\n"
            f"• :{REWIND_REACTION}: 리액션 — 그 메시지가 속한 턴 직전으로 되돌리기\n"
            f"• :{CANCEL_REACTION}: 리액션 — 진행 중인 작업 중단"
        )
        view.append(blocks.section(footer))

        await self._say(channel, thread_ts, header, view)
        return True

    async def _cmd_default(
        self, channel: str, thread_ts: str, message_ts: str, arg: str,
    ) -> bool:
        """Handle !default model <val> or !default effort <val>."""
        parts = arg.split(None, 1)
        if len(parts) != 2 or parts[0] not in _SETTINGS:
            await self._say(
                channel, thread_ts,
                ":warning: 사용법: `!default model sonnet` / `!default effort high` / `!default perm auto`",
            )
            return True

        kind, value = parts[0], parts[1].strip()
        name, valid = _SETTINGS[kind]
        canon = self._canonical(kind, value)
        if canon is None:
            options = ", ".join(f"`{v}`" for v in valid)
            await self._say(channel, thread_ts, f":warning: 선택 가능: {options}")
            return True

        self._claude.set_default(kind, canon)
        await self._add_reaction(channel, message_ts, "white_check_mark")
        await self._say(
            channel, thread_ts, f":gear: 기본 {name}이(가) *{canon}*(으)로 변경되었습니다.",
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
        await self._say(channel, thread_ts, ":arrows_counterclockwise: 세션을 재시작합니다...")

        restart_prompt = arg if arg else "이전 대화 내용을 참고해서, 이어서 작업을 계속 진행해줘."
        asyncio.create_task(
            self._handle_claude_thread_reply(channel, thread_ts, restart_prompt, message_ts)
        )
        return True

    # ------------------------------------------------------------------
    # 리액션 (:x: 중단 / :rewind: 되돌리기)
    # ------------------------------------------------------------------

    async def _resolve_thread_root(self, channel: str, message_ts: str) -> str | None:
        """메시지가 속한 스레드의 루트 ts를 반환한다 (브릿지가 아는 스레드만)."""
        if self._claude.get_thread_project(message_ts):
            return message_ts
        try:
            resp = await self._app.client.conversations_replies(
                channel=channel, ts=message_ts, limit=1,
            )
            msgs = resp.get("messages", [])
        except Exception as exc:
            logger.debug("Failed to resolve thread for reaction: %s", exc)
            return None
        if not msgs:
            return None
        root_ts = msgs[0].get("thread_ts", message_ts)
        return root_ts if self._claude.get_thread_project(root_ts) else None

    async def _handle_reaction_added(self, event: dict) -> None:
        """reaction_added 처리 — :x:는 작업 중단, :rewind:는 되돌리기."""
        reaction = event.get("reaction")
        if reaction not in (CANCEL_REACTION, REWIND_REACTION):
            return
        item = event.get("item", {})
        if item.get("type") != "message":
            return
        channel, message_ts = item.get("channel", ""), item.get("ts", "")

        thread_ts = await self._resolve_thread_root(channel, message_ts)
        if not thread_ts:
            return

        if reaction == REWIND_REACTION:
            await self._offer_rewind(channel, thread_ts, message_ts)
            return

        if thread_ts not in self._active_threads:
            return
        logger.info("Cancel requested via :x: reaction for thread %s", thread_ts)
        if await self._claude.cancel_thread(thread_ts):
            await self._say(channel, thread_ts, ":no_entry_sign: 작업이 중단되었습니다.")

    async def _offer_rewind(self, channel: str, thread_ts: str, message_ts: str) -> None:
        """되돌릴 턴을 확정하고 범위 선택 버튼을 게시한다."""
        turn = self._claude.find_turn(thread_ts, message_ts)
        if turn is None:
            await self._say(
                channel, thread_ts,
                ":rewind: 되돌릴 지점을 찾지 못했습니다. 되돌리려는 요청 이후의 메시지에 리액션을 달아주세요.",
            )
            return
        if thread_ts in self._active_threads:
            await self._say(
                channel, thread_ts,
                f":rewind: 작업이 진행 중입니다. :{CANCEL_REACTION}: 로 먼저 중단한 뒤 되돌려주세요.",
            )
            return

        can_conv = self._claude.resolve_resume_point(thread_ts, turn) is not None
        files: list[str] = turn.get("files", [])
        if not can_conv and not files:
            # 세션의 첫 턴이라 되돌릴 기준점도, 복구할 파일도 없다.
            await self._say(channel, thread_ts, (
                ":rewind: 이 턴은 세션의 시작이라 되돌릴 지점이 없습니다.\n"
                "처음부터 다시 하려면 `!restart` 를 사용하세요."
            ))
            return

        lines = [
            ":rewind: *이 지점으로 되돌립니다*",
            f"> _{turn.get('text') or '(내용 없음)'}_",
            f"> 복구 대상: {self._describe_files(files)}",
        ]
        if not can_conv:
            lines.append("> :warning: 이 턴은 되돌릴 기준점이 없어 대화는 되돌릴 수 없습니다.")
        text = "\n".join(lines)

        # 어떤 턴인지는 turn의 slack_ts로 다시 찾는다 (버튼 value 길이 제한 회피).
        value = json.dumps({"t": thread_ts, "s": turn["slack_ts"]})
        await self._say(
            channel, thread_ts, text,
            blocks.rewind_blocks(text, value, can_conv, bool(files)),
        )

    @staticmethod
    def _describe_files(files: list[str]) -> str:
        """되돌리기 안내에 쓸 파일 목록 요약."""
        if not files:
            return "_이 턴에서 수정된 파일 없음_"
        shown = ", ".join(f"`{Path(f).name}`" for f in files[:_REWIND_FILES_SHOWN])
        extra = len(files) - _REWIND_FILES_SHOWN
        return f"{shown} 외 {extra}개" if extra > 0 else shown

    async def _handle_rewind_choice(self, ack: Any, body: dict[str, Any]) -> None:
        """되돌리기 범위 버튼 클릭 — 선택한 범위대로 실행한다."""
        await ack()
        action = body["actions"][0]
        scope = action["action_id"].split(":", 1)[1]
        data = json.loads(action["value"])
        thread_ts, slack_ts = data["t"], data["s"]
        channel, msg_ts = body["channel"]["id"], body["message"]["ts"]
        who = f"<@{body.get('user', {}).get('id', '')}>"

        if scope == "cancel":
            await self._replace_message(channel, msg_ts, ":rewind: 되돌리기를 취소했습니다.")
            return

        turn = self._claude.find_turn(thread_ts, str(slack_ts))
        if turn is None or turn["slack_ts"] != slack_ts:
            await self._replace_message(
                channel, msg_ts, ":rewind: 대상 턴이 더 이상 유효하지 않습니다.",
            )
            return

        results: list[str] = []
        if scope in ("files", "both"):
            ok, detail = await self._claude.rewind_files(thread_ts, turn)
            results.append(
                f":file_folder: 코드 되돌림 — {len(turn.get('files', []))}개 파일 복구"
                if ok else f":warning: 코드 되돌리기 실패 — {detail}"
            )
        if scope in ("conv", "both"):
            resume_at = self._claude.resolve_resume_point(thread_ts, turn)
            if resume_at:
                # rewind_conversation이 턴 기록을 잘라내므로 파일 되돌리기 이후에 호출한다.
                self._claude.rewind_conversation(thread_ts, turn, resume_at)
                results.append(":speech_balloon: 대화 되돌림 — 다음 메시지부터 이 지점에서 이어집니다")
            else:
                results.append(":warning: 대화 되돌리기 실패 — 기준점을 찾지 못했습니다")

        summary = "\n".join(f"> {line}" for line in results)
        await self._replace_message(
            channel, msg_ts,
            f":rewind: *{who} 되돌리기* — _{turn.get('text') or ''}_\n{summary}",
        )

    # ------------------------------------------------------------------
    # Permission approval flow (can_use_tool → Slack buttons)
    # ------------------------------------------------------------------

    async def _post_buttons_and_wait(
        self, channel: str, thread_ts: str, *,
        approval_id: str, text: str, blocks_: list[dict],
        finish_base: str, timeout_outcome: str,
        outcome: Callable[[Any, str], str],
    ) -> Any | None:
        """버튼 메시지를 게시하고 클릭(또는 타임아웃)을 기다린다.

        클릭 시 버튼을 결과 텍스트로 교체하고 future의 결과값을 반환한다.
        타임아웃 시 timeout_outcome을 표시하고 None을 반환한다.
        작업 중단으로 대기가 취소되면 메시지를 무효화 표시 후 CancelledError를 다시 던진다.
        outcome은 (결과값, 사용자 멘션)을 받아 결과 표시 텍스트를 만든다.
        """
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending_approvals[approval_id] = future
        msg_ts: str | None = None

        try:
            resp = await self._app.client.chat_postMessage(
                channel=channel, thread_ts=thread_ts, text=text, blocks=blocks_,
            )
            msg_ts = resp["ts"]
            try:
                result, user_id = await asyncio.wait_for(
                    future, timeout=APPROVAL_TIMEOUT_SECONDS,
                )
            except TimeoutError:
                await self._replace_message(channel, msg_ts, f"{finish_base}\n{timeout_outcome}")
                return None
            who = f"<@{user_id}>" if user_id else "사용자"
            await self._replace_message(channel, msg_ts, f"{finish_base}\n{outcome(result, who)}")
            return result
        except asyncio.CancelledError:
            # 작업 중단/프로세스 종료로 대기가 취소된 경우
            if msg_ts:
                try:
                    await asyncio.shield(self._replace_message(
                        channel, msg_ts,
                        f"{finish_base}\n:heavy_minus_sign: 작업이 종료되어 무효화되었습니다.",
                    ))
                except Exception:
                    pass
            raise
        finally:
            self._pending_approvals.pop(approval_id, None)

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
        summary = fmt.permission_summary(tool_name, request.get("input", {}))

        approval_id = uuid.uuid4().hex[:12]
        mention = self._mention(thread_ts)
        view = [
            blocks.section(f":lock: {mention}*승인이 필요합니다* — *{tool_name}*\n```{summary}```"),
            blocks.actions(
                blocks.button("✅ 승인", "perm_allow", approval_id, style="primary"),
                blocks.button("🚫 거부", "perm_deny", approval_id, style="danger"),
            ),
        ]
        logger.info("Permission request %s posted for thread %s: %s %s",
                    approval_id, thread_ts, tool_name, summary[:100])

        allowed = await self._post_buttons_and_wait(
            channel, thread_ts,
            approval_id=approval_id,
            # text는 알림 미리보기로도 쓰이므로 멘션을 함께 넣는다.
            text=f"{mention}승인 필요: {tool_name} — {summary[:100]}",
            blocks_=view,
            finish_base=f":lock: *{tool_name}*\n```{summary}```",
            timeout_outcome=f":hourglass: {APPROVAL_TIMEOUT_SECONDS // 60}분 내 응답이 없어 자동 거부되었습니다.",
            outcome=lambda allowed, who: (
                f":white_check_mark: {who} 승인" if allowed else f":no_entry_sign: {who} 거부"
            ),
        )
        if allowed is None:
            return {
                "behavior": "deny",
                "message": "Slack에서 제한 시간 내 승인을 받지 못했습니다. "
                           "다른 방법으로 진행하거나 사용자에게 확인하세요.",
            }
        if allowed:
            return {"behavior": "allow"}
        return {
            "behavior": "deny",
            "message": "사용자가 Slack에서 이 작업을 거부했습니다.",
        }

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
        answers: dict[str, str] = {}

        for q in tool_input.get("questions", []):
            answer = await self._ask_single_question(channel, thread_ts, q)
            if answer is None:  # 타임아웃 — 부분 답변으로는 진행하지 않는다
                return {"behavior": "allow"}
            answers[q.get("question", "")] = answer

        return {"behavior": "allow", "updatedInput": {**tool_input, "answers": answers}}

    async def _ask_single_question(
        self, channel: str, thread_ts: str, q: dict[str, Any],
    ) -> str | None:
        """질문 하나를 게시하고 선택된 옵션 label을 반환한다 (타임아웃 시 None)."""
        question = q.get("question", "")
        header = q.get("header", "")
        options = q.get("options", [])

        approval_id = uuid.uuid4().hex[:12]
        title = f":speech_balloon: *[{header}]* {question}" if header else f":speech_balloon: *{question}*"
        # 최초 게시에만 멘션을 붙인다 (버튼 클릭 후 남는 텍스트에는 불필요).
        mention = self._mention(thread_ts)
        view = [blocks.section(
            title.replace(":speech_balloon: ", f":speech_balloon: {mention}", 1)
        )]
        desc_lines = [
            f"• *{o.get('label', '')}* — {o.get('description', '')}"
            for o in options if o.get("description")
        ]
        if desc_lines:
            view.append(blocks.section("\n".join(desc_lines)))
        if q.get("multiSelect"):
            view.append({
                "type": "context",
                "elements": [{
                    "type": "mrkdwn",
                    "text": "복수 선택 질문이지만 Slack에서는 하나만 선택할 수 있습니다.",
                }],
            })
        view.append(blocks.actions(*(
            blocks.button(
                o.get("label", "?")[:75], f"answer:{i}",
                json.dumps({"aid": approval_id, "label": o.get("label", "")}),
            )
            for i, o in enumerate(options[:5])
        )))

        logger.info("Question %s posted for thread %s: %s", approval_id, thread_ts, question[:80])
        return await self._post_buttons_and_wait(
            channel, thread_ts,
            approval_id=approval_id,
            text=f"{mention}질문: {question}",
            blocks_=view,
            finish_base=title,
            timeout_outcome=(
                f":hourglass: {APPROVAL_TIMEOUT_SECONDS // 60}분 내 응답이 없었습니다. "
                "스레드에 답장으로 답변해 주세요."
            ),
            outcome=lambda label, who: f":white_check_mark: {who} 답변: *{label}*",
        )

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
    # 메시지 수신 + Claude 턴 생명주기
    # ------------------------------------------------------------------

    async def _handle_slack_message(self, event: dict[str, Any]) -> None:
        # Filter: Ignore bot messages (prevents self-echo loops).
        if event.get("bot_id"):
            return

        thread_ts: str | None = event.get("thread_ts")
        text: str = event.get("text", "")
        channel: str = event.get("channel", "")
        files: list[dict] = event.get("files", [])
        user_id: str = event.get("user", "")

        # Case 2: Top-level message — only respond if the bot is mentioned.
        if not thread_ts:
            if f"<@{self._bot_user_id}>" not in text:
                return
            # Show the folder browser starting at PROJECTS_ROOT
            await self._app.client.chat_postMessage(
                channel=channel, text="프로젝트 폴더를 선택하세요:",
                blocks=self._browser_blocks(""),
            )
            return

        # Case 1: Threaded reply — continue the Claude conversation for that thread.
        if not self._claude.get_thread_project(thread_ts):
            return
        message_ts = event.get("ts", thread_ts)
        if user_id:
            self._thread_users[thread_ts] = user_id

        # Handle slash commands before forwarding to Claude.
        if await self._handle_thread_command(channel, thread_ts, message_ts, text):
            return

        if files:
            text += format_file_metadata(files)
        if thread_ts in self._active_threads:
            await self._enqueue_message(channel, thread_ts, text, message_ts)
            return
        asyncio.create_task(
            self._handle_claude_thread_reply(channel, thread_ts, text, message_ts)
        )

    async def _enqueue_message(
        self, channel: str, thread_ts: str, text: str, message_ts: str,
    ) -> None:
        """작업 중인 스레드에 온 메시지를 큐에 넣고 대기 표시를 남긴다."""
        queue = self._thread_queues.setdefault(thread_ts, deque())
        position = len(queue) + 1
        logger.info("Thread %s is active, queuing message (#%d).", thread_ts, position)
        await self._add_reaction(channel, message_ts, "eyes")
        status_ts = await self._say(channel, thread_ts, f":hourglass: 대기 중… (#{position})")
        queue.append((channel, text, message_ts, status_ts))

    async def _process_thread_queue(self, thread_ts: str) -> None:
        """Merge and process all queued messages for a thread."""
        queue = self._thread_queues.pop(thread_ts, None)
        if not queue:
            return
        channel = queue[0][0]
        texts: list[str] = []
        last_message_ts: str | None = None
        for msg_channel, text, msg_ts, status_ts in queue:
            texts.append(text)
            last_message_ts = msg_ts
            await self._remove_reaction(msg_channel, msg_ts, "eyes")
            await self._delete_message(msg_channel, status_ts)
        logger.info("Processing %d merged queued messages for thread %s", len(texts), thread_ts)
        asyncio.create_task(self._handle_claude_thread_reply(
            channel, thread_ts, "\n\n".join(texts), last_message_ts,
        ))

    async def _handle_claude_thread_reply(
        self, channel: str, thread_ts: str, text: str, message_ts: str | None = None,
    ) -> None:
        """Spawn Claude for a thread reply and post the response."""
        react_ts = message_ts or thread_ts
        logger.info("Handling thread reply: thread=%s, react_ts=%s, channel=%s",
                    thread_ts, react_ts, channel)
        self._active_threads.add(thread_ts)
        await self._add_reaction(channel, react_ts, "hourglass_flowing_sand")
        poster = EventPoster(self._app.client, channel, thread_ts)

        async def on_permission(request: dict[str, Any]) -> dict[str, Any]:
            return await self._request_permission(channel, thread_ts, request)

        try:
            result = await self._claude.handle_thread_reply(
                channel, thread_ts, text,
                on_event=poster.handle_event, on_permission=on_permission,
                message_ts=react_ts,
            )
            await self._post_response(
                channel, thread_ts, result.text,
                progress_ts=await poster.flush(), usage_footer=fmt.usage_footer(result),
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

    async def _post_response(
        self, channel: str, thread_ts: str, text: str, *,
        progress_ts: str | None = None, usage_footer: str = "",
    ) -> None:
        """Post a response to Slack, splitting if it exceeds the message length limit.

        진행 상황 메시지는 지우고 최종 응답은 항상 *새* 메시지로 게시한다.
        ``chat.update``로 덮어쓰면 Slack이 알림을 보내지 않아 작업이 끝난 것을
        알아채기 어렵기 때문이다. 요청한 사용자를 멘션해 알림을 보장한다.
        """
        text = fmt.markdown_to_slack(text)
        await self._delete_message(channel, progress_ts)
        if not text.strip():
            return

        footer_suffix = "\n\n" + usage_footer if usage_footer else ""
        mention = self._mention(thread_ts)
        chunks = fmt.split_message(text, SLACK_MAX_MESSAGE_LENGTH)

        # 너무 여러 조각으로 나뉘면 요약만 남기고 전문은 파일로 첨부한다.
        if len(chunks) > _MAX_RESPONSE_CHUNKS:
            await self._app.client.chat_postMessage(
                channel=channel, thread_ts=thread_ts, mrkdwn=True,
                text=mention + text[:3000] + "\n\n_(전체 응답은 파일로 첨부되었습니다)_" + footer_suffix,
            )
            await self._app.client.files_upload_v2(
                channel=channel, thread_ts=thread_ts,
                content=text, filename="response.md", title="전체 응답",
            )
            return

        for i, chunk in enumerate(chunks):
            if i == 0:
                chunk = mention + chunk
            if i == len(chunks) - 1:
                chunk += footer_suffix
            await self._app.client.chat_postMessage(
                channel=channel, thread_ts=thread_ts, text=chunk, mrkdwn=True,
            )

    async def _post_error(self, channel: str, thread_ts: str, exc: Exception) -> None:
        """Post an error summary to the Slack thread so the user knows what went wrong."""
        error_msg = str(exc)
        if len(error_msg) > 500:
            error_msg = error_msg[:497] + "…"
        # 실패도 사용자가 알아채야 하는 종료 상태이므로 멘션한다.
        await self._say(channel, thread_ts, (
            f":warning: {self._mention(thread_ts)}*오류가 발생했습니다*\n"
            f"`{type(exc).__name__}: {error_msg}`"
        ))

    # ------------------------------------------------------------------
    # Startup
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Start the Slack Socket Mode handler."""
        self._bot_user_id = await self._claude.initialize()
        await self._handler.start_async()
