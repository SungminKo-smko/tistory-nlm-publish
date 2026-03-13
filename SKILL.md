---
name: tistory-nlm-publish
description: Deterministic workflow for generating a Korean NotebookLM report, cleaning markdown, enriching source-body images, and publishing to Tistory through a manifest-driven private-first CDP workflow.
---

# Tistory + NotebookLM Deterministic Publishing Skill

This skill must not improvise the workflow.

## Environment setup (venv)

Create and use a local virtual environment before running scripts.

```bash
cd <skill-root>
python3 -m venv .venv
source .venv/bin/activate
pip install -U pip
pip install -r requirements.txt
python -m playwright install chromium
```

The publish flow is CDP attach only.

- Do not pass `--user-data-dir` in new runs.
- Use a browser that is already logged in to Kakao/Tistory and exposed over CDP.
- Default CDP endpoint is `http://127.0.0.1:18800`, or pass `--cdp-url`.

Always execute the fixed script pipeline in the exact order below.

## Pipeline

1. Prepare NotebookLM artifacts and local publishing bundle
2. Validate exactly 10 unique SEO tags
3. Publish privately to Tistory using `manifest.json`
4. Verify the rendered private post while logged in
5. Optionally verify a public URL only after the post is intentionally made public

## Required scripts

- `python scripts/tistory_nlm_workflow.py prepare ...`
- `python scripts/tistory_nlm_workflow.py validate-tags ...`
- `python scripts/publish_tistory.py publish ...`
- `python scripts/publish_tistory.py verify-render ...`

Optional:

- `python scripts/publish_tistory.py verify-public ...`

## Behavioral rules

- Do not manually re-order steps.
- Do not regenerate artifacts if `manifest.json` already exists unless the user explicitly wants a fresh run.
- Do not publish publicly from automation. Always choose private publish.
- Use `manifest.json` as the single source of truth for title, markdown path, thumbnail path, tags, publish checkpoints, and verification state.
- Do not infer alternate file paths if the manifest already defines them.
- Fail loudly if any hard gate fails.
- If safe private publish controls are not found, stop instead of falling back to generic public-facing buttons.

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
python scripts/publish_tistory.py publish \
  --run-dir runs/<run_id> \
  --blog-host "<blog>.tistory.com" \
  --cdp-url "http://127.0.0.1:18800"
```

Expected behavior:

- attach to the running browser over CDP
- preflight the logged-in Tistory context for the target host
- open the new-post editor for that host
- force markdown-capable editor mode
- fill title/body/tags from `manifest.json`
- open publish dialog
- upload representative image from the local thumbnail file
- force private publish selection
- click only a safe private submit button
- persist publish checkpoints and result into `manifest.json`

If the publish success signal exists but `post_url` was not auto-detected, the manifest may remain in `pending_confirmation` until a concrete post URL is supplied for `verify-render`.

Deprecated compatibility flags:

- `--edit-url`
- `--user-data-dir`

Do not use them in new runs.

## Step 4. Verify rendered private post

Run:

```bash
python scripts/publish_tistory.py verify-render \
  --run-dir runs/<run_id> \
  --cdp-url "http://127.0.0.1:18800"
```

Hard gates:

- the logged-in browser can open the rendered post URL
- rendered body contains required sections
- no major raw markdown leakage
- at least one body image exists
- rendered page appears to match the manifest title
- `manifest.json` is updated at `verification.render`

If `post_url` was not auto-detected during publish, pass:

```bash
python scripts/publish_tistory.py verify-render \
  --run-dir runs/<run_id> \
  --cdp-url "http://127.0.0.1:18800" \
  --post-url "https://<blog>.tistory.com/<post-id>"
```

## Step 5. Optional public verification

Run this only after the post is intentionally made public:

```bash
python scripts/publish_tistory.py verify-public \
  --run-dir runs/<run_id> \
  --public-url "https://<blog>.tistory.com/<post-id>"
```

Hard gates:

- public page opens successfully
- rendered body contains required sections
- no major raw markdown leakage
- at least one body image exists
- `og:image` exists and is not the Tistory placeholder
- `manifest.json` is updated at `verification.public`

Completion must not be declared unless the required hard gates for the executed flow have passed.
