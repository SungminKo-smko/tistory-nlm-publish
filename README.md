# tistory-nlm-publish

Private-first Tistory publishing workflow for NotebookLM content.

이 레포는 다음 두 가지 용도로 사용할 수 있습니다.

- AI 에이전트가 `SKILL.md`를 읽고 정해진 파이프라인대로 실행하는 스킬
- 사람이 직접 `prepare -> validate-tags -> publish -> verify-render` 흐름을 실행하는 자동화 도구

## What This Repo Does

- NotebookLM 결과물을 로컬 발행 번들로 준비합니다.
- 마크다운/HTML/썸네일/manifest를 한 run 디렉터리에 정리합니다.
- Tistory 편집기에 CDP attach 방식으로 붙어서 비공개 발행을 시도합니다.
- private 렌더 검증과 optional public 검증을 분리합니다.

## Repository Layout

- `SKILL.md`: AI 에이전트용 실행 규칙
- `requirements.txt`: Python 의존성
- `scripts/tistory_nlm_workflow.py`: NotebookLM prepare, tag validation
- `scripts/publish_tistory.py`: Tistory publish, private render verify, public verify

## Install For AI Agents

### Option 1. Use as a workspace-local skill

이 레포를 그대로 checkout 한 뒤, AI 에이전트가 이 폴더를 작업 디렉터리 또는 참고 가능한 workspace 안에서 보게 하면 됩니다.

예시:

```bash
git clone https://github.com/SungminKo-smko/tistory-nlm-publish.git
cd tistory-nlm-publish
```

그 다음 에이전트에게 이 레포의 `SKILL.md`를 사용하라고 지시하면 됩니다.

예시 프롬프트:

```text
Use the skill at /absolute/path/to/tistory-nlm-publish/SKILL.md
```

### Option 2. Install as a global Codex skill

Codex 계열 환경에서 전역 스킬처럼 쓰고 싶으면 `$CODEX_HOME/skills` 아래에 배치하면 됩니다.

```bash
export CODEX_HOME="${CODEX_HOME:-$HOME/.codex}"
mkdir -p "$CODEX_HOME/skills"
git clone https://github.com/SungminKo-smko/tistory-nlm-publish.git \
  "$CODEX_HOME/skills/tistory-nlm-publish"
```

설치 후 에이전트에게 `tistory-nlm-publish` 스킬을 사용하라고 지시하면 됩니다.

예시:

```text
Use the tistory-nlm-publish skill for this Tistory publishing task.
```

### What the AI agent needs

AI 에이전트가 실제 발행까지 수행하려면 아래 조건이 필요합니다.

- Python 가상환경과 의존성이 설치되어 있어야 함
- `nlm` CLI가 로그인된 상태여야 함
- Tistory/Kakao 로그인 상태의 브라우저가 CDP endpoint로 열려 있어야 함
- 대상 블로그 host를 알고 있어야 함

## Install For Humans

### 1. Clone the repository

```bash
git clone https://github.com/SungminKo-smko/tistory-nlm-publish.git
cd tistory-nlm-publish
```

### 2. Create a virtual environment

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -U pip
pip install -r requirements.txt
python -m playwright install chromium
```

### 3. Prepare external prerequisites

- `nlm` CLI 설치 및 로그인
- Tistory/Kakao에 로그인된 Chromium 계열 브라우저 준비
- 그 브라우저를 CDP endpoint와 함께 실행

예시:

```bash
/Applications/Google\ Chrome.app/Contents/MacOS/Google\ Chrome \
  --remote-debugging-port=18800
```

발행 전에 브라우저에서 대상 블로그의 새 글 화면을 한 번 직접 열어 두면 context 선택이 더 안정적입니다.

예시:

```text
https://<blog>.tistory.com/manage/newpost/
```

## Human Workflow

### 1. Prepare NotebookLM artifacts

```bash
python scripts/tistory_nlm_workflow.py prepare \
  --topic "<topic>" \
  --research-query "<research query>" \
  --runs-dir runs
```

출력:

- `runs/<run_id>/post_raw.md`
- `runs/<run_id>/post.md`
- `runs/<run_id>/post.html`
- `runs/<run_id>/thumbnail.png`
- `runs/<run_id>/manifest.json`

### 2. Validate exactly 10 unique tags

```bash
python scripts/tistory_nlm_workflow.py validate-tags \
  --run-dir runs/<run_id> \
  --tags "tag1,tag2,tag3,tag4,tag5,tag6,tag7,tag8,tag9,tag10"
```

규칙:

- 정확히 10개
- 중복 금지
- 빈 값 금지

### 3. Publish privately to Tistory

```bash
python scripts/publish_tistory.py publish \
  --run-dir runs/<run_id> \
  --blog-host "<blog>.tistory.com" \
  --cdp-url "http://127.0.0.1:18800"
```

설명:

- attach 대상은 `--cdp-url`
- 대상 블로그 식별자는 `--blog-host`
- `--user-data-dir`는 더 이상 기본 경로가 아니며 deprecated 호환만 남아 있음

### 4. Verify the private rendered post

```bash
python scripts/publish_tistory.py verify-render \
  --run-dir runs/<run_id> \
  --cdp-url "http://127.0.0.1:18800"
```

만약 `post_url` 자동 감지에 실패했다면 명시적으로 넘길 수 있습니다.

```bash
python scripts/publish_tistory.py verify-render \
  --run-dir runs/<run_id> \
  --cdp-url "http://127.0.0.1:18800" \
  --post-url "https://<blog>.tistory.com/<post-id>"
```

### 5. Optional public verification

이 단계는 글을 의도적으로 공개 전환했을 때만 실행합니다.

```bash
python scripts/publish_tistory.py verify-public \
  --run-dir runs/<run_id> \
  --public-url "https://<blog>.tistory.com/<post-id>"
```

## Publish State Model

`manifest.json`에는 아래 상태가 누적됩니다.

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

`pending_confirmation`은 publish 성공 신호는 있었지만 `post_url`이 자동 감지되지 않았을 때 사용됩니다.

## Safety Rules

- 기본 운영 모드는 `private first`입니다.
- private 선택이 확인되지 않으면 최종 발행 버튼을 누르지 않도록 설계되어 있습니다.
- public 검증은 별도 명령으로만 수행합니다.
- broad selector 대신 보수적인 selector와 상태 확인을 우선합니다.

## Troubleshooting

### `ModuleNotFoundError`

가상환경이 활성화되지 않았거나 의존성이 설치되지 않은 상태입니다.

```bash
source .venv/bin/activate
pip install -r requirements.txt
```

### `No attached browser context matched ...`

CDP 브라우저에 대상 Tistory blog host 탭이나 쿠키가 없는 상태입니다.

- 브라우저를 로그인 상태로 유지
- 대상 블로그 새 글 화면을 먼저 열기
- 다시 `publish` 실행

### `Publish confirmation signal was not detected`

Tistory UI가 바뀌었거나 publish dialog 흐름이 예상과 달랐을 가능성이 큽니다.

- `publish.log`
- `manifest.json`
- 마지막 screenshot 경로

를 함께 확인하세요.

## Related File

- Skill instructions: [SKILL.md](./SKILL.md)
