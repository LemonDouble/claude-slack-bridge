"""
session.py — Session entry point for Claude Code MCP.

Each Claude Code session starts one instance of this process.
The process runs an MCP stdio server with the ``ask_on_slack`` tool.
It posts messages to the channel in SLACK_CHANNEL and waits for replies
via the daemon's Unix socket — zero polling, OS-level blocking.
"""

import asyncio
import logging

from fastmcp import FastMCP
from slack_bolt.async_app import AsyncApp

from config import Config
from log_setup import setup_logging
from mcp_server import MCPServer
from session_broker import SessionBroker

setup_logging()
logger = logging.getLogger(__name__)


async def run(config: Config) -> None:
    """
    Wire the session components and run the MCP stdio server.

    Args:
        config: Validated configuration (reads SLACK_CHANNEL from env).
    """
    app = AsyncApp(token=config.slack_bot_token)

    async def post_message(text: str, thread_ts: str | None = None) -> str:
        kwargs: dict = dict(
            channel=config.slack_channel,
            text=f"<!channel> {text}",
            mrkdwn=True,
        )
        if thread_ts is not None:
            kwargs["thread_ts"] = thread_ts
        response = await app.client.chat_postMessage(**kwargs)
        if not response.get("ok"):
            raise RuntimeError(f"Slack API error: {response.get('error')}")
        ts: str = response["ts"]
        logger.info("Posted to %s, thread_ts=%s", config.slack_channel, thread_ts or ts)
        return thread_ts or ts

    broker = SessionBroker(
        post_message=post_message,
        timeout_minutes=config.timeout_limit_minutes,
    )
    mcp_server = MCPServer(
        broker=broker,
        slack_client=app.client,
        channel=config.slack_channel,
    )
    mcp = FastMCP(name="ClaudeSlackBridge")
    mcp_server.register(mcp)

    logger.info("Session started for channel %s.", config.slack_channel)
    await mcp.run_async()


if __name__ == "__main__":
    cfg = Config()  # type: ignore[call-arg]
    asyncio.run(run(cfg))
