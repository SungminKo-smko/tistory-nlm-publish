#!/usr/bin/env python3
"""
Manifest-driven Tistory publisher.

Primary flow:
1. Attach to an existing Chromium session over CDP
2. Preflight the already-logged-in Tistory context for the target blog host
3. Publish with explicit private-only controls and checkpointed state updates
"""

from __future__ import annotations

import argparse
import datetime as dt
import html
import json
import mimetypes
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import stat
from dataclasses import dataclass
from pathlib import Path
from stat import S_IMODE
from typing import Any, Callable, Dict, List, Optional
from urllib.parse import urlparse

try:
    import requests
    from bs4 import BeautifulSoup
    import markdown as markdown_lib
    from playwright.sync_api import Browser, BrowserContext, Page, Playwright, sync_playwright
except ModuleNotFoundError as exc:
    print(
        "ERROR: Missing Python dependency "
        f"'{exc.name}'. Activate the repo venv first with "
        "'source .venv/bin/activate' or run './bin/tistory-publish ...'.",
        file=sys.stderr,
    )
    sys.exit(1)


VIEWPORT_W = 1280
VIEWPORT_H = 650
DEFAULT_TIMEOUT_MS = 15000
DEFAULT_CDP_URL = os.environ.get("OPENCLAW_CDP_URL", "http://127.0.0.1:18800")
DEFAULT_BLOG_HOST = (os.environ.get("TISTORY_BLOG_HOST") or "").strip() or None
AUTO_HEADLESS_PROFILE = Path(
    os.environ.get("TISTORY_HEADLESS_PROFILE", str(Path.home() / ".tistory-headless-auto"))
)
AUTO_HEADLESS_PORT_SPAN = 20
PROFILE_LOCK_FILENAMES = [
    "SingletonLock",
    "SingletonSocket",
    "SingletonCookie",
    "DevToolsActivePort",
]
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36"
)

RAW_MD_LEAK_PATTERNS = [
    r"(?m)^##\s+",
    r"(?m)^-\s+",
    r"\*\*",
]

MARKDOWN_IMAGE_PATTERN = re.compile(r"!\[([^\]]*)\]\(([^)\s]+)\)")
HTML_IMAGE_PATTERN = re.compile(r"<img\b[^>]*>", re.IGNORECASE)

REQUIRED_RENDER_SECTIONS = ["핵심요약", "핵심이슈"]

TISTORY_OG_PLACEHOLDER_TOKENS = [
    "/img/blank",
    "/manage/images/default",
    "tistory.com/skin/images",
]

CONTENT_SELECTORS = [
    "article",
    ".tt_article_useless_p_margin",
    ".entry-content",
    ".contents_style",
    ".area_view",
    "#content",
]


class PublishError(RuntimeError):
    pass


@dataclass
class LoginCredentials:
    email: str
    password: str
    source: str


TISTORY_SECRET_FILE = Path.home() / ".openclaw" / "secrets" / "tistory-login.json"


@dataclass
class AttachmentInfo:
    url: str
    key: str
    filename: str


@dataclass
class BlogUrls:
    host: str
    home_url: str
    manage_url: str
    edit_url: str


@dataclass
class AttachState:
    playwright: Playwright
    browser: Optional[Browser]
    context: BrowserContext
    context_index: int
    cdp_url: Optional[str]
    launch_mode: str = "cdp"


@dataclass
class PublishSession:
    run_dir: Path
    manifest: Dict[str, Any]
    blog: BlogUrls
    cdp_url: str
    attach_state: Optional[AttachState] = None
    page: Optional[Page] = None


def now_iso() -> str:
    return dt.datetime.now().isoformat(timespec="seconds")


def json_load(path: Path) -> Dict[str, Any]:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def json_dump(path: Path, data: Dict[str, Any]) -> None:
    with path.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def append_publish_log(run_dir: Path, message: str) -> None:
    line = f"[{now_iso()}] {message}"
    print(line)
    with (run_dir / "publish.log").open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def manifest_defaults() -> Dict[str, Any]:
    return {
        "blog": {
            "host": None,
            "home_url": None,
            "edit_url": None,
        },
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
            "infographic_url": None,
            "thumbnail_ref": None,
            "expected_body_images": 0,
            "markdown_local_images": 0,
            "markdown_unresolved_images": 0,
            "last_checkpoint": None,
            "checkpoints": {},
            "edit_url": None,
            "post_url": None,
            "post_id": None,
            "published_at": None,
            "last_error": None,
        },
    }


def merge_defaults(target: Dict[str, Any], defaults: Dict[str, Any]) -> Dict[str, Any]:
    for key, value in defaults.items():
        if key not in target:
            target[key] = value
            continue
        if isinstance(value, dict) and isinstance(target[key], dict):
            merge_defaults(target[key], value)
    return target


def ensure_manifest_defaults(manifest: Dict[str, Any]) -> Dict[str, Any]:
    return merge_defaults(manifest, manifest_defaults())


def load_manifest_or_fail(run_dir: Path) -> Dict[str, Any]:
    manifest_path = run_dir / "manifest.json"
    if not manifest_path.exists():
        raise PublishError(f"manifest.json not found: {manifest_path}")
    return ensure_manifest_defaults(json_load(manifest_path))


def save_manifest(run_dir: Path, data: Dict[str, Any]) -> None:
    json_dump(run_dir / "manifest.json", data)


def normalize_blog_host(value: str) -> str:
    raw = (value or "").strip()
    if not raw:
        raise PublishError("blog host is required.")
    parsed = urlparse(raw if "://" in raw else f"https://{raw}")
    host = (parsed.netloc or parsed.path).strip().lower().rstrip("/")
    if not host:
        raise PublishError(f"invalid blog host: {value}")
    return host


def derive_blog_host_from_url(url: str) -> str:
    parsed = urlparse(url)
    host = parsed.netloc.strip().lower()
    if not host:
        raise PublishError(f"invalid URL: {url}")
    return host


def cdp_version_url(cdp_url: str) -> str:
    parsed = urlparse(cdp_url)
    scheme = parsed.scheme.lower()
    if scheme in {"http", "https"}:
        return f"{scheme}://{parsed.netloc}/json/version"
    if scheme in {"ws", "wss"}:
        http_scheme = "https" if scheme == "wss" else "http"
        return f"{http_scheme}://{parsed.netloc}/json/version"
    raise PublishError(f"Unsupported CDP URL scheme: {cdp_url}")


def fetch_cdp_metadata(cdp_url: str) -> Dict[str, Any]:
    version_url = cdp_version_url(cdp_url)
    try:
        response = requests.get(version_url, timeout=5)
        response.raise_for_status()
        data = response.json()
    except Exception as exc:
        raise PublishError(f"Failed to query CDP metadata at {version_url}: {exc}") from exc
    if not isinstance(data, dict):
        raise PublishError(f"Unexpected CDP metadata payload from {version_url}")
    return data


def cdp_metadata_is_headless(metadata: Dict[str, Any]) -> bool:
    browser_name = str(metadata.get("Browser") or "")
    user_agent = str(metadata.get("User-Agent") or "")
    combined = f"{browser_name} {user_agent}"
    return "HeadlessChrome" in combined


def cdp_host_and_port(cdp_url: str) -> tuple[str, int]:
    parsed = urlparse(cdp_url)
    host = parsed.hostname or "127.0.0.1"
    if parsed.port is None:
        raise PublishError(f"CDP URL missing port: {cdp_url}")
    return host, parsed.port


def is_tcp_port_open(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.5)
        return sock.connect_ex((host, port)) == 0


def find_chrome_binary() -> str:
    env_value = os.environ.get("TISTORY_CHROME_BIN")
    candidates = [env_value] if env_value else []
    candidates.extend(
        [
            shutil.which("google-chrome"),
            shutil.which("chromium"),
            shutil.which("chromium-browser"),
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            "/Applications/Chromium.app/Contents/MacOS/Chromium",
            "/usr/bin/google-chrome",
            "/usr/bin/chromium",
            "/usr/bin/chromium-browser",
        ]
    )
    for candidate in candidates:
        if candidate and Path(candidate).exists():
            return candidate
    raise PublishError(
        "Unable to find a Chromium/Chrome binary for headless fallback. "
        "Set TISTORY_CHROME_BIN to the browser executable path."
    )


def wait_for_cdp_ready(cdp_url: str, require_headless: bool, timeout_sec: int = 20) -> None:
    start = time.time()
    last_error: Optional[Exception] = None
    while time.time() - start <= timeout_sec:
        try:
            metadata = fetch_cdp_metadata(cdp_url)
            if require_headless and not cdp_metadata_is_headless(metadata):
                raise PublishError(f"CDP endpoint came up but is not headless: {cdp_url}")
            return
        except Exception as exc:
            last_error = exc
            time.sleep(0.5)
    raise PublishError(f"Timed out waiting for CDP endpoint {cdp_url}: {last_error}")


def list_chrome_process_commands() -> List[str]:
    try:
        result = subprocess.run(
            ["ps", "ax", "-o", "command="],
            capture_output=True,
            text=True,
            check=True,
        )
    except Exception:
        return []
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def profile_is_in_use(profile_dir: Path) -> bool:
    resolved = str(profile_dir.resolve())
    marker = f"--user-data-dir={resolved}"
    for command in list_chrome_process_commands():
        if marker in command and "Google Chrome" in command:
            return True
    return False


