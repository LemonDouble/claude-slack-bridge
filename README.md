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

- **데몬** (상시 실행 Docker 컨테이너): Slack Socket Mode WebSocket 연결 하나와 Unix 도메인 소켓 서버를 유지합니다. 모든 Slack 답변 이벤트를 수신하고 올바른 대기 세션으로 라우팅합니다.
- **세션** (Claude 세션마다 `docker exec`으로 시작): MCP stdio 서버를 실행하고, Slack에 메시지를 게시한 후, 데몬이 답변을 전달할 때까지 Unix 소켓에서 블로킹 대기합니다. 폴링 없이 OS 수준의 블로킹 I/O를 사용합니다.

```
컨테이너 (상시 실행):
  main.py → SlackDaemon
    ├── Slack Socket Mode WebSocket
    └── Unix 소켓: /tmp/slack-bridge.sock

Claude 세션별 (docker exec):
  session.py
    ├── 메시지 게시 → Slack HTTP API (SLACK_CHANNEL은 .mcp.json에서 설정)
    └── 답변 대기 → /tmp/slack-bridge.sock
```

`SLACK_BOT_TOKEN`과 `SLACK_APP_TOKEN`은 `.env`에만 설정하면 됩니다(한 번만). 각 프로젝트의 `.mcp.json`에는 `SLACK_CHANNEL`만 지정하면 됩니다.

---

## 빠른 시작

### 1. Slack 앱 생성 및 토큰 발급

[docs/slack-setup.md](docs/slack-setup.md)를 참고하여 Slack 앱을 생성하고, `xoxb-` 및 `xapp-` 토큰을 발급받은 후, 봇을 채널에 초대하세요.

### 2. 클론, 설정, 데몬 시작

```bash
git clone https://github.com/your-username/claude-slack-bridge.git
cd claude-slack-bridge
cp .env.example .env   # SLACK_BOT_TOKEN과 SLACK_APP_TOKEN을 입력하세요
docker compose up -d --build
```

컨테이너는 시스템 부팅 시 자동으로 시작되며(`restart: unless-stopped`), Socket Mode를 사용하므로 공개 URL이나 인바운드 방화벽 규칙이 필요 없습니다.

**이 작업은 한 번만 수행합니다.** 데몬은 백그라운드에서 계속 실행되며 모든 Claude Code 프로젝트를 서비스합니다.

### 3. Claude Code 프로젝트에 `.mcp.json` 추가

Claude가 질문할 수 있도록 하려는 프로젝트의 루트에 `.mcp.json`을 생성하세요:

```json
{
  "mcpServers": {
    "claude-slack-bridge": {
      "command": "docker",
      "args": [
        "exec", "-i",
        "-e", "SLACK_CHANNEL",
        "-e", "TIMEOUT_LIMIT_MINUTES",
        "claude-slack-bridge",
        "python", "session.py"
      ],
      "env": {
        "SLACK_CHANNEL": "#your-project-channel",
        "TIMEOUT_LIMIT_MINUTES": "5"
      }
    }
  }
}
```

> **중요:** `.mcp.json`을 `.gitignore`에 추가하세요 — 채널 이름이 포함되어 있으며 프로젝트별로 다릅니다.

### 4. `CLAUDE.md`에 Slack 통신 규칙 추가

Claude가 첫 메시지를 보낸 후 자동으로 모든 커뮤니케이션을 Slack으로 전환하도록 하려면, 프로젝트의 `CLAUDE.md`에 다음을 추가하세요:

```markdown
Once you use `mcp__claude-slack-bridge__ask_on_slack` for the first time in a conversation, ALL further communication with the user must go through that tool. Do not use `AskUserQuestion`, and do not ask questions or request feedback as text in the terminal. Continue communicating exclusively via Slack until the user explicitly tells you to switch back to the terminal.
```

이 규칙이 없으면 Claude는 필요할 때만 Slack을 사용하지만, 이 규칙이 있으면 첫 메시지 이후 세션이 끝날 때까지 Slack을 통해서만 소통합니다.

설정 완료입니다. Claude Code에서 프로젝트를 열면 `ask_on_slack` 도구를 사용할 수 있습니다.

---

## 설정

### `.env` (데몬 — 한 번만 설정, 모든 프로젝트 공유)

