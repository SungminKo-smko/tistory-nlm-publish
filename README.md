# tistory-nlm-publish

NotebookLM 리서치 결과를 한국어 티스토리 글로 정리하고, 로그인된 headless 브라우저에 CDP로 붙어 비공개 발행과 렌더 검증까지 수행하는 도구입니다.

이 레포는 두 방식으로 사용할 수 있습니다.

- AI 에이전트가 `SKILL.md`를 읽고 정해진 순서대로 실행하는 스킬
- 사람이 직접 CLI를 실행하는 자동화 도구

GitHub에서 clone한 뒤 아래 bootstrap만 실행하면, 어떤 에이전트든 같은 Python/Playwright 실행 환경을 바로 맞출 수 있습니다.

```bash
git clone https://github.com/SungminKo-smko/tistory-nlm-publish.git
cd tistory-nlm-publish
./bin/bootstrap-skill
```

## 구성

- `SKILL.md`: Codex/OpenAI/OpenClaw 기준 실행 규칙
- `AGENTS.md`: AGENTS.md 계열 에이전트용 얇은 adapter
- `CLAUDE.md`: Claude Code 계열 에이전트용 adapter
- `scripts/tistory_nlm_workflow.py`: prepare, validate-tags
- `scripts/publish_tistory.py`: publish, verify-render, verify-public
- `requirements.txt`: Python 의존성
- `bin/bootstrap-skill`: clone 직후 venv + dependency + Playwright bootstrap

## GitHub로부터 설치

### 공통 1회 bootstrap

```bash
git clone https://github.com/SungminKo-smko/tistory-nlm-publish.git
cd tistory-nlm-publish
./bin/bootstrap-skill
```

이 bootstrap은 다음을 자동으로 수행합니다.

- repo `.venv` 생성
- `requirements.txt` 설치
- Playwright Chromium 설치

### Codex / OpenAI / OpenClaw

workspace-local skill로 사용할 때:

```bash
git clone https://github.com/SungminKo-smko/tistory-nlm-publish.git
cd tistory-nlm-publish
./bin/bootstrap-skill
```

그 다음 에이전트에게 이 레포의 `SKILL.md`를 사용하라고 지시하면 됩니다.

예시:

```text
Use the skill at /absolute/path/to/tistory-nlm-publish/SKILL.md
```

전역 Codex skill로 둘 때:

```bash
export CODEX_HOME="${CODEX_HOME:-$HOME/.codex}"
mkdir -p "$CODEX_HOME/skills"
git clone https://github.com/SungminKo-smko/tistory-nlm-publish.git \
  "$CODEX_HOME/skills/tistory-nlm-publish"
cd "$CODEX_HOME/skills/tistory-nlm-publish"
./bin/bootstrap-skill
```

예시:

```text
Use the tistory-nlm-publish skill for this task.
```

### Claude Code

Claude Code에서 repo instruction 파일을 직접 읽게 할 때는 이 레포를 clone한 뒤 `CLAUDE.md`를 가리키면 됩니다.

```bash
git clone https://github.com/SungminKo-smko/tistory-nlm-publish.git
cd tistory-nlm-publish
./bin/bootstrap-skill
```

예시:

```text
Use the instructions at /absolute/path/to/tistory-nlm-publish/CLAUDE.md
```

### OpenCode / AGENTS.md 계열 에이전트

`AGENTS.md`를 읽는 에이전트에서는 이 레포를 clone한 뒤 `AGENTS.md`를 사용하면 됩니다.

```bash
git clone https://github.com/SungminKo-smko/tistory-nlm-publish.git
cd tistory-nlm-publish
./bin/bootstrap-skill
```

예시:

```text
Use the instructions at /absolute/path/to/tistory-nlm-publish/AGENTS.md
```

### AI 에이전트가 추가로 필요로 하는 것

- `nlm` CLI가 설치 및 로그인되어 있어야 함
- Python 3와 venv 사용 가능
- Tistory/Kakao에 로그인된 headless Chromium 계열 브라우저가 있어야 함
- 브라우저가 CDP endpoint로 열려 있어야 함
- 대상 블로그 host를 알고 있어야 함

## 책임 분담

### 에이전트가 하는 일

- `prepare -> validate-tags -> publish -> verify-render -> verify-public` 순서를 지켜 실행
- `manifest.json` 기준으로 run bundle 상태 관리
- private-first 발행, 대표 이미지 업로드, 렌더 검증 수행
- 안전한 범위 안에서 publish 재시도와 상태 복구 시도

### 사용자가 준비해야 하는 것