def clear_stale_profile_artifacts(profile_dir: Path, run_dir: Optional[Path] = None) -> None:
    if profile_is_in_use(profile_dir):
        if run_dir is not None:
            append_publish_log(
                run_dir,
                f"Profile {profile_dir} is currently in use; skipping stale lock cleanup.",
            )
        return

    removed: List[str] = []
    for filename in PROFILE_LOCK_FILENAMES:
        target = profile_dir / filename
        try:
            if target.is_symlink() or target.is_file():
                target.unlink()
                removed.append(filename)
            elif target.exists():
                shutil.rmtree(target)
                removed.append(filename)
        except FileNotFoundError:
            continue
        except Exception:
            continue

    if removed and run_dir is not None:
        append_publish_log(
            run_dir,
            f"Removed stale Chrome profile artifacts from {profile_dir}: {', '.join(removed)}",
        )


def create_fallback_profile_dir(run_dir: Optional[Path] = None) -> Path:
    base_dir = AUTO_HEADLESS_PROFILE.parent
    base_dir.mkdir(parents=True, exist_ok=True)
    temp_dir = Path(
        tempfile.mkdtemp(prefix=f"{AUTO_HEADLESS_PROFILE.name}-fallback-", dir=str(base_dir))
    )
    if AUTO_HEADLESS_PROFILE.exists():
        for child in AUTO_HEADLESS_PROFILE.iterdir():
            if child.name in PROFILE_LOCK_FILENAMES:
                continue
            destination = temp_dir / child.name
            try:
                if child.is_dir():
                    shutil.copytree(
                        child,
                        destination,
                        symlinks=True,
                        ignore=shutil.ignore_patterns(*PROFILE_LOCK_FILENAMES),
                    )
                else:
                    shutil.copy2(child, destination)
            except Exception:
                continue
    clear_stale_profile_artifacts(temp_dir, run_dir)
    return temp_dir


def launch_headless_browser(
    profile_dir: Path,
    launch_port: int,
    blog: BlogUrls,
    run_dir: Optional[Path] = None,
) -> str:
    chrome_binary = find_chrome_binary()
    profile_dir.mkdir(parents=True, exist_ok=True)
    clear_stale_profile_artifacts(profile_dir, run_dir)
    log_handle = subprocess.DEVNULL
    if run_dir is not None:
        log_handle = (run_dir / "headless_fallback.log").open("a", encoding="utf-8")
    subprocess.Popen(
        [
            chrome_binary,
            "--headless=new",
            "--no-sandbox",
            "--disable-dev-shm-usage",
            "--remote-debugging-address=127.0.0.1",
            f"--remote-debugging-port={launch_port}",
            f"--user-data-dir={profile_dir}",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-default-apps",
            blog.edit_url,
        ],
        stdout=log_handle,
        stderr=log_handle,
        start_new_session=True,
    )
    candidate_url = f"http://127.0.0.1:{launch_port}"
    try:
        wait_for_cdp_ready(candidate_url, require_headless=True)
    finally:
        if log_handle is not subprocess.DEVNULL:
            log_handle.close()
    return candidate_url


def find_or_launch_headless_cdp(
    source_cdp_url: str,
    blog: BlogUrls,
    run_dir: Optional[Path] = None,
) -> str:
    host, source_port = cdp_host_and_port(source_cdp_url)
    preferred_port = 18801 if source_port == 18800 else source_port + 1

    for port in range(preferred_port, preferred_port + AUTO_HEADLESS_PORT_SPAN):
        candidate_url = f"http://{host}:{port}"
        try:
            metadata = fetch_cdp_metadata(candidate_url)
        except PublishError:
            continue
        if cdp_metadata_is_headless(metadata):
            return candidate_url

    launch_port = preferred_port
    while is_tcp_port_open(host, launch_port):
        launch_port += 1
        if launch_port >= preferred_port + AUTO_HEADLESS_PORT_SPAN:
            raise PublishError("Unable to find a free port for headless CDP fallback.")

    primary_profile = AUTO_HEADLESS_PROFILE
    primary_profile.mkdir(parents=True, exist_ok=True)

    if profile_is_in_use(primary_profile):
        if run_dir is not None:
            append_publish_log(
                run_dir,
                f"Primary headless profile {primary_profile} is already in use; creating fallback profile.",
            )
        fallback_profile = create_fallback_profile_dir(run_dir)
        return launch_headless_browser(fallback_profile, launch_port, blog, run_dir)

    try:
        return launch_headless_browser(primary_profile, launch_port, blog, run_dir)
    except PublishError as exc:
        if run_dir is not None:
            append_publish_log(
                run_dir,
                f"Primary headless profile launch failed: {exc}; retrying with fallback profile.",
            )
        fallback_profile = create_fallback_profile_dir(run_dir)
        return launch_headless_browser(fallback_profile, launch_port, blog, run_dir)


def extract_source_cookies(source_cdp_url: str, blog: BlogUrls) -> List[Dict[str, Any]]:
    source_pw = sync_playwright().start()
    try:
        source_browser = source_pw.chromium.connect_over_cdp(source_cdp_url)
        if not source_browser.contexts:
            raise PublishError("No browser context found on source CDP browser.")
        source_context, _ = select_context_for_blog(source_browser, blog)
        return source_context.cookies([blog.home_url, blog.manage_url, blog.edit_url])
    finally:
        source_pw.stop()


def migrate_tistory_session_to_headless(
    source_cdp_url: str,
    target_cdp_url: str,
    blog: BlogUrls,
) -> None:
    cookies = extract_source_cookies(source_cdp_url, blog)

    target_pw = sync_playwright().start()
    try:
        target_browser = target_pw.chromium.connect_over_cdp(target_cdp_url)
        if not target_browser.contexts:
            raise PublishError("No browser context found on fallback headless browser.")
        target_context = target_browser.contexts[0]
        if cookies:
            target_context.add_cookies(cookies)
        page = first_page(target_context)
        try:
            page.goto(blog.edit_url, wait_until="domcontentloaded")
            page.wait_for_timeout(1000)
        except Exception:
            pass
    finally:
        target_pw.stop()


def cdp_has_matching_context(cdp_url: str, blog: BlogUrls) -> bool:
    playwright = sync_playwright().start()
    try:
        browser = playwright.chromium.connect_over_cdp(cdp_url)
        if not browser.contexts:
            return False
        try:
            select_context_for_blog(browser, blog)
        except PublishError:
            return False
        return True
    finally:
        playwright.stop()


def resolve_attachable_cdp_url(cdp_url: str, blog: BlogUrls, run_dir: Optional[Path] = None) -> str:
    metadata = fetch_cdp_metadata(cdp_url)
    if cdp_metadata_is_headless(metadata):
        return cdp_url

    browser_name = str(metadata.get("Browser") or "unknown")
    if run_dir is not None:
        append_publish_log(
            run_dir,
            f"Headed CDP detected at {cdp_url} ({browser_name}); attempting headless fallback.",
        )
    target_cdp_url = find_or_launch_headless_cdp(cdp_url, blog)
    if cdp_has_matching_context(target_cdp_url, blog):
        if run_dir is not None:
            append_publish_log(
                run_dir,
                f"Reusing existing headless CDP session at {target_cdp_url}.",
            )
        return target_cdp_url

    migrate_tistory_session_to_headless(cdp_url, target_cdp_url, blog)
    if not cdp_has_matching_context(target_cdp_url, blog):
        raise PublishError(
            "Headless fallback was prepared, but no Tistory session matching the target blog was available. "
            "Open the target blog in the headed browser or log in again before retrying."
        )
    if run_dir is not None:
        append_publish_log(
            run_dir,
            f"Headless fallback ready at {target_cdp_url}; continuing publish flow there.",
        )
    return target_cdp_url


def build_blog_urls(blog_host: str) -> BlogUrls:
    host = normalize_blog_host(blog_host)
    return BlogUrls(
        host=host,
        home_url=f"https://{host}/",
        manage_url=f"https://{host}/manage",
        edit_url=f"https://{host}/manage/newpost/",
    )


def resolve_blog_urls(
    manifest: Dict[str, Any],
    blog_host: Optional[str],
    edit_url: Optional[str],
) -> BlogUrls:
    if blog_host:
        return build_blog_urls(blog_host)
    if edit_url:
        return build_blog_urls(derive_blog_host_from_url(edit_url))
    if DEFAULT_BLOG_HOST:
        return build_blog_urls(DEFAULT_BLOG_HOST)
    manifest_host = manifest.get("blog", {}).get("host") or manifest.get("publish", {}).get("blog_host")
    if manifest_host:
        return build_blog_urls(manifest_host)
    manifest_post_url = manifest.get("publish", {}).get("post_url")
    if manifest_post_url:
        return build_blog_urls(derive_blog_host_from_url(manifest_post_url))
    raise PublishError("blog host is missing. Pass --blog-host or set TISTORY_BLOG_HOST.")


def normalize_tag_key(tag: str) -> str:
    return tag.strip().casefold()


def inspect_markdown_image_inputs(markdown_text: str, run_dir: Path) -> Dict[str, int]:
    local_images = 0
    unresolved_images = 0
    total_refs = 0

    for match in MARKDOWN_IMAGE_PATTERN.finditer(markdown_text):
        total_refs += 1
        image_target = match.group(2)
        if resolve_local_image_path(run_dir, image_target):
            local_images += 1
        else:
            unresolved_images += 1

    return {
        "total_markdown_images": total_refs,
        "local_markdown_images": local_images,
        "unresolved_markdown_images": unresolved_images,
        "expected_body_images": 1 + local_images,
    }