| 변수 | 필수 | 설명 |
|---|---|---|
| `SLACK_BOT_TOKEN` | Yes | Bot OAuth 토큰 (`xoxb-...`) |
| `SLACK_APP_TOKEN` | Yes | Socket Mode 앱 토큰 (`xapp-...`) |
| `PROJECTS_DIR` | Yes | 모든 프로젝트가 포함된 상위 디렉토리의 절대 경로 |

### `.mcp.json` (프로젝트별 — Claude Code 프로젝트마다 설정)

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

프로젝트 디렉토리(`/projects/`) 내의 파일을 Slack 스레드에 업로드합니다. ML 학습 그래프, 로그, CSV, 이미지 등 결과물 공유에 유용합니다.

**입력:** `file_path` — 업로드할 파일의 절대 경로, `message` (선택) — 파일과 함께 보낼 코멘트
**출력:** 업로드 확인 문자열
**제한:** `/projects/` 디렉토리 밖의 파일은 보안상 업로드할 수 없습니다.

사용 예시:

> *"학습 결과 그래프를 Slack에 올려줘."*
> *"로그 파일을 Slack으로 보내줘."*

---

## 실시간 진행 상황 표시

Slack → Claude 방향에서 Claude가 작업 중일 때, 스레드에 실시간 진행 상황이 표시됩니다.

- Claude의 `stream-json` 출력을 파싱하여 도구 사용 이벤트를 실시간으로 Slack 스레드에 포스트합니다.
- 하나의 메시지를 계속 업데이트하는 방식으로 스레드를 깔끔하게 유지합니다 (3초 간격 throttle).
- 작업 완료 시 진행 상황 메시지는 삭제되고 최종 결과만 남습니다.

표시 예시:
```
🚀 세션 시작 (a1b2c3d4…)
🖥️ $ python train.py --epochs 100
📄 Read /src/model.py
✏️ Edit /src/config.py
🔍 Grep "learning_rate"
🤖 Agent research ML papers
```

### Idle 타임아웃

기존의 전체 시간 제한 대신 **idle 타임아웃**을 사용합니다. Claude가 출력을 생성하는 한 시간 제한 없이 계속 실행됩니다. 마지막 출력 이후 `TIMEOUT_LIMIT_MINUTES` 동안 아무 출력이 없을 때만 타임아웃이 발생합니다. 기본값은 12시간(720분)으로, ML 학습 등 장시간 작업에 적합합니다.

### 세션 데이터 영속화

Claude CLI의 세션 데이터(`.claude/`)는 Docker named volume(`claude-data`)에 저장되어 컨테이너를 재시작해도 유지됩니다. 이를 통해:

- **`--resume`으로 이전 대화 이어가기** — 컨테이너 재시작 후에도 기존 스레드에서 대화를 계속할 수 있습니다.
- **thread→project 매핑 유지** — 어떤 스레드가 어떤 프로젝트에 연결되어 있는지 기억합니다.
- **7일 자동 정리** — 컨테이너 시작 시 7일 이상 된 세션 파일이 자동으로 삭제되어 디스크 누적을 방지합니다.

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

#### 1. `.env`에 `PROJECTS_DIR` 설정

모든 프로젝트가 포함된 상위 디렉토리를 지정하세요:

```
PROJECTS_DIR=/path/to/your/projects
```

이 디렉토리는 컨테이너 내부의 `/projects/`에 마운트됩니다. **1단계 하위 디렉토리**가 각각 별도의 프로젝트로 인식됩니다.

```
/path/to/your/projects/
├── project-a/     ← "project-a" 버튼으로 표시
├── project-b/     ← "project-b" 버튼으로 표시
└── my-app/        ← "my-app" 버튼으로 표시
```

#### 2. Slack 앱에서 Interactivity 활성화

Block Kit 버튼과 모달을 사용하므로 Slack 앱 설정에서 **Interactivity**를 활성화해야 합니다. Socket Mode가 페이로드를 처리하므로 Request URL은 필요 없습니다 — 토글만 켜면 됩니다.

#### 3. 재빌드

```bash
docker compose up -d --build
```

#### 새 프로젝트 추가

`PROJECTS_DIR` 안에 폴더를 생성하기만 하면 됩니다 — 설정 변경이나 재빌드가 필요 없습니다. 봇이 멘션될 때마다 디렉토리를 스캔합니다.

Slack에서 **"+ New Project"** 버튼을 사용하여 직접 프로젝트를 생성할 수도 있습니다.

---

## 프로젝트 구조