- `topic`과 필요 시 `research-query`
- 최종 태그 10개
- NotebookLM 로그인 상태
- Tistory/Kakao 로그인된 headless 브라우저와 CDP endpoint
- 대상 블로그 host

### 사용자의 개입이 필요한 경우

- NotebookLM 로그인이 풀린 경우
- headless Tistory 브라우저가 없거나 로그인이 풀린 경우
- 태그 10개가 아직 정해지지 않은 경우
- `post_url` 자동 복구가 실패한 경우
- Tistory UI 상태가 달라져 스크립트가 안전하게 판단할 수 없는 경우

## 사람이 설치할 때

### 1. clone

```bash
git clone https://github.com/SungminKo-smko/tistory-nlm-publish.git
cd tistory-nlm-publish
```

### 2. bootstrap

```bash
./bin/bootstrap-skill
```

기본 실행은 직접 `python scripts/...` 대신 아래 wrapper를 권장합니다. 이 wrapper는 항상 repo의 `.venv`를 사용합니다.

```bash
./bin/tistory-workflow ...
./bin/tistory-publish ...
```

### 3. 기본 환경 변수 설정

가급적 아래 두 값을 먼저 고정해 두는 것을 권장합니다.

```bash
export TISTORY_BLOG_HOST="<blog>.tistory.com"
export OPENCLAW_CDP_URL="http://127.0.0.1:18800"
```

선택 사항:

```bash
export TISTORY_ALLOW_HEADED_CDP=1
```

`TISTORY_ALLOW_HEADED_CDP=1`은 운영 환경이 기존 headed `18800`에 묶여 있고, headless fallback이 불가능하거나 불안정할 때만 켭니다. 기본 경로는 여전히 headless CDP입니다.

블로그 host 해석 우선순위는 다음과 같습니다.

1. `--blog-host`
2. `TISTORY_BLOG_HOST`
3. `manifest.json`에 저장된 host
4. 기존 `post_url`

### 4. 외부 선행 조건

- `nlm` CLI 설치
- `nlm login --check` 통과
- Tistory/Kakao 로그인된 headless Chromium 브라우저 준비
- 브라우저를 CDP 포트와 함께 실행
- 선택 사항: 로그인 페이지가 감지될 때만 사용할 Tistory 로그인 비밀값 준비

### 5. 선택: 로그인 자동 복구용 비밀값 설정

기본 경로는 **이미 로그인된 브라우저 세션 재사용**입니다. 아래 비밀값은 발행 preflight 중 실제로 로그인 페이지가 감지된 경우에만 사용됩니다. 이미 로그인된 정상 경로에서는 읽지 않아도 됩니다.

우선순위는 다음과 같습니다.

1. 환경 변수 `TISTORY_LOGIN_EMAIL`, `TISTORY_LOGIN_PASSWORD`
2. 로컬 파일 `~/.openclaw/secrets/tistory-login.json`

환경 변수는 둘 다 함께 설정해야 하며, 설정되어 있으면 로컬 파일보다 우선합니다.

```bash
export TISTORY_LOGIN_EMAIL="you@example.com"
export TISTORY_LOGIN_PASSWORD="<your-password>"
```

또는 저장소 밖의 로컬 파일을 사용합니다.

```bash
mkdir -p "$HOME/.openclaw/secrets"
cat > "$HOME/.openclaw/secrets/tistory-login.json" <<'JSON'
{
  "email": "you@example.com",
  "password": "<your-password>"
}
JSON
chmod 600 "$HOME/.openclaw/secrets/tistory-login.json"
```

주의:

- 비밀값 파일은 반드시 저장소 **밖**에 둡니다.
- 파일 권한이 `600`보다 넓으면 스크립트가 거부합니다.
- 비밀번호를 `manifest.json`, `publish.log`, tracked 파일, 메모 파일에 적지 마세요.
- 자동 로그인은 Kakao/Tistory 로그인 페이지로 실제 이동했을 때만 시도합니다.
- 비밀값이 없거나 로그인 UI가 달라 자동 로그인이 실패하면, 기존처럼 수동 로그인 후 재시도 경로를 유지합니다.

예시:

```bash
/Applications/Google\ Chrome.app/Contents/MacOS/Google\ Chrome \
  --headless=new \
  --remote-debugging-port=18800 \
  --user-data-dir="$HOME/.tistory-headless-profile" \
  about:blank
```