def validate_publish_inputs(manifest: Dict[str, Any]) -> Dict[str, int]:
    markdown_path = Path(manifest["markdown_path"])
    thumbnail_path = Path(manifest["thumbnail_path"])
    if not markdown_path.exists():
        raise PublishError(f"Markdown file missing: {markdown_path}")
    if not thumbnail_path.exists():
        raise PublishError(f"Thumbnail file missing: {thumbnail_path}")

    tags = manifest.get("tags", [])
    if len(tags) != 10:
        raise PublishError("Exactly 10 validated tags are required before publish.")
    if any(not str(tag).strip() for tag in tags):
        raise PublishError("Empty tags are not allowed.")

    seen = set()
    duplicates = []
    for tag in tags:
        key = normalize_tag_key(str(tag))
        if key in seen:
            duplicates.append(tag)
            continue
        seen.add(key)
    if duplicates:
        raise PublishError(f"Duplicate tags are not allowed: {', '.join(map(str, duplicates))}")

    return inspect_markdown_image_inputs(
        markdown_path.read_text(encoding="utf-8"),
        markdown_path.parent,
    )


def read_markdown(manifest: Dict[str, Any]) -> str:
    return Path(manifest["markdown_path"]).read_text(encoding="utf-8")


LIST_LINE_RE = re.compile(r"^\s*(?:[-*+]|\d+\.)\s+")
TASK_LIST_RE = re.compile(r"^(\s*)[-*]\s+\[[ xX]\]\s+")
INFOGRAPHIC_ALT_TEXT = "인포그래픽"


def normalize_markdown_for_tinymce(markdown_text: str) -> str:
    normalized_lines: List[str] = []
    for raw_line in markdown_text.splitlines():
        line = TASK_LIST_RE.sub(r"\1* ", raw_line.rstrip())
        if LIST_LINE_RE.match(line):
            if normalized_lines:
                prev = normalized_lines[-1]
                if (
                    prev.strip()
                    and not LIST_LINE_RE.match(prev)
                    and not prev.lstrip().startswith((">", "|"))
                ):
                    normalized_lines.append("")
        normalized_lines.append(line)
    return "\n".join(normalized_lines)


def build_infographic_html_block(infographic_src: str) -> str:
    src = html.escape(infographic_src, quote=True)
    alt = html.escape(INFOGRAPHIC_ALT_TEXT, quote=True)
    return f'<p><img src="{src}" alt="{alt}" /></p>'


def ensure_infographic_first_markdown(markdown_text: str, infographic_src: str) -> str:
    src_pattern = re.escape(infographic_src)
    stripped = re.sub(
        rf"(?im)^\s*!\[[^\]]*\]\(\s*{src_pattern}\s*\)\s*$\n?",
        "",
        markdown_text,
    )
    stripped = re.sub(r"\n{3,}", "\n\n", stripped).strip()
    infographic_line = f"![{INFOGRAPHIC_ALT_TEXT}]({infographic_src})"
    return infographic_line if not stripped else f"{infographic_line}\n\n{stripped}"


def ensure_infographic_first_html(body_html: str, infographic_src: str) -> str:
    src_pattern = re.escape(infographic_src)
    stripped = re.sub(
        rf"(?is)<p>\s*<img\b[^>]*src=['\"]{src_pattern}['\"][^>]*>\s*</p>",
        "",
        body_html,
    )
    stripped = re.sub(
        rf"(?is)<img\b[^>]*src=['\"]{src_pattern}['\"][^>]*>",
        "",
        stripped,
    )
    stripped = stripped.strip()
    infographic_block = build_infographic_html_block(infographic_src)
    return infographic_block if not stripped else infographic_block + stripped


def safe_screenshot(page: Optional[Page], path: Path) -> None:
    if page is None:
        return
    try:
        page.screenshot(path=str(path), full_page=True)
    except Exception:
        pass


def score_context_for_blog(context: BrowserContext, blog: BlogUrls) -> int:
    score = 0
    try:
        for page in context.pages:
            parsed = urlparse(page.url)
            if parsed.netloc == blog.host:
                score += 4
                if parsed.path.startswith("/manage"):
                    score += 2
    except Exception:
        pass

    cookies = []
    try:
        cookies = context.cookies([blog.home_url, blog.manage_url, blog.edit_url])
    except Exception:
        try:
            cookies = context.cookies()
        except Exception:
            cookies = []

    for cookie in cookies:
        domain = (cookie.get("domain") or "").lstrip(".").lower()
        if not domain:
            continue
        if domain == blog.host:
            score += 5
        elif domain.endswith("tistory.com"):
            score += 2
        elif domain.endswith("kakao.com"):
            score += 1
    return score


def select_context_for_blog(browser: Browser, blog: BlogUrls) -> tuple[BrowserContext, int]:
    best_context = None
    best_index = -1
    best_score = -1

    for index, context in enumerate(browser.contexts):
        score = score_context_for_blog(context, blog)
        if score > best_score:
            best_context = context
            best_index = index
            best_score = score

    if best_context is None or best_score <= 0:
        raise PublishError(
            f"No attached browser context matched {blog.host}. "
            "Open the target Tistory blog in the logged-in browser first."
        )

    return best_context, best_index


def launch_persistent_headless_context(
    source_cdp_url: str,
    blog: BlogUrls,
    run_dir: Optional[Path] = None,
) -> AttachState:
    try:
        cookies = extract_source_cookies(source_cdp_url, blog)
    except Exception as exc:
        raise PublishError(
            "Local persistent headless fallback needs the source browser session, "
            f"but cookies could not be read from {source_cdp_url}: {exc}. "
            "Reopen the logged-in source browser/CDP session first, then retry publish."
        ) from exc
    profile_dir = create_fallback_profile_dir(run_dir)
    chrome_binary = find_chrome_binary()
    playwright = sync_playwright().start()
    try:
        context = playwright.chromium.launch_persistent_context(
            user_data_dir=str(profile_dir),
            headless=True,
            executable_path=chrome_binary,
            args=["--no-sandbox", "--disable-dev-shm-usage", "--ozone-platform=headless"],
            viewport={"width": VIEWPORT_W, "height": VIEWPORT_H},
        )
        context.set_default_timeout(DEFAULT_TIMEOUT_MS)
        if cookies:
            context.add_cookies(cookies)
        page = first_page(context)
        try:
            page.goto(blog.edit_url, wait_until="domcontentloaded")
            page.wait_for_timeout(1000)
        finally:
            try:
                page.close()
            except Exception:
                pass
        return AttachState(
            playwright=playwright,
            browser=None,
            context=context,
            context_index=0,
            cdp_url=None,
            launch_mode="persistent",
        )
    except Exception:
        playwright.stop()
        raise


def connect_attach_state(
    playwright: Playwright,
    target_cdp_url: str,
    blog: BlogUrls,
    launch_mode: str,
) -> AttachState:
    try:
        browser = playwright.chromium.connect_over_cdp(target_cdp_url)
    except Exception as exc:
        raise PublishError(f"Failed to attach CDP browser at {target_cdp_url}: {exc}") from exc

    if not browser.contexts:
        raise PublishError("No browser context found on attached CDP browser.")

    context, context_index = select_context_for_blog(browser, blog)
    context.set_default_timeout(DEFAULT_TIMEOUT_MS)
    return AttachState(
        playwright=playwright,
        browser=browser,
        context=context,
        context_index=context_index,
        cdp_url=target_cdp_url,
        launch_mode=launch_mode,
    )


def attach_cdp(
    cdp_url: str,
    blog: BlogUrls,
    run_dir: Optional[Path] = None,
) -> AttachState:
    try:
        metadata = fetch_cdp_metadata(cdp_url)
        browser_name = str(metadata.get("Browser") or "unknown")
        user_agent = str(metadata.get("User-Agent") or "unknown")
        if run_dir is not None:
            append_publish_log(
                run_dir,
                f"Attempting direct CDP attach: cdp_url={cdp_url} browser={browser_name} user_agent={user_agent}",
            )
    except PublishError as exc:
        metadata = None
        if run_dir is not None:
            append_publish_log(run_dir, f"CDP metadata probe failed at {cdp_url}: {exc}")

    direct_error: Optional[Exception] = None
    playwright = sync_playwright().start()
    try:
        return connect_attach_state(playwright, cdp_url, blog, "cdp")
    except Exception as exc:
        direct_error = exc
        playwright.stop()
        if run_dir is not None:
            append_publish_log(
                run_dir,
                f"Direct CDP attach failed ({exc}); trying browser-session recovery.",
            )

    try:
        effective_cdp_url = resolve_attachable_cdp_url(cdp_url, blog, run_dir)
        if effective_cdp_url != cdp_url:
            playwright = sync_playwright().start()
            try:
                return connect_attach_state(playwright, effective_cdp_url, blog, "cdp_recovered")
            except Exception:
                playwright.stop()
                raise
    except PublishError as exc:
        if run_dir is not None:
            append_publish_log(
                run_dir,
                f"CDP recovery path failed ({exc}); trying local browser fallback.",
            )

    if direct_error is None and metadata is not None:
        raise PublishError(
            f"Unable to attach a usable browser context for {blog.host} via {cdp_url}."
        )
    return launch_persistent_headless_context(cdp_url, blog, run_dir)


def close_attach_state(attach_state: Optional[AttachState]) -> None:
    if attach_state is None:
        return
    try:
        if attach_state.launch_mode == "persistent":
            attach_state.context.close()
    except Exception:
        pass
    try:
        attach_state.playwright.stop()
    except Exception:
        pass


def first_page(context: BrowserContext) -> Page:
    page = context.new_page()
    page.set_viewport_size({"width": VIEWPORT_W, "height": VIEWPORT_H})
    return page


def normalized_text(text: str) -> str:
    return re.sub(r"\s+", "", text or "")