```
claude-slack-bridge/
├── src/
│   ├── main.py            # 데몬 진입점 — SlackDaemon 시작
│   ├── session.py         # 세션 진입점 — MCP stdio 서버 (docker exec 대상)
│   ├── slack_daemon.py    # Slack Socket Mode + Unix 소켓 서버
│   ├── session_broker.py  # Unix 소켓 클라이언트 — 메시지 게시, 답변 대기
│   ├── mcp_server.py      # ask_on_slack MCP 도구 등록
│   └── config.py          # 환경 변수 유효성 검사 (pydantic-settings)
├── docs/
│   ├── slack-setup.md        # Slack 앱 생성 단계별 가이드
│   └── mcp-client-setup.md   # Claude Code 프로젝트에 .mcp.json 설정 방법
├── Dockerfile
├── docker-compose.yml
├── docker-compose.gpu.yml  # GPU 사용 시 override 파일
└── requirements.txt
```

---

## 동작 원리 (내부 구조)

1. **데몬 시작** (`docker compose up -d`): `SlackDaemon`이 Socket Mode로 Slack에 연결하고, 컨테이너 내부의 `/tmp/slack-bridge.sock`에 Unix 도메인 소켓을 엽니다.
2. **Claude가 `ask_on_slack` 호출**: `docker exec`으로 컨테이너 내부에서 이미 실행 중인 세션 프로세스(`session.py`)가 프로젝트의 `.mcp.json`에 있는 `SLACK_CHANNEL`을 사용하여 Slack HTTP API로 메시지를 게시합니다.
3. **세션이 데몬에 등록**: 세션이 `/tmp/slack-bridge.sock`에 연결하고 `REGISTER {thread_ts}`를 전송합니다. 이후 블로킹 대기 — 폴링 없이 데이터가 도착하면 OS가 깨워줍니다.
4. **사용자가 Slack에서 답변**: Socket Mode 이벤트가 데몬에 도착합니다. 데몬은 해당 `thread_ts`에 등록된 세션을 찾아 Unix 소켓에 답변 텍스트를 쓰고 연결을 닫습니다.
5. **세션 블로킹 해제**: 소켓에서 답변을 읽고 Claude Code에 반환합니다.

여러 동시 세션은 각각 고유한 `docker exec` 프로세스와 데몬에 대한 소켓 연결을 가집니다. `thread_ts`로 라우팅되어 답변이 항상 올바른 대기자에게 전달됩니다.

---

## 요구 사항

- Docker (Docker Compose 포함)
- Slack 앱을 생성할 수 있는 Slack 워크스페이스
- Claude Code (또는 MCP 호환 클라이언트)

### (선택) GPU 사용

컨테이너 내에서 GPU를 사용하려면(ML 학습 등) 호스트에 NVIDIA Container Toolkit을 설치해야 합니다.

#### 사전 조건

- NVIDIA GPU가 장착된 머신
- NVIDIA GPU 드라이버 설치 완료 (`nvidia-smi`로 확인)
  - WSL2 환경: **Windows 측**에 NVIDIA 드라이버를 설치하면 WSL2에서 자동으로 사용 가능

#### 1. NVIDIA Container Toolkit 설치

```bash
# GPG 키 및 저장소 추가
curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey | sudo gpg --dearmor -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg
curl -s -L https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list | \
  sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#g' | \
  sudo tee /etc/apt/sources.list.d/nvidia-container-toolkit.list

# 설치
sudo apt-get update
sudo apt-get install -y nvidia-container-toolkit

# Docker 런타임에 등록 및 재시작
sudo nvidia-ctk runtime configure --runtime=docker
sudo systemctl restart docker
```

#### 2. 설치 확인

```bash
# Docker에서 GPU 접근 가능한지 확인
docker run --rm --gpus all nvidia/cuda:12.6.3-base-ubuntu24.04 nvidia-smi
```

정상적으로 GPU 정보가 출력되면 설정 완료입니다.

#### 3. GPU 모드로 실행

GPU override 파일을 함께 지정하여 실행합니다:

```bash
docker compose -f docker-compose.yml -f docker-compose.gpu.yml up -d --build
```

> **참고:** GPU가 없는 환경에서는 기본 `docker compose up -d --build`로 실행하면 됩니다 — GPU 설정은 별도 override 파일(`docker-compose.gpu.yml`)로 분리되어 있어 기본 동작에 영향을 주지 않습니다.

---

## 라이선스

MIT
