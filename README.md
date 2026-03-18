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

### 3. 외부 선행 조건

- `nlm` CLI 설치
- `nlm login --check` 통과
- Tistory/Kakao 로그인된 headless Chromium 브라우저 준비
- 브라우저를 CDP 포트와 함께 실행

예시:

```bash
/Applications/Google\ Chrome.app/Contents/MacOS/Google\ Chrome \
  --headless=new \
  --remote-debugging-port=18800 \
  --user-data-dir="$HOME/.tistory-headless-profile" \
  about:blank
```

`publish_tistory.py`는 headed CDP endpoint를 거부합니다. 반드시 headless 세션을 사용해야 합니다.

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

```bash
./bin/tistory-publish publish \
  --run-dir runs/<run_id> \
  --blog-host "<blog>.tistory.com" \
  --cdp-url "http://127.0.0.1:18800"
```

특징:

- attach 입력은 `--cdp-url`, `--blog-host`
- shared headless browser에서 대상 host에 맞는 context를 탐색
- 비공개 선택 확인 전에는 최종 발행을 진행하지 않음
- publish checkpoint, attempt, screenshot 경로를 manifest에 기록

### 4. private render verify

```bash
./bin/tistory-publish verify-render \
  --run-dir runs/<run_id> \
  --cdp-url "http://127.0.0.1:18800"
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
- headless 모드로 실행했는지 확인
- 대상 블로그 새 글 화면을 먼저 열기
- 다시 `publish` 실행

### `Publish confirmation signal was not detected`

다음을 함께 확인합니다.

- `publish.log`
- `manifest.json`
- `publish.last_screenshot`

## 관련 문서

- [SKILL.md](./SKILL.md)
