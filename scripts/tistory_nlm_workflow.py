#!/usr/bin/env python3

from __future__ import annotations
import argparse
import datetime as dt
import io
import json
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urljoin, urlparse

try:
    import requests
    from bs4 import BeautifulSoup
    from PIL import Image, ImageOps
except ModuleNotFoundError as exc:
    print(
        "ERROR: Missing Python dependency "
        f"'{exc.name}'. Activate the repo venv first with "
        "'source .venv/bin/activate' or run './bin/tistory-workflow ...'.",
        file=sys.stderr,
    )
    sys.exit(1)


REQUEST_TIMEOUT = 20
RESEARCH_POLL_SEC = 20
ARTIFACT_POLL_SEC = 20
ARTIFACT_CREATE_TIMEOUT_SEC = 120
ARTIFACT_DISCOVERY_TIMEOUT_SEC = 180
ARTIFACT_DISCOVERY_POLL_SEC = 10

USER_AGENT = "Mozilla/5.0"
REFERENCE_HEADING_PATTERN = re.compile(
    r"(?im)^##\s*(?:\d+\.\s*)?(?:참고\s*소스(?:\s*\(Reference List\))?|참고\s*자료|reference\s*list|references?)\s*$"
)
NUMBERED_SECTION_PATTERN = re.compile(r"(?m)^##\s+(\d+)\.\s+")
MARKDOWN_IMAGE_WITH_TARGET_PATTERN = re.compile(r"!\[([^\]]*)\]\(([^)\s]+)\)")

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


def run_cmd(args: List[str], log_path: Path, timeout: Optional[int] = None) -> str:
    log(log_path, "RUN: " + " ".join(args))
    try:
        p = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout or ""
        stderr = exc.stderr or ""
        if stdout:
            log(log_path, "STDOUT: " + stdout)
        if stderr:
            log(log_path, "STDERR: " + stderr)
        raise WorkflowError(
            f"command timed out after {timeout}s: {' '.join(args)}"
        ) from exc

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


def normalize_source_key(row: Dict[str, Any]) -> Optional[str]:
    url = (row.get("url") or "").strip()
    title = (row.get("title") or "").strip()

    candidate = url
    if not candidate and (title.startswith("http://") or title.startswith("https://")):
        candidate = title

    if not candidate:
        return None

    parsed = urlparse(candidate)
    host = parsed.netloc.lower().strip()
    path = parsed.path.rstrip("/")
    query = parsed.query
    normalized = f"{host}{path}"
    if query:
        normalized += f"?{query}"
    return normalized or None


def dedupe_notebook_sources(ctx: RunCtx) -> int:
    out = run_cmd(["nlm", "source", "list", ctx.notebook_id, "--json"], ctx.log_path)
    rows = json.loads(out)

    seen: Dict[str, str] = {}
    duplicate_ids: List[str] = []

    for row in rows:
        source_id = (row.get("id") or "").strip()
        if not source_id:
            continue
        key = normalize_source_key(row)
        if not key:
            continue
        if key in seen:
            duplicate_ids.append(source_id)
        else:
            seen[key] = source_id

    if not duplicate_ids:
        append_log(ctx.log_path, "source dedupe: no duplicates found")
        return 0

    append_log(ctx.log_path, f"source dedupe: deleting {len(duplicate_ids)} duplicate source(s)")
    run_cmd(["nlm", "source", "delete", *duplicate_ids, "--confirm"], ctx.log_path)
    return len(duplicate_ids)


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

    try:
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
    except WorkflowError as e:
        append_log(ctx.log_path, f"research import error: {e}")
        # Recovery: import can succeed server-side and still time out locally.
        if notebook_has_sources(ctx):
            append_log(ctx.log_path, "research import appears completed despite timeout/error; continuing with existing notebook sources")
        else:
            raise

    dedupe_notebook_sources(ctx)


def list_studio_artifacts(ctx: RunCtx) -> List[Dict[str, Any]]:
    out = run_cmd(
        [
            "nlm",
            "studio",
            "status",
            ctx.notebook_id,
        ],
        ctx.log_path,
    )

    try:
        rows = json.loads(out)
    except Exception as exc:
        raise WorkflowError("unable to parse studio status output") from exc

    if not isinstance(rows, list):
        raise WorkflowError("unexpected studio status payload")

    return rows


def snapshot_artifact_ids(ctx: RunCtx, artifact_type: str) -> set[str]:
    rows = list_studio_artifacts(ctx)
    ids = set()
    for row in rows:
        if (row.get("type") or "").lower() != artifact_type:
            continue
        artifact_id = (row.get("id") or "").strip()
        if artifact_id:
            ids.add(artifact_id)
    return ids


