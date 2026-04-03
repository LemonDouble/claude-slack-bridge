"""
main.py — Daemon entry point.

Starts the SlackDaemon, which holds:
  - One Slack Socket Mode WebSocket connection (receives all reply events)
  - One Unix domain socket server (session processes connect here to wait for replies)

Session processes are started per Claude Code MCP invocation.
They post messages to Slack themselves and register with this daemon to
receive the reply when it arrives — no polling, OS-level blocking I/O.
"""

import asyncio
import json
import logging
from pathlib import Path

from config import Config
from log_setup import setup_logging
from slack_daemon import SlackDaemon

CLAUDE_SETTINGS_PATH = Path.home() / ".claude" / "settings.json"

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



def ensure_claude_settings() -> None:
    """Write Claude settings.json so that claude -p can use MCP tools."""
    settings: dict = {}
    if CLAUDE_SETTINGS_PATH.exists():
        try:
            settings = json.loads(CLAUDE_SETTINGS_PATH.read_text())
        except (json.JSONDecodeError, OSError):
            pass

    project_root = str(Path(__file__).resolve().parent.parent)
    tools_mcp_path = str(Path(__file__).resolve().parent / "tools_mcp.py")
    if "mcpServers" not in settings:
        settings["mcpServers"] = {}
    settings["mcpServers"]["slack-tools"] = {
        "command": "uv",
        "args": ["run", "--project", project_root, "python", tools_mcp_path],
    }

    CLAUDE_SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    CLAUDE_SETTINGS_PATH.write_text(json.dumps(settings, indent=2))
    logger.info("Wrote Claude settings with slack-tools MCP to %s", CLAUDE_SETTINGS_PATH)


if __name__ == "__main__":
    cfg = Config()  # type: ignore[call-arg]
    ensure_claude_settings()
    asyncio.run(run(cfg))
