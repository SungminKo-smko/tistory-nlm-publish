# tistory-nlm-publish

NotebookLM 리서치 결과를 한국어 블로그 글로 정리하고, 티스토리에 비공개 발행한 뒤 렌더 검증까지 수행하는 스킬이다.

이 문서는 **에이전트/유지보수자용 운영 문서**다. 빠른 실행 규칙은 `SKILL.md`를 따르고, 여기서는 설치/의존성/동작 구조/트러블슈팅을 정리한다.

## 1. 목적

이 스킬은 아래 흐름을 고정된 순서로 실행한다.

1. NotebookLM 리서치/보고서/인포그래픽 생성
2. 마크다운 정리 및 참고소스/이미지 보강
3. 정확히 10개의 태그 검증
4. 티스토리 비공개 발행
5. 렌더 검증

핵심 원칙:
- 임의로 순서를 바꾸지 않는다
- `manifest.json`을 단일 진실원으로 사용한다
- 공개 발행은 자동화하지 않는다

## 2. 선행 설치

### 필수 CLI

다음 명령이 PATH에 있어야 한다.

- `nlm`
- `agent-browser`
- `python3`

`nlm`은 이 프로젝트의 내장 명령이 아니라, 아래 저장소 기반 CLI 의존성이다.

- https://github.com/jacob-bd/notebooklm-mcp-cli

즉, 이 스킬을 쓰기 전에 **`notebooklm-mcp-cli`가 설치되어 있어야** 하고, 설치 후 `nlm` 명령이 PATH에서 실행 가능해야 한다.

예시 설치 흐름:

```bash
# notebooklm-mcp-cli 설치
# 설치 방법은 저장소 문서를 따른다:
# https://github.com/jacob-bd/notebooklm-mcp-cli

# 설치 확인
nlm --help
nlm login --check
```

### Python 의존성

```bash
cd skills/tistory-nlm-publish
python3 -m venv .venv
source .venv/bin/activate
pip install -U pip
pip install -r requirements.txt
python -m playwright install chromium
```

### NotebookLM 인증

`notebooklm-mcp-cli` 설치 후, `prepare` 전에 반드시 아래가 통과해야 한다.

```bash
nlm login --check
```

## 3. 티스토리 로그인 의존성

발행은 `agent-browser`의 영속 세션 `tistory-publisher`를 사용한다.

자동 로그인용 환경변수:

- `TISTORY_LOGIN_EMAIL`
- `TISTORY_LOGIN_PASSWORD`

역할:
- 티스토리 편집기 진입 시 카카오 로그인 화면이 나오면 자동 입력에 사용
- 세션이 살아 있으면 재사용되므로 항상 필요하지는 않음
- 다만 세션 만료 상황까지 안정적으로 처리하려면 설정해두는 편이 좋음

주의:
- 로그인 화면 구조가 바뀌면 자동 로그인 로직이 깨질 수 있음
- 그 경우 `publish.log`와 `01_after_kakao_login_click.png`를 먼저 확인

## 4. 파일 구조

```text
skills/tistory-nlm-publish/
├── SKILL.md
├── README.md
├── requirements.txt
└── scripts/
    ├── tistory_nlm_workflow.py
    ├── publish_tistory_browser.py
    └── publish_tistory.py
```

주요 산출물:

```text
runs/<run_id>/
├── post_raw.md
├── post.md
├── post.html
├── thumbnail.png
├── manifest.json
├── workflow.log
└── publish.log
```

## 5. 실제 사용 순서

### 5.1 prepare

```bash
python scripts/tistory_nlm_workflow.py prepare \
  --topic "<title>" \
  --research-query "<query>" \
  --runs-dir runs
```

이 단계에서 하는 일:
- NotebookLM 로그인 확인
- 같은 제목의 notebook 재사용 또는 새 notebook 생성
- 리서치 시작/대기/소스 import
- 보고서/인포그래픽 생성 및 다운로드
- 본문 마크다운 정리
- 참고소스 목록 재작성
- 본문 이미지 보강
- `manifest.json` 생성

### 5.2 validate-tags

```bash
python scripts/tistory_nlm_workflow.py validate-tags \
  --run-dir runs/<run_id> \
  --tags "tag1,tag2,tag3,tag4,tag5,tag6,tag7,tag8,tag9,tag10"
```

