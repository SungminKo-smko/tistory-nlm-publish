#!/usr/bin/env python3

from __future__ import annotations
import argparse
import datetime as dt
import json
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urljoin

import markdown
import requests
from bs4 import BeautifulSoup


REQUEST_TIMEOUT = 20
RESEARCH_POLL_SEC = 20
ARTIFACT_POLL_SEC = 20

USER_AGENT = "Mozilla/5.0"

DEFAULT_REPORT_PROMPT = (
    "[지침1] 한국어로 작성할 것. "
    "[지침2] 일반인도 바로 이해할 수 있게 쉬운 표현으로 설명할 것. "
    "[지침3] 참고소스를 원문URL 링크가 추가된 마크다운 목록으로 정리할 것. "
    "[지침4] 보고서 각 핵심 섹션에 참고소스와 직접 관련된 이미지를 마크다운 이미지 문법(![설명](URL))으로 본문 중간에 삽입할 것. "
    "[지침5] 모든 내용은 마크다운 문법으로 작성할 것."
    "[지침6] 보고서 제목은 연구 주제와 최대한 일치하게 작성할 것."
)


class WorkflowError(RuntimeError):
    pass


def now_slug():
    return dt.datetime.now().strftime("%Y%m%d_%H%M%S")


def ensure_dir(path: Path):
    path.mkdir(parents=True, exist_ok=True)


def log(path: Path, msg: str):
    ts = dt.datetime.now().isoformat(timespec="seconds")
    line = f"[{ts}] {msg}"
    print(line)
    with path.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def append_log(path: Path, msg: str):
    log(path, msg)


def run_cmd(args: List[str], log_path: Path) -> str:
    log(log_path, "RUN: " + " ".join(args))
    p = subprocess.run(args, capture_output=True, text=True)

    if p.stdout:
        log(log_path, "STDOUT: " + p.stdout)

    if p.stderr:
        log(log_path, "STDERR: " + p.stderr)

    if p.returncode != 0:
        raise WorkflowError(p.stderr)

    return p.stdout


@dataclass
class RunCtx:

    run_id: str
    run_dir: Path
    log_path: Path

    topic: str
    query: str

    notebook_id: str = ""
    research_task_id: str = ""
    report_artifact_id: str = ""
    infographic_artifact_id: str = ""

    @property
    def raw_md(self):
        return self.run_dir / "post_raw.md"

    @property
    def md(self):
        return self.run_dir / "post.md"

    @property
    def thumb(self):
        return self.run_dir / "thumbnail.png"

    @property
    def html(self):
        return self.run_dir / "post.html"

    @property
    def manifest(self):
        return self.run_dir / "manifest.json"


# --------------------------------------------------
# NLM helpers
# --------------------------------------------------


def check_login(ctx: RunCtx):
    run_cmd(["nlm", "login", "--check"], ctx.log_path)


def create_notebook(ctx: RunCtx):

    # Idempotency: reuse existing notebook with same title if present.
    try:
        existing_raw = run_cmd(["nlm", "notebook", "list"], ctx.log_path)
        rows = json.loads(existing_raw)
        same = [r for r in rows if (r.get("title") or "").strip() == ctx.topic.strip()]
        if same:
            # notebook list is usually newest-first; still sort by updated_at desc defensively
            same.sort(key=lambda r: r.get("updated_at") or "", reverse=True)
            ctx.notebook_id = same[0].get("id")
            if ctx.notebook_id:
                append_log(ctx.log_path, f"reusing existing notebook: {ctx.notebook_id}")
                return
    except Exception:
        pass

    out = run_cmd(["nlm", "notebook", "create", ctx.topic], ctx.log_path)

    m = re.search(r"ID:\s*([a-f0-9]{8}-[a-f0-9\-]{27,})", out, flags=re.I)

    if not m:
        # fallback: pick last UUID-looking token
        all_ids = re.findall(r"([a-f0-9]{8}-[a-f0-9\-]{27,})", out, flags=re.I)
        notebook_id = all_ids[-1] if all_ids else None
    else:
        notebook_id = m.group(1)

    if not notebook_id:
        raise WorkflowError("Notebook id not detected")

    ctx.notebook_id = notebook_id