def update_publish_metadata(session: PublishSession) -> None:
    session.manifest["blog"]["host"] = session.blog.host
    session.manifest["blog"]["home_url"] = session.blog.home_url
    session.manifest["blog"]["edit_url"] = session.blog.edit_url
    session.manifest["publish"]["blog_host"] = session.blog.host
    session.manifest["publish"]["edit_url"] = session.blog.edit_url
    session.manifest["publish"]["cdp_url"] = (
        session.attach_state.cdp_url
        if session.attach_state is not None and session.attach_state.cdp_url is not None
        else session.cdp_url
    )
    if session.attach_state is not None:
        session.manifest["publish"]["launch_mode"] = session.attach_state.launch_mode
    if session.attach_state is not None:
        session.manifest["publish"]["context_index"] = session.attach_state.context_index
    save_manifest(session.run_dir, session.manifest)


def mark_checkpoint(
    session: PublishSession,
    step: str,
    status: str,
    detail: Optional[Dict[str, Any]] = None,
) -> None:
    publish = session.manifest["publish"]
    checkpoints = publish.setdefault("checkpoints", {})
    checkpoint = checkpoints.setdefault(step, {})
    checkpoint["status"] = status
    checkpoint[f"{status}_at"] = now_iso()
    if detail:
        checkpoint.update(detail)
    publish["current_step"] = step if status == "in_progress" else None
    if status == "completed":
        publish["last_checkpoint"] = step
    save_manifest(session.run_dir, session.manifest)


def sanitize_error_message(exc: Exception) -> str:
    message = str(exc).strip() or exc.__class__.__name__
    lowered = message.lower()
    secret_markers = [
        "password",
        "login_password",
        "tistory_login_password",
        "loginid",
        "email",
        "tistory_login_email",
        "secret file",
        "secret configuration",
        "local tistory secret",
        str(TISTORY_SECRET_FILE).lower(),
    ]
    if any(marker in lowered for marker in secret_markers):
        return "Sensitive login step failed; inspect the local browser session and secret configuration."
    return message


def mark_publish_failure(session: PublishSession, step: str, exc: Exception) -> None:
    safe_error = sanitize_error_message(exc)
    session.manifest["publish"]["status"] = "failed"
    session.manifest["publish"]["last_error"] = safe_error
    mark_checkpoint(session, step, "failed", {"error": safe_error})
    save_manifest(session.run_dir, session.manifest)


def capture_publish_screenshot(session: PublishSession, name: str) -> None:
    if session.page is None:
        return
    path = session.run_dir / f"{name}.png"
    safe_screenshot(session.page, path)
    session.manifest["publish"]["last_screenshot"] = str(path)
    save_manifest(session.run_dir, session.manifest)


def run_publish_step(session: PublishSession, step: str, fn: Callable[[PublishSession], None]) -> None:
    append_publish_log(session.run_dir, f"STEP {step} start")
    mark_checkpoint(session, step, "in_progress")
    try:
        fn(session)
    except Exception as exc:
        capture_publish_screenshot(session, f"{step}_failed")
        mark_publish_failure(session, step, exc)
        raise
    capture_publish_screenshot(session, f"{step}_checkpoint")
    mark_checkpoint(session, step, "completed")
    append_publish_log(session.run_dir, f"STEP {step} done")


def get_single_visible_locator(page: Page, selectors: List[str]):
    for selector in selectors:
        locator = page.locator(selector)
        try:
            count = locator.count()
        except Exception:
            continue
        for index in range(min(count, 5)):
            candidate = locator.nth(index)
            try:
                if candidate.is_visible():
                    return candidate
            except Exception:
                continue
    return None


def get_first_existing_locator(page: Page, selectors: List[str]):
    for selector in selectors:
        locator = page.locator(selector)
        try:
            if locator.count() > 0:
                return locator.first
        except Exception:
            continue
    return None


def click_visible(page: Page, selectors: List[str]) -> bool:
    locator = get_single_visible_locator(page, selectors)
    if locator is None:
        return False
    try:
        locator.click()
        return True
    except Exception:
        try:
            locator.click(force=True)
            return True
        except Exception:
            return False


def visible_button_texts(page: Page) -> List[str]:
    try:
        return page.evaluate(
            """() => {
              const visible = (el) => !!el && !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
              return [...document.querySelectorAll('button,[role="button"]')]
                .filter(visible)
                .map((el) => (el.textContent || '').trim())
                .filter(Boolean);
            }"""
        )
    except Exception:
        return []


def publish_url_pattern(blog_host: str) -> re.Pattern[str]:
    return re.compile(
        rf"https?://{re.escape(blog_host)}/(?:\d+|entry/[^\"'\\s<]+)(?:[/?#][^\"'\\s<]*)?$"
    )


def publish_success_markers() -> List[str]:
    return [
        "발행이 완료",
        "글이 발행",
        "저장되었습니다",
        "등록되었습니다",
        "저장했어요",
    ]


def wait_for_publish_completion(page: Page, blog_host: str, previous_url: str) -> None:
    pattern = publish_url_pattern(blog_host)
    markers = publish_success_markers()
    deadline = time.monotonic() + 180
    last_buttons: List[str] = []
    last_url = previous_url

    while time.monotonic() < deadline:
        try:
            current_url = page.url or previous_url
            last_url = current_url
        except Exception:
            current_url = last_url

        if current_url != previous_url or pattern.match(current_url):
            return

        try:
            body_text = page.locator("body").inner_text(timeout=1000)
        except Exception:
            body_text = ""
        if any(marker in body_text for marker in markers):
            return

        try:
            buttons = visible_button_texts(page)
            last_buttons = buttons or last_buttons
        except Exception:
            buttons = last_buttons

        normalized_buttons = [re.sub(r"\s+", "", text) for text in buttons]
        has_private_submit = any(text in {"비공개저장", "비공개발행"} for text in normalized_buttons)
        has_saving_indicator = any("저장중" in text for text in normalized_buttons)
        if not has_private_submit and not has_saving_indicator:
            return

        page.wait_for_timeout(1000)

    raise PublishError(
        "Timed out while waiting for publish completion. "
        f"last_url={last_url} visible_buttons={', '.join(last_buttons)}"
    )


def fill_visible(page: Page, selectors: List[str], value: str) -> bool:
    locator = get_single_visible_locator(page, selectors)
    if locator is None:
        return False
    try:
        locator.fill(value)
        return True
    except Exception:
        try:
            locator.click()
            page.keyboard.press("Meta+A")
            page.keyboard.press("Control+A")
            page.keyboard.press("Backspace")
            page.keyboard.insert_text(value)
            return True
        except Exception:
            return False


def dismiss_common_popups(page: Page) -> None:
    candidates = [
        "button:has-text('닫기')",
        "button:has-text('취소')",
        "button:has-text('나중에')",
        "button:has-text('괜찮아요')",
        "button:has-text('확인')",
    ]
    for selector in candidates:
        try:
            locator = page.locator(selector).first
            if locator.is_visible():
                locator.click(timeout=800)
                page.wait_for_timeout(250)
        except Exception:
            continue


def validate_secret_file_permissions(path: Path) -> None:
    mode = S_IMODE(path.stat().st_mode)
    if mode & 0o077:
        raise PublishError(
            f"Secret file permissions are too open for {path}. Use chmod 600 and retry."
        )



def load_credentials_from_secret_file(path: Path) -> Optional[LoginCredentials]:
    if not path.exists():
        return None
    validate_secret_file_permissions(path)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise PublishError("Failed to read the local Tistory secret file.") from exc
    if not isinstance(payload, dict):
        raise PublishError("Local Tistory secret file must contain a JSON object.")
    email = str(payload.get("email") or payload.get("login_email") or "").strip()
    password = str(payload.get("password") or payload.get("login_password") or "").strip()
    if not email or not password:
        raise PublishError("Local Tistory secret file is missing email/password fields.")
    return LoginCredentials(email=email, password=password, source="secret_file")



def load_login_credentials() -> Optional[LoginCredentials]:
    env_email = (os.environ.get("TISTORY_LOGIN_EMAIL") or "").strip()
    env_password = (os.environ.get("TISTORY_LOGIN_PASSWORD") or "").strip()
    if env_email and env_password:
        return LoginCredentials(email=env_email, password=env_password, source="environment")
    if env_email or env_password:
        raise PublishError("Both TISTORY_LOGIN_EMAIL and TISTORY_LOGIN_PASSWORD must be set together.")
    return load_credentials_from_secret_file(TISTORY_SECRET_FILE)


def clear_login_sensitive_fields(page: Page, selectors: List[str]) -> None:
    locator = get_single_visible_locator(page, selectors)
    if locator is None:
        return
    try:
        locator.fill("")
        return
    except Exception:
        pass
    try:
        locator.click()
        page.keyboard.press("Meta+A")
        page.keyboard.press("Control+A")
        page.keyboard.press("Backspace")
    except Exception:
        return



