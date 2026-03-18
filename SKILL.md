---
name: tistory-nlm-publish
description: Prepare NotebookLM-derived Korean blog bundles, validate publish inputs, and publish to Tistory through a manifest-driven private-first headless CDP workflow. Use when Codex needs to run or repair the end-to-end `prepare -> validate-tags -> publish -> verify-render -> verify-public` flow for this repo.
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
- Confirm a headless Chromium session is already logged in to Tistory/Kakao and exposed over CDP before `publish` or `verify-render`.
- Reject headed CDP endpoints. `scripts/publish_tistory.py` enforces headless mode.

Use:

```bash
nlm login --check
```

If the logged-in browser is missing, stop and ask for a valid headless CDP session instead of launching a separate profile by default.

## Use these entrypoints

- `./bin/tistory-workflow prepare ...`
- `./bin/tistory-workflow validate-tags ...`
- `./bin/tistory-publish publish ...`
- `./bin/tistory-publish verify-render ...`
- `./bin/tistory-publish verify-public ...`

Use the wrapper commands by default. They force the repo `.venv` so agents do not accidentally run against system Python.

## Run in this order

1. `prepare`
2. `validate-tags`
3. `publish`
4. `verify-render`
5. `verify-public` only after an intentional public switch

Do not reorder these steps unless the user explicitly asks for partial recovery work.

## Apply these operating rules

- Use `manifest.json` as the single source of truth for title, markdown path, thumbnail path, tags, publish checkpoints, and verification state.
- Reuse an existing run bundle when `manifest.json` already exists unless the user explicitly asks for a fresh run.
- Require exactly 10 unique, non-empty tags before publish.
- Keep automation private-first. Do not use broad public-facing selectors or generic publish buttons.
- Treat `pending_confirmation` as incomplete. Do not claim success until a concrete post URL exists and the relevant verification step passes.
- Stop immediately if headless CDP preflight, private visibility confirmation, or render validation fails.

## Responsibility split

### Agent responsibilities

- Run the pipeline in the fixed order: `prepare -> validate-tags -> publish -> verify-render -> verify-public`.
- Check NotebookLM auth, Python dependencies, and CDP reachability before running the relevant step.
- Read and write only through `manifest.json` state, run artifacts, and the documented CLI entrypoints.
- Populate title/body/thumbnail from the generated run bundle and perform the private-first publish flow.
- Attempt automatic recovery when publish state is incomplete but the browser session is still usable.
- Refuse unsafe publish paths such as broad public selectors, headed CDP endpoints, or missing private confirmation.

### User responsibilities

- Provide the research intent: at minimum `topic`, and when needed a better `research-query`.
- Provide 10 final tags if the workflow is being run with `validate-tags` as-is.
- Keep NotebookLM logged in before `prepare`.
- Keep a headless Chromium session logged in to Tistory/Kakao and exposed over CDP before `publish` or `verify-render`.
- Provide the target `blog-host` and, if auto-detection fails, the concrete `post-url`.

### Automatic recovery the agent should attempt first

- Reuse an existing run bundle when possible instead of regenerating content.
- Re-attach to the correct headless CDP context for the target blog host.
- Re-enter the editor and resume the deterministic publish state machine from a safe fresh attempt.
- Recover from missing publish confirmation only when the browser session still looks valid and a concrete post URL can be derived safely.

### Conditions that require user intervention

- NotebookLM login is missing or expired.
- No valid headless Tistory/Kakao CDP session exists.
- The target blog host is unknown.
- Final tags are not available.
- Publish completed in Tistory but `post_url` cannot be recovered automatically.
- Tistory UI or account state blocks progress in a way the scripted flow cannot safely disambiguate.

## How the agent should behave at handoff points

- If NotebookLM auth is missing: stop before `prepare` and ask for NotebookLM login.
- If the headless browser is missing or not logged in: stop before `publish` and ask for a valid logged-in headless CDP session.
- If tags are missing: ask the user for 10 final tags unless the user explicitly delegates tag creation to the agent.
- If `publish.status` becomes `pending_confirmation`: do not claim success; recover a concrete post URL first or ask the user for it.
- If `verify-render` or `verify-public` fails: report the failure as incomplete, include the blocking checkpoint, and do not claim the post is done.

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

Expect the manifest to include default `blog`, `publish`, and `verification` state.

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

```bash
./bin/tistory-publish publish \
  --run-dir runs/<run_id> \
  --blog-host "<blog>.tistory.com" \
  --cdp-url "http://127.0.0.1:18800"
```

Expected behavior:

- attach to the running headless browser over CDP
- preflight the logged-in Tistory context for the target host
- open the new-post editor for that host
- force markdown-capable editor mode
- fill title/body/tags from `manifest.json`
- open publish dialog
- upload representative image from the local thumbnail file
- force private publish selection
- click only a safe private submit button
- persist publish checkpoints and result into `manifest.json`

If publish cannot resolve `post_url`, leave the manifest incomplete and recover with a concrete URL before verification.

## Step 4: verify-render

```bash
./bin/tistory-publish verify-render \
  --run-dir runs/<run_id> \
  --cdp-url "http://127.0.0.1:18800"
```

Hard gates:

- the logged-in headless browser can open the rendered post URL
- rendered body contains required sections
- no major raw markdown leakage
- at least one body image exists
- rendered page appears to match the manifest title
- `manifest.json` is updated at `verification.render`

If publish did not store `post_url`, pass it explicitly:

```bash
./bin/tistory-publish verify-render \
  --run-dir runs/<run_id> \
  --cdp-url "http://127.0.0.1:18800" \
  --post-url "https://<blog>.tistory.com/<post-id>"
```

## Step 5: verify-public

```bash
./bin/tistory-publish verify-public \
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

Declare completion only after the hard gates for the executed path have passed.
