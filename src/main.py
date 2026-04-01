"""
main.py — Daemon entry point.

Starts the SlackDaemon, which holds:
  - One Slack Socket Mode WebSocket connection (receives all reply events)
  - One Unix domain socket server (session processes connect here to wait for replies)

Session processes are started per Claude session via:
    docker exec -i -e SLACK_CHANNEL=#channel claude-slack-bridge python session.py

They post messages to Slack themselves and register with this daemon to
receive the reply when it arrives — no polling, OS-level blocking I/O.
"""

import asyncio
import logging
import time
from pathlib import Path

from config import Config
from slack_daemon import SlackDaemon

SESSION_MAX_AGE_DAYS = 7

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
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


def cleanup_old_sessions() -> None:
    """Delete Claude session files older than SESSION_MAX_AGE_DAYS."""
    claude_dir = Path.home() / ".claude" / "projects"
    if not claude_dir.exists():
        return

    cutoff = time.time() - SESSION_MAX_AGE_DAYS * 86400
    removed = 0
    for path in claude_dir.rglob("*"):
        if path.is_file() and path.stat().st_mtime < cutoff:
            path.unlink()
            removed += 1

    # Remove empty directories left behind.
    for path in sorted(claude_dir.rglob("*"), reverse=True):
        if path.is_dir() and not any(path.iterdir()):
            path.rmdir()

    if removed:
        logger.info("Cleaned up %d session files older than %d days.", removed, SESSION_MAX_AGE_DAYS)


if __name__ == "__main__":
    cfg = Config()  # type: ignore[call-arg]
    cleanup_old_sessions()
    asyncio.run(run(cfg))