def attempt_login_if_needed(page: Page, run_dir: Path) -> bool:
    if not page_looks_like_login(page):
        return False

    credentials = load_login_credentials()
    if credentials is None:
        raise PublishError(
            "Attached browser is not logged in for this blog. "
            "No local Tistory login credentials were available; log in manually and retry."
        )

    append_publish_log(run_dir, "Login page detected; attempting local secret-backed login.")

    email_selectors = [
        "input[name='loginId']",
        "input[name='email']",
        "input[type='email']",
        "input[placeholder*='이메일']",
        "input[placeholder*='전화번호']",
        "input[placeholder*='email']",
        "input[placeholder*='phone']",
        "input[id*='loginId']",
    ]
    password_selectors = [
        "input[name='password']",
        "input[type='password']",
        "input[id*='password']",
        "input[autocomplete='current-password']",
    ]
    submit_selectors = [
        "button[type='submit']",
        "button:has-text('로그인')",
        "button:has-text('Log In')",
        "button:has-text('Login')",
        "input[type='submit']",
    ]
    kakao_entry_selectors = [
        "a.link_kakao_id",
        "a:has-text('카카오계정으로 로그인')",
        "button:has-text('카카오계정으로 로그인')",
        "a:has-text('Log in with Kakao Account')",
        "button:has-text('Log in with Kakao Account')",
    ]

    try:
        if get_single_visible_locator(page, email_selectors) is None and click_visible(page, kakao_entry_selectors):
            page.wait_for_load_state("domcontentloaded")
            page.wait_for_timeout(1800)
            dismiss_common_popups(page)
        if not fill_visible(page, email_selectors, credentials.email):
            raise PublishError("Login page detected, but the Kakao email field was not found.")
        if not fill_visible(page, password_selectors, credentials.password):
            raise PublishError("Login page detected, but the Kakao password field was not found.")
        if not click_visible(page, submit_selectors):
            raise PublishError("Login page detected, but the Kakao login button was not found.")

        page.wait_for_load_state("domcontentloaded")
        page.wait_for_timeout(2500)
        dismiss_common_popups(page)

        if page_looks_like_login(page):
            raise PublishError(
                "Automatic Kakao login did not complete. "
                "Finish any extra verification in the browser and retry."
            )
    except Exception:
        clear_login_sensitive_fields(page, password_selectors)
        clear_login_sensitive_fields(page, email_selectors)
        raise

    safe_screenshot(page, run_dir / "02a_login_completed.png")
    return True


def page_looks_like_login(page: Page) -> bool:
    current = page.url.lower()
    if any(token in current for token in ["accounts.kakao.com", "/login", "auth"]):
        return True
    try:
        text = page.locator("body").inner_text(timeout=3000)
    except Exception:
        return False
    markers = ["카카오계정", "로그인", "이메일 또는 전화번호", "비밀번호"]
    return sum(1 for token in markers if token in text) >= 2


def preflight_context(session: PublishSession) -> None:
    if session.attach_state is None:
        raise PublishError("CDP is not attached.")
    page = first_page(session.attach_state.context)
    session.page = page

    page.goto(session.blog.home_url, wait_until="domcontentloaded")
    page.wait_for_timeout(1000)
    safe_screenshot(page, session.run_dir / "01_preflight_home.png")

    page.goto(session.blog.edit_url, wait_until="domcontentloaded")
    page.wait_for_timeout(1200)
    dismiss_common_popups(page)
    safe_screenshot(page, session.run_dir / "02_preflight_editor.png")

    if page_looks_like_login(page):
        try:
            attempted_login = attempt_login_if_needed(page, session.run_dir)
        except PublishError:
            raise
        except Exception as exc:
            raise PublishError("Automatic Kakao login failed before editor preflight completed.") from exc
        if attempted_login:
            page.goto(session.blog.edit_url, wait_until="domcontentloaded")
            page.wait_for_timeout(1200)
            dismiss_common_popups(page)
            safe_screenshot(page, session.run_dir / "02b_preflight_editor_post_login.png")
        if page_looks_like_login(page):
            raise PublishError(
                f"Attached browser is not logged in for {session.blog.host}. "
                "Open the Tistory editor in the attached browser first."
            )

    if "/manage/" not in page.url:
        raise PublishError(f"Unexpected editor URL after preflight: {page.url}")

    try:
        cookies = session.attach_state.context.cookies([session.blog.home_url, session.blog.manage_url])
    except Exception:
        cookies = []
    if not cookies:
        raise PublishError(f"No usable Tistory session cookies detected for {session.blog.host}.")


def detect_editor_variant(page: Page) -> Optional[str]:
    variants = [
        ("codemirror", ".CodeMirror textarea"),
        ("markdown_textarea", "[data-mode='markdown'] textarea"),
        ("textarea_body", "textarea[placeholder*='본문']"),
        ("textarea_content", "textarea[placeholder*='내용']"),
    ]
    for variant, selector in variants:
        locator = get_single_visible_locator(page, [selector])
        if locator is not None:
            return variant
    try:
        if page.evaluate("() => !!(window.tinymce && window.tinymce.activeEditor)"):
            return "tinymce_wysiwyg"
    except Exception:
        pass
    iframe_locator = get_single_visible_locator(page, ["iframe#editor-tistory_ifr"])
    if iframe_locator is not None:
        return "tinymce_wysiwyg"
    return None


def ensure_markdown_mode(page: Page, run_dir: Path) -> str:
    dismiss_common_popups(page)

    mode_menu_selectors = [
        "button:has-text('기본모드')",
        "button:has-text('기본 모드')",
        "[role='button']:has-text('기본모드')",
        "[role='button']:has-text('기본 모드')",
    ]
    click_visible(page, mode_menu_selectors)
    page.wait_for_timeout(500)

    markdown_selectors = [
        "button:has-text('마크다운')",
        "[role='menuitem']:has-text('마크다운')",
        "[role='button']:has-text('마크다운')",
        "li:has-text('마크다운')",
        "text=마크다운",
    ]
    if click_visible(page, markdown_selectors):
        page.wait_for_timeout(600)
        click_visible(
            page,
            [
                "button:has-text('확인')",
                "button:has-text('전환')",
                "button:has-text('변환')",
                "[role='button']:has-text('확인')",
            ],
        )
        page.wait_for_timeout(900)

    editor_variant = detect_editor_variant(page)
    if editor_variant is None:
        safe_screenshot(page, run_dir / "03_markdown_mode_failed.png")
        raise PublishError("Failed to confirm a supported editor.")

    safe_screenshot(page, run_dir / "03_markdown_mode_ready.png")
    return editor_variant


def fill_title(page: Page, title: str, run_dir: Path) -> None:
    title_selectors = [
        "input[name='title']",
        "textarea[name='title']",
        "input[placeholder*='제목']",
        "textarea[placeholder*='제목']",
    ]
    if not fill_visible(page, title_selectors, title):
        safe_screenshot(page, run_dir / "04_title_fill_failed.png")
        raise PublishError("Failed to fill title field.")


def fill_body(
    page: Page,
    markdown_text: str,
    run_dir: Path,
    editor_variant: Optional[str],
    infographic_src: str,
) -> None:
    sanitized_markdown = sanitize_markdown_for_publish(markdown_text, run_dir)
    prepared_markdown = materialize_local_body_images_for_publish(page, run_dir, sanitized_markdown)
    prepared_markdown = ensure_infographic_first_markdown(prepared_markdown, infographic_src)
    if editor_variant == "tinymce_wysiwyg":
        normalized_markdown = normalize_markdown_for_tinymce(prepared_markdown)
        html_text = markdown_lib.markdown(
            normalized_markdown,
            extensions=["extra", "nl2br", "sane_lists"],
        )
        html_text = ensure_infographic_first_html(html_text, infographic_src)
        try:
            success = page.evaluate(
                """async ({ html, infographicSrc }) => {
                  const editor = window.tinymce && window.tinymce.activeEditor;
                  if (!editor) return false;
                  editor.focus();
                  editor.setContent(html);
                  const body = editor.getBody();
                  if (body) {
                    const existing = Array.from(body.querySelectorAll('img')).filter((img) => {
                      const src = (img.getAttribute('src') || '').trim();
                      const alt = (img.getAttribute('alt') || '').trim();
                      return src === infographicSrc || alt === '인포그래픽';
                    });
                    for (const node of existing) {
                      const wrapper = node.parentElement;
                      if (wrapper && wrapper.tagName === 'P' && wrapper.childElementCount === 1) {
                        wrapper.remove();
                      } else {
                        node.remove();
                      }
                    }
                    const wrapper = editor.dom.create('p');
                    const image = editor.dom.create('img', { src: infographicSrc, alt: '인포그래픽' });
                    wrapper.appendChild(image);
                    body.insertBefore(wrapper, body.firstChild);
                  }
                  if (editor.uploadImages) {
                    try {
                      await editor.uploadImages();
                    } catch (error) {
                      console.warn('tinymce uploadImages failed', error);
                    }
                  }
                  editor.save();
                  editor.fire('change');
                  return true;
                }""",
                {"html": html_text, "infographicSrc": infographic_src},
            )
            if not success:
                raise PublishError("TinyMCE editor handle was not available.")
            page.wait_for_timeout(900)
            safe_screenshot(page, run_dir / "05_body_filled.png")
            return
        except Exception as exc:
            safe_screenshot(page, run_dir / "05_body_fill_failed.png")
            raise PublishError(f"Failed to fill TinyMCE body field: {exc}") from exc

    body_selector_map = {
        "codemirror": [".CodeMirror textarea", "div.CodeMirror textarea"],
        "markdown_textarea": ["[data-mode='markdown'] textarea"],
        "textarea_body": ["textarea[placeholder*='본문']"],
        "textarea_content": ["textarea[placeholder*='내용']"],
    }
    body_selectors = body_selector_map.get(editor_variant or "", []) + [
        ".CodeMirror textarea",
        "[data-mode='markdown'] textarea",
        "div.CodeMirror textarea",
        "textarea[placeholder*='본문']",
        "textarea[placeholder*='내용']",
    ]
    for selector in body_selectors:
        locator = page.locator(selector)
        try:
            count = locator.count()
        except Exception:
            continue
        for index in range(min(count, 5)):
            candidate = locator.nth(index)
            try:
                if not candidate.is_visible():
                    continue
            except Exception:
                continue
            try:
                placeholder = (candidate.get_attribute("placeholder") or "").lower()
                name = (candidate.get_attribute("name") or "").lower()
                if "제목" in placeholder or "title" in name:
                    continue
            except Exception:
                pass
            try:
                candidate.click()
                candidate.fill(prepared_markdown)
            except Exception:
                try:
                    candidate.click()
                    page.keyboard.press("Meta+A")
                    page.keyboard.press("Control+A")
                    page.keyboard.press("Backspace")
                    page.keyboard.insert_text(prepared_markdown)
                except Exception:
                    continue
            page.wait_for_timeout(900)
            safe_screenshot(page, run_dir / "05_body_filled.png")
            return

    safe_screenshot(page, run_dir / "05_body_fill_failed.png")
    raise PublishError("Failed to fill markdown body field.")


