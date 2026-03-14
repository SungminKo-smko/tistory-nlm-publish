#!/usr/bin/env python3
"""
Agent-browser-based Tistory publisher using @ref selectors.
Session persistence via --session-name ensures login state is preserved.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests
from bs4 import BeautifulSoup


SESSION_NAME = "tistory-publisher"
DEFAULT_TIMEOUT_MS = 30000
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36"
)
TISTORY_EMAIL_ENV = "TISTORY_LOGIN_EMAIL"
TISTORY_PASSWORD_ENV = "TISTORY_LOGIN_PASSWORD"

REQUIRED_RENDER_SECTIONS = ["핵심요약", "핵심이슈"]
RAW_MD_LEAK_PATTERNS = [r"(?m)^##\s+", r"(?m)^-\s+", r"\*\*"]


class PublishError(RuntimeError):
    pass


def now_iso() -> str:
    return dt.datetime.now().isoformat(timespec="seconds")


def log(run_dir: Path, message: str) -> None:
    line = f"[{now_iso()}] {message}"
    print(line)
    with (run_dir / "publish.log").open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def json_load(path: Path) -> Dict[str, Any]:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def json_dump(path: Path, data: Dict[str, Any]) -> None:
    with path.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def load_manifest(run_dir: Path) -> Dict[str, Any]:
    manifest_path = run_dir / "manifest.json"
    if not manifest_path.exists():
        raise PublishError(f"manifest.json not found: {manifest_path}")
    return json_load(manifest_path)


def save_manifest(run_dir: Path, data: Dict[str, Any]) -> None:
    json_dump(run_dir / "manifest.json", data)


def run_browser(
    *args: str,
    check: bool = True,
    timeout: Optional[int] = None,
) -> subprocess.CompletedProcess[str]:
    cmd = [
        "agent-browser",
        "--session-name",
        SESSION_NAME,
        "--user-agent",
        USER_AGENT,
        *args,
    ]
    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    if check and result.returncode != 0:
        raise PublishError(f"Browser command failed: {' '.join(cmd)}\n{result.stderr}")
    return result


def browser_open(url: str, run_dir: Path) -> None:
    log(run_dir, f"Opening: {url}")
    run_browser("open", url)
    time.sleep(3)


def browser_wait_load(run_dir: Path) -> None:
    log(run_dir, "Waiting for network idle...")
    run_browser("wait", "--load", "networkidle")


def browser_snapshot_json(run_dir: Path) -> Dict[str, Any]:
    result = run_browser("snapshot", "-i", "--json")
    return json.loads(result.stdout)


def browser_screenshot(path: Path, run_dir: Path) -> None:
    log(run_dir, f"Screenshot: {path}")
    run_browser("screenshot", str(path))


def browser_click(ref: str, run_dir: Path) -> None:
    log(run_dir, f"Clicking: {ref}")
    run_browser("click", ref)
    time.sleep(1)


def browser_fill(ref: str, text: str, run_dir: Path) -> None:
    log(run_dir, f"Filling {ref}: {text[:100]}...")
    run_browser("fill", ref, text)
    time.sleep(0.5)


def browser_eval(script: str) -> str:
    result = run_browser("eval", script)
    return result.stdout.strip()


def browser_upload(ref: str, file_path: str, run_dir: Path) -> None:
    log(run_dir, f"Uploading {file_path} to {ref}")
    run_browser("upload", ref, file_path)
    time.sleep(2)


def browser_press(key: str) -> None:
    run_browser("press", key)


def remove_existing_tag_links(snapshot: Dict[str, Any], run_dir: Path) -> Dict[str, Any]:
    refs = snapshot.get("data", {}).get("refs", {})
    delete_refs = [f"@{ref_id}" for ref_id, node in refs.items() if (node.get("role") or "").lower() == "link" and "태그 삭제" in (node.get("name") or "")]
    for ref in delete_refs:
        browser_click(ref, run_dir)
    if delete_refs:
        time.sleep(1)
        return browser_snapshot_json(run_dir)
    return snapshot


def upload_infographic_and_get_url(run_dir: Path, thumbnail_path: str) -> Optional[str]:
    snapshot = browser_snapshot_json(run_dir)
    attach_ref = find_ref_by_label(snapshot, "첨부", exact=True, role="button")
    if not attach_ref:
        return None
    browser_click(attach_ref, run_dir)
    menu_snapshot = browser_snapshot_json(run_dir)
    photo_ref = find_ref_by_label(menu_snapshot, "사진", exact=True, role="menuitem")
    if not photo_ref:
        return None
    browser_click(photo_ref, run_dir)
    picker_snapshot = browser_snapshot_json(run_dir)
    choose_ref = find_ref_by_label(picker_snapshot, "Choose File", exact=True, role="button") or find_ref_by_label(picker_snapshot, "파일 선택", exact=True, role="button")
    if not choose_ref:
        return None
    browser_upload(choose_ref, thumbnail_path, run_dir)
    result = browser_eval(
        "(() => {"
        "const ifr=document.querySelector('iframe');"
        "if(!ifr || !ifr.contentDocument) return '';"
        "const imgs=[...ifr.contentDocument.querySelectorAll('img')].map(i=>i.getAttribute('src')).filter(Boolean);"
        "return imgs.length ? imgs[imgs.length-1] : '';"
        "})()"
    )
    browser_press("Escape")
    time.sleep(0.5)
    browser_press("Escape")
    time.sleep(0.5)
    value = result.strip().strip('"')
    return value or None


def prepend_infographic(markdown_text: str, infographic_url: Optional[str]) -> str:
    if not infographic_url:
        return markdown_text
    return f"![인포그래픽]({infographic_url})\n\n{markdown_text}"


def ensure_representative_image(
    run_dir: Path,
    publish_snapshot: Dict[str, Any],
    thumbnail_path: str,
) -> Dict[str, Any]:
    delete_ref = find_ref_by_label(publish_snapshot, "삭제", exact=True, role="button")
    if delete_ref:
        log(run_dir, "Representative image already set in publish dialog")
        return publish_snapshot

    upload_ref = (
        find_ref_by_label(publish_snapshot, "Choose File", exact=True, role="button")
        or find_ref_by_label(publish_snapshot, "파일 선택", exact=True, role="button")
        or find_ref_by_label(publish_snapshot, "대표 이미지", role="button")
    )
    if not upload_ref:
        raise PublishError("Representative image upload control not found in publish dialog")

    log(run_dir, "Uploading representative thumbnail image...")
    browser_upload(upload_ref, thumbnail_path, run_dir)
    refreshed = browser_snapshot_json(run_dir)
    if not find_ref_by_label(refreshed, "삭제", exact=True, role="button"):
        raise PublishError("Representative image upload was not confirmed in publish dialog")
    return refreshed


def browser_get_url() -> str:
    result = run_browser("get", "url")
    return result.stdout.strip()


def get_tistory_credentials() -> tuple[str, str]:
    email = (os.getenv(TISTORY_EMAIL_ENV) or "").strip()
    password = (os.getenv(TISTORY_PASSWORD_ENV) or "").strip()
    if not email or not password:
        raise PublishError(
            f"Missing login env vars: {TISTORY_EMAIL_ENV}, {TISTORY_PASSWORD_ENV}"
        )
    return email, password


def find_ref_by_label(
    snapshot: Dict[str, Any],
    *labels: str,
    exact: bool = False,
    role: Optional[str] = None,
    exclude_labels: Optional[List[str]] = None,
) -> Optional[str]:
    refs = snapshot.get("data", {}).get("refs", {})
    excluded = [label.lower() for label in (exclude_labels or [])]
    for ref_id, node in refs.items():
        node_role = (node.get("role") or "").lower()
        if role and node_role != role.lower():
            continue
        node_name = node.get("name", "").lower()
        if any(ex in node_name for ex in excluded):
            continue
        for label in labels:
            if exact:
                if label.lower() == node_name:
                    return f"@{ref_id}"
            else:
                if label.lower() in node_name:
                    return f"@{ref_id}"
    return None


def find_textbox_ref(snapshot: Dict[str, Any], exclude_labels: List[str]) -> Optional[str]:
    refs = snapshot.get("data", {}).get("refs", {})
    lowered_excludes = [label.lower() for label in exclude_labels]
    for ref_id, node in refs.items():
        if (node.get("role") or "").lower() != "textbox":
            continue
        node_name = (node.get("name") or "").lower()
        if any(label in node_name for label in lowered_excludes):
            continue
        return f"@{ref_id}"
    return None


def browser_dialog_accept(run_dir: Path) -> None:
    log(run_dir, "Pre-registering dialog accept handler...")
    run_browser("dialog", "accept")


def is_login_page(snapshot: Dict[str, Any]) -> bool:
    markers = [
        ("카카오계정으로 로그인", "link"),
        ("카카오계정으로 로그인", "button"),
        ("카카오계정", "textbox"),
        ("계정정보 입력", "textbox"),
        ("비밀번호", "textbox"),
        ("로그인", "button"),
    ]
    return any(find_ref_by_label(snapshot, label, role=role) for label, role in markers)


def auto_login_if_needed(run_dir: Path, edit_url: str, initial_snapshot: Dict[str, Any]) -> Dict[str, Any]:
    if find_ref_by_label(initial_snapshot, "제목", role="textbox"):
        return initial_snapshot
    if not is_login_page(initial_snapshot):
        return initial_snapshot

    email, password = get_tistory_credentials()
    log(run_dir, "Login page detected. Attempting automatic login with env credentials...")

    login_home_btn = find_ref_by_label(initial_snapshot, "카카오계정으로 로그인", role="link") or find_ref_by_label(initial_snapshot, "카카오계정으로 로그인", role="button")
    if login_home_btn:
        browser_click(login_home_btn, run_dir)
        browser_wait_load(run_dir)

    for _ in range(3):
        snapshot = browser_snapshot_json(run_dir)
        if find_ref_by_label(snapshot, "제목", role="textbox"):
            return snapshot

        email_ref = (
            find_ref_by_label(snapshot, "이메일 또는 전화번호", role="textbox")
            or find_ref_by_label(snapshot, "카카오계정", role="textbox")
            or find_ref_by_label(snapshot, "계정정보 입력", role="textbox")
        )
        password_ref = find_ref_by_label(snapshot, "비밀번호", role="textbox")
        submit_ref = find_ref_by_label(snapshot, "로그인", role="button")

        if email_ref and password_ref:
            browser_fill(email_ref, email, run_dir)
            browser_fill(password_ref, password, run_dir)
            if submit_ref:
                browser_click(submit_ref, run_dir)
            else:
                run_browser("press", "Enter")
            time.sleep(3)
            browser_open(edit_url, run_dir)
            browser_wait_load(run_dir)
            return browser_snapshot_json(run_dir)

        time.sleep(1)

    raise PublishError("Automatic login failed. Check Tistory credentials or login flow changes.")


def try_extract_post_url_from_manage_list(title: str, blog_host: str) -> Optional[str]:
    script = (
        "(() => {"
        f"const title = {json.dumps(title)};"
        f"const host = {json.dumps(blog_host)};"
        "const anchors = [...document.querySelectorAll('a[href]')];"
        "const hit = anchors.find((a) => (a.textContent || '').trim() === title || (a.getAttribute('title') || '').trim() === title);"
        "if (!hit) return '';"
        "const href = hit.href || '';"
        "try {"
        "  const u = new URL(href);"
        "  if (u.host !== host) return '';"
        "  return /^\\/\\d+(?:\\/)?$/.test(u.pathname) ? href : '';"
        "} catch (_) {"
        "  return '';"
        "}"
        "})()"
    )
    value = browser_eval(script)
    result = value.strip().strip('"')
    return result or None


def normalize_blog_host(value: str) -> str:
    raw = (value or "").strip()
    if not raw:
        raise PublishError("blog host is required")
    if "://" not in raw:
        raw = f"https://{raw}"
    from urllib.parse import urlparse
    parsed = urlparse(raw)
    host = (parsed.netloc or parsed.path).strip().lower().rstrip("/")
    if not host:
        raise PublishError(f"invalid blog host: {value}")
    return host


def build_edit_url(blog_host: str) -> str:
    return f"https://{blog_host}/manage/newpost/"


def publish_to_tistory(
    run_dir: Path,
    blog_host: str,
    title: str,
    body_markdown: str,
    tags: List[str],
    thumbnail_path: str,
) -> str:
    edit_url = build_edit_url(blog_host)
    
    log(run_dir, "Opening Tistory editor...")
    browser_open(edit_url, run_dir)
    browser_wait_load(run_dir)
    browser_screenshot(run_dir / "01_editor_loaded.png", run_dir)
    
    log(run_dir, "Getting page structure...")
    snapshot = browser_snapshot_json(run_dir)
    snapshot = auto_login_if_needed(run_dir, edit_url, snapshot)

    infographic_url = upload_infographic_and_get_url(run_dir, thumbnail_path)
    body_markdown = prepend_infographic(body_markdown, infographic_url)
    
    title_ref = find_ref_by_label(snapshot, "제목", role="textbox")
    
    if not title_ref:
        raise PublishError("Title input not found in page")
    log(run_dir, f"Found UI elements - title:{title_ref}")
    
    log(run_dir, "Switching editor to markdown mode...")
    mode_menu_ref = find_ref_by_label(snapshot, "기본모드", exact=True, role="button")
    if not mode_menu_ref:
        raise PublishError("Mode menu button not found")
    browser_click(mode_menu_ref, run_dir)
    mode_snapshot = browser_snapshot_json(run_dir)
    markdown_mode_ref = find_ref_by_label(mode_snapshot, "마크다운", exact=True, role="menuitem")
    if not markdown_mode_ref:
        raise PublishError("Markdown mode menu item not found")
    browser_dialog_accept(run_dir)
    browser_click(markdown_mode_ref, run_dir)
    time.sleep(1)

    markdown_snapshot = browser_snapshot_json(run_dir)
    body_ref = find_textbox_ref(markdown_snapshot, ["제목", "태그"])
    if not body_ref:
        raise PublishError("Markdown body textbox not found after mode switch")

    log(run_dir, "Filling title...")
    browser_fill(title_ref, title, run_dir)

    log(run_dir, "Filling markdown body...")
    browser_fill(body_ref, body_markdown, run_dir)

    post_switch_snapshot = browser_snapshot_json(run_dir)
    post_switch_snapshot = remove_existing_tag_links(post_switch_snapshot, run_dir)
    tag_ref = find_ref_by_label(post_switch_snapshot, "태그", role="textbox")
    complete_ref = find_ref_by_label(post_switch_snapshot, "완료", exact=True, role="button") or find_ref_by_label(post_switch_snapshot, "발행", exact=True, role="button")
    if not tag_ref:
        raise PublishError("Tag input not found after markdown mode switch")
    if not complete_ref:
        raise PublishError("Complete button not found after markdown mode switch")
    
    log(run_dir, "Filling tags...")
    tag_text = ",".join(tags)
    browser_fill(tag_ref, tag_text, run_dir)
    
    browser_screenshot(run_dir / "02_content_filled.png", run_dir)
    
    log(run_dir, "Clicking complete button...")
    browser_click(complete_ref, run_dir)
    time.sleep(3)
    
    log(run_dir, "Checking for publish dialog...")
    snapshot2 = browser_snapshot_json(run_dir)
    snapshot2 = ensure_representative_image(run_dir, snapshot2, thumbnail_path)
    
    private_ref = find_ref_by_label(snapshot2, "비공개", exact=True, role="radio")
    publish_btn_ref = (
        find_ref_by_label(snapshot2, "비공개 저장", exact=True, role="button")
        or find_ref_by_label(snapshot2, "비공개발행", exact=True, role="button")
        or find_ref_by_label(snapshot2, "저장", exact=True, role="button", exclude_labels=["임시저장"])
        or find_ref_by_label(snapshot2, "발행", exact=True, role="button")
    )
    
    if private_ref:
        log(run_dir, f"Selecting private publish: {private_ref}")
        browser_click(private_ref, run_dir)
    
    if publish_btn_ref:
        browser_screenshot(run_dir / "03_before_publish.png", run_dir)
        log(run_dir, f"Clicking final publish button: {publish_btn_ref}")
        browser_click(publish_btn_ref, run_dir)
        time.sleep(5)
    
    browser_wait_load(run_dir)
    browser_screenshot(run_dir / "04_published.png", run_dir)
    
    log(run_dir, "Extracting post URL...")
    current_url = browser_get_url()
    
    post_url = None
    if "/manage/" in current_url and ("/edit/" in current_url or "/modify/" in current_url):
        match = re.search(r"/(\d+)", current_url)
        if match:
            post_id = match.group(1)
            post_url = f"https://{blog_host}/{post_id}"
    
    if not post_url:
        match = re.search(r"https?://[^/]+/(\d+)", current_url)
        if match:
            post_url = current_url

    if not post_url and "/manage/posts" in current_url:
        post_url = try_extract_post_url_from_manage_list(title, blog_host)
    
    if not post_url:
        raise PublishError(f"Failed to extract post URL. Current URL: {current_url}")
    
    log(run_dir, f"Published successfully: {post_url}")
    return post_url


def verify_render(run_dir: Path, post_url: str, expected_title: str) -> Dict[str, Any]:
    log(run_dir, f"Verifying render: {post_url}")
    
    browser_open(post_url, run_dir)
    browser_wait_load(run_dir)
    browser_screenshot(run_dir / "verify_render.png", run_dir)
    
    html = browser_eval("(() => document.documentElement.outerHTML)()")
    
    soup = BeautifulSoup(html, "html.parser")
    
    title_elem = soup.find("title")
    title_text = title_elem.get_text() if title_elem else ""
    title_match = expected_title.lower() in title_text.lower()
    
    body_selectors = ["article", ".post-content", ".entry-content", "#content"]
    body_text = ""
    for sel in body_selectors:
        elem = soup.select_one(sel)
        if elem:
            body_text = elem.get_text()
            break
    
    sections_ok = all(section in body_text for section in REQUIRED_RENDER_SECTIONS)
    markdown_leak = any(re.search(pattern, body_text) for pattern in RAW_MD_LEAK_PATTERNS)
    body_images = len(soup.select("article img, .post-content img, .entry-content img"))
    
    result = {
        "status": "success" if (title_match and sections_ok and not markdown_leak) else "failed",
        "checked_at": now_iso(),
        "url": post_url,
        "title_match": title_match,
        "required_sections_ok": sections_ok,
        "raw_markdown_leak": markdown_leak,
        "body_images_found": body_images,
        "details": {
            "title": title_text,
            "body_length": len(body_text),
        },
    }
    
    log(run_dir, f"Verification result: {result['status']}")
    return result


def cmd_publish(run_dir: Path, blog_host: str) -> None:
    run_dir = Path(run_dir)
    manifest = load_manifest(run_dir)
    
    log(run_dir, f"Starting publish for {blog_host}")
    
    title = manifest["title"]
    markdown_path = Path(manifest["markdown_path"])
    with markdown_path.open("r", encoding="utf-8") as f:
        body_markdown = f.read()
    
    tags = manifest.get("tags", [])
    if not tags:
        raise PublishError("Tags are required (run validate-tags first)")
    
    thumbnail_path = str(run_dir / "thumbnail.png")
    if not Path(thumbnail_path).exists():
        raise PublishError(f"Thumbnail not found: {thumbnail_path}")
    
    manifest.setdefault("publish", {})
    manifest["publish"]["status"] = "in_progress"
    manifest["publish"]["blog_host"] = blog_host
    manifest["publish"]["started_at"] = now_iso()
    save_manifest(run_dir, manifest)
    
    try:
        post_url = publish_to_tistory(
            run_dir=run_dir,
            blog_host=blog_host,
            title=title,
            body_markdown=body_markdown,
            tags=tags,
            thumbnail_path=thumbnail_path,
        )
        
        manifest["publish"]["status"] = "published_private"
        manifest["publish"]["post_url"] = post_url
        manifest["publish"]["published_at"] = now_iso()
        
        match = re.search(r"/(\d+)", post_url)
        if match:
            manifest["publish"]["post_id"] = match.group(1)
        
        save_manifest(run_dir, manifest)
        
        log(run_dir, f"✓ Publish complete: {post_url}")
        print(f"\n✓ Published: {post_url}\n")
        
    except Exception as e:
        manifest["publish"]["status"] = "failed"
        manifest["publish"]["last_error"] = str(e)
        save_manifest(run_dir, manifest)
        raise


def cmd_verify_render(run_dir: Path, post_url: Optional[str] = None) -> None:
    run_dir = Path(run_dir)
    manifest = load_manifest(run_dir)
    
    if not post_url:
        post_url = manifest.get("publish", {}).get("post_url")
        if not post_url:
            raise PublishError("post_url not found in manifest and not provided")
    
    title = manifest["title"]
    
    result = verify_render(run_dir, post_url, title)
    
    manifest.setdefault("verification", {})
    manifest["verification"]["render"] = result
    save_manifest(run_dir, manifest)
    
    if result["status"] == "success":
        log(run_dir, "✓ Render verification passed")
        print("\n✓ Verification passed\n")
    else:
        log(run_dir, f"✗ Render verification failed: {result}")
        raise PublishError("Render verification failed")


def cmd_verify_public(run_dir: Path, public_url: str) -> None:
    run_dir = Path(run_dir)
    manifest = load_manifest(run_dir)
    
    log(run_dir, f"Verifying public URL: {public_url}")
    
    response = requests.get(public_url, timeout=30)
    response.raise_for_status()
    
    soup = BeautifulSoup(response.text, "html.parser")
    
    og_image_elem = soup.find("meta", property="og:image")
    og_image = og_image_elem.get("content") if og_image_elem else None
    
    body_selectors = ["article", ".post-content", ".entry-content"]
    body_text = ""
    for sel in body_selectors:
        elem = soup.select_one(sel)
        if elem:
            body_text = elem.get_text()
            break
    
    sections_ok = all(section in body_text for section in REQUIRED_RENDER_SECTIONS)
    markdown_leak = any(re.search(pattern, body_text) for pattern in RAW_MD_LEAK_PATTERNS)
    body_images = len(soup.select("article img, .post-content img"))
    
    result = {
        "status": "success" if (og_image and sections_ok and not markdown_leak) else "failed",
        "checked_at": now_iso(),
        "url": public_url,
        "og_image": og_image,
        "required_sections_ok": sections_ok,
        "raw_markdown_leak": markdown_leak,
        "body_images_found": body_images,
    }
    
    manifest.setdefault("verification", {})
    manifest["verification"]["public"] = result
    save_manifest(run_dir, manifest)
    
    log(run_dir, f"Public verification: {result['status']}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Tistory publisher using agent-browser")
    subparsers = parser.add_subparsers(dest="command", required=True)
    
    pub = subparsers.add_parser("publish", help="Publish to Tistory")
    pub.add_argument("--run-dir", required=True, help="Run directory")
    pub.add_argument("--blog-host", required=True, help="Blog host")
    
    vr = subparsers.add_parser("verify-render", help="Verify rendered post")
    vr.add_argument("--run-dir", required=True, help="Run directory")
    vr.add_argument("--post-url", help="Post URL")
    
    vp = subparsers.add_parser("verify-public", help="Verify public post")
    vp.add_argument("--run-dir", required=True, help="Run directory")
    vp.add_argument("--public-url", required=True, help="Public post URL")
    
    args = parser.parse_args()
    
    try:
        if args.command == "publish":
            cmd_publish(
                run_dir=Path(args.run_dir),
                blog_host=normalize_blog_host(args.blog_host),
            )
        elif args.command == "verify-render":
            cmd_verify_render(
                run_dir=Path(args.run_dir),
                post_url=args.post_url,
            )
        elif args.command == "verify-public":
            cmd_verify_public(
                run_dir=Path(args.run_dir),
                public_url=args.public_url,
            )
    except PublishError as e:
        print(f"\nERROR: {e}\n", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"\nUNEXPECTED ERROR: {e}\n", file=sys.stderr)
        raise


if __name__ == "__main__":
    main()
