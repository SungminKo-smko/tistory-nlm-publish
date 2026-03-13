#!/usr/bin/env python3
"""
Manifest-driven Tistory publisher.

Primary flow:
1. Attach to an existing Chromium session over CDP
2. Preflight the already-logged-in Tistory context for the target blog host
3. Publish with explicit private-only controls and checkpointed state updates
4. Verify the rendered private post with the same logged-in browser context
5. Optionally verify a public URL later with a separate command
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup
from playwright.sync_api import Browser, BrowserContext, Page, Playwright, sync_playwright


VIEWPORT_W = 1280
VIEWPORT_H = 650
DEFAULT_TIMEOUT_MS = 15000
DEFAULT_CDP_URL = os.environ.get("OPENCLAW_CDP_URL", "http://127.0.0.1:18800")
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36"
)

RAW_MD_LEAK_PATTERNS = [
    r"(?m)^##\s+",
    r"(?m)^-\s+",
    r"\*\*",
]

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
class BlogUrls:
    host: str
    home_url: str
    manage_url: str
    edit_url: str


@dataclass
class AttachState:
    playwright: Playwright
    browser: Browser
    context: BrowserContext
    context_index: int


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
            "last_checkpoint": None,
            "checkpoints": {},
            "edit_url": None,
            "post_url": None,
            "post_id": None,
            "published_at": None,
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
    manifest_host = manifest.get("blog", {}).get("host") or manifest.get("publish", {}).get("blog_host")
    if manifest_host:
        return build_blog_urls(manifest_host)
    manifest_post_url = manifest.get("publish", {}).get("post_url")
    if manifest_post_url:
        return build_blog_urls(derive_blog_host_from_url(manifest_post_url))
    raise PublishError("blog host is missing. Pass --blog-host.")


def normalize_tag_key(tag: str) -> str:
    return tag.strip().casefold()


def validate_publish_inputs(manifest: Dict[str, Any]) -> None:
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


def read_markdown(manifest: Dict[str, Any]) -> str:
    return Path(manifest["markdown_path"]).read_text(encoding="utf-8")


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


def attach_cdp(cdp_url: str, blog: BlogUrls) -> AttachState:
    playwright = sync_playwright().start()
    try:
        browser = playwright.chromium.connect_over_cdp(cdp_url)
    except Exception as exc:
        playwright.stop()
        raise PublishError(f"Failed to attach CDP browser at {cdp_url}: {exc}") from exc

    if not browser.contexts:
        playwright.stop()
        raise PublishError("No browser context found on attached CDP browser.")

    context, context_index = select_context_for_blog(browser, blog)
    context.set_default_timeout(DEFAULT_TIMEOUT_MS)
    return AttachState(
        playwright=playwright,
        browser=browser,
        context=context,
        context_index=context_index,
    )


def close_attach_state(attach_state: Optional[AttachState]) -> None:
    if attach_state is None:
        return
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
    session.manifest["publish"]["cdp_url"] = session.cdp_url
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


def mark_publish_failure(session: PublishSession, step: str, exc: Exception) -> None:
    session.manifest["publish"]["status"] = "failed"
    session.manifest["publish"]["last_error"] = str(exc)
    mark_checkpoint(session, step, "failed", {"error": str(exc)})
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
        raise PublishError("Failed to confirm markdown-capable editor.")

    safe_screenshot(page, run_dir / "03_markdown_mode_ready.png")
    return editor_variant


def fill_title_and_body(
    page: Page,
    title: str,
    markdown_text: str,
    run_dir: Path,
    editor_variant: Optional[str],
) -> None:
    title_selectors = [
        "input[name='title']",
        "textarea[name='title']",
        "input[placeholder*='제목']",
        "textarea[placeholder*='제목']",
    ]
    if not fill_visible(page, title_selectors, title):
        safe_screenshot(page, run_dir / "04_title_fill_failed.png")
        raise PublishError("Failed to fill title field.")

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
                candidate.fill(markdown_text)
            except Exception:
                try:
                    candidate.click()
                    page.keyboard.press("Meta+A")
                    page.keyboard.press("Control+A")
                    page.keyboard.press("Backspace")
                    page.keyboard.insert_text(markdown_text)
                except Exception:
                    continue
            page.wait_for_timeout(900)
            safe_screenshot(page, run_dir / "05_body_filled.png")
            return

    safe_screenshot(page, run_dir / "05_body_fill_failed.png")
    raise PublishError("Failed to fill markdown body field.")


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


def upload_representative_image(page: Page, thumbnail_path: str, run_dir: Path) -> None:
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
        locator.set_input_files(thumbnail_path)
    except Exception as exc:
        safe_screenshot(page, run_dir / "09_thumbnail_upload_failed.png")
        raise PublishError(f"Failed to upload representative image: {exc}") from exc

    page.wait_for_timeout(1800)
    body_text = page.locator("body").inner_text(timeout=5000)
    if "대표 이미지" not in body_text and "대표이미지" not in body_text:
        safe_screenshot(page, run_dir / "10_thumbnail_upload_weak_signal.png")
    safe_screenshot(page, run_dir / "10_thumbnail_uploaded.png")


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
    result = page.evaluate(
        """() => {
          const visible = (el) => !!el && !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
          const norm = (s) => (s || '').replace(/\\s+/g, '').trim();
          const buttons = [...document.querySelectorAll('button,[role="button"]')].filter(visible);
          const texts = buttons.map((btn) => (btn.textContent || '').trim()).filter(Boolean);

          const exactPrivate = buttons.find((btn) => {
            const text = norm(btn.textContent);
            return text === '비공개저장' || text === '비공개발행';
          });
          const genericSave = buttons.find((btn) => norm(btn.textContent) === '저장');
          const publicSubmit = buttons.find((btn) => norm(btn.textContent) === '공개발행');

          if (exactPrivate) {
            exactPrivate.click();
            return { clicked: true, text: (exactPrivate.textContent || '').trim(), buttons: texts };
          }

          if (genericSave && !publicSubmit) {
            genericSave.click();
            return { clicked: true, text: (genericSave.textContent || '').trim(), buttons: texts };
          }

          return {
            clicked: false,
            reason: publicSubmit ? 'unsafe_public_submit_visible' : 'safe_private_submit_not_found',
            buttons: texts,
          };
        }"""
    )
    if result.get("clicked"):
        page.wait_for_timeout(2500)
        safe_screenshot(page, run_dir / "12_after_publish.png")
        return

    safe_screenshot(page, run_dir / "12_publish_submit_failed.png")
    reason = result.get("reason") or "unknown"
    buttons = ", ".join(result.get("buttons") or [])
    raise PublishError(f"Failed to find a safe private publish button ({reason}). Visible buttons: {buttons}")


def try_extract_post_url(page: Page, blog_host: str) -> Optional[str]:
    current = page.url
    pattern = re.compile(rf"https?://{re.escape(blog_host)}/\d+(?:[/?#].*)?$")
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

    match = re.search(rf"https?://{re.escape(blog_host)}/\d+(?:[/?#][^\"'\\s<]*)?", html)
    if match:
        return match.group(0)
    return None


def has_publish_success_signal(page: Page) -> bool:
    try:
        body_text = page.locator("body").inner_text(timeout=3000)
    except Exception:
        return False

    success_markers = [
        "발행이 완료",
        "글이 발행",
        "비공개 저장",
        "저장되었습니다",
        "등록되었습니다",
    ]
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


def verify_private_render_result(result: Dict[str, Any]) -> None:
    status_code = result.get("status_code")
    if status_code and int(status_code) >= 400:
        raise PublishError(f"Rendered page returned HTTP {status_code}")
    if result["raw_markdown_leak"]:
        raise PublishError("Raw markdown leakage detected on rendered page.")
    if not result["required_sections_ok"]:
        raise PublishError("Required sections are missing on rendered page.")
    if result["body_images_found"] < 1:
        raise PublishError("No body images found on rendered page.")
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
    session.attach_state = attach_cdp(session.cdp_url, session.blog)
    update_publish_metadata(session)


def step_preflight_context(session: PublishSession) -> None:
    preflight_context(session)


def step_prepare_editor(session: PublishSession) -> None:
    if session.page is None:
        raise PublishError("Editor page is not ready.")
    editor_variant = ensure_markdown_mode(session.page, session.run_dir)
    session.manifest["publish"]["editor_variant"] = editor_variant
    save_manifest(session.run_dir, session.manifest)


def step_fill_content(session: PublishSession) -> None:
    if session.page is None:
        raise PublishError("Editor page is not ready.")
    fill_title_and_body(
        session.page,
        title=session.manifest.get("title") or "제목 없음",
        markdown_text=read_markdown(session.manifest),
        run_dir=session.run_dir,
        editor_variant=session.manifest["publish"].get("editor_variant"),
    )
    fill_tags(session.page, session.manifest["tags"], session.run_dir)


def step_publish_dialog(session: PublishSession) -> None:
    if session.page is None:
        raise PublishError("Editor page is not ready.")
    open_publish_dialog(session.page, session.run_dir)
    ensure_tags_in_publish_dialog(session.page, session.manifest["tags"])
    upload_representative_image(session.page, session.manifest["thumbnail_path"], session.run_dir)
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
        attempt["error"] = error
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
    validate_publish_inputs(manifest)
    blog = resolve_blog_urls(manifest, blog_host=blog_host, edit_url=edit_url)

    if user_data_dir:
        append_publish_log(run_dir, "--user-data-dir is deprecated and ignored in CDP attach mode.")
    if edit_url:
        append_publish_log(run_dir, "--edit-url is deprecated; derived --blog-host from the URL.")

    manifest["verification"]["render"] = manifest_defaults()["verification"]["render"]
    manifest["verification"]["public"] = manifest_defaults()["verification"]["public"]
    save_manifest(run_dir, manifest)

    session = PublishSession(
        run_dir=run_dir,
        manifest=manifest,
        blog=blog,
        cdp_url=cdp_url,
    )

    append_publish_log(run_dir, f"Starting publish flow for {blog.host}")
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

    attach_state = attach_cdp(cdp_url)
    page: Optional[Page] = None
    try:
        page = first_page(attach_state.context)
        page.goto(blog.home_url, wait_until="domcontentloaded")
        page.wait_for_timeout(800)
        result = build_render_check(page, resolved_post_url, manifest.get("title") or "")
        verify_private_render_result(result)
        safe_screenshot(page, run_dir / "13_verify_render.png")
    except Exception as exc:
        render_state["status"] = "failed"
        render_state["checked_at"] = now_iso()
        render_state["url"] = resolved_post_url
        render_state["details"] = {"error": str(exc)}
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

        if result["raw_markdown_leak"]:
            raise PublishError("Raw markdown leakage detected on public page.")
        if not result["required_sections_ok"]:
            raise PublishError("Required sections are missing on public page.")
        if result["body_images_found"] < 1:
            raise PublishError("No body images found on public page.")
    except Exception as exc:
        public_state["status"] = "failed"
        public_state["checked_at"] = now_iso()
        public_state["url"] = resolved_public_url
        public_state["details"] = {"error": str(exc)}
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
    p_pub.add_argument("--blog-host", default=None)
    p_pub.add_argument("--cdp-url", default=DEFAULT_CDP_URL)
    p_pub.add_argument("--edit-url", default=None, help=argparse.SUPPRESS)
    p_pub.add_argument("--user-data-dir", default=None, help=argparse.SUPPRESS)

    p_render = sub.add_parser("verify-render")
    p_render.add_argument("--run-dir", required=True)
    p_render.add_argument("--cdp-url", default=DEFAULT_CDP_URL)
    p_render.add_argument("--post-url", default=None)
    p_render.add_argument("--blog-host", default=None)
    p_render.add_argument("--user-data-dir", default=None, help=argparse.SUPPRESS)

    p_public = sub.add_parser("verify-public")
    p_public.add_argument("--run-dir", required=True)
    p_public.add_argument("--public-url", default=None)

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
        elif args.cmd == "verify-render":
            cmd_verify_render(
                run_dir=Path(args.run_dir),
                cdp_url=args.cdp_url,
                post_url=args.post_url,
                blog_host=args.blog_host,
                user_data_dir=args.user_data_dir,
            )
        elif args.cmd == "verify-public":
            cmd_verify_public(
                run_dir=Path(args.run_dir),
                public_url=args.public_url,
            )
        else:
            raise PublishError(f"Unknown command: {args.cmd}")
    except PublishError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