def fill_title_and_body(
    page: Page,
    title: str,
    markdown_text: str,
    run_dir: Path,
    editor_variant: Optional[str],
    infographic_src: str,
) -> None:
    fill_title(page, title, run_dir)
    fill_body(page, markdown_text, run_dir, editor_variant, infographic_src)


def resolve_local_image_path(run_dir: Path, raw_target: str) -> Optional[Path]:
    target = (raw_target or "").strip()
    if not target:
        return None
    parsed = urlparse(target)
    if parsed.scheme in {"http", "https", "data"}:
        return None
    candidate = Path(target)
    if not candidate.is_absolute():
        candidate = run_dir / candidate
    try:
        resolved = candidate.resolve()
    except Exception:
        resolved = candidate
    return resolved if resolved.exists() and resolved.is_file() else None


def sanitize_markdown_for_publish(markdown_text: str, run_dir: Path) -> str:
    def replace_markdown_image(match: re.Match[str]) -> str:
        image_target = match.group(2)
        return match.group(0) if resolve_local_image_path(run_dir, image_target) else ""

    stripped = MARKDOWN_IMAGE_PATTERN.sub(replace_markdown_image, markdown_text)
    stripped = HTML_IMAGE_PATTERN.sub("", stripped)
    stripped = re.sub(r"\n{3,}", "\n\n", stripped)
    return stripped.strip()


def requests_session_from_page(page: Page, blog_host: str) -> requests.Session:
    cookies = page.context.cookies([f"https://{blog_host}/", f"https://{blog_host}/manage"])
    session = requests.Session()
    for cookie in cookies:
        session.cookies.set(
            cookie["name"],
            cookie["value"],
            domain=cookie.get("domain"),
            path=cookie.get("path"),
        )
    return session


def upload_local_body_image(page: Page, blog_host: str, image_path: Path) -> AttachmentInfo:
    mime_type = mimetypes.guess_type(str(image_path))[0] or "application/octet-stream"
    attach_url = f"https://{blog_host}/manage/post/attach.json"
    session = requests_session_from_page(page, blog_host)
    with image_path.open("rb") as image_file:
        response = session.post(
            attach_url,
            files={"file": (image_path.name, image_file, mime_type)},
            timeout=30,
        )
    response.raise_for_status()
    payload = response.json()
    image_url = str(payload.get("url") or "").strip()
    image_key = str(payload.get("key") or "").strip()
    image_filename = str(payload.get("filename") or payload.get("name") or image_path.name).strip()
    if not image_url or not image_key:
        raise PublishError(f"Failed to upload body image: {image_path}")
    return AttachmentInfo(url=image_url, key=image_key, filename=image_filename)


def materialize_local_body_images_for_publish(page: Page, run_dir: Path, markdown_text: str) -> str:
    parsed = urlparse(page.url or "")
    blog_host = parsed.netloc.strip().lower()
    if not blog_host:
        return markdown_text

    cache: Dict[str, AttachmentInfo] = {}

    def replace_markdown_image(match: re.Match[str]) -> str:
        alt_text = match.group(1) or "본문 이미지"
        image_target = match.group(2)
        local_path = resolve_local_image_path(run_dir, image_target)
        if local_path is None:
            return ""
        cache_key = str(local_path)
        attachment = cache.get(cache_key)
        if attachment is None:
            attachment = upload_local_body_image(page, blog_host, local_path)
            cache[cache_key] = attachment
        return (
            f'<img src="{html.escape(attachment.url, quote=True)}" '
            f'alt="{html.escape(alt_text, quote=True)}" '
            'style="max-height:300px;height:auto;width:auto;" />'
        )

    return MARKDOWN_IMAGE_PATTERN.sub(replace_markdown_image, markdown_text)


def fill_tags(page: Page, tags: List[str], run_dir: Path) -> None:
    tags_joined = ",".join(tags)
    if fill_visible(
        page,
        [
            "input[placeholder*='태그']",
            "textarea[placeholder*='태그']",
            "input[name='tag']",
            "input[name='tags']",
        ],
        tags_joined,
    ):
        page.wait_for_timeout(500)
        safe_screenshot(page, run_dir / "06_tags_filled.png")
        return
    append_publish_log(run_dir, "Tag input not found before publish; will retry inside publish dialog.")


def open_publish_dialog(page: Page, run_dir: Path) -> None:
    if not click_visible(
        page,
        [
            "button:has-text('완료')",
            "[role='button']:has-text('완료')",
            "button:has-text('발행')",
            "[role='button']:has-text('발행')",
        ],
    ):
        safe_screenshot(page, run_dir / "07_publish_dialog_failed.png")
        raise PublishError("Failed to open publish dialog.")
    page.wait_for_timeout(1200)
    safe_screenshot(page, run_dir / "08_publish_dialog_open.png")


def ensure_tags_in_publish_dialog(page: Page, tags: List[str]) -> None:
    fill_visible(
        page,
        [
            "input[placeholder*='태그']",
            "textarea[placeholder*='태그']",
            "input[name='tag']",
            "input[name='tags']",
        ],
        ",".join(tags),
    )
    page.wait_for_timeout(400)


def upload_representative_image(page: Page, thumbnail_path: str, run_dir: Path) -> AttachmentInfo:
    click_visible(
        page,
        [
            "button:has-text('대표 이미지')",
            "[role='button']:has-text('대표 이미지')",
            "button:has-text('대표이미지')",
            "[role='button']:has-text('대표이미지')",
        ],
    )
    page.wait_for_timeout(400)

    locator = get_first_existing_locator(
        page,
        [
            "xpath=//*[contains(normalize-space(.), '대표 이미지')]//input[@type='file']",
            "xpath=//*[contains(normalize-space(.), '대표이미지')]//input[@type='file']",
            "input[type='file']",
        ],
    )
    if locator is None:
        safe_screenshot(page, run_dir / "09_thumbnail_upload_failed.png")
        raise PublishError("Representative image file input not found.")

    try:
        with page.expect_response(
            lambda response: response.request.method == "POST"
            and response.url.endswith("/manage/post/attach.json"),
            timeout=20000,
        ) as response_info:
            locator.set_input_files(thumbnail_path)
    except Exception as exc:
        safe_screenshot(page, run_dir / "09_thumbnail_upload_failed.png")
        raise PublishError(f"Failed to upload representative image: {exc}") from exc

    response = response_info.value
    try:
        payload = response.json()
    except Exception as exc:
        safe_screenshot(page, run_dir / "09_thumbnail_upload_failed.png")
        raise PublishError("Representative image upload response was not valid JSON.") from exc

    image_url = str(payload.get("url") or "").strip()
    image_key = str(payload.get("key") or "").strip()
    image_filename = str(payload.get("filename") or payload.get("name") or "").strip()
    if not image_url or not image_key:
        safe_screenshot(page, run_dir / "09_thumbnail_upload_failed.png")
        raise PublishError("Representative image upload did not return a usable URL/key.")

    page.wait_for_timeout(1800)
    body_text = page.locator("body").inner_text(timeout=5000)
    if "대표 이미지" not in body_text and "대표이미지" not in body_text:
        safe_screenshot(page, run_dir / "10_thumbnail_upload_weak_signal.png")
    safe_screenshot(page, run_dir / "10_thumbnail_uploaded.png")
    return AttachmentInfo(url=image_url, key=image_key, filename=image_filename)


def close_publish_dialog(page: Page, run_dir: Path) -> None:
    if not click_visible(
        page,
        [
            "button:has-text('취소')",
            "[role='button']:has-text('취소')",
        ],
    ):
        safe_screenshot(page, run_dir / "08_publish_dialog_close_failed.png")
        raise PublishError("Failed to close publish dialog after seeding infographic.")
    page.wait_for_timeout(1000)


def choose_private_publish(page: Page, run_dir: Path) -> None:
    result = page.evaluate(
        """() => {
          const visible = (el) => !!el && !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
          const norm = (s) => (s || '').replace(/\\s+/g, '').trim();
          const nodes = [...document.querySelectorAll('label,button,[role="radio"],li,div,span')].filter(visible);
          const candidate = nodes.find((el) => norm(el.textContent) === '비공개');
          if (!candidate) return { found: false, checked: false };

          const clickable = candidate.closest('label,[role="radio"],button,li,div') || candidate;
          clickable.click();

          const radios = [
            clickable,
            clickable.parentElement,
            clickable.closest('label'),
            clickable.closest('[role="radio"]'),
            clickable.closest('li'),
            clickable.closest('div'),
          ].filter(Boolean);

          let checked = false;
          for (const node of radios) {
            const input = node.querySelector && node.querySelector('input[type="radio"]');
            if (input && input.checked) {
              checked = true;
              break;
            }
            if (node.getAttribute && node.getAttribute('aria-checked') === 'true') {
              checked = true;
              break;
            }
          }

          return {
            found: true,
            checked,
            text: (clickable.textContent || '').trim(),
          };
        }"""
    )
    if not result.get("found"):
        safe_screenshot(page, run_dir / "11_private_select_failed.png")
        raise PublishError("Private visibility option not found in publish dialog.")
    if not result.get("checked"):
        safe_screenshot(page, run_dir / "11_private_select_failed.png")
        raise PublishError("Failed to confirm private visibility selection.")
    page.wait_for_timeout(500)
    safe_screenshot(page, run_dir / "11_private_selected.png")


