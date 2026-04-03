# Claude ↔ Slack Bridge

Claude Code와 Slack을 연결하는 양방향 브릿지입니다.

- **Claude → Slack:** Claude가 작업 중 질문이 필요하면 Slack으로 메시지를 보내고, 답변을 기다린 후 작업을 재개합니다.
- **Slack → Claude:** Slack에서 봇을 멘션하면 프로젝트를 선택하는 UI가 나타나고, 해당 프로젝트 컨텍스트로 Claude가 실행됩니다.

```
Claude Code  ──ask_on_slack──▶  Slack 채널  ──답변──▶  Claude Code 재개
Slack @bot   ──프로젝트 선택──▶  claude -p (프로젝트 디렉토리) ──▶  스레드에 답변
```

---
![slack-claude-small](https://github.com/user-attachments/assets/d4460f40-5c68-48a0-8fc5-9b386881a765)



## 주요 기능

Claude가 작업 중 사람의 판단이 필요한 경우(승인, 확인, 누락된 정보 등) `ask_on_slack` MCP 도구를 호출합니다. 브릿지는 다음과 같이 동작합니다:

1. Slack 채널에 질문을 게시합니다.
2. Claude의 실행을 차단하고 대기합니다.
3. 답변을 수신합니다 — **반드시 Slack 스레드에서 답변해야 합니다. 채널에 직접 보낸 메시지는 인식되지 않습니다.**
4. 답변을 Claude에게 전달하고, Claude는 이어서 작업을 계속합니다.

여러 세션과 요청이 동시에 처리되며, 각각 고유한 Slack 스레드에 매핑되어 답변이 올바른 대기자에게 전달됩니다.

---

## 아키텍처

**데몬 + 세션** 모델을 사용하여 여러 Claude Code 세션을 동시에 지원합니다.

- **데몬** (상시 실행 프로세스): Slack Socket Mode WebSocket 연결 하나와 Unix 도메인 소켓 서버를 유지합니다. 모든 Slack 답변 이벤트를 수신하고 올바른 대기 세션으로 라우팅합니다.
- **세션** (Claude 세션마다 MCP로 시작): MCP stdio 서버를 실행하고, Slack에 메시지를 게시한 후, 데몬이 답변을 전달할 때까지 Unix 소켓에서 블로킹 대기합니다. 폴링 없이 OS 수준의 블로킹 I/O를 사용합니다.

```
데몬 (상시 실행):
  main.py → SlackDaemon
    ├── Slack Socket Mode WebSocket
    └── Unix 소켓: /tmp/slack-bridge.sock

Claude 세션별 (MCP 호출):
  session.py
    ├── 메시지 게시 → Slack HTTP API
    └── 답변 대기 → /tmp/slack-bridge.sock
```

`SLACK_BOT_TOKEN`과 `SLACK_APP_TOKEN`은 `.env`에만 설정하면 됩니다(한 번만). 각 프로젝트의 MCP 설정에는 `SLACK_CHANNEL`만 지정하면 됩니다.

---

## 빠른 시작

### 1. Slack 앱 생성 및 토큰 발급

[docs/slack-setup.md](docs/slack-setup.md)를 참고하여 Slack 앱을 생성하고, `xoxb-` 및 `xapp-` 토큰을 발급받은 후, 봇을 채널에 초대하세요.

### 2. 클론 및 설정

```bash
git clone https://github.com/LemonDouble/claude-slack-bridge.git
cd claude-slack-bridge
cp .env.example .env   # SLACK_BOT_TOKEN, SLACK_APP_TOKEN, PROJECTS_DIR을 입력하세요
```

### 3. 의존성 설치 및 데몬 실행

[uv](https://docs.astral.sh/uv/)를 사용합니다:

```bash
uv run python src/main.py
```

Socket Mode를 사용하므로 공개 URL이나 인바운드 방화벽 규칙이 필요 없습니다.

### 4. MCP 설정

Claude Code의 글로벌 설정(`~/.claude/settings.json`)에 MCP 서버를 등록합니다:

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

또는 프로젝트별 `.mcp.json`에 동일한 설정을 넣을 수 있습니다.

> **참고:** `.mcp.json`을 사용할 경우 `.gitignore`에 추가하세요.

### 5. `CLAUDE.md`에 Slack 통신 규칙 추가

Claude가 첫 메시지를 보낸 후 모든 커뮤니케이션을 Slack으로 전환하도록 하려면, 프로젝트 상위 디렉토리에 `CLAUDE.md`를 추가하세요:

```markdown
# 프로젝트 지침

## 커뮤니케이션

대화에서 `mcp__claude-slack-bridge__ask_on_slack`을 처음 사용한 이후부터는 모든 커뮤니케이션을 해당 도구를 통해 수행해야 합니다. `AskUserQuestion`을 사용하거나 터미널에 텍스트로 질문이나 피드백 요청을 하지 마세요. 사용자가 명시적으로 터미널로 전환하라고 지시할 때까지 Slack을 통해서만 소통하세요.
```

예를 들어 `PROJECTS_DIR`이 `/home/user/projects`이면, `/home/user/projects/CLAUDE.md`에 넣으면 하위 모든 프로젝트에 적용됩니다.

이 규칙이 없으면 Claude는 필요할 때만 Slack을 사용하고, 이 규칙이 있으면 첫 메시지 이후 Slack을 통해서만 소통합니다.

설정 완료입니다. Claude Code에서 프로젝트를 열면 `ask_on_slack` 도구를 사용할 수 있습니다.

---

## 설정

### `.env` (한 번만 설정, 모든 프로젝트 공유)

| 변수 | 필수 | 설명 |
|---|---|---|
| `SLACK_BOT_TOKEN` | Yes | Bot OAuth 토큰 (`xoxb-...`) |
| `SLACK_APP_TOKEN` | Yes | Socket Mode 앱 토큰 (`xapp-...`) |
| `PROJECTS_DIR` | Yes | 모든 프로젝트가 포함된 상위 디렉토리의 절대 경로 |

### MCP 설정 (프로젝트별)

| 변수 | 필수 | 기본값 | 설명 |
|---|---|---|---|
| `SLACK_CHANNEL` | Yes | — | 대상 채널 이름 또는 ID (예: `#my-project`) |
| `TIMEOUT_LIMIT_MINUTES` | No | `720` | Idle 타임아웃 대기 시간(분). 기본 12시간. |

프로젝트마다 `SLACK_CHANNEL`을 설정하여 각 프로젝트가 전용 채널에 메시지를 게시하도록 합니다.

---

## MCP 도구

### `ask_on_slack` — 질문하고 답변 대기

Claude가 컨텍스트만으로는 해결할 수 없는 결정이 필요할 때 자동으로 이 도구를 호출합니다.

**입력:** `message` — 보낼 질문 또는 메시지
**출력:** 답변 텍스트
**타임아웃:** `TIMEOUT_LIMIT_MINUTES` 내에 답변이 없으면 에러 발생 (기본 12시간)

> **스레드에서 답변하세요.** Slack에 메시지가 나타나면 **답변(Reply)**을 클릭하여 스레드를 열고 답변을 입력하세요. 채널에 직접 보낸 메시지는 인식되지 않습니다.

명시적으로 Claude에게 요청할 수도 있습니다:

> *"기존 파일을 덮어쓸지 Slack에서 물어봐."*

### `notify_on_slack` — 알림 전송 (블로킹 없음)

답변을 기다리지 않고 즉시 반환되는 알림 도구입니다. 장시간 작업의 중간 보고에 유용합니다.

**입력:** `message` — 알림 텍스트
**출력:** 전송 확인 문자열

사용 예시:

> *"학습 시작한다고 Slack에 알려줘."*
> *"진행 상황을 Slack으로 보고하면서 작업해줘."*

### `upload_to_slack` — 파일 업로드

`PROJECTS_DIR` 내의 파일을 Slack 스레드에 업로드합니다. ML 학습 그래프, 로그, CSV, 이미지 등 결과물 공유에 유용합니다.

**입력:** `file_path` — 업로드할 파일의 절대 경로, `message` (선택) — 파일과 함께 보낼 코멘트
**출력:** 업로드 확인 문자열
**제한:** `PROJECTS_DIR` 밖의 파일은 보안상 업로드할 수 없습니다.

사용 예시:

> *"학습 결과 그래프를 Slack에 올려줘."*
> *"로그 파일을 Slack으로 보내줘."*

### `download_slack_file` — Slack 첨부파일 다운로드

Slack 메시지에 첨부된 파일(이미지, 문서 등)을 로컬로 다운로드합니다. 사용자가 Slack에서 파일을 보내면 메시지에 파일 ID와 메타데이터가 자동으로 포함되며, Claude가 이 도구를 호출하여 파일을 다운로드하고 내용을 확인할 수 있습니다.

**입력:** `file_id` — Slack 파일 ID (`F`로 시작, 예: `F08U1ABCDEF`)
**출력:** 다운로드된 파일의 절대 경로
**저장 위치:** `{프로젝트 디렉토리}/.slack-downloads/`

사용 예시:

> *Slack에서 스크린샷을 첨부하며 "이 에러 고쳐줘"*
> *Slack에서 CSV 파일을 보내며 "이 데이터 분석해줘"*

---

## 실시간 진행 상황 표시

Slack → Claude 방향에서 Claude가 작업 중일 때, 스레드에 실시간 진행 상황이 표시됩니다.

- Claude의 `stream-json` 출력을 파싱하여 도구 사용 이벤트를 실시간으로 Slack 스레드에 포스트합니다.
- 하나의 메시지를 계속 업데이트하는 방식으로 스레드를 깔끔하게 유지합니다 (3초 간격 throttle).
- 작업 완료 시 진행 상황 메시지가 최종 응답으로 자연스럽게 전환됩니다 (단일 메시지인 경우 in-place 업데이트).

표시 예시:
```
🚀 세션 시작 (a1b2c3d4…)
🖥️ $ python train.py --epochs 100
📄 Read /src/model.py
✏️ Edit /src/config.py
🔍 Grep "learning_rate"
🤖 Agent research ML papers
```

### 메시지 큐잉 및 병합

Claude가 작업 중인 스레드에 추가 메시지를 보내면, 메시지가 큐에 저장됩니다.

- 대기 중인 메시지에 👀 리액션과 `:hourglass: 대기 중… (#N)` 상태 메시지가 표시됩니다.
- 현재 작업이 끝나면 큐에 쌓인 메시지를 하나로 병합하여 Claude에게 전달합니다.
- 처리 시작 시 대기 상태 표시가 자동으로 제거되고, 기존과 동일한 진행 상황 표시가 시작됩니다.

### Idle 타임아웃

전체 시간 제한 대신 **idle 타임아웃**을 사용합니다. Claude가 출력을 생성하는 한 시간 제한 없이 계속 실행됩니다. 마지막 출력 이후 `TIMEOUT_LIMIT_MINUTES` 동안 아무 출력이 없을 때만 타임아웃이 발생합니다. 기본값은 12시간(720분)으로, ML 학습 등 장시간 작업에 적합합니다.

### 세션 데이터

Claude CLI의 세션 데이터는 `~/.claude/`에 저장됩니다:

- **`--resume`으로 이전 대화 이어가기** — 프로세스 재시작 후에도 기존 스레드에서 대화를 계속할 수 있습니다.
- **thread→project 매핑 유지** — 어떤 스레드가 어떤 프로젝트에 연결되어 있는지 기억합니다.

---

## Slack → Claude (프로젝트 인식 봇)

Slack 채널에서 봇을 멘션하면 Claude 세션이 시작됩니다. Block Kit UI로 프로젝트를 선택할 수 있어 채널-프로젝트 매핑이 필요 없습니다.

### 동작 방식

1. Slack 채널에서 `@claude-bot`을 멘션합니다.
2. `PROJECTS_DIR`에서 발견된 프로젝트 목록이 버튼 형태의 인터랙티브 UI로 표시됩니다.
3. 프로젝트를 클릭하면 스레드가 생성되고, 해당 프로젝트 디렉토리에서 Claude가 실행됩니다(`CLAUDE.md`, 코드베이스 등 참조 가능).
4. 스레드에서 답변하여 대화를 계속합니다.
5. 새 프로젝트가 필요하면 **"+ New Project"** 버튼을 클릭 → 모달에서 이름을 입력 → 폴더가 생성되고 스레드가 바로 시작됩니다.

### 설정

`.env`에 `PROJECTS_DIR`을 설정하세요. **1단계 하위 디렉토리**가 각각 별도의 프로젝트로 인식됩니다.

```
/path/to/your/projects/
├── project-a/     ← "project-a" 버튼으로 표시
├── project-b/     ← "project-b" 버튼으로 표시
└── my-app/        ← "my-app" 버튼으로 표시
```

Slack 앱 설정에서 **Interactivity**를 활성화해야 합니다. Socket Mode가 페이로드를 처리하므로 Request URL은 필요 없습니다 — 토글만 켜면 됩니다.

새 프로젝트 추가는 `PROJECTS_DIR` 안에 폴더를 생성하기만 하면 됩니다. 봇이 멘션될 때마다 디렉토리를 스캔합니다. Slack에서 **"+ New Project"** 버튼으로도 생성 가능합니다.

---

## 프로젝트 구조

```
claude-slack-bridge/
├── src/
│   ├── main.py            # 데몬 진입점 — SlackDaemon 시작
│   ├── session.py         # 세션 진입점 — MCP stdio 서버
│   ├── slack_daemon.py    # Slack Socket Mode + Unix 소켓 서버
│   ├── session_broker.py  # Unix 소켓 클라이언트 — 메시지 게시, 답변 대기
│   ├── mcp_server.py      # ask_on_slack MCP 도구 등록
│   ├── tools_mcp.py       # notify/upload/download MCP 도구 (Slack→Claude 방향)
│   ├── file_downloader.py # Slack 파일 다운로드 유틸리티
│   ├── log_setup.py       # 공통 로깅 설정 (stdout INFO + error.log ERROR)
│   └── config.py          # 환경 변수 유효성 검사 (pydantic-settings)
├── docs/
│   ├── slack-setup.md     # Slack 앱 생성 가이드
│   └── mcp-setup.md       # MCP 클라이언트 설정 가이드
├── pyproject.toml
└── uv.lock
```

---

## 요구 사항

- Python 3.12+
- [uv](https://docs.astral.sh/uv/)
- Slack 앱을 생성할 수 있는 Slack 워크스페이스
- Claude Code (또는 MCP 호환 클라이언트)

---

## 라이선스

MIT
