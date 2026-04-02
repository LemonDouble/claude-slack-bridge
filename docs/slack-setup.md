# Slack 앱 설정 가이드

## 1단계 — Slack 앱 생성

1. https://api.slack.com/apps 접속
2. **Create New App** → **From scratch** 클릭
3. 앱 이름 입력 (예: `claude-bridge`)
4. 워크스페이스 선택
5. **Create App** 클릭

---

## 2단계 — Bot 토큰 발급 (`xoxb-...`)

1. 좌측 사이드바에서 **OAuth & Permissions** 클릭
2. **Bot Token Scopes**에서 아래 스코프 추가:

   | 스코프 | 용도 |
   |---|---|
   | `chat:write` | 메시지 전송 |
   | `channels:history` | 공개 채널 답변 읽기 |
   | `groups:history` | 비공개 채널 답변 읽기 |
   | `im:history` | DM 답변 읽기 |
   | `im:write` | DM 대화 열기 |
   | `files:write` | 파일 업로드 |

3. 상단으로 스크롤 → **Install to Workspace** 클릭
4. **Allow** 클릭
5. **Bot User OAuth Token** 복사 (`xoxb-...`로 시작)

---

## 3단계 — Socket Mode 활성화 및 App 토큰 발급 (`xapp-...`)

1. 좌측 사이드바에서 **Socket Mode** 클릭
2. **Enable Socket Mode** → ON
3. App-Level Token 생성 프롬프트가 나타남 → 또는 **Settings → Basic Information → App-Level Tokens**로 이동
4. 토큰 이름 입력 (예: `socket-mode`)
5. 스코프 추가: `connections:write`
6. **Generate** 클릭
7. 토큰 복사 (`xapp-...`로 시작)

---

## 4단계 — Event Subscriptions 활성화

1. 좌측 사이드바에서 **Event Subscriptions** 클릭
2. **Enable Events** → ON
3. **Subscribe to bot events**에서 추가:
   - `message.channels` — 공개 채널 메시지
   - `message.groups` — 비공개 채널 메시지
   - `message.im` — DM 메시지
4. **Save Changes** 클릭
5. 프롬프트가 나타나면 앱 재설치 (**OAuth & Permissions** → **Reinstall to Workspace**)

---

## 5단계 — Interactivity 활성화

Slack → Claude 방향의 프로젝트 선택 UI(Block Kit 버튼, 모달)를 사용하려면 활성화 필요합니다.

1. 좌측 사이드바에서 **Interactivity & Shortcuts** 클릭
2. **Interactivity** → ON
3. Socket Mode가 페이로드를 처리하므로 Request URL은 필요 없음 — 토글만 켜면 됩니다
4. **Save Changes** 클릭

---

## 6단계 — 채널 생성 및 봇 초대

1. Slack에서 **+** → **Create channel** 클릭
2. 프로젝트에 맞는 이름 설정 (예: `chat-claude`)
3. 채널 생성 후, 채널 설정에서 봇 앱을 추가

---

## 앱 이름 변경 (선택)

### 앱 이름 변경
1. https://api.slack.com/apps → 앱 선택
2. **Settings → Basic Information**에서 **App Name** 수정
3. **Save Changes**

### 봇 표시 이름 변경
1. **App Home** → **Your App's Presence in Slack** → **Edit** 클릭
2. 표시 이름 수정 후 저장

### 변경 적용
앱 재설치 필요: **OAuth & Permissions** → **Reinstall to Workspace** → **Allow**

---

## 결과 요약

| 변수 | 값 | 설정 위치 |
|---|---|---|
| `SLACK_BOT_TOKEN` | `xoxb-...` | `.env` |
| `SLACK_APP_TOKEN` | `xapp-...` | `.env` |
| `SLACK_CHANNEL` | `#채널이름` | MCP 설정 (프로젝트별) |