def click_safe_private_submit(page: Page, run_dir: Path) -> None:
    buttons = visible_button_texts(page)
    previous_url = page.url

    private_button = get_single_visible_locator(
        page,
        [
            "button:has-text('비공개 저장')",
            "[role='button']:has-text('비공개 저장')",
            "button:has-text('비공개 발행')",
            "[role='button']:has-text('비공개 발행')",
        ],
    )
    public_button = get_single_visible_locator(
        page,
        [
            "button:has-text('공개 발행')",
            "[role='button']:has-text('공개 발행')",
        ],
    )

    if private_button is None:
        safe_screenshot(page, run_dir / "12_publish_submit_failed.png")
        reason = "unsafe_public_submit_visible" if public_button is not None else "safe_private_submit_not_found"
        raise PublishError(
            f"Failed to find a safe private publish button ({reason}). Visible buttons: {', '.join(buttons)}"
        )

    try:
        private_button.click(timeout=5000)
    except Exception:
        try:
            private_button.click(force=True, timeout=5000)
        except Exception as exc:
            safe_screenshot(page, run_dir / "12_publish_submit_failed.png")
            raise PublishError(
                f"Failed to click the private publish button. Visible buttons: {', '.join(buttons)}"
            ) from exc

    try:
        wait_for_publish_completion(page, derive_blog_host_from_url(page.url or previous_url), previous_url)
    except Exception as exc:
        safe_screenshot(page, run_dir / "12_publish_submit_failed.png")
        current_buttons = ", ".join(visible_button_texts(page))
        raise PublishError(
            f"Private publish click did not produce a completion signal. Visible buttons after click: {current_buttons}"
        ) from exc

    page.wait_for_timeout(2000)
    safe_screenshot(page, run_dir / "12_after_publish.png")


def try_extract_post_url(page: Page, blog_host: str) -> Optional[str]:
    pattern = publish_url_pattern(blog_host)
    current = page.url
    if pattern.match(current):
        return current

    hrefs: List[str] = []
    try:
        hrefs = page.locator("a").evaluate_all(
            "(els) => els.map((el) => el.href).filter(Boolean)"
        )
    except Exception:
        hrefs = []
    for href in hrefs:
        if pattern.match(href):
            return href

    try:
        html = page.content()
    except Exception:
        return None

    match = pattern.search(html)
    if match:
        return match.group(0)

    try:
        body_text = page.locator("body").inner_text(timeout=3000)
    except Exception:
        return None
    match = pattern.search(body_text)
    if match:
        return match.group(0)
    return None


def has_publish_success_signal(page: Page) -> bool:
    try:
        body_text = page.locator("body").inner_text(timeout=3000)
    except Exception:
        return False

    success_markers = publish_success_markers()
    return any(marker in body_text for marker in success_markers)


def extract_post_id(url: Optional[str]) -> Optional[str]:
    if not url:
        return None
    match = re.search(r"/(\d+)(?:[/?#]|$)", url)
    return match.group(1) if match else None


def normalized_contains(text: str, needle: str) -> bool:
    return normalized_text(needle) in normalized_text(text)


def detect_raw_markdown(text: str) -> bool:
    return any(re.search(pattern, text) for pattern in RAW_MD_LEAK_PATTERNS)


def build_render_check(page: Page, url: str, expected_title: str) -> Dict[str, Any]:
    response = page.goto(url, wait_until="domcontentloaded")
    page.wait_for_timeout(1500)

    content_probe = page.evaluate(
        """(selectors) => {
          let root = null;
          for (const selector of selectors) {
            root = document.querySelector(selector);
            if (root) break;
          }
          root = root || document.body;
          return {
            text: root ? (root.innerText || '') : '',
            imgCount: root ? root.querySelectorAll('img').length : 0,
          };
        }""",
        CONTENT_SELECTORS,
    )
    body_text = content_probe.get("text", "")
    title_text = ""
    try:
        title_text = page.title()
    except Exception:
        pass

    return {
        "url": url,
        "final_url": page.url,
        "status_code": response.status if response else None,
        "body_images_found": int(content_probe.get("imgCount", 0)),
        "raw_markdown_leak": detect_raw_markdown(body_text),
        "required_sections_ok": all(normalized_contains(body_text, section) for section in REQUIRED_RENDER_SECTIONS),
        "title_match": normalized_contains(body_text, expected_title) or normalized_contains(title_text, expected_title),
        "page_title": title_text,
    }


def verify_private_render_result(result: Dict[str, Any], expected_body_images: int = 1) -> None:
    status_code = result.get("status_code")
    if status_code and int(status_code) >= 400:
        raise PublishError(f"Rendered page returned HTTP {status_code}")
    if result["raw_markdown_leak"]:
        raise PublishError("Raw markdown leakage detected on rendered page.")
    if not result["required_sections_ok"]:
        raise PublishError("Required sections are missing on rendered page.")
    if result["body_images_found"] < expected_body_images:
        raise PublishError(
            f"Rendered page is missing body images. "
            f"expected_at_least={expected_body_images} found={result['body_images_found']}"
        )
    if not result["title_match"]:
        raise PublishError("Rendered page does not appear to contain the expected title.")


def verify_public_page(public_url: str) -> Dict[str, Any]:
    response = requests.get(public_url, headers={"User-Agent": USER_AGENT}, timeout=20)
    if response.status_code != 200:
        raise PublishError(f"Public URL not accessible: {public_url} ({response.status_code})")

    soup = BeautifulSoup(response.text, "html.parser")
    root = None
    for selector in CONTENT_SELECTORS:
        root = soup.select_one(selector)
        if root is not None:
            break
    if root is None:
        root = soup
    text = root.get_text("\n", strip=True)
    og_image = None
    og = soup.find("meta", attrs={"property": "og:image"})
    if og and og.get("content"):
        og_image = og["content"].strip()

    return {
        "url": public_url,
        "status_code": response.status_code,
        "og_image": og_image,
        "body_images_found": len(root.find_all("img")),
        "raw_markdown_leak": detect_raw_markdown(text),
        "required_sections_ok": all(normalized_contains(text, section) for section in REQUIRED_RENDER_SECTIONS),
    }


def ensure_non_placeholder_og(og_image: Optional[str]) -> None:
    if not og_image:
        raise PublishError("og:image not found.")
    lowered = og_image.lower()
    if any(token in lowered for token in TISTORY_OG_PLACEHOLDER_TOKENS):
        raise PublishError(f"og:image looks like placeholder: {og_image}")


def step_attach_cdp(session: PublishSession) -> None:
    session.attach_state = attach_cdp(
        session.cdp_url,
        session.blog,
        session.run_dir,
    )
    session.cdp_url = session.attach_state.cdp_url
    update_publish_metadata(session)


def step_preflight_context(session: PublishSession) -> None:
    preflight_context(session)


def step_prepare_editor(session: PublishSession) -> None:
    if session.page is None:
        raise PublishError("Editor page is not ready.")
    editor_variant = ensure_markdown_mode(session.page, session.run_dir)
    session.manifest["publish"]["editor_variant"] = editor_variant
    save_manifest(session.run_dir, session.manifest)


def step_fill_title(session: PublishSession) -> None:
    if session.page is None:
        raise PublishError("Editor page is not ready.")
    fill_title(
        session.page,
        title=session.manifest.get("title") or "제목 없음",
        run_dir=session.run_dir,
    )


def step_seed_infographic(session: PublishSession) -> None:
    if session.page is None:
        raise PublishError("Editor page is not ready.")
    open_publish_dialog(session.page, session.run_dir)
    attachment = upload_representative_image(
        session.page,
        session.manifest["thumbnail_path"],
        session.run_dir,
    )
    session.manifest["publish"]["infographic_url"] = attachment.url
    session.manifest["publish"]["thumbnail_ref"] = f"kage@{attachment.key}"
    save_manifest(session.run_dir, session.manifest)
    close_publish_dialog(session.page, session.run_dir)


def step_fill_content(session: PublishSession) -> None:
    if session.page is None:
        raise PublishError("Editor page is not ready.")
    infographic_url = session.manifest["publish"].get("infographic_url")
    if not infographic_url:
        raise PublishError("Infographic upload URL missing before content fill.")
    fill_body(
        session.page,
        markdown_text=read_markdown(session.manifest),
        run_dir=session.run_dir,
        editor_variant=session.manifest["publish"].get("editor_variant"),
        infographic_src=infographic_url,
    )
    fill_tags(session.page, session.manifest["tags"], session.run_dir)


def step_publish_dialog(session: PublishSession) -> None:
    if session.page is None:
        raise PublishError("Editor page is not ready.")
    open_publish_dialog(session.page, session.run_dir)
    ensure_tags_in_publish_dialog(session.page, session.manifest["tags"])
    append_publish_log(session.run_dir, "Publish dialog reopened after infographic seeding; skipping representative image re-upload.")
    choose_private_publish(session.page, session.run_dir)


