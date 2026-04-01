"""
tools_mcp.py — Lightweight MCP server for container-side claude -p.

Provides ``notify_on_slack`` and ``upload_to_slack`` tools so that the
Slack→Claude direction can send notifications and upload files without
the full session broker.  Reads channel/thread context from environment
variables set by the daemon when spawning claude -p.

Does NOT include ``ask_on_slack`` to avoid recursive loops.
"""

import logging
import os
from pathlib import Path

from fastmcp import FastMCP
from slack_sdk.web.async_client import AsyncWebClient

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

PROJECTS_ROOT = Path("/projects")

mcp = FastMCP(name="SlackTools")
_client: AsyncWebClient | None = None


def _get_client() -> AsyncWebClient:
    global _client
    if _client is None:
        token = os.environ.get("SLACK_BOT_TOKEN", "")
        _client = AsyncWebClient(token=token)
    return _client


@mcp.tool()
async def notify_on_slack(message: str) -> str:
    """
    Send a notification to Slack without waiting for a reply.

    Use this for progress updates, status reports, or any message that
    does not require human input. This tool returns immediately so your
    work is not interrupted.

    Examples: "학습을 시작합니다", "epoch 50/100 완료", "배포가 완료되었습니다"

    Args:
        message: The notification text to post.

    Returns:
        Confirmation string.
    """
    channel = os.environ.get("SLACK_CHANNEL", "")
    thread_ts = os.environ.get("SLACK_THREAD_TS", "")

    if not channel:
        return "오류: SLACK_CHANNEL 환경변수가 설정되지 않았습니다."

    client = _get_client()
    kwargs: dict = dict(channel=channel, text=message, mrkdwn=True)
    if thread_ts:
        kwargs["thread_ts"] = thread_ts

    await client.chat_postMessage(**kwargs)
    logger.info("Notification posted to %s (thread: %s)", channel, thread_ts)
    return f"알림이 전송되었습니다."


@mcp.tool()
async def upload_to_slack(file_path: str, message: str = "") -> str:
    """
    Upload a file to the Slack thread.

    Use this to share files with the user — training graphs, logs, CSVs,
    images, generated code, etc. The file must be inside the /projects/
    directory.

    Args:
        file_path: Absolute path to the file to upload (must be under /projects/).
        message:   Optional comment to post alongside the file.

    Returns:
        Confirmation string or error message.
    """
    channel = os.environ.get("SLACK_CHANNEL", "")
    thread_ts = os.environ.get("SLACK_THREAD_TS", "")

    if not channel:
        return "오류: SLACK_CHANNEL 환경변수가 설정되지 않았습니다."

    path = Path(file_path)

    try:
        path.resolve().relative_to(PROJECTS_ROOT.resolve())
    except ValueError:
        return f"오류: /projects/ 디렉토리 밖의 파일은 업로드할 수 없습니다. (요청: {file_path})"

    if not path.exists():
        return f"오류: 파일을 찾을 수 없습니다. ({file_path})"

    if not path.is_file():
        return f"오류: 디렉토리는 업로드할 수 없습니다. ({file_path})"

    client = _get_client()
    kwargs: dict = dict(
        channel=channel,
        file=str(path),
        filename=path.name,
        title=path.name,
        initial_comment=message,
    )
    if thread_ts:
        kwargs["thread_ts"] = thread_ts

    await client.files_upload_v2(**kwargs)
    logger.info("File uploaded: %s", file_path)
    return f"파일이 업로드되었습니다: {path.name}"


if __name__ == "__main__":
    import asyncio
    asyncio.run(mcp.run_async())