def parse_artifact_id(output: str) -> Optional[str]:
    m = re.search(r"ID:\s*([a-f0-9\-]{36})", output, flags=re.I)
    if m:
        return m.group(1)
    all_ids = re.findall(r"([a-f0-9]{8}-[a-f0-9\-]{27,})", output, flags=re.I)
    return all_ids[-1] if all_ids else None


def wait_for_new_artifact(
    ctx: RunCtx,
    artifact_type: str,
    before_ids: set[str],
    timeout_sec: int = ARTIFACT_DISCOVERY_TIMEOUT_SEC,
) -> Optional[str]:
    start = time.time()

    while True:
        rows = list_studio_artifacts(ctx)
        for row in rows:
            if (row.get("type") or "").lower() != artifact_type:
                continue
            artifact_id = (row.get("id") or "").strip()
            if artifact_id and artifact_id not in before_ids:
                append_log(
                    ctx.log_path,
                    f"recovered {artifact_type} artifact id from studio status: {artifact_id}",
                )
                return artifact_id

        if time.time() - start > timeout_sec:
            return None

        time.sleep(ARTIFACT_DISCOVERY_POLL_SEC)


def start_artifact_with_recovery(
    ctx: RunCtx,
    artifact_type: str,
    create_args: List[str],
) -> str:
    before_ids = snapshot_artifact_ids(ctx, artifact_type)

    try:
        out = run_cmd(create_args, ctx.log_path, timeout=ARTIFACT_CREATE_TIMEOUT_SEC)
    except WorkflowError as exc:
        append_log(
            ctx.log_path,
            f"{artifact_type} create command did not finish cleanly; trying studio-status recovery: {exc}",
        )
        out = ""

    artifact_id = parse_artifact_id(out)
    if artifact_id:
        return artifact_id

    recovered = wait_for_new_artifact(ctx, artifact_type, before_ids)
    if recovered:
        return recovered

    raise WorkflowError(f"{artifact_type} id missing")


def create_report(ctx: RunCtx):
    ctx.report_artifact_id = start_artifact_with_recovery(
        ctx,
        "report",
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
    )


def create_infographic(ctx: RunCtx):
    ctx.infographic_artifact_id = start_artifact_with_recovery(
        ctx,
        "infographic",
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
    )


def summarize_artifact_progress(
    rows: List[Dict[str, Any]],
    report_id: str,
    infographic_id: str,
) -> str:
    statuses: Dict[str, str] = {}
    for row in rows:
        artifact_id = (row.get("id") or "").strip()
        status = (row.get("status") or "unknown").lower()
        if artifact_id:
            statuses[artifact_id] = status
    report_status = statuses.get(report_id, "missing")
    infographic_status = statuses.get(infographic_id, "missing")
    return f"artifact progress: report={report_status} infographic={infographic_status}"


def wait_artifacts(ctx: RunCtx):

    start = time.time()
    last_summary = None

    while True:
        rows = list_studio_artifacts(ctx)

        report_done = False
        infographic_done = False
        for r in rows:
            rid = r.get("id")
            st = (r.get("status") or "").lower()
            if rid == ctx.report_artifact_id:
                if st == "failed":
                    raise WorkflowError("report artifact failed")
                if st == "completed":
                    report_done = True
            if rid == ctx.infographic_artifact_id:
                if st == "failed":
                    raise WorkflowError("infographic artifact failed")
                if st == "completed":
                    infographic_done = True

        summary = summarize_artifact_progress(rows, ctx.report_artifact_id, ctx.infographic_artifact_id)
        if summary != last_summary:
            append_log(ctx.log_path, summary)
            last_summary = summary

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


def strip_existing_reference_sections(md_text: str) -> str:
    lines = md_text.splitlines()
    for idx, line in enumerate(lines):
        if REFERENCE_HEADING_PATTERN.match(line.strip()):
            return "\n".join(lines[:idx]).rstrip() + "\n"
    return md_text


def build_reference_heading(md_text: str) -> str:
    base = strip_existing_reference_sections(md_text)
    section_numbers = [int(match.group(1)) for match in NUMBERED_SECTION_PATTERN.finditer(base)]
    if not section_numbers:
        return "## 참고 소스 (Reference List)"
    return f"## {max(section_numbers) + 1}. 참고 소스 (Reference List)"


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
        return strip_existing_reference_sections(md_text)

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

    new_sec = build_reference_heading(md_text) + "\n\n" + "\n".join(items) + "\n"
    base = strip_existing_reference_sections(md_text).rstrip()
    return base + "\n\n" + new_sec


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
        dedupe_notebook_sources(ctx)
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
    text = inject_relevant_source_images(text, sources, ctx.run_dir)

    ctx.md.write_text(text, encoding="utf-8")

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


