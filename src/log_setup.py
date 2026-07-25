"""
log_setup.py — 공통 로깅 설정.

stdout에는 INFO 이상, error.log에는 ERROR 이상 기록.
각 진입점(main.py, tools_mcp.py)에서 한 번만 호출.
"""

import logging
from pathlib import Path

_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
ERROR_LOG_PATH = Path(__file__).resolve().parent.parent / "error.log"


def setup_logging() -> None:
    root = logging.getLogger()
    if root.handlers:  # 이미 설정됨 (basicConfig와 동일한 판정)
        return
    logging.basicConfig(level=logging.INFO, format=_FORMAT)

    errors = logging.FileHandler(ERROR_LOG_PATH, encoding="utf-8")
    errors.setLevel(logging.ERROR)
    errors.setFormatter(logging.Formatter(_FORMAT))
    root.addHandler(errors)