def notebook_has_sources(ctx: RunCtx) -> bool:
    try:
        out = run_cmd(["nlm", "source", "list", ctx.notebook_id, "--json"], ctx.log_path)
        rows = json.loads(out)
        return len(rows) > 0
    except Exception:
        return False


def start_research(ctx: RunCtx):

    out = run_cmd(
        [
            "nlm",
            "research",
            "start",
            ctx.query,
            "--notebook-id",
            ctx.notebook_id,
            "--mode",
            "deep",
        ],
        ctx.log_path,
    )

    m = re.search(r"Task ID:\s*([a-f0-9\-]{36})", out, flags=re.I)
    if not m:
        # fallback: prefer last UUID in output
        all_ids = re.findall(r"([a-f0-9]{8}-[a-f0-9\-]{27,})", out, flags=re.I)
        task_id = all_ids[-1] if all_ids else None
    else:
        task_id = m.group(1)

    if not task_id:
        raise WorkflowError("research task id missing")

    ctx.research_task_id = task_id


def wait_research(ctx: RunCtx):

    start = time.time()

    while True:

        out = run_cmd(
            [
                "nlm",
                "research",
                "status",
                ctx.notebook_id,
                "--max-wait",
                "300",
            ],
            ctx.log_path,
        )

        low = out.lower()

        if "completed" in low or "success" in low:
            return

        if "failed" in low:
            raise WorkflowError("research failed")

        if time.time() - start > 3600:
            raise WorkflowError("research timeout")

        time.sleep(RESEARCH_POLL_SEC)


def import_sources(ctx: RunCtx):

    run_cmd(
        [
            "nlm",
            "research",
            "import",
            ctx.notebook_id,
            ctx.research_task_id,
        ],
        ctx.log_path,
    )


def create_report(ctx: RunCtx):

    out = run_cmd(
        [
            "nlm",
            "report",
            "create",
            ctx.notebook_id,
            "--format",
            "Create Your Own",
            "--prompt",
            DEFAULT_REPORT_PROMPT,
            "--language",
            "ko",
            "--confirm",
        ],
        ctx.log_path,
    )

    m = re.search(r"ID:\s*([a-f0-9\-]{36})", out, flags=re.I)
    if not m:
        all_ids = re.findall(r"([a-f0-9]{8}-[a-f0-9\-]{27,})", out, flags=re.I)
        artifact_id = all_ids[-1] if all_ids else None
    else:
        artifact_id = m.group(1)

    if not artifact_id:
        raise WorkflowError("report artifact id missing")

    ctx.report_artifact_id = artifact_id


def create_infographic(ctx: RunCtx):

    out = run_cmd(
        [
            "nlm",
            "infographic",
            "create",
            ctx.notebook_id,
            "--orientation",
            "square",
            "--style",
            "professional",
            "--detail",
            "detailed",
            "--language",
            "ko",
            "--focus",
            "한글 인포그래픽, 이미지의 정중앙(위아래,좌우 모두 중앙)에 큰 제목 텍스트 배치",
            "--confirm",
        ],
        ctx.log_path,
    )

    m = re.search(r"ID:\s*([a-f0-9\-]{36})", out, flags=re.I)
    if not m:
        all_ids = re.findall(r"([a-f0-9]{8}-[a-f0-9\-]{27,})", out, flags=re.I)
        artifact_id = all_ids[-1] if all_ids else None
    else:
        artifact_id = m.group(1)

    if not artifact_id:
        raise WorkflowError("infographic id missing")

    ctx.infographic_artifact_id = artifact_id


