# Claude ↔ Slack Bridge

Slack에서 Claude Code를 실행하는 브릿지입니다.

Slack에서 봇을 멘션하면 폴더 탐색 UI로 프로젝트를 선택하고, 해당 프로젝트 디렉토리에서 Claude Code가 실행됩니다. 터미널에서 쓰던 세션을 Slack에서 이어가거나, Slack에서 시작한 세션을 집에 돌아와 터미널에서 이어갈 수 있습니다.

```
Slack @bot ──폴더 선택──▶ 세션 선택 (새 세션 / 기존 세션 이어가기)
                              │
                              ▼
              claude -p (프로젝트 디렉토리) ──▶ 스레드에 실시간 진행 상황 + 응답
```

---
![slack-claude-small](https://github.com/user-attachments/assets/d4460f40-5c68-48a0-8fc5-9b386881a765)

## 주요 기능

- **폴더 탐색 UI** — `PROJECTS_DIR` 트리를 버튼으로 탐색. 깊이 제한 없이 어느 폴더에서든 세션 시작 가능
- **세션 선택** — 폴더의 기존 Claude CLI 세션(터미널에서 시작한 것 포함)을 골라 이어가기
- **터미널 ↔ Slack 세션 공유** — 같은 세션 저장소(`~/.claude/projects/`)를 사용하므로 어느 쪽에서든 이어서 작업 가능
- **권한 승인 플로우** — auto 권한 모드에서 승인이 필요한 작업이 생기면 Slack 버튼으로 승인/거부
- **실시간 진행 상황** — 도구 사용 이벤트를 스레드에 실시간 표시
- **모델/effort/권한 변경, 작업 중단, 메시지 큐잉** — 스레드 안에서 `!` 명령어와 리액션으로 제어

---

## 빠른 시작

### 1. Slack 앱 생성 및 토큰 발급

[docs/slack-setup.md](docs/slack-setup.md)를 참고하여 Slack 앱을 생성하고, `xoxb-` 및 `xapp-` 토큰을 발급받은 후, 봇을 채널에 초대하세요. **Interactivity** 토글과 `reaction_added` 이벤트 구독이 필요합니다.

### 2. 클론 및 설정

```bash
git clone https://github.com/LemonDouble/claude-slack-bridge.git
cd claude-slack-bridge
cp .env.example .env   # SLACK_BOT_TOKEN, SLACK_APP_TOKEN, PROJECTS_DIR 입력
```

### 3. 데몬 실행

[uv](https://docs.astral.sh/uv/)를 사용합니다:

```bash
uv run python src/main.py
```

Socket Mode를 사용하므로 공개 URL이나 인바운드 방화벽 규칙이 필요 없습니다.

설정 끝입니다. Slack 채널에서 봇을 멘션하면 시작됩니다.

---

## 설정 (`.env`)

| 변수 | 필수 | 기본값 | 설명 |
|---|---|---|---|
| `SLACK_BOT_TOKEN` | Yes | — | Bot OAuth 토큰 (`xoxb-...`) |
| `SLACK_APP_TOKEN` | Yes | — | Socket Mode 앱 토큰 (`xapp-...`) |
| `PROJECTS_DIR` | Yes | `~/claude-projects` | 프로젝트 루트 디렉토리의 절대 경로 |
| `TIMEOUT_LIMIT_MINUTES` | No | `720` | Idle 타임아웃(분). 마지막 출력 이후 이 시간 동안 출력이 없으면 중단 |

---

## 사용 방법

### 폴더 탐색 및 시작

1. 채널에서 `@봇이름`을 멘션합니다.
2. `PROJECTS_DIR`의 하위 폴더가 버튼으로 표시됩니다. 폴더를 클릭해 트리를 탐색합니다 (깊이 제한 없음).
3. 원하는 위치에서 **▶ 여기서 시작**을 클릭합니다.
4. 해당 폴더에 기존 Claude CLI 세션이 있으면 세션 목록이 표시됩니다:
   - **🆕 새 세션** — 새로 시작
   - **이어가기** — 기존 세션(터미널에서 쓰던 것 포함)을 resume
5. 스레드에서 대화를 계속합니다.

**➕ 새 폴더** 버튼으로 현재 위치 아래에 폴더를 만들며 바로 시작할 수 있습니다. `group/my-project`처럼 하위 경로도 입력 가능합니다.

### 터미널 ↔ Slack 세션 공유

Claude CLI는 세션을 `~/.claude/projects/<프로젝트 경로 인코딩>/`에 저장하며, 이 브릿지도 같은 저장소를 사용합니다.

- **터미널 → Slack**: 폴더 선택 후 세션 목록에서 터미널 세션을 골라 이어갑니다.
- **Slack → 터미널**: 스레드에서 `!settings`를 입력하면 `cd <프로젝트> && claude --resume <세션ID>` 명령어가 표시됩니다. 프로젝트 디렉토리에서 `claude --resume`을 실행해 목록에서 골라도 됩니다 — Slack에서 시작한 세션은 `Slack: <첫 메시지>` 이름으로 표시됩니다.

resume은 세션 ID를 유지하므로, 같은 스레드와 터미널을 오가며 작업해도 하나의 대화로 이어집니다.

### 권한 모드와 승인 플로우

Claude는 기본적으로 **auto 권한 모드**로 실행됩니다. 안전한 작업(프로젝트 내 읽기/쓰기 등)은 자동 승인되고, 승인이 필요한 작업(프로젝트 밖 쓰기, 위험 분류 명령 등)이 생기면 스레드에 승인 요청이 표시됩니다:

```
🔒 승인이 필요합니다 — Bash
`kubectl apply -f deploy.yaml`
[✅ 승인] [🚫 거부]
```

- 버튼을 누르면 Claude가 즉시 이어서 실행합니다 (거부 시 우회하거나 사유를 보고).
- 10분 내 응답이 없으면 자동 거부되고 작업은 계속 진행됩니다.
- 승인 없이 전부 실행하려면 `!perm bypassPermissions`로 스레드 권한 모드를 바꿀 수 있습니다 (기존 동작과 동일).

### 스레드 명령어

| 명령어 | 설명 |
|---|---|
| `!model sonnet\|opus\|haiku` | 이 스레드의 모델 변경 |
| `!effort low\|medium\|high\|xhigh\|max` | 이 스레드의 effort 변경 |
| `!perm auto\|acceptEdits\|bypassPermissions` | 이 스레드의 권한 모드 변경 |
| `!settings` | 현재 설정, 세션 ID, 터미널 이어가기 명령어 확인 |
| `!help` | 사용 가능한 명령어 목록 표시 |
| `!restart` | 현재 스레드의 Claude 세션 재시작 |
| `!default model\|effort\|perm <값>` | 기본값 변경 (전체 적용, 영구 저장) |

- 기본값: **sonnet** / **high** / **auto**
- `!default`로 변경한 기본값은 데몬을 재시작해도 유지됩니다.

### 실시간 진행 상황 표시

Claude가 작업 중일 때 스레드에 진행 상황이 실시간으로 표시됩니다.

- `stream-json` 출력을 파싱하여 도구 사용 이벤트를 하나의 메시지에 계속 갱신합니다 (3초 간격 throttle).
- 작업 완료 시 진행 상황 메시지가 최종 응답으로 자연스럽게 전환됩니다.

```
🚀 세션 시작 (a1b2c3d4…)
🖥️ $ python train.py --epochs 100
📄 Read /src/model.py
✏️ Edit /src/config.py
```

응답 끝에는 사용량 요약이 포함됩니다:

```
📊 Opus 4.6 | Tokens In: 17,410 Out: 523 (cache hit 65%) | Cost: $0.0442 | Time: 2.7s
```

### 메시지 큐잉 및 병합

Claude가 작업 중인 스레드에 추가 메시지를 보내면 큐에 저장됩니다. 대기 중인 메시지에 👀 리액션과 `:hourglass: 대기 중… (#N)` 표시가 붙고, 현재 작업이 끝나면 하나로 병합되어 전달됩니다.

### 작업 중단 (:x: 리액션)

스레드 내 아무 메시지에 ❌ 리액션을 달면 진행 중인 작업을 중단합니다. SIGINT를 보내 세션 상태가 보존되며(이후 resume 가능), 10초 내 종료되지 않으면 강제 종료합니다.

### Slack MCP 도구

Slack에서 실행된 Claude에는 아래 도구가 자동으로 제공됩니다 (`src/tools_mcp.py`):

| 도구 | 설명 |
|---|---|
| `notify_on_slack` | 작업 중 진행 상황을 스레드에 알림 (블로킹 없음) |
| `upload_to_slack` | `PROJECTS_DIR` 내 파일을 스레드에 업로드 |
| `download_slack_file` | 메시지에 첨부된 파일을 다운로드 (`{프로젝트}/.slack-downloads/`) |

사용 예시:

> *"진행 상황을 Slack으로 보고하면서 작업해줘."*
> *"학습 결과 그래프를 Slack에 올려줘."*
> *스크린샷을 첨부하며 "이 에러 고쳐줘"*

### Idle 타임아웃

전체 시간 제한 대신 **idle 타임아웃**을 사용합니다. Claude가 출력을 생성하는 한 시간 제한 없이 계속 실행됩니다. 기본값은 12시간(720분)으로, ML 학습 등 장시간 작업에 적합합니다.

---

## 프로젝트 구조

```
claude-slack-bridge/
├── src/
│   ├── main.py             # 데몬 진입점
│   ├── slack_daemon.py     # Slack Socket Mode + 폴더/세션 선택 UI + ! 명령어
│   ├── claude_handler.py   # Claude CLI 호출, 세션/모델/effort 관리
│   ├── session_catalog.py  # ~/.claude/projects/ 세션 파일 스캔 (세션 선택용)
│   ├── event_poster.py     # 실시간 진행 상황 Slack 포스팅
│   ├── tools_mcp.py        # Slack에서 실행된 Claude용 MCP 도구
│   ├── file_downloader.py  # Slack 파일 다운로드/업로드 검증
│   ├── constants.py        # 공유 상수 + .env 로드
│   ├── log_setup.py        # 공통 로깅 설정
│   └── config.py           # 환경 변수 유효성 검사 (pydantic-settings)
├── docs/
│   └── slack-setup.md      # Slack 앱 생성 가이드
├── pyproject.toml
└── uv.lock
```

---

## 요구 사항

- Python 3.14+
- [uv](https://docs.astral.sh/uv/)
- Claude Code CLI (`claude` 명령어가 PATH에 있어야 함)
- Slack 앱을 생성할 수 있는 Slack 워크스페이스

---

## 라이선스

MIT
