# Tistory NLM Publish

This repository is an agent-usable skill bundle for preparing NotebookLM-based Korean blog posts and publishing them to Tistory with a private-first workflow.

Source of truth:

- `./SKILL.md`

Use these wrapper commands, not `python scripts/...` directly:

- `./bin/tistory-workflow prepare ...`
- `./bin/tistory-workflow validate-tags ...`
- `./bin/tistory-publish publish ...`

Operating rules:

- Follow the fixed order: `prepare -> validate-tags -> publish`
- Prefer setting `TISTORY_BLOG_HOST` in the environment so agents do not need to pass `--blog-host` every time
- Require a Chromium CDP session before `publish`
- Do not require a specific browser flavor if the session can reach the target Tistory editor safely
- If the session lands on Kakao/Tistory login, use `TISTORY_LOGIN_EMAIL` / `TISTORY_LOGIN_PASSWORD` or `~/.openclaw/secrets/tistory-login.json` (`chmod 600`) for one narrow auto-login attempt
- Keep the flow private-first
- Treat `manifest.json` as the single source of truth
- Do not claim success until a concrete `post_url` exists

One-time bootstrap after cloning from GitHub:

```bash
./bin/bootstrap-skill
```

If your agent does not automatically load repository instruction files, explicitly point it at `SKILL.md`.