def wait_artifacts(ctx: RunCtx):

    start = time.time()

    while True:

        out = run_cmd(
            [
                "nlm",
                "studio",
                "status",
                ctx.notebook_id,
            ],
            ctx.log_path,
        )

        report_done = False
        infographic_done = False
        try:
            rows = json.loads(out)
            for r in rows:
                rid = r.get("id")
                st = (r.get("status") or "").lower()
                if rid == ctx.report_artifact_id and st == "completed":
                    report_done = True
                if rid == ctx.infographic_artifact_id and st == "completed":
                    infographic_done = True
        except Exception:
            # Fallback for unexpected output format
            low = out.lower()
            report_done = ctx.report_artifact_id in out and "completed" in low
            infographic_done = ctx.infographic_artifact_id in out and "completed" in low

        if report_done and infographic_done:
            return

        if time.time() - start > 3600:
            raise WorkflowError("artifact timeout")

        time.sleep(ARTIFACT_POLL_SEC)


def download_report(ctx: RunCtx):

    run_cmd(
        [
            "nlm",
            "download",
            "report",
            ctx.notebook_id,
            "--id",
            ctx.report_artifact_id,
            "--output",
            str(ctx.raw_md),
        ],
        ctx.log_path,
    )


def download_infographic(ctx: RunCtx):

    run_cmd(
        [
            "nlm",
            "download",
            "infographic",
            ctx.notebook_id,
            "--id",
            ctx.infographic_artifact_id,
            "--output",
            str(ctx.thumb),
        ],
        ctx.log_path,
    )


# --------------------------------------------------
# Markdown cleanup
# --------------------------------------------------


def cleanup_md(text: str, title: str):

    if not text.startswith("#"):
        text = f"# {title}\n\n" + text

    text = re.sub(r"\]\s+\(", "](", text)

    return text


def get_notebook_sources(ctx: RunCtx) -> List[Dict[str, str]]:
    out = run_cmd(["nlm", "source", "list", ctx.notebook_id, "--json"], ctx.log_path)
    try:
        rows = json.loads(out)
    except Exception:
        return []

    sources: List[Dict[str, str]] = []
    for r in rows:
        url = (r.get("url") or "").strip()
        title = (r.get("title") or "").strip()
        if not url:
            continue
        sources.append({"title": title or url, "url": url})
    return sources


def rewrite_reference_section(md_text: str, sources: List[Dict[str, str]], max_items: int = 12) -> str:
    if not sources:
        return md_text

    items: List[str] = []
    seen = set()
    for s in sources:
        u = s["url"]
        if u in seen:
            continue
        seen.add(u)
        t = s["title"].replace("[", "(").replace("]", ")")
        items.append(f"* [{t}]({u})")
        if len(items) >= max_items:
            break

    new_sec = "## 8. 참고 소스 (Reference List)\n\n" + "\n".join(items) + "\n"

    # Replace existing section if present.
    pat = re.compile(r"##\s*8\.\s*참고\s*소스[\s\S]*$", re.MULTILINE)
    if pat.search(md_text):
        return pat.sub(new_sec, md_text)

    # Fallback: append section at end.
    return md_text.rstrip() + "\n\n" + new_sec


# --------------------------------------------------
# manifest
# --------------------------------------------------


def build_manifest(ctx: RunCtx):

    m = {
        "run_id": ctx.run_id,
        "title": ctx.topic,
        "blog": {
            "host": None,
            "home_url": None,
            "edit_url": None,
        },
        "markdown_path": str(ctx.md),
        "html_path": str(ctx.html),
        "raw_markdown_path": str(ctx.raw_md),
        "thumbnail_path": str(ctx.thumb),
        "tags": [],
        "publish": {
            "status": "not_started",
            "visibility": "private",
            "blog_host": None,
            "cdp_url": None,
            "current_step": None,
            "attempts": [],
            "last_screenshot": None,
            "editor_variant": None,
            "context_index": None,
            "edit_url": None,
            "post_url": None,
            "post_id": None,
            "published_at": None,
            "last_checkpoint": None,
            "checkpoints": {},
            "last_error": None,
        },
        "verification": {
            "render": {
                "status": "not_started",
                "checked_at": None,
                "url": None,
                "body_images_found": 0,
                "raw_markdown_leak": None,
                "required_sections_ok": None,
                "title_match": None,
                "details": {},
            },
            "public": {
                "status": "not_started",
                "checked_at": None,
                "url": None,
                "og_image": None,
                "body_images_found": 0,
                "raw_markdown_leak": None,
                "required_sections_ok": None,
                "details": {},
            },
        },
    }

    ctx.manifest.write_text(json.dumps(m, indent=2, ensure_ascii=False), encoding="utf-8")


