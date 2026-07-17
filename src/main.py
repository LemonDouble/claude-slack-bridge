"""
main.py — Daemon entry point.

Starts the SlackDaemon, which holds one Slack Socket Mode WebSocket
connection and forwards Slack messages to the Claude Code CLI.
"""

import asyncio
import logging

from config import Config
from log_setup import setup_logging
from slack_daemon import SlackDaemon

setup_logging()
logger = logging.getLogger(__name__)


async def run(config: Config) -> None:
    """
    Start the daemon.

    Args:
        config: Validated application configuration.
    """
    daemon = SlackDaemon(
        bot_token=config.slack_bot_token,
        app_token=config.slack_app_token,
        idle_timeout_minutes=config.timeout_limit_minutes,
    )
    logger.info("Starting Claude <-> Slack Daemon.")
    await daemon.start()


if __name__ == "__main__":
    cfg = Config()  # type: ignore[call-arg]
    asyncio.run(run(cfg))
