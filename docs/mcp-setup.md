# MCP 클라이언트 설정 가이드

Claude Code에서 Slack 브릿지를 사용하기 위한 MCP 설정 방법입니다.

---

## 사전 준비

데몬이 실행 중이어야 합니다:

```bash
cd /path/to/claude-slack-bridge
uv run python src/main.py
```

---

## 동작 방식

- **데몬** (상시 실행): Slack Socket Mode WebSocket과 Unix 소켓 서버(`/tmp/slack-bridge.sock`)를 유지합니다.
- **세션** (Claude Code에서 MCP로 호출): `session.py`가 Slack에 메시지를 게시하고, 데몬의 Unix 소켓에 연결하여 답변을 블로킹 대기합니다.

Slack 토큰(`SLACK_BOT_TOKEN`, `SLACK_APP_TOKEN`)은 `.env`에 이미 설정되어 있습니다. MCP 설정에는 `SLACK_CHANNEL`만 지정하면 됩니다.

---

## 방법 1 — 글로벌 설정 (모든 프로젝트에서 사용)

`~/.claude/settings.json`의 `mcpServers`에 추가:

```json
{
  "mcpServers": {
    "claude-slack-bridge": {
      "command": "uv",
      "args": [
        "run",
        "--project", "/path/to/claude-slack-bridge",
        "python", "/path/to/claude-slack-bridge/src/session.py"
      ],
      "env": {
        "SLACK_CHANNEL": "#your-channel",
        "TIMEOUT_LIMIT_MINUTES": "5"
      }
    }
  }
}
```

---

## 방법 2 — 프로젝트별 설정

프로젝트 루트에 `.mcp.json` 파일 생성:

```json
{
  "mcpServers": {
    "claude-slack-bridge": {
      "command": "uv",
      "args": [
        "run",
        "--project", "/path/to/claude-slack-bridge",
        "python", "/path/to/claude-slack-bridge/src/session.py"
      ],
      "env": {
        "SLACK_CHANNEL": "#my-project-channel",
        "TIMEOUT_LIMIT_MINUTES": "5"
      }
    }
  }
}
```

> `.mcp.json`은 `.gitignore`에 추가하세요.

---

## CLAUDE.md 설정

Claude가 Slack 도구를 한 번 사용한 후 계속 Slack으로 소통하도록 하려면, 프로젝트 상위 디렉토리에 `CLAUDE.md`를 넣으세요.

예를 들어 `PROJECTS_DIR=/home/user/projects`이면:

**`/home/user/projects/CLAUDE.md`:**
```markdown
# 프로젝트 지침

## 커뮤니케이션

대화에서 `mcp__claude-slack-bridge__ask_on_slack`을 처음 사용한 이후부터는 모든 커뮤니케이션을 해당 도구를 통해 수행해야 합니다. `AskUserQuestion`을 사용하거나 터미널에 텍스트로 질문이나 피드백 요청을 하지 마세요. 사용자가 명시적으로 터미널로 전환하라고 지시할 때까지 Slack을 통해서만 소통하세요.
```

Claude Code는 현재 디렉토리에서 상위로 올라가며 `CLAUDE.md`를 찾으므로, 상위 디렉토리에 넣으면 하위 모든 프로젝트에 자동 적용됩니다.

---

## 환경 변수

### MCP 설정에서 사용

| 변수 | 필수 | 기본값 | 설명 |
|---|---|---|---|
| `SLACK_CHANNEL` | Yes | — | 채널 이름 또는 ID (예: `#my-project`) |
| `TIMEOUT_LIMIT_MINUTES` | No | `720` | 답변 대기 타임아웃(분). 기본 12시간 |

---

## 여러 프로젝트에서 사용

프로젝트마다 다른 `SLACK_CHANNEL`을 지정할 수 있습니다. 모든 세션은 같은 데몬을 공유하므로 충돌이 없습니다.

```
프로젝트 A → SLACK_CHANNEL=#backend  → #backend 채널에 게시
프로젝트 B → SLACK_CHANNEL=#frontend → #frontend 채널에 게시
```

---

## 확인 방법

1. 데몬이 실행 중인지 확인 (로그에 `Bolt app is running!` 출력)
2. Claude Code에서 프로젝트를 열고 *"어떤 MCP 도구를 사용할 수 있어?"* 라고 물어보세요 — `ask_on_slack`이 목록에 나타나야 합니다.
3. Claude에게 *"Slack에서 물어봐"* 라고 요청하면, Slack 채널에 메시지가 나타나고 스레드에서 답변하면 Claude가 이어서 작업합니다.
