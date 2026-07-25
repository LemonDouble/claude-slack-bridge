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
- **터미널 ↔ Slack 세션 공유** — 같은 세션 저장소(`~/.claude/projects/`)를 사용하므로 어느 쪽에서든 이어서 작업 가능. Slack 세션도 터미널 `claude --resume` 목록에 그대로 표시됨
- **되돌리기(rewind)** — ⏪ 리액션으로 특정 턴 직전의 대화/코드 상태로 복구
- **권한 승인 플로우** — auto 권한 모드에서 승인이 필요한 작업이 생기면 Slack 버튼으로 승인/거부
- **실시간 진행 상황** — 도구 사용 이벤트를 스레드에 실시간 표시
- **모델/effort/권한 변경, 작업 중단, 메시지 큐잉** — 드롭다운과 리액션으로 제어

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
| `PROJECTS_DIR` | No | `~/claude-projects` | 프로젝트 루트 디렉토리의 절대 경로 |
| `TIMEOUT_LIMIT_MINUTES` | No | `720` | Idle 타임아웃(분). 마지막 출력 이후 이 시간 동안 출력이 없으면 중단 |

값은 `src/config.py`(pydantic-settings)가 시작 시 한 번 읽고 검증합니다. 필수 토큰이 없으면 데몬이 바로 실패합니다.

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

- **터미널 → Slack**: 폴더 선택 후 세션 목록에서 터미널 세션을 골라 이어갑니다. 이어가기를 누르면 **직전까지의 진행 상황**이 함께 표시됩니다:

  ```
  📋 지금까지의 요청 (11턴 중 최근 3개)
  > • ㅇㅇ ArgoCD Sync만 하면 되게 준비해주지. API Key는 지웠어.
  > • 2026/07/25 03:49:27 job err: unable to read multipart part…
  > • 백업 안됨 항목이 87개정도 있는데. 이거 왜 그런지 알 수 있으려나?

  💬 마지막 응답
  > 원인을 규명했습니다. 87개는 유실된 게 아니라 서버에 이미 다 있습니다…

  claude-projects/k8s 폴더에서 시작합니다. 이어서 무엇을 할까요?
  ```

  트랜스크립트에서 그대로 발췌하므로 모델 호출도, 비용도, 대기 시간도 없습니다 (9MB 세션 기준 약 50ms). 되돌리기로 버려진 분기는 제외하고 실제로 이어질 대화만 보여줍니다.

- **Slack → 터미널**: 스레드에서 `!settings`를 입력하면 `cd <프로젝트> && claude --resume <세션ID>` 명령어가 표시됩니다. 프로젝트 디렉토리에서 `claude --resume`을 실행해 목록에서 골라도 됩니다 — Slack에서 시작한 세션은 `Slack: <첫 메시지>` 이름으로 표시됩니다.

resume은 세션 ID를 유지하므로, 같은 스레드와 터미널을 오가며 작업해도 하나의 대화로 이어집니다.

> Claude CLI는 `/resume` 목록에서 SDK 세션(`entrypoint`가 `sdk-cli`/`sdk-ts`/`sdk-py`)을 숨깁니다. 브릿지는 `CLAUDE_CODE_ENTRYPOINT=claude-in-slack`을 지정해 이 필터를 피하므로, Slack에서 만든 세션도 터미널 목록에 정상적으로 나타납니다. 이 설정 이전에 만들어진 세션은 계속 숨겨지지만, `!settings`에 표시되는 `claude --resume <세션ID>`로는 여전히 이어갈 수 있습니다.

### 되돌리기 (⏪ 리액션)

Claude Code의 rewind와 같은 기능입니다. 스레드의 아무 메시지에나 ⏪ 리액션을 달면, **그 메시지가 속한 턴 직전**으로 되돌립니다. 내 요청에 달든 봇 응답에 달든 결과는 같습니다.

```
⏪ 이 지점으로 되돌립니다
> deployment 수정해줘
> 복구 대상: `deploy.yaml`, `svc.yaml`
[💬 대화만] [📁 코드만] [🔄 둘 다] [취소]
```

- **대화만** — 그 턴 이후의 대화를 잘라냅니다. 다음 메시지를 보낼 때 `--resume-session-at`으로 적용되며, 세션 ID는 유지됩니다.
- **코드만** — 그 턴이 시작될 때의 파일 상태로 복구합니다 (`--rewind-files`).
- **둘 다** — 코드를 복구한 뒤 대화를 되돌립니다.

제약 사항:

- 파일 복구는 Claude가 `Edit`/`Write`/`NotebookEdit`로 수정한 파일만 대상입니다. Bash로 바꾼 파일은 복구되지 않습니다.
- 파일 체크포인트는 브릿지가 `CLAUDE_CODE_ENABLE_SDK_FILE_CHECKPOINTING=1`을 켠 이후의 턴부터 기록됩니다. 그 이전 턴은 대화만 되돌릴 수 있습니다.
- 작업이 진행 중일 때는 되돌릴 수 없습니다. ❌ 로 먼저 중단하세요.

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

