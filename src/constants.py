import os
from pathlib import Path

SOCKET_PATH = "/tmp/slack-bridge.sock"
PROJECTS_ROOT = Path(os.environ.get("PROJECTS_DIR", "/home/lemon/claude-projects"))
VALID_MODELS = ("sonnet", "opus", "haiku")
VALID_EFFORTS = ("low", "medium", "high", "xhigh", "max")
DEFAULT_MODEL = "sonnet"
DEFAULT_EFFORT = "high"
STATE_FILE = Path.home() / ".claude" / "slack-bridge-state.json"
SLACK_MAX_MESSAGE_LENGTH = 40000