# --------------------------------------------------
# main workflow
# --------------------------------------------------


def prepare(topic, query, runs_dir):

    run_id = now_slug()

    run_dir = Path(runs_dir) / run_id

    ensure_dir(run_dir)

    log_path = run_dir / "workflow.log"

    ctx = RunCtx(run_id, run_dir, log_path, topic, query)

    log(log_path, "prepare start")

    check_login(ctx)

    create_notebook(ctx)

    if notebook_has_sources(ctx):
        append_log(ctx.log_path, "notebook already has sources; skipping research start/import")
    else:
        start_research(ctx)
        wait_research(ctx)
        import_sources(ctx)

    create_report(ctx)

    create_infographic(ctx)

    wait_artifacts(ctx)

    download_report(ctx)

    download_infographic(ctx)

    text = ctx.raw_md.read_text(encoding="utf-8")

    text = cleanup_md(text, topic)

    # Ensure reference links are real notebook source URLs (not hallucinated links).
    sources = get_notebook_sources(ctx)
    text = rewrite_reference_section(text, sources)
    text = force_real_source_images(text, sources)

    ctx.md.write_text(text, encoding="utf-8")
    ctx.html.write_text(render_html_with_safe_layout(text), encoding="utf-8")

    build_manifest(ctx)

    log(log_path, "prepare finished")

    print("run directory:", run_dir)


def _discover_source_image(url: str) -> Optional[str]:
    def is_live_image(candidate_url: str) -> bool:
        headers = {"User-Agent": USER_AGENT}
        try:
            hr = requests.head(candidate_url, timeout=REQUEST_TIMEOUT, allow_redirects=True, headers=headers)
            ctype = (hr.headers.get("content-type") or "").lower()
            if hr.status_code < 400 and "image" in ctype:
                return True
            if hr.status_code not in {403, 405}:
                return False
        except Exception:
            pass

        try:
            gr = requests.get(candidate_url, timeout=REQUEST_TIMEOUT, allow_redirects=True, headers=headers, stream=True)
            ctype = (gr.headers.get("content-type") or "").lower()
            ok = gr.status_code < 400 and "image" in ctype
            gr.close()
            return ok
        except Exception:
            return False

    try:
        r = requests.get(url, timeout=REQUEST_TIMEOUT, headers={"User-Agent": USER_AGENT})
        r.raise_for_status()
    except Exception:
        return None

    soup = BeautifulSoup(r.text, "html.parser")
    candidate_urls: List[str] = []

    for key, value in [
        ("property", "og:image"),
        ("name", "twitter:image"),
        ("name", "twitter:image:src"),
        ("property", "og:image:url"),
    ]:
        node = soup.select_one(f'meta[{key}="{value}"]')
        content = node.get("content") if node else None
        if isinstance(content, str) and content.strip():
            candidate_urls.append(urljoin(url, content))

    for img_node in soup.select("article img[src], main img[src], img[src]")[:20]:
        src_val = img_node.get("src")
        if not isinstance(src_val, str):
            continue
        src = src_val.strip()
        if not src:
            continue
        candidate_urls.append(urljoin(url, src))

    seen = set()
    for candidate in candidate_urls:
        if candidate in seen:
            continue
        seen.add(candidate)
        if is_live_image(candidate):
            return candidate

    return None


