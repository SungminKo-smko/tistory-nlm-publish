# Tistory NLM Publish

Use this repository as a local skill/tool bundle for Tistory publishing.

Canonical instructions:

- `./SKILL.md`

Quick start after cloning from GitHub:

```bash
./bin/bootstrap-skill
```

Then use only these entrypoints:

- `./bin/tistory-workflow prepare ...`
- `./bin/tistory-workflow validate-tags ...`
- `./bin/tistory-publish publish ...`
- `./bin/tistory-publish verify-render ...`
- `./bin/tistory-publish verify-public ...`

Hard constraints:

- NotebookLM must already be logged in
- Prefer setting `TISTORY_BLOG_HOST` in the environment so the target blog host is stable across runs
- A Chromium session must be exposed over CDP before `publish` or `verify-render`
- Prefer a headless CDP endpoint; if the supplied endpoint is headed, `./bin/tistory-publish` may launch or reuse a headless fallback session and migrate the Tistory cookies automatically. Only use `--allow-headed-cdp` when the environment is pinned to a headed `18800` workflow and fallback is unavailable.
- If the session lands on Kakao/Tistory login, use `TISTORY_LOGIN_EMAIL` / `TISTORY_LOGIN_PASSWORD` or `~/.openclaw/secrets/tistory-login.json` (`chmod 600`) for one narrow auto-login attempt
- The pipeline is private-first and manifest-driven