def keyword_tokens(text: str) -> set[str]:
    raw_tokens = re.findall(r"[가-힣A-Za-z0-9]{2,}", text.lower())
    stopwords = {
        "https", "http", "www", "com", "co", "kr", "the", "and",
        "for", "with", "from", "this", "that", "2025", "2026",
        "section", "reference", "list", "image",
    }
    return {token for token in raw_tokens if token not in stopwords}


def strip_markdown_images(md_text: str) -> str:
    stripped = MARKDOWN_IMAGE_WITH_TARGET_PATTERN.sub("", md_text)
    stripped = re.sub(r"\n{3,}", "\n\n", stripped)
    return stripped.strip()


def build_source_image_candidates(sources: List[Dict[str, str]]) -> List[Dict[str, str]]:
    candidates: List[Dict[str, str]] = []
    for s in sources:
        u = s.get("url")
        if not u:
            continue
        img = _discover_source_image(u)
        if not img:
            continue
        candidates.append(
            {
                "source_url": u,
                "title": s.get("title") or u,
                "image_url": img,
                "tokens": keyword_tokens((s.get("title") or "") + " " + u),
            }
        )
        if len(candidates) >= 12:
            break
    return candidates


def choose_relevant_candidate(
    heading: str,
    body: str,
    candidates: List[Dict[str, str]],
    used_source_urls: set[str],
) -> Optional[Dict[str, str]]:
    heading_tokens = keyword_tokens(heading)
    section_tokens = keyword_tokens(f"{heading} {body}")
    best_candidate = None
    best_score = 0
    for candidate in candidates:
        if candidate["source_url"] in used_source_urls:
            continue
        overlap_total = len(section_tokens & candidate["tokens"])
        overlap_heading = len(heading_tokens & candidate["tokens"])
        score = overlap_total + (overlap_heading * 2)
        if overlap_heading == 0 and overlap_total < 2:
            continue
        if score > best_score:
            best_score = score
            best_candidate = candidate
    return best_candidate if best_score > 0 else None


def download_and_resize_source_image(image_url: str, output_path: Path, max_height: int = 300) -> Path:
    response = requests.get(image_url, timeout=REQUEST_TIMEOUT, headers={"User-Agent": USER_AGENT})
    response.raise_for_status()
    image = Image.open(io.BytesIO(response.content))
    image = ImageOps.exif_transpose(image)
    if image.height > max_height:
        scale = max_height / float(image.height)
        new_size = (max(1, round(image.width * scale)), max_height)
        image = image.resize(new_size, Image.Resampling.LANCZOS)

    ensure_dir(output_path.parent)
    if image.mode not in {"RGB", "RGBA"}:
        image = image.convert("RGB")
    if image.mode == "RGBA":
        image.save(output_path.with_suffix(".png"), format="PNG")
        return output_path.with_suffix(".png")
    image.save(output_path.with_suffix(".jpg"), format="JPEG", quality=90)
    return output_path.with_suffix(".jpg")


def insert_markdown_image_after_first_paragraph(body: str, alt_text: str, image_path: str) -> str:
    lines = body.splitlines()
    insert_at = len(lines)
    seen_content = False
    for idx, line in enumerate(lines):
        if line.strip():
            seen_content = True
            continue
        if seen_content:
            insert_at = idx
            break
    image_block = [f'![{alt_text}]({image_path})', ""]
    lines[insert_at:insert_at] = image_block
    return "\n".join(lines).strip()


def inject_relevant_source_images(md_text: str, sources: List[Dict[str, str]], run_dir: Path) -> str:
    stripped = strip_markdown_images(md_text)
    candidates = build_source_image_candidates(sources)
    if not candidates:
        return stripped

    parts = re.split(r"(?m)(^##\s+.*$)", stripped)
    if len(parts) <= 1:
        return stripped

    used_source_urls: set[str] = set()
    rebuilt = [parts[0]]
    body_image_dir = run_dir / "body_images"
    image_index = 1

    for idx in range(1, len(parts), 2):
        heading = parts[idx]
        body = parts[idx + 1] if idx + 1 < len(parts) else ""
        normalized_heading = heading.strip().lower()
        if "핵심요약" in normalized_heading or "핵심이슈" in normalized_heading or "참고" in normalized_heading:
            rebuilt.extend([heading, body])
            continue

        candidate = choose_relevant_candidate(heading, body, candidates, used_source_urls)
        if candidate is not None:
            try:
                local_path = download_and_resize_source_image(
                    candidate["image_url"],
                    body_image_dir / f"body_image_{image_index:02d}",
                )
            except Exception:
                local_path = None
            if local_path is not None:
                rel_path = local_path.relative_to(run_dir).as_posix()
                body = insert_markdown_image_after_first_paragraph(body, candidate["title"], rel_path)
                used_source_urls.add(candidate["source_url"])
                image_index += 1

        rebuilt.extend([heading, body])

    return "".join(rebuilt).strip()

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
