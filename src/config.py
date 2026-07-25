"""
config.py — Application configuration.

모든 환경 변수는 여기서 한 번만 읽고 검증한다. 다른 모듈은 ``config``
싱글턴을 가져다 쓴다 (``load_dotenv``를 직접 호출하지 않는다).
"""

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Config(BaseSettings):
    """
    Validated configuration loaded from environment variables / .env.

    Required:
      - SLACK_BOT_TOKEN: Bot OAuth token (xoxb-...)
      - SLACK_APP_TOKEN: App-level token for Socket Mode (xapp-...)

    Optional:
      - PROJECTS_DIR: 프로젝트 루트 (기본 ~/claude-projects)
      - TIMEOUT_LIMIT_MINUTES: Claude 서브프로세스 idle 타임아웃(분, 기본 720)
    """

    slack_bot_token: str
    slack_app_token: str
    projects_dir: Path = Path.home() / "claude-projects"
    timeout_limit_minutes: int = 720

    model_config = SettingsConfigDict(
        env_file=Path(__file__).resolve().parent.parent / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )


config = Config()  # type: ignore[call-arg]
