---
name: tistory-nlm-publish
description: Prepare NotebookLM-derived Korean blog bundles, validate publish inputs, and publish to Tistory through a manifest-driven private-first CDP workflow. Use when Codex needs to run or repair the end-to-end `prepare -> validate-tags -> publish` flow for this repo.
---

# Tistory NLM Publish

Run the deterministic pipeline. Do not improvise alternative flows.

## Install from GitHub

If this skill was cloned from GitHub, bootstrap it before first use:

```bash
./bin/bootstrap-skill
```

Adapter files are included for agents that prefer repo-local instruction files:

- `SKILL.md` for Codex/OpenAI/OpenClaw
- `CLAUDE.md` for Claude Code style use
- `AGENTS.md` for AGENTS.md-aware agents

## Check prerequisites

- Confirm NotebookLM auth before using `prepare`.
- Confirm Python dependencies from `requirements.txt` are installed before running scripts.
- Confirm a browser session is already logged in to Tistory/Kakao and exposed over CDP before `publish`.
- Optional: prepare secret-backed login recovery only for login-page recovery, using env vars first (`TISTORY_LOGIN_EMAIL`, `TISTORY_LOGIN_PASSWORD`) and then `~/.openclaw/secrets/tistory-login.json` with restrictive permissions (`chmod 600`).

Use:

```bash
nlm login --check
```

If the logged-in browser is missing entirely, stop and ask for a valid CDP session. Do not require a specific browser flavor if the session can reach the target Tistory editor safely.

## Use these entrypoints

- `./bin/tistory-workflow prepare ...`
- `./bin/tistory-workflow validate-tags ...`
- `./bin/tistory-publish publish ...`

Use the wrapper commands by default. They force the repo `.venv` so agents do not accidentally run against system Python.

## Run in this order

1. `prepare`
2. `validate-tags`
3. `publish`

Do not reorder these steps unless the user explicitly asks for partial recovery work.

## Apply these operating rules

- Use `manifest.json` as the single source of truth for title, markdown path, thumbnail path, tags, and publish checkpoints.
- Reuse an existing run bundle when `manifest.json` already exists unless the user explicitly asks for a fresh run.
- Require exactly 10 unique, non-empty tags before publish.
- Keep automation private-first. Do not use broad public-facing selectors or generic publish buttons.
- Treat `pending_confirmation` as incomplete. Do not claim success until a concrete post URL exists and private publish completes with a recoverable post state.
- Stop immediately if CDP preflight or private visibility confirmation fails.

## Responsibility split

### Agent responsibilities

- Run the pipeline in the fixed order: `prepare -> validate-tags -> publish`.
- Check NotebookLM auth, Python dependencies, and CDP reachability before running the relevant step.
- Read and write only through `manifest.json` state, run artifacts, and the documented CLI entrypoints.
- During markdown cleanup, keep only topic-relevant source images and avoid generic or weakly related reference images.
- Populate title/body/thumbnail from the generated run bundle and perform the private-first publish flow.
- Attempt automatic recovery when publish state is incomplete but the browser session is still usable.
- Refuse unsafe publish paths such as broad public selectors or missing private confirmation.

### User responsibilities

- Provide the research intent: at minimum `topic`, and when needed a better `research-query`.
- Provide 10 final tags if the workflow is being run with `validate-tags` as-is.
- Keep NotebookLM logged in before `prepare`.
- Keep a Chromium session exposed over CDP before `publish`.
- Keep a browser session logged in to Tistory/Kakao and exposed over CDP before `publish`.
- Provide the target `blog-host`.

### Automatic recovery the agent should attempt first

- Reuse an existing run bundle when possible instead of regenerating content.
- Re-attach to the correct CDP context for the target blog host, or recover to another usable browser session when needed.
- Re-enter the editor and resume the deterministic publish state machine from a safe fresh attempt.
- Recover from missing publish confirmation only when the browser session still looks valid and a concrete post URL can be derived safely.

### Conditions that require user intervention

- NotebookLM login is missing or expired.
- No valid Tistory/Kakao CDP session exists and automatic browser recovery could not be prepared.
- The target blog host is unknown.
- Final tags are not available.
- Publish completed in Tistory but `post_url` cannot be recovered automatically.
- Tistory UI or account state blocks progress in a way the scripted flow cannot safely disambiguate.

## How the agent should behave at handoff points

- If NotebookLM auth is missing: stop before `prepare` and ask for NotebookLM login.
- If the browser session is missing or not logged in: stop before `publish` and ask for a valid logged-in CDP session.
- If tags are missing: ask the user for 10 final tags unless the user explicitly delegates tag creation to the agent.
- If `publish.status` becomes `pending_confirmation`: do not claim success; recover a concrete post URL first or ask the user for it.

## Step 1: prepare

```bash
./bin/tistory-workflow prepare \
  --topic "<topic>" \
  --research-query "<research query>" \
  --runs-dir runs
```

This creates:

- `runs/<run_id>/post_raw.md`
- `runs/<run_id>/post.md`
- `runs/<run_id>/thumbnail.png`
- `runs/<run_id>/manifest.json`
- `runs/<run_id>/workflow.log`

Expect the manifest to include default `blog` and `publish` state.

## Step 2: validate-tags

```bash
./bin/tistory-workflow validate-tags \
  --run-dir runs/<run_id> \
  --tags "tag1,tag2,tag3,tag4,tag5,tag6,tag7,tag8,tag9,tag10"
```

Hard rules:

- exactly 10 tags
- no duplicates
- no empty values

## Step 3: publish

Set `TISTORY_BLOG_HOST="<blog>.tistory.com"` in the environment to make the target blog the default. `--blog-host` still overrides it when needed.

```bash
./bin/tistory-publish publish \
  --run-dir runs/<run_id> \
  --cdp-url "http://127.0.0.1:18800"
```

Expected behavior:

- attach to the running browser session over CDP
- preflight the Tistory context for the target host
- if a Kakao/Tistory login page is detected, attempt one narrow secret-backed login using env vars first and `~/.openclaw/secrets/tistory-login.json` second
- otherwise keep the already-logged-in path unchanged and preserve the manual-login fallback
- open the new-post editor for that host
- use the pre-corrected markdown bundle where reference images were filtered to topic-relevant matches during `prepare`
- force markdown-capable editor mode
- fill title/body/tags from `manifest.json`
- open publish dialog
- upload representative image from the local thumbnail file
- force private publish selection
- click only a safe private submit button
- persist publish checkpoints and result into `manifest.json`

If publish cannot resolve `post_url`, leave the manifest incomplete and recover with a concrete URL before claiming completion.

Declare completion only after `publish` has a concrete `post_url`.
