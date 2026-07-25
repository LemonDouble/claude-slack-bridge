"""
main.py — Daemon entry point.

Starts the SlackDaemon, which holds one Slack Socket Mode WebSocket
connection and forwards Slack messages to the Claude Code CLI.
"""

import asyncio
import logging

from config import config
from log_setup import setup_logging
from slack_daemon import SlackDaemon

setup_logging()
logger = logging.getLogger(__name__)

async def run() -> None:
    # SlackDaemon 생성자가 aiohttp 세션을 만들므로 반드시 루프 안에서 만든다.
    logger.info("Starting Claude <-> Slack Daemon.")
    await SlackDaemon(
        bot_token=config.slack_bot_token,
        app_token=config.slack_app_token,
        idle_timeout_minutes=config.timeout_limit_minutes,
    ).start()


if __name__ == "__main__":
    asyncio.run(run())