def step_finalize_publish(session: PublishSession) -> None:
    if session.page is None:
        raise PublishError("Editor page is not ready.")
    click_safe_private_submit(session.page, session.run_dir)

    post_url = try_extract_post_url(session.page, session.blog.host)
    success_signal = has_publish_success_signal(session.page)
    if not post_url and not success_signal:
        raise PublishError("Publish confirmation signal was not detected.")

    session.manifest["publish"]["status"] = "published_private" if post_url else "pending_confirmation"
    session.manifest["publish"]["visibility"] = "private"
    session.manifest["publish"]["post_url"] = post_url
    session.manifest["publish"]["post_id"] = extract_post_id(post_url)
    session.manifest["publish"]["published_at"] = now_iso() if post_url else None
    session.manifest["publish"]["last_error"] = None
    save_manifest(session.run_dir, session.manifest)
    append_publish_log(
        session.run_dir,
        f"Publish finished. status={session.manifest['publish']['status']} post_url={post_url}",
    )


def start_publish_attempt(session: PublishSession) -> None:
    attempts = session.manifest["publish"].setdefault("attempts", [])
    attempts.append(
        {
            "started_at": now_iso(),
            "status": "in_progress",
            "blog_host": session.blog.host,
            "cdp_url": session.cdp_url,
        }
    )
    save_manifest(session.run_dir, session.manifest)


def finalize_publish_attempt(session: PublishSession, status: str, error: Optional[str] = None) -> None:
    attempts = session.manifest["publish"].setdefault("attempts", [])
    if not attempts:
        return
    attempt = attempts[-1]
    attempt["finished_at"] = now_iso()
    attempt["status"] = status
    if error:
        attempt["error"] = sanitize_error_message(PublishError(error))
    if session.manifest["publish"].get("post_url"):
        attempt["post_url"] = session.manifest["publish"]["post_url"]
    save_manifest(session.run_dir, session.manifest)


def run_publish_state_machine(session: PublishSession) -> None:
    session.manifest["publish"]["status"] = "in_progress"
    session.manifest["publish"]["last_error"] = None
    save_manifest(session.run_dir, session.manifest)

    steps = [
        ("attach_cdp", step_attach_cdp),
        ("preflight_context", step_preflight_context),
        ("prepare_editor", step_prepare_editor),
        ("fill_title", step_fill_title),
        ("seed_infographic", step_seed_infographic),
        ("fill_content", step_fill_content),
        ("publish_dialog", step_publish_dialog),
        ("finalize_publish", step_finalize_publish),
    ]

    for step_name, step_fn in steps:
        run_publish_step(session, step_name, step_fn)


def cmd_publish(
    run_dir: Path,
    blog_host: Optional[str],
    cdp_url: str,
    edit_url: Optional[str],
    user_data_dir: Optional[str],
) -> None:
    manifest = load_manifest_or_fail(run_dir)
    image_expectations = validate_publish_inputs(manifest)
    blog = resolve_blog_urls(manifest, blog_host=blog_host, edit_url=edit_url)

    if user_data_dir:
        append_publish_log(run_dir, "--user-data-dir is deprecated and ignored in CDP attach mode.")
    if edit_url:
        append_publish_log(run_dir, "--edit-url is deprecated; derived --blog-host from the URL.")

    manifest["publish"]["expected_body_images"] = image_expectations["expected_body_images"]
    manifest["publish"]["markdown_local_images"] = image_expectations["local_markdown_images"]
    manifest["publish"]["markdown_unresolved_images"] = image_expectations["unresolved_markdown_images"]
    save_manifest(run_dir, manifest)

    session = PublishSession(
        run_dir=run_dir,
        manifest=manifest,
        blog=blog,
        cdp_url=cdp_url,
    )

    append_publish_log(run_dir, f"Starting publish flow for {blog.host}")
    append_publish_log(
        run_dir,
        "Pre-publish image expectation: "
        f"expected_body_images>={image_expectations['expected_body_images']} "
        f"(infographic=1, local_markdown={image_expectations['local_markdown_images']}, "
        f"unresolved_markdown={image_expectations['unresolved_markdown_images']})",
    )
    start_publish_attempt(session)
    try:
        run_publish_state_machine(session)
    except Exception as exc:
        finalize_publish_attempt(session, "failed", str(exc))
        raise
    else:
        finalize_publish_attempt(session, session.manifest["publish"]["status"])
    finally:
        close_attach_state(session.attach_state)


def cmd_verify_render(
    run_dir: Path,
    cdp_url: str,
    post_url: Optional[str],
    blog_host: Optional[str],
    user_data_dir: Optional[str],
    allow_headed_cdp: bool,
) -> None:
    manifest = load_manifest_or_fail(run_dir)
    blog = resolve_blog_urls(manifest, blog_host=blog_host, edit_url=None)
    resolved_post_url = post_url or manifest.get("publish", {}).get("post_url")
    if not resolved_post_url:
        raise PublishError("Render URL missing. Pass --post-url or publish first.")

    if user_data_dir:
        append_publish_log(run_dir, "--user-data-dir is deprecated and ignored in CDP attach mode.")

    append_publish_log(run_dir, f"Starting private render verification: {resolved_post_url}")
    render_state = manifest["verification"]["render"]
    render_state["status"] = "in_progress"
    render_state["url"] = resolved_post_url
    save_manifest(run_dir, manifest)

    attach_state = attach_cdp(
        cdp_url,
        blog,
        run_dir,
        allow_headed_cdp=allow_headed_cdp,
    )
    manifest["publish"]["cdp_url"] = attach_state.cdp_url
    save_manifest(run_dir, manifest)
    page: Optional[Page] = None
    try:
        page = first_page(attach_state.context)
        page.goto(blog.home_url, wait_until="domcontentloaded")
        page.wait_for_timeout(800)
        result = build_render_check(page, resolved_post_url, manifest.get("title") or "")
        expected_body_images = int(manifest.get("publish", {}).get("expected_body_images") or 1)
        verify_private_render_result(result, expected_body_images=expected_body_images)
        safe_screenshot(page, run_dir / "13_verify_render.png")
    except Exception as exc:
        render_state["status"] = "failed"
        render_state["checked_at"] = now_iso()
        render_state["url"] = resolved_post_url
        render_state["details"] = {"error": sanitize_error_message(exc)}
        save_manifest(run_dir, manifest)
        raise
    finally:
        close_attach_state(attach_state)

    render_state["status"] = "passed"
    render_state["checked_at"] = now_iso()
    render_state["url"] = resolved_post_url
    render_state["body_images_found"] = result["body_images_found"]
    render_state["raw_markdown_leak"] = result["raw_markdown_leak"]
    render_state["required_sections_ok"] = result["required_sections_ok"]
    render_state["title_match"] = result["title_match"]
    render_state["details"] = result
    manifest["publish"]["post_url"] = resolved_post_url
    manifest["publish"]["post_id"] = extract_post_id(resolved_post_url)
    save_manifest(run_dir, manifest)
    append_publish_log(run_dir, "Private render verification passed.")


def cmd_verify_public(run_dir: Path, public_url: Optional[str]) -> None:
    manifest = load_manifest_or_fail(run_dir)
    resolved_public_url = public_url or manifest.get("verification", {}).get("public", {}).get("url")
    if not resolved_public_url:
        raise PublishError("Public URL missing. Pass --public-url when verifying a public post.")

    append_publish_log(run_dir, f"Starting public verification: {resolved_public_url}")
    public_state = manifest["verification"]["public"]
    public_state["status"] = "in_progress"
    public_state["url"] = resolved_public_url
    save_manifest(run_dir, manifest)

    try:
        result = verify_public_page(resolved_public_url)
        ensure_non_placeholder_og(result["og_image"])
        expected_body_images = int(manifest.get("publish", {}).get("expected_body_images") or 1)

        if result["raw_markdown_leak"]:
            raise PublishError("Raw markdown leakage detected on public page.")
        if not result["required_sections_ok"]:
            raise PublishError("Required sections are missing on public page.")
        if result["body_images_found"] < expected_body_images:
            raise PublishError(
                f"Public page is missing body images. "
                f"expected_at_least={expected_body_images} found={result['body_images_found']}"
            )
    except Exception as exc:
        public_state["status"] = "failed"
        public_state["checked_at"] = now_iso()
        public_state["url"] = resolved_public_url
        public_state["details"] = {"error": sanitize_error_message(exc)}
        save_manifest(run_dir, manifest)
        raise

    public_state["status"] = "passed"
    public_state["checked_at"] = now_iso()
    public_state["url"] = resolved_public_url
    public_state["og_image"] = result["og_image"]
    public_state["body_images_found"] = result["body_images_found"]
    public_state["raw_markdown_leak"] = result["raw_markdown_leak"]
    public_state["required_sections_ok"] = result["required_sections_ok"]
    public_state["details"] = result
    save_manifest(run_dir, manifest)
    append_publish_log(run_dir, "Public verification passed.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Manifest-driven Tistory publisher")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_pub = sub.add_parser("publish")
    p_pub.add_argument("--run-dir", required=True)
    p_pub.add_argument("--blog-host", default=DEFAULT_BLOG_HOST)
    p_pub.add_argument("--cdp-url", default=DEFAULT_CDP_URL)
    p_pub.add_argument("--edit-url", default=None, help=argparse.SUPPRESS)
    p_pub.add_argument("--user-data-dir", default=None, help=argparse.SUPPRESS)

    args = parser.parse_args()

    try:
        if args.cmd == "publish":
            cmd_publish(
                run_dir=Path(args.run_dir),
                blog_host=args.blog_host,
                cdp_url=args.cdp_url,
                edit_url=args.edit_url,
                user_data_dir=args.user_data_dir,
            )
        else:
            raise PublishError(f"Unknown command: {args.cmd}")
    except PublishError as exc:
        print(f"ERROR: {sanitize_error_message(exc)}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
