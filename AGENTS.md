# Tistory NLM Publish

This repository is an agent-usable skill bundle for preparing NotebookLM-based Korean blog posts and publishing them to Tistory with a private-first workflow.

Source of truth:

- `./SKILL.md`

Use these wrapper commands, not `python scripts/...` directly:

- `./bin/tistory-workflow prepare ...`
- `./bin/tistory-workflow validate-tags ...`
- `./bin/tistory-publish publish ...`
- `./bin/tistory-publish verify-render ...`
- `./bin/tistory-publish verify-public ...`

Operating rules:

- Follow the fixed order: `prepare -> validate-tags -> publish -> verify-render -> verify-public`
- Require a Chromium CDP session before `publish` or `verify-render`
- Prefer a headless CDP endpoint; if the supplied endpoint is headed, `./bin/tistory-publish` may launch or reuse a headless fallback session and migrate the Tistory cookies automatically
- If the session lands on Kakao/Tistory login, use `TISTORY_LOGIN_EMAIL` / `TISTORY_LOGIN_PASSWORD` or `~/.openclaw/secrets/tistory-login.json` (`chmod 600`) for one narrow auto-login attempt
- Keep the flow private-first
- Treat `manifest.json` as the single source of truth
- Do not claim success until a concrete `post_url` exists and the relevant verification step passes

One-time bootstrap after cloning from GitHub:

```bash
./bin/bootstrap-skill
```

If your agent does not automatically load repository instruction files, explicitly point it at `SKILL.md`.
