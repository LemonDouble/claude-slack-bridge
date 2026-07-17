"""
config.py — Application configuration.

Loads and validates all required environment variables using pydantic-settings.
This is the single source of truth for settings throughout the application.
"""

from pathlib import Path

from pydantic_settings import BaseSettings


class Config(BaseSettings):
    """
    Validated configuration loaded from environment variables.

    Required variables (must be set in the environment or a .env file):
      - SLACK_BOT_TOKEN: Bot OAuth token (xoxb-...)
      - SLACK_APP_TOKEN: App-level token for Socket Mode (xapp-...)

    Optional:
      - TIMEOUT_LIMIT_MINUTES: idle timeout for Claude subprocesses (default 720)
      - PROJECTS_DIR: root directory of projects (used via constants.PROJECTS_ROOT)
    """

    slack_bot_token: str
    slack_app_token: str
    timeout_limit_minutes: int = 720
    projects_dir: str = str(Path.home() / "claude-projects")

    model_config = {
        "env_file": str(Path(__file__).resolve().parent.parent / ".env"),
        "env_file_encoding": "utf-8",
        "extra": "ignore",
    }
