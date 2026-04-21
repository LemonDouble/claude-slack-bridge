"""
mcp_server.py — MCP server module.

Registers MCP tools on a FastMCP instance:
  - ``ask_on_slack``    — post a message and wait for a reply (blocking)
  - ``notify_on_slack`` — post a notification without waiting (fire-and-forget)
  - ``upload_to_slack`` — upload a file from PROJECTS_DIR to the Slack thread
"""

import logging
from pathlib import Path
from typing import Any

from fastmcp import FastMCP

from file_downloader import download_file_by_id, validate_upload_path

logger = logging.getLogger(__name__)


class MCPServer:
    """
    Registers MCP tools that bridge Claude Code to Slack.

    Args:
        broker:       Object with ``send_and_wait(message) -> str``.
        slack_client: Async Slack WebClient for direct API calls.
        channel:      Slack channel name or ID for this session.
    """

    def __init__(self, broker: Any, slack_client: Any, channel: str) -> None:
        self._broker = broker
        self._slack_client = slack_client
        self._channel = channel
        self._thread_ts: str | None = None

    def register(self, mcp: FastMCP) -> None:
        """Register all MCP tools on the provided FastMCP instance."""
        mcp.tool()(self.ask_on_slack)
        mcp.tool()(self.notify_on_slack)
        mcp.tool()(self.upload_to_slack)
        mcp.tool()(self.download_slack_file)
        logger.info("Registered MCP tools: ask_on_slack, notify_on_slack, upload_to_slack, download_slack_file")

    async def ask_on_slack(self, message: str) -> str:
        """
        Post a message to Slack and wait for a human reply.

        Use this tool whenever you need a human decision, clarification, or
        approval that cannot be determined from existing context. The tool
        blocks until a reply is received in the Slack thread.

        Args:
            message: The question or message to send to the Slack channel.

        Returns:
            The text of the human's reply.
        """
        logger.info("ask_on_slack called with message: %r", message)
        reply = await self._broker.send_and_wait(message)
        return reply

    async def notify_on_slack(self, message: str) -> str:
        """
        Send a notification to Slack without waiting for a reply.

        Use this for progress updates, status reports, or any message that
        does not require human input. This tool returns immediately so your
        work is not interrupted.

        Examples: "학습을 시작합니다", "epoch 50/100 완료", "배포가 완료되었습니다"

        Args:
            message: The notification text to post.

        Returns:
            Confirmation string with the thread timestamp.
        """
        logger.info("notify_on_slack called with message: %r", message)
        kwargs: dict = dict(
            channel=self._channel,
            text=message,
            mrkdwn=True,
        )
        if self._thread_ts:
            kwargs["thread_ts"] = self._thread_ts

        response = await self._slack_client.chat_postMessage(**kwargs)
        ts = response["ts"]
        if not self._thread_ts:
            self._thread_ts = ts
        logger.info("Notification posted, thread_ts=%s", self._thread_ts)
        return f"알림이 전송되었습니다. (thread: {self._thread_ts})"

    async def upload_to_slack(self, file_path: str, message: str = "") -> str:
        """
        Upload a file to the Slack thread.

        Use this to share files with the user — training graphs, logs, CSVs,
        images, generated code, etc. The file must be inside the PROJECTS_DIR
        directory.

        Args:
            file_path: Absolute path to the file to upload (must be under PROJECTS_DIR).
            message:   Optional comment to post alongside the file.

        Returns:
            Confirmation string or error message.
        """
        logger.info("upload_to_slack called: file=%s, message=%r", file_path, message)

        result = validate_upload_path(file_path)
        if isinstance(result, str):
            return result
        path = result

        # Ensure we have a thread to upload into.
        if not self._thread_ts:
            response = await self._slack_client.chat_postMessage(
                channel=self._channel,
                text=message or f"`{path.name}` 파일을 업로드합니다.",
                mrkdwn=True,
            )
            self._thread_ts = response["ts"]

        await self._slack_client.files_upload_v2(
            channel=self._channel,
            thread_ts=self._thread_ts,
            file=str(path),
            filename=path.name,
            title=path.name,
            initial_comment=message,
        )

        logger.info("File uploaded: %s", file_path)
        return f"파일이 업로드되었습니다: {path.name}"

    async def download_slack_file(self, file_id: str) -> str:
        """
        Download a file from Slack by its file ID.

        Use this when a Slack reply includes attached files. The reply text
        will list file metadata with IDs — call this tool with the file_id
        to download it to the local project directory.

        The downloaded file can then be read with the Read tool (images
        are viewable directly) or processed with other tools.

        Args:
            file_id: Slack 파일 ID (F로 시작, 예: F08U1ABCDEF).

        Returns:
            다운로드된 파일의 절대 경로, 또는 에러 메시지.
        """
        logger.info("download_slack_file called: file_id=%s", file_id)
        try:
            path = await download_file_by_id(
                file_id=file_id,
                bot_token=self._slack_client.token,
                dest_dir=Path.cwd(),
            )
            return str(path)
        except Exception as exc:
            return f"오류: 파일 다운로드 실패 — {exc}"