def force_real_source_images(md_text: str, sources: List[Dict[str, str]]) -> str:
    image_pool: List[str] = []
    for s in sources:
        u = s.get("url")
        if not u:
            continue
        img = _discover_source_image(u)
        if img and img not in image_pool:
            image_pool.append(img)
        if len(image_pool) >= 12:
            break

    if not image_pool:
        return md_text

    live_cache: Dict[str, bool] = {}
    idx = 0

    def is_live_image(candidate_url: str) -> bool:
        if candidate_url in live_cache:
            return live_cache[candidate_url]
        headers = {"User-Agent": USER_AGENT}
        ok = False
        try:
            hr = requests.head(candidate_url, timeout=REQUEST_TIMEOUT, allow_redirects=True, headers=headers)
            ctype = (hr.headers.get("content-type") or "").lower()
            if hr.status_code < 400 and "image" in ctype:
                ok = True
            elif hr.status_code in {403, 405}:
                gr = requests.get(candidate_url, timeout=REQUEST_TIMEOUT, allow_redirects=True, headers=headers, stream=True)
                gctype = (gr.headers.get("content-type") or "").lower()
                ok = gr.status_code < 400 and "image" in gctype
                gr.close()
        except Exception:
            ok = False
        live_cache[candidate_url] = ok
        return ok

    def repl(m):
        nonlocal idx
        alt = m.group(1)
        old_url = m.group(2)
        if is_live_image(old_url):
            return m.group(0)
        if not image_pool:
            return m.group(0)
        new_url = image_pool[idx % len(image_pool)]
        idx += 1
        return f"![{alt}]({new_url})"

    out = re.sub(r"!\[([^\]]*)\]\((https?://[^)\s]+)\)", repl, md_text)

    # If report has no image at all, insert one after H1.
    if "![" not in out:
        lines = out.splitlines()
        if lines and lines[0].startswith("#"):
            lines = [lines[0], "", f"![대표 이미지]({image_pool[0]})", ""] + lines[1:]
            out = "\n".join(lines)
    return out


def render_html_with_safe_layout(md_text: str) -> str:
    html = markdown.markdown(md_text, extensions=["tables", "fenced_code"])
    # Keep images within content width.
    html = re.sub(
        r"<img\s+",
        "<img style=\"max-width:100%;height:auto;display:block;margin:12px auto;\" ",
        html,
    )
    return html


def validate_tags(run_dir, tags):

    run_dir = Path(run_dir)

    manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))

    tags = [t.strip() for t in tags.split(",") if t.strip()]

    if len(tags) != 10:
        raise WorkflowError("exactly 10 tags required")

    seen = set()
    duplicates = []
    for tag in tags:
        key = tag.casefold()
        if key in seen:
            duplicates.append(tag)
            continue
        seen.add(key)

    if duplicates:
        joined = ", ".join(duplicates)
        raise WorkflowError(f"duplicate tags are not allowed: {joined}")

    manifest["tags"] = tags

    (run_dir / "manifest.json").write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    print("tags saved")


# --------------------------------------------------
# CLI
# --------------------------------------------------


def main():

    parser = argparse.ArgumentParser()

    sub = parser.add_subparsers(dest="cmd")

    p = sub.add_parser("prepare")

    p.add_argument("--topic", required=True)

    p.add_argument("--research-query", required=True)

    p.add_argument("--runs-dir", default="runs")

    p2 = sub.add_parser("validate-tags")

    p2.add_argument("--run-dir", required=True)

    p2.add_argument("--tags", required=True)

    args = parser.parse_args()

    try:

        if args.cmd == "prepare":

            prepare(args.topic, args.research_query, args.runs_dir)

        elif args.cmd == "validate-tags":

            validate_tags(args.run_dir, args.tags)

    except WorkflowError as e:

        print("ERROR:", e)

        sys.exit(1)


if __name__ == "__main__":

    main()
