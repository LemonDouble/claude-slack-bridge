"""
log_setup.py — 공통 로깅 설정.

stdout에는 INFO 이상, error.log에는 ERROR 이상 기록.
각 진입점(main.py, session.py, tools_mcp.py)에서 한 번만 호출.
"""

import logging
from pathlib import Path

_configured = False

ERROR_LOG_PATH = Path(__file__).resolve().parent.parent / "error.log"


def setup_logging() -> None:
    global _configured
    if _configured:
        return
    _configured = True

    root = logging.getLogger()
    root.setLevel(logging.DEBUG)

    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")

    # stdout handler — INFO 이상
    stream_handler = logging.StreamHandler()
    stream_handler.setLevel(logging.INFO)
    stream_handler.setFormatter(fmt)
    root.addHandler(stream_handler)

    # error.log handler — ERROR 이상
    file_handler = logging.FileHandler(ERROR_LOG_PATH, encoding="utf-8")
    file_handler.setLevel(logging.ERROR)
    file_handler.setFormatter(fmt)
    root.addHandler(file_handler)
