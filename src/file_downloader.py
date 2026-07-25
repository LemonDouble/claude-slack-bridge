"""
file_downloader.py — Slack 파일 다운로드 유틸리티.

Slack file_id로 파일 정보를 조회하고 다운로드합니다.
"""

import logging
from pathlib import Path

import aiohttp
from slack_sdk.web.async_client import AsyncWebClient

from constants import PROJECTS_ROOT

logger = logging.getLogger(__name__)

DOWNLOADS_DIR_NAME = ".slack-downloads"


def validate_upload_path(file_path: str) -> Path | str:
    """PROJECTS_ROOT 내 파일인지 검증. 성공 시 Path, 실패 시 에러 문자열 반환."""
    path = Path(file_path)
    try:
        path.resolve().relative_to(PROJECTS_ROOT.resolve())
    except ValueError:
        return f"오류: PROJECTS_DIR 디렉토리 밖의 파일은 업로드할 수 없습니다. (요청: {file_path})"
    if not path.exists():
        return f"오류: 파일을 찾을 수 없습니다. ({file_path})"
    if not path.is_file():
        return f"오류: 디렉토리는 업로드할 수 없습니다. ({file_path})"
    return path


def format_file_metadata(files: list[dict]) -> str:
    """Slack 파일 메타데이터를 Claude에게 전달할 텍스트로 포맷합니다."""
    if not files:
        return ""

    lines = ["\n\n[첨부 파일]"]
    for f in files:
        size = f.get("size", 0)
        if size >= 1024 * 1024:
            size_str = f"{size / 1024 / 1024:.1f}MB"
        elif size >= 1024:
            size_str = f"{size / 1024:.1f}KB"
        else:
            size_str = f"{size}B"

        lines.append(
            f"- id: {f.get('id')}, "
            f"name: {f.get('name')}, "
            f"type: {f.get('mimetype', 'unknown')}, "
            f"size: {size_str}"
        )
    lines.append("\n필요한 파일은 download_slack_file 도구로 다운로드할 수 있습니다.")
    return "\n".join(lines)


async def download_file_by_id(
    file_id: str,
    client: AsyncWebClient,
    dest_dir: Path,
) -> Path:
    """Slack file_id로 파일을 다운로드합니다.

    파일 정보 조회는 slack_sdk가, 실제 내려받기는 aiohttp가 담당합니다
    (url_private는 Web API가 아니라 인증 헤더가 붙은 일반 GET입니다).

    Args:
        file_id: Slack 파일 ID (``F...``).
        client: 인증된 Slack AsyncWebClient.
        dest_dir: 파일을 저장할 디렉토리.

    Returns:
        다운로드된 파일의 경로.

    Raises:
        RuntimeError: 다운로드 실패 시.
    """
    file_info = (await client.files_info(file=file_id))["file"]
    url = file_info.get("url_private_download") or file_info.get("url_private")
    if not url:
        raise RuntimeError(f"파일 다운로드 URL을 찾을 수 없습니다: {file_id}")

    filename = file_info.get("name", "unknown_file")
    download_dir = dest_dir / DOWNLOADS_DIR_NAME
    download_dir.mkdir(parents=True, exist_ok=True)
    dest = download_dir / f"{file_id}_{filename}"

    # 이미 다운로드된 파일이면 바로 반환
    if dest.exists():
        logger.info("이미 다운로드됨: %s", dest)
        return dest

    headers = {"Authorization": f"Bearer {client.token}"}
    async with aiohttp.ClientSession(headers=headers) as session, session.get(url) as resp:
        if resp.status != 200:
            raise RuntimeError(f"파일 다운로드 실패 (HTTP {resp.status}): {filename}")
        dest.write_bytes(await resp.read())

    logger.info("파일 다운로드 완료: %s → %s", filename, dest)
    return dest