기본 권장은 headless CDP 세션입니다. 다만 `--cdp-url`가 headed 브라우저를 가리키더라도, 스크립트는 가능한 경우 자동으로 별도 headless Chromium 세션을 띄우고 티스토리 쿠키를 넘겨받아 이어서 진행합니다. 운영 서버가 기존 headed `18800`에 묶여 있다면 `--allow-headed-cdp` 또는 `TISTORY_ALLOW_HEADED_CDP=1`로 직접 attach 호환 모드를 켤 수 있습니다.

발행 전에 같은 프로필로 대상 블로그의 새 글 화면을 한 번 열어 두면 context 선택이 더 안정적입니다.

```text
https://<blog>.tistory.com/manage/newpost/
```

## 사용 순서

### 1. prepare

```bash
./bin/tistory-workflow prepare \
  --topic "<topic>" \
  --research-query "<research query>" \
  --runs-dir runs
```

출력:

- `runs/<run_id>/post_raw.md`
- `runs/<run_id>/post.md`
- `runs/<run_id>/thumbnail.png`
- `runs/<run_id>/manifest.json`
- `runs/<run_id>/workflow.log`

### 2. validate-tags

```bash
./bin/tistory-workflow validate-tags \
  --run-dir runs/<run_id> \
  --tags "tag1,tag2,tag3,tag4,tag5,tag6,tag7,tag8,tag9,tag10"
```

규칙:

- 정확히 10개
- 중복 금지
- 빈 값 금지

### 3. private publish

먼저 기본 블로그 주소를 환경 변수로 잡아두는 것을 권장합니다.

```bash
export TISTORY_BLOG_HOST="<blog>.tistory.com"
```

```bash
./bin/tistory-publish publish \
  --run-dir runs/<run_id> \
  --cdp-url "http://127.0.0.1:18800"
```

특징:

- attach 입력은 `--cdp-url`, `--blog-host`
- 블로그 host는 `--blog-host`가 최우선이고, 없으면 `TISTORY_BLOG_HOST`, 그다음 manifest 값을 사용
- shared headless browser에서 대상 host에 맞는 context를 탐색
- preflight가 로그인 페이지를 감지하면 로컬 비밀값으로 1회 로그인 복구를 시도하고, 아니면 기존 로그인 세션 경로를 그대로 사용
- 비밀값이 없거나 로그인 UI가 달라 자동 로그인이 실패하면 기존처럼 수동 로그인 fallback으로 멈춤
- 비공개 선택 확인 전에는 최종 발행을 진행하지 않음
- publish checkpoint, attempt, screenshot 경로를 manifest에 기록

환경 변수를 이미 설정했다면 더 짧게 실행할 수 있습니다.

```bash
./bin/tistory-publish publish --run-dir runs/<run_id>
```

### 4. private render verify

```bash
./bin/tistory-publish verify-render \
  --run-dir runs/<run_id> \
  --cdp-url "http://127.0.0.1:18800"
```

환경 변수를 이미 설정했다면 아래처럼 실행해도 됩니다.

```bash
./bin/tistory-publish verify-render --run-dir runs/<run_id>
```

`post_url` 자동 감지가 실패한 경우:

```bash
./bin/tistory-publish verify-render \
  --run-dir runs/<run_id> \
  --cdp-url "http://127.0.0.1:18800" \
  --post-url "https://<blog>.tistory.com/<post-id>"
```

### 5. optional public verify

```bash
./bin/tistory-publish verify-public \
  --run-dir runs/<run_id> \
  --public-url "https://<blog>.tistory.com/<post-id>"
```

## Manifest 상태

`manifest.json`은 아래 핵심 상태를 유지합니다.

- `publish.status`
- `publish.attempts`
- `publish.checkpoints`
- `publish.last_screenshot`
- `publish.editor_variant`
- `publish.context_index`
- `verification.render`
- `verification.public`

대표 상태:

- `not_started`
- `in_progress`
- `published_private`
- `pending_confirmation`
- `failed`

## 트러블슈팅

### `ModuleNotFoundError`

```bash
source .venv/bin/activate
pip install -r requirements.txt
```

### `No attached browser context matched ...`

- 로그인된 브라우저인지 확인
- headed 18800만 떠 있어도 자동 fallback이 가능하지만, 크롬 실행 파일을 찾지 못하면 `TISTORY_CHROME_BIN`을 지정
- 대상 블로그 새 글 화면을 먼저 열기
- 다시 `publish` 실행

### `Publish confirmation signal was not detected`

다음을 함께 확인합니다.

- `publish.log`
- `manifest.json`
- `publish.last_screenshot`

## 관련 문서

- [SKILL.md](./SKILL.md)
