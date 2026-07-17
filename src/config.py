"""
config.py — Application configuration.

Loads and validates the daemon's environment variables using pydantic-settings.
(PROJECTS_DIR 등 경로류 설정은 constants.py가 .env에서 직접 읽는다.)
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
    """

    slack_bot_token: str
    slack_app_token: str
    timeout_limit_minutes: int = 720

    model_config = {
        "env_file": str(Path(__file__).resolve().parent.parent / ".env"),
        "env_file_encoding": "utf-8",
        "extra": "ignore",
    }
