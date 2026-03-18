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
- Tistory/Kakao must already be logged in inside a headless Chromium session exposed over CDP
- Headed CDP browsers are not allowed
- The pipeline is private-first and manifest-driven
