---
name: tistory-nlm-publish
description: Deterministic workflow for generating a Korean NotebookLM report, cleaning markdown, enriching source-body images, and publishing to Tistory through a manifest-driven private-first agent-browser workflow.
---

# Tistory + NotebookLM Deterministic Publishing Skill

This skill must not improvise the workflow.

## Environment setup (venv)

Prerequisites:
- `nlm` CLI installed and authenticated (`nlm login --check` must pass)
- `nlm` is provided by `notebooklm-mcp-cli`: https://github.com/jacob-bd/notebooklm-mcp-cli
- `agent-browser` CLI installed and available on PATH
- Python 3 available locally

Create and use a local virtual environment before running scripts.

```bash
cd <skill-root>
python3 -m venv .venv
source .venv/bin/activate
pip install -U pip
pip install -r requirements.txt
python -m playwright install chromium
```

The publish flow uses agent-browser with persistent sessions.

- Browser state is persisted via `--session-name tistory-publisher`
- Login page can be handled automatically using env vars:
  - `TISTORY_LOGIN_EMAIL`
  - `TISTORY_LOGIN_PASSWORD`
- Those env vars are only used when Tistory/Kakao login is required or the saved session expired
- Subsequent runs reuse the saved session automatically
- No need for CDP server or browser flags

Always execute the fixed script pipeline in the exact order below.

## Pipeline

1. Prepare NotebookLM artifacts and local publishing bundle
2. Validate exactly 10 unique SEO tags
3. Publish privately to Tistory using `manifest.json`
4. Verify the rendered private post while logged in
5. Optionally verify a public URL only after the post is intentionally made public

## Execution Mode

**AUTOMATIC MODE (Default)**: Execute all steps 1-4 sequentially without user confirmation between steps. Only stop if a hard gate fails.

When the user provides:
- Topic
- Research query (or use topic as query)
- Blog host (e.g., `mini-sugar.tistory.com`)
- Optional: 10 SEO tags (or auto-generate from topic)

Execute immediately in sequence:
1. `prepare` → 2. `validate-tags` → 3. `publish` → 4. `verify-render`

Do NOT ask for confirmation between steps. Do NOT pause to show intermediate results unless a step fails.

## Required scripts

- `python scripts/tistory_nlm_workflow.py prepare ...`
- `python scripts/tistory_nlm_workflow.py validate-tags ...`
- `python scripts/publish_tistory_browser.py publish ...`
- `python scripts/publish_tistory_browser.py verify-render ...`

Optional:

- `python scripts/publish_tistory_browser.py verify-public ...`

Note: Uses agent-browser CLI (Vercel) instead of Playwright CDP for browser automation.

## Behavioral rules

- **Execute all steps automatically without user confirmation between steps.**
- Do not manually re-order steps.
- Do not regenerate artifacts if `manifest.json` already exists unless the user explicitly wants a fresh run.
- Do not publish publicly from automation. Always choose private publish.
- Use `manifest.json` as the single source of truth for title, markdown path, thumbnail path, tags, publish checkpoints, and verification state.
- Do not infer alternate file paths if the manifest already defines them.
- Fail loudly if any hard gate fails.
- If safe private publish controls are not found, stop instead of falling back to generic public-facing buttons.
- Imported NotebookLM sources are deduplicated by normalized source URL after import and when reusing an existing notebook.
- **Report only final result after verify-render completes successfully.**

## Step 1. Prepare

Run:

```bash
python scripts/tistory_nlm_workflow.py prepare \
  --topic "<topic>" \
  --research-query "<research query>" \
  --runs-dir runs
```

This creates:

- `runs/<run_id>/post_raw.md`
- `runs/<run_id>/post.md`
- `runs/<run_id>/post.html`
- `runs/<run_id>/thumbnail.png`
- `runs/<run_id>/manifest.json`
- `runs/<run_id>/workflow.log`

Manifest defaults now include:

- `blog.host/home_url/edit_url`
- `publish.status/visibility/current_step/attempts/checkpoints/post_url`
- `verification.render`
- `verification.public`

## Step 2. Validate tags

Run:

```bash
python scripts/tistory_nlm_workflow.py validate-tags \
  --run-dir runs/<run_id> \
  --tags "tag1,tag2,tag3,tag4,tag5,tag6,tag7,tag8,tag9,tag10"
```

Hard rules:

- exactly 10 tags
- no duplicates
- no empty values

## Step 3. Publish privately

Run:

```bash
python scripts/publish_tistory_browser.py publish \
  --run-dir runs/<run_id> \
  --blog-host "<blog>.tistory.com"
```

Expected behavior:

- opens Tistory editor using agent-browser with persistent session
- auto-logins if login page appears (using `TISTORY_LOGIN_EMAIL` / `TISTORY_LOGIN_PASSWORD`)
- switches to markdown mode
- pre-registers dialog accept before markdown conversion click
- fills title/body/tags from `manifest.json`
- opens publish dialog
- selects private publish
- clicks private save/publish button
- extracts and returns post URL
- updates `manifest.json` with publish status

## Step 4. Verify rendered private post

Run:

```bash
python scripts/publish_tistory_browser.py verify-render \
  --run-dir runs/<run_id>
```

Hard gates:

- rendered page title matches expected title
- no major raw markdown leakage
- rendered body must either contain the legacy required sections (`핵심요약`, `핵심이슈`) or qualify as a structured briefing/article with headings, sufficient body length, and at least one body image
- `manifest.json` is updated at `verification.render`

If `post_url` was not auto-detected during publish, pass:

```bash
python scripts/publish_tistory_browser.py verify-render \
  --run-dir runs/<run_id> \
  --post-url "https://<blog>.tistory.com/<post-id>"
```

## Step 5. Optional public verification

Run this only after the post is intentionally made public:

```bash
python scripts/publish_tistory_browser.py verify-public \
  --run-dir runs/<run_id> \
  --public-url "https://<blog>.tistory.com/<post-id>"
```

Hard gates:

- public page opens successfully
- rendered body passes the same content validation rule used by `verify-render` (legacy required sections or structured rendered content)
- no major raw markdown leakage
- `og:image` exists and is not the Tistory placeholder
- `manifest.json` is updated at `verification.public`

Completion must not be declared unless the required hard gates for the executed flow have passed.

For deeper operational notes, troubleshooting, and maintenance details, read `README.md` in this skill directory.