### 설정 변경 (드롭다운)

`!settings`를 입력하면 현재 설정과 함께 선택 가능한 값이 드롭다운으로 표시됩니다. 값을 외울 필요 없이 고르면 바로 적용됩니다.

```
⚙️ 현재 스레드 설정 — 아래에서 바로 바꿀 수 있습니다
> 기본값: sonnet / high / auto
> 프로젝트: /home/me/claude-projects/k8s
> 세션: ebeb1ae8-…

모델 — 현재 `sonnet`        [ sonnet          ▾ ]
effort — 현재 `high`        [ high            ▾ ]
권한 모드 — 현재 `auto`      [ auto            ▾ ]
```

`!model` / `!effort` / `!perm`을 인자 없이 입력하면 해당 항목의 드롭다운만 표시됩니다.

| 값 | 선택 가능 |
|---|---|
| 모델 | `opus` `opus[1m]` `sonnet` `sonnet[1m]` `haiku` `fable` `fable[1m]` `opusplan` `best` |
| effort | `low` `medium` `high` `xhigh` `max` |
| 권한 | `auto` `acceptEdits` `bypassPermissions` |

### 스레드 명령어

| 명령어 | 설명 |
|---|---|
| `!settings` / `!help` | 현재 설정 드롭다운, 세션 ID, 터미널 이어가기 명령어 확인 |
| `!model <값>` | 이 스레드의 모델 변경 (인자 없으면 드롭다운) |
| `!effort <값>` | 이 스레드의 effort 변경 (인자 없으면 드롭다운) |
| `!perm <값>` | 이 스레드의 권한 모드 변경 (인자 없으면 드롭다운) |
| `!restart` | 현재 스레드의 Claude 세션 재시작 |
| `!default model\|effort\|perm <값>` | 기본값 변경 (전체 적용, 영구 저장) |

- 기본값: **sonnet** / **high** / **auto**
- `!default`로 변경한 기본값은 데몬을 재시작해도 유지됩니다.

### 실시간 진행 상황 표시

Claude가 작업 중일 때 스레드에 진행 상황이 실시간으로 표시됩니다.

- `stream-json` 출력을 파싱하여 도구 사용 이벤트를 하나의 메시지에 계속 갱신합니다 (3초 간격 throttle).
- 작업이 끝나면 진행 상황 메시지는 지워지고, 최종 응답이 **새 메시지**로 게시됩니다.

### 완료 알림

작업이 끝나면 요청한 사람을 멘션합니다. 승인 요청·질문·오류도 마찬가지입니다.

```
@lemon deployment의 리소스 제한을 수정했습니다. …
📊 Opus 5 | Tokens In: 17,410 Out: 523 | Cost: $0.0442 | Time: 2.7s
```

Slack은 `chat.update`로 수정된 메시지에 알림을 보내지 않습니다. 예전에는 진행 상황 메시지를 최종 응답으로 덮어쓰는 방식이라 작업이 끝나도 알림이 오지 않았습니다. 지금은 새 메시지로 게시하고 멘션을 붙여, Slack 알림 설정이 "멘션만"이어도 확실히 알림이 옵니다.

```
🚀 세션 시작 (a1b2c3d4…)
🖥️ $ python train.py --epochs 100
📄 Read /src/model.py
✏️ Edit /src/config.py
```

응답 끝에는 사용량 요약이 포함됩니다:

```
📊 Opus 5 | Tokens In: 17,410 Out: 523 (cache hit 65%) | Cost: $0.0442 | Time: 2.7s
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
│   ├── slack_daemon.py     # Slack 이벤트 라우팅 + 스레드 상태 (Socket Mode)
│   ├── slack_blocks.py     # Block Kit 화면 구성 (순수 함수)
│   ├── slack_format.py     # 마크다운→mrkdwn, 메시지 분할, 사용량 요약 (순수 함수)
│   ├── claude_handler.py   # Claude CLI 호출, 세션/설정/턴 추적/되돌리기
│   ├── session_catalog.py  # ~/.claude/projects/ 트랜스크립트 파싱 (세션 목록·복기)
│   ├── event_poster.py     # 실시간 진행 상황 Slack 포스팅
│   ├── tools_mcp.py        # Slack에서 실행된 Claude용 MCP 도구
│   ├── file_downloader.py  # Slack 파일 다운로드/업로드 검증
│   ├── config.py           # 환경 변수 로드/검증 (pydantic-settings) — 유일한 .env 진입점
│   ├── constants.py        # 공유 상수
│   └── log_setup.py        # 공통 로깅 설정
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