규칙:
- 정확히 10개
- 중복 금지
- 빈 값 금지

### 5.3 publish

```bash
python scripts/publish_tistory_browser.py publish \
  --run-dir runs/<run_id> \
  --blog-host "<blog>.tistory.com"
```

이 단계에서 하는 일:
- 티스토리 편집기 진입
- 필요 시 카카오 자동 로그인
- 인포그래픽 업로드
- 마크다운 모드 전환
- 제목/본문/태그 입력
- 대표 이미지 확인
- 비공개 발행
- post URL 추출 및 `manifest.json` 갱신

### 5.4 verify-render

```bash
python scripts/publish_tistory_browser.py verify-render \
  --run-dir runs/<run_id> \
  --post-url "https://<blog>.tistory.com/<post-id>"
```

성격:
- **로그인된 브라우저 세션으로 비공개 글 렌더 확인**

현재 검증 기준:
- 페이지 제목 일치
- raw markdown 대량 누출 없음
- 아래 둘 중 하나 충족
  - legacy 섹션명(`핵심요약`, `핵심이슈`) 존재
  - 구조화된 브리핑 본문으로 판단 가능(헤딩/본문 길이/이미지)

### 5.5 verify-public

```bash
python scripts/publish_tistory_browser.py verify-public \
  --run-dir runs/<run_id> \
  --public-url "https://<blog>.tistory.com/<post-id>"
```

성격:
- **공개된 URL만** 검사
- requests 기반 검사
- `og:image`까지 확인

## 6. 최근 수정된 운영 포인트

### 6.1 카카오 로그인 전환 대응

이전에는 `카카오계정으로 로그인` 클릭 뒤 같은 화면에서 즉시 입력창이 나타난다고 가정했다.
현재는:
- 클릭 후 URL 전환을 기록
- 로그인 폼 등장까지 polling
- 전이 중 스크린샷 저장

그래서 로그인 단계가 더 안정적으로 동작한다.

### 6.2 NotebookLM source 중복 제거

`research import` 이후 같은 URL이 두 번씩 들어오는 케이스가 있어, 현재는 아래 시점에 URL 기준 dedupe를 수행한다.

- import 직후
- 기존 notebook 재사용 시

의도:
- 동일한 effective source가 반복 누적되는 것을 방지
- 제목만 같은 다른 문서를 실수로 지우지 않도록 **URL 기반으로만** dedupe

### 6.3 렌더 검증 완화

기존에는 `핵심요약`, `핵심이슈`가 정확히 있어야 통과했다.
현재는 시장/뉴스 브리핑 글처럼 구조가 분명하면 통과할 수 있도록 fallback이 추가되어 있다.

## 7. 트러블슈팅

### 로그인 실패

확인 순서:
1. `TISTORY_LOGIN_EMAIL`, `TISTORY_LOGIN_PASSWORD` 설정 여부
2. `publish.log` 확인
3. `01_editor_loaded.png`, `01_after_kakao_login_click.png` 확인
4. 카카오 로그인 UI 변경 여부 확인

### publish 실패

주요 확인 포인트:
- `manifest.json`에 태그가 있는지
- `thumbnail.png`가 존재하는지
- 티스토리 편집기에서 대표 이미지 업로드 컨트롤이 보이는지
- 비공개 버튼/저장 버튼 셀렉터가 바뀌지 않았는지

### verify-render 실패

주요 원인:
- 비공개 글인데 로그인 세션이 풀림
- 본문 렌더 영역 선택자 변경
- raw markdown 누출
- 본문이 너무 짧거나 구조가 약해서 fallback 기준 미달

### NotebookLM source 중복

확인 방법:
- `nlm source list <notebook_id> --json`
- 같은 URL이 2회 이상 보이는지 확인

현재 스크립트는 재사용/재import 시 자동 정리한다.

## 8. 커밋/운영 메모

이 스킬은 실행 중 다음이 자주 변한다.
- 로그인 플로우
- 티스토리 편집기 셀렉터
- NotebookLM import 결과 형태

그래서 변경 시에는 가급적 아래까지 같이 확인하는 편이 좋다.
- `python -m py_compile scripts/*.py`
- 실제 private publish 1회
- `verify-render` 성공 여부
