from __future__ import annotations

import argparse
import asyncio
import hashlib
import mimetypes
import re
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup, NavigableString, Tag
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import TimeoutError as PlaywrightTimeoutError
from playwright.async_api import async_playwright


LOGIN_PATTERNS = (
    r"\blogin\b",
    r"\blog in\b",
    r"\bsign in\b",
    r"\bsignin\b",
    r"\bauth\b",
    r"\bsso\b",
    r"\bpassword\b",
    r"\baws builder id\b",
)


class LoginRequired(RuntimeError):
    """Raised when capture cannot continue because login is still required."""


@dataclass(frozen=True)
class CapturePaths:
    out_dir: Path
    markdown: Path
    pdf: Path
    screenshot: Path
    html: Path
    assets_dir: Path


@dataclass(frozen=True)
class CourseNavItem:
    index: int
    title: str


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Capture a Chrome-rendered URL as PDF, screenshot, and Markdown."
    )
    parser.add_argument("url", help="URL to capture")
    parser.add_argument("--out", default="output/capture", help="Output directory.")
    parser.add_argument(
        "--profile-dir",
        default=".chrome-profile",
        help="Dedicated Chrome profile directory.",
    )
    parser.add_argument(
        "--headed",
        action="store_true",
        help="Show the Chrome window. Use this for manual login.",
    )
    parser.add_argument(
        "--prompt-login",
        action="store_true",
        help="Pause for manual login if a login page is detected.",
    )
    parser.add_argument(
        "--wait-login-seconds",
        type=int,
        default=0,
        help="Poll for this many seconds until the page no longer looks like login.",
    )
    parser.add_argument(
        "--allow-login-capture",
        action="store_true",
        help="Export the current page even if it still looks like a login screen.",
    )
    parser.add_argument(
        "--timeout-ms",
        type=int,
        default=45000,
        help="Navigation and wait timeout in milliseconds.",
    )
    parser.add_argument(
        "--settle-seconds",
        type=int,
        default=10,
        help="Wait this many seconds for rendered text to stabilize before export.",
    )
    parser.add_argument(
        "--min-text-chars",
        type=int,
        default=800,
        help="Minimum rendered text length to prefer before export.",
    )
    parser.add_argument(
        "--no-expand-interactive",
        action="store_true",
        help="Skip best-effort expansion of details/buttons/flashcards before export.",
    )
    parser.add_argument("--title", default=None, help="Optional Markdown title.")
    parser.add_argument(
        "--split-course-navigation",
        action="store_true",
        help="Detect Course Navigation and export each item to its own Markdown/assets directory.",
    )
    parser.add_argument(
        "--delete-orphans",
        action="store_true",
        help="Delete the output directory before capture so old lesson folders/assets do not linger.",
    )
    parser.add_argument(
        "--markdown-only",
        action="store_true",
        help="Only write Markdown and assets; skip PDF, screenshot, and HTML.",
    )
    return parser.parse_args(argv)


def build_paths(out_dir: Path) -> CapturePaths:
    out_dir.mkdir(parents=True, exist_ok=True)
    return CapturePaths(
        out_dir=out_dir,
        markdown=out_dir / "page.md",
        pdf=out_dir / "page.pdf",
        screenshot=out_dir / "page.png",
        html=out_dir / "page.html",
        assets_dir=out_dir / "assets",
    )


def build_named_paths(parent_dir: Path, title: str) -> CapturePaths:
    safe = safe_filename(title)
    out_dir = parent_dir / safe
    out_dir.mkdir(parents=True, exist_ok=True)
    return CapturePaths(
        out_dir=out_dir,
        markdown=out_dir / f"{safe}.md",
        pdf=out_dir / f"{safe}.pdf",
        screenshot=out_dir / f"{safe}.png",
        html=out_dir / f"{safe}.html",
        assets_dir=out_dir / "assets",
    )


def safe_filename(value: str) -> str:
    cleaned = clean_text(value)
    cleaned = re.sub(r'[\\/:*?"<>|]+', " ", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" .")
    return cleaned[:120] or "Untitled"


async def wait_for_page(page, timeout_ms: int) -> None:
    try:
        await page.wait_for_load_state("domcontentloaded", timeout=timeout_ms)
    except PlaywrightTimeoutError:
        pass
    try:
        await page.wait_for_load_state("networkidle", timeout=min(timeout_ms, 15000))
    except PlaywrightTimeoutError:
        pass


async def scroll_full_page(page) -> None:
    for _ in range(3):
        try:
            await page.evaluate(
                """
                async () => {
                  await new Promise((resolve) => {
                    let total = 0;
                    const step = Math.max(400, Math.floor(window.innerHeight * 0.8));
                    const timer = setInterval(() => {
                      const maxScroll = document.documentElement.scrollHeight - window.innerHeight;
                      window.scrollBy(0, step);
                      total += step;
                      if (total >= maxScroll) {
                        clearInterval(timer);
                        window.scrollTo(0, 0);
                        resolve();
                      }
                    }, 120);
                  });
                }
                """
            )
            return
        except PlaywrightError as exc:
            if not is_navigation_race(exc):
                raise
            await wait_for_page(page, 10000)


async def scroll_all_containers(page) -> None:
    for _ in range(3):
        try:
            await page.evaluate(
                """
                async () => {
                  const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
                  const nodes = [document.scrollingElement, ...document.querySelectorAll('*')];
                  const scrollables = nodes
                    .filter(Boolean)
                    .filter((el) => el.scrollHeight > el.clientHeight + 40)
                    .sort((a, b) => b.scrollHeight - a.scrollHeight)
                    .slice(0, 12);

                  for (const el of scrollables) {
                    const step = Math.max(350, Math.floor((el.clientHeight || window.innerHeight) * 0.8));
                    for (let top = 0; top <= el.scrollHeight; top += step) {
                      el.scrollTop = top;
                      await sleep(80);
                    }
                    el.scrollTop = 0;
                    await sleep(80);
                  }
                  window.scrollTo(0, 0);
                }
                """
            )
            return
        except PlaywrightError as exc:
            if not is_navigation_race(exc):
                raise
            await wait_for_page(page, 10000)


async def expand_interactive_content(page) -> None:
    for _ in range(3):
        try:
            await page.evaluate(
                """
                async () => {
                  const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

                  document.querySelectorAll('details:not([open])').forEach((node) => {
                    node.setAttribute('open', '');
                  });

                  const candidates = Array.from(document.querySelectorAll(
                    'button, [role="button"], [aria-expanded="false"], [aria-label], [data-testid], [class*="flash" i], [class*="card" i], [class*="marker" i]'
                  ));
                  const seen = new Set();
                  let clicked = 0;

                  for (const el of candidates) {
                    if (clicked >= 80 || seen.has(el)) continue;
                    seen.add(el);

                    const rect = el.getBoundingClientRect();
                    const label = [
                      el.innerText,
                      el.getAttribute('aria-label'),
                      el.getAttribute('title'),
                      el.getAttribute('data-testid'),
                      el.className && String(el.className),
                    ].filter(Boolean).join(' ').toLowerCase();
                    const interesting = /flash|card|flip|reveal|show|answer|marker|hotspot|knowledge|check|expand|more|next|continue/.test(label);
                    const expandable = el.getAttribute('aria-expanded') === 'false';
                    const visible = rect.width > 0 && rect.height > 0;
                    if (!visible || (!interesting && !expandable)) continue;

                    try {
                      el.scrollIntoView({ block: 'center', inline: 'center' });
                      await sleep(80);
                      el.click();
                      clicked += 1;
                      await sleep(180);
                    } catch (_) {}
                  }
                }
                """
            )
            await wait_for_page(page, 10000)
            return
        except PlaywrightError as exc:
            if not is_navigation_race(exc):
                raise
            await wait_for_page(page, 10000)


async def annotate_markers_for_capture(page) -> int:
    total = 0
    for frame in page.frames:
        for _ in range(3):
            try:
                count = await frame.evaluate(
                    """
                () => {
                  document.querySelectorAll('[data-capture-marker-overlay]').forEach((node) => node.remove());

                  const markerSelectors = [
                    '[class*="marker" i]',
                    '[class*="hotspot" i]',
                    '[class*="pin" i]',
                    '[class*="map-point" i]',
                    '[data-testid*="marker" i]',
                    '[data-testid*="hotspot" i]',
                    '[aria-label*="marker" i]',
                    '[aria-label*="hotspot" i]',
                    '[aria-label*="point" i]',
                    'button',
                    '[role="button"]'
                  ];
                  const candidates = Array.from(document.querySelectorAll(markerSelectors.join(',')));
                  const markers = [];
                  const seen = new Set();

                  for (const el of candidates) {
                    if (seen.has(el)) continue;
                    seen.add(el);

                    const rect = el.getBoundingClientRect();
                    if (rect.width <= 0 || rect.height <= 0) continue;
                    if (rect.width > 180 || rect.height > 180) continue;

                    const label = [
                      el.innerText,
                      el.getAttribute('aria-label'),
                      el.getAttribute('title'),
                      el.getAttribute('data-testid'),
                      el.className && String(el.className)
                    ].filter(Boolean).join(' ').toLowerCase();

                    const looksLikeMarker = /marker|hotspot|pin|point|popover|tooltip|number|step|location|select|click/.test(label);
                    const compactIcon = rect.width <= 80 && rect.height <= 80;
                    const insideMediaArea = Boolean(el.closest('figure, picture, [class*="image" i], [class*="diagram" i], [class*="media" i], [class*="graphic" i], [class*="hotspot" i], [class*="marker" i]'));

                    if (!looksLikeMarker && !(compactIcon && insideMediaArea)) continue;
                    markers.push({ el, rect, top: rect.top + window.scrollY, left: rect.left + window.scrollX });
                  }

                  markers.sort((a, b) => (a.top - b.top) || (a.left - b.left));
                  const limited = markers.slice(0, 80);

                  limited.forEach((item, index) => {
                    const el = item.el;
                    const rect = el.getBoundingClientRect();
                    const number = String(index + 1);

                    const cleanLabel = (value) => String(value || '')
                      .replace(/\\bclick\\s+to\\s+flip\\b/ig, ' ')
                      .replace(/\\b(?:front|back)\\s+of\\s+card\\b/ig, ' ')
                      .replace(/\\b(?:visible|hidden|zoom\\s+image|select|click)\\b/ig, ' ')
                      .replace(/\\s+/g, ' ')
                      .replace(/^[\\s:;,.|/-]+|[\\s:;,.|/-]+$/g, '')
                      .trim();
                    const isUsefulLabel = (value) => {
                      const label = cleanLabel(value);
                      if (!label || label.length < 3 || label.length > 140) return false;
                      return !/^(?:icon|button|marker|hotspot|pin|point|x|x mark|close)$/i.test(label);
                    };
                    const contextualLabel = (marker) => {
                      const containers = [
                        marker.closest('li'),
                        marker.closest('[class*="flash" i]'),
                        marker.closest('[class*="card" i]'),
                        marker.closest('[class*="tile" i]'),
                        marker.parentElement
                      ].filter(Boolean);
                      const labels = [];
                      for (const container of containers) {
                        for (const node of container.querySelectorAll('h1,h2,h3,h4,h5,h6,strong,b,[class*="title" i],[class*="heading" i]')) {
                          const label = cleanLabel(node.innerText || node.textContent || '');
                          if (isUsefulLabel(label)) labels.push(label);
                        }
                        const clone = container.cloneNode(true);
                        clone.querySelectorAll('button,[role="button"],svg,[data-capture-marker-overlay]').forEach((node) => node.remove());
                        const bodyLabel = cleanLabel(clone.innerText || clone.textContent || '');
                        if (isUsefulLabel(bodyLabel)) labels.push(bodyLabel);
                      }
                      labels.sort((a, b) => a.length - b.length);
                      return labels[0] || '';
                    };
                    const explicitLabel = cleanLabel([
                      el.getAttribute('aria-label'),
                      el.getAttribute('title'),
                      el.innerText
                    ].filter(Boolean).join(' '));
                    el.setAttribute(
                      'data-capture-marker-label',
                      isUsefulLabel(explicitLabel) ? explicitLabel : contextualLabel(el)
                    );

                    const badge = document.createElement('div');
                    badge.setAttribute('data-capture-marker-overlay', 'true');
                    badge.textContent = number;
                    badge.style.position = 'absolute';
                    badge.style.left = String(rect.left + window.scrollX + Math.max(0, rect.width - 18)) + 'px';
                    badge.style.top = String(rect.top + window.scrollY + 2) + 'px';
                    badge.style.width = '24px';
                    badge.style.height = '24px';
                    badge.style.borderRadius = '999px';
                    badge.style.background = '#d00000';
                    badge.style.color = '#ffffff';
                    badge.style.border = '2px solid #ffffff';
                    badge.style.boxShadow = '0 1px 6px rgba(0,0,0,0.45)';
                    badge.style.font = '700 14px/20px Arial, sans-serif';
                    badge.style.textAlign = 'center';
                    badge.style.paddingTop = '2px';
                    badge.style.zIndex = '2147483647';
                    badge.style.pointerEvents = 'none';
                    document.body.appendChild(badge);
                  });

                  return limited.length;
                }
                """
                )
                total += int(count or 0)
                break
            except PlaywrightError as exc:
                if not is_navigation_race(exc):
                    break
                await asyncio.sleep(0.5)
    if total:
        print(f"Annotated {total} marker candidates for screenshot/PDF.", file=sys.stderr)
    return total


async def capture_marker_contexts(page, assets_dir: Path) -> list[tuple[str, str, str]]:
    assets_dir.mkdir(parents=True, exist_ok=True)
    contexts: list[tuple[str, str, str]] = []

    for frame in page.frames:
        try:
            markers = await frame.query_selector_all("[data-capture-marker-number]")
        except PlaywrightError:
            continue

        for marker in markers[:80]:
            try:
                number = await marker.get_attribute("data-capture-marker-number")
                if not number:
                    continue
                label = clean_marker_label(
                    await marker.get_attribute("data-capture-marker-label") or ""
                )
                handle = await marker.evaluate_handle(
                    """
                    (el) => el.closest('figure, picture, [class*="image" i], [class*="diagram" i], [class*="media" i], [class*="graphic" i], [class*="hotspot" i], [class*="marker" i]')
                      || el.parentElement
                      || el
                    """
                )
                element = handle.as_element()
                if not element:
                    continue
                filename = f"marker-{int(number):03d}.png"
                path = assets_dir / filename
                await element.screenshot(path=str(path))
                contexts.append((number, f"assets/{filename}", label))
            except (PlaywrightError, ValueError):
                continue

    return contexts


async def page_text(page) -> str:
    for _ in range(5):
        try:
            return await page.evaluate(
                "() => (document.body && document.body.innerText || '').slice(0, 20000)"
            )
        except PlaywrightError as exc:
            if not is_navigation_race(exc):
                raise
            await wait_for_page(page, 10000)
            await asyncio.sleep(0.5)
    return ""


async def frame_text(frame) -> str:
    for _ in range(3):
        try:
            return await frame.evaluate(
                "() => (document.body && document.body.innerText || '').slice(0, 50000)"
            )
        except PlaywrightError as exc:
            if not is_navigation_race(exc):
                return ""
            await asyncio.sleep(0.5)
    return ""


async def all_frames_text(page) -> str:
    chunks = []
    for frame in page.frames:
        text = await frame_text(frame)
        if text.strip():
            chunks.append(text)
    return "\n\n".join(chunks)


async def wait_for_content_ready(
    page, settle_seconds: int, min_text_chars: int, timeout_ms: int
) -> None:
    if settle_seconds <= 0:
        return

    print(
        f"Waiting up to {settle_seconds}s for rendered content to settle...",
        file=sys.stderr,
    )
    deadline = asyncio.get_running_loop().time() + settle_seconds
    previous_length = -1
    stable_rounds = 0

    while asyncio.get_running_loop().time() < deadline:
        await wait_for_page(page, min(timeout_ms, 10000))
        text = await all_frames_text(page)
        length = len(clean_text(text))
        if length >= min_text_chars and abs(length - previous_length) < 80:
            stable_rounds += 1
            if stable_rounds >= 2:
                print(f"Rendered text settled at {length} characters.", file=sys.stderr)
                return
        else:
            stable_rounds = 0
        previous_length = length
        await asyncio.sleep(1)

    text = await all_frames_text(page)
    print(
        f"Continuing after settle wait with {len(clean_text(text))} rendered text characters.",
        file=sys.stderr,
    )


async def capture_best_screenshot(page, screenshot_path: Path) -> None:
    try:
        handle = await page.evaluate_handle(
            """
            () => {
              const nodes = [document.scrollingElement, ...document.querySelectorAll('*')].filter(Boolean);
              const ranked = nodes
                .map((el) => {
                  const rect = el.getBoundingClientRect();
                  return {
                    el,
                    rect,
                    scrollHeight: el.scrollHeight || 0,
                    clientHeight: el.clientHeight || 0,
                    area: Math.max(0, rect.width) * Math.max(0, rect.height),
                  };
                })
                .filter((item) => item.scrollHeight > item.clientHeight + 80 && item.area > 20000)
                .sort((a, b) => (b.scrollHeight * b.rect.width) - (a.scrollHeight * a.rect.width));
              return ranked[0]?.el || document.scrollingElement || document.body;
            }
            """
        )
        element = handle.as_element()
        if element:
            await element.screenshot(path=str(screenshot_path))
            return
    except PlaywrightError:
        pass

    await page.screenshot(path=str(screenshot_path), full_page=True)


async def prepare_full_page_layout_for_capture(page) -> None:
    for _ in range(3):
        try:
            await page.evaluate(
                """
                () => {
                  const nodes = [document.scrollingElement, document.body, ...document.querySelectorAll('*')].filter(Boolean);
                  const scrollables = nodes
                    .filter((el) => el.scrollHeight > el.clientHeight + 80)
                    .sort((a, b) => b.scrollHeight - a.scrollHeight)
                    .slice(0, 20);

                  for (const el of scrollables) {
                    el.setAttribute('data-capture-original-style', el.getAttribute('style') || '');
                    el.style.overflow = 'visible';
                    el.style.overflowY = 'visible';
                    el.style.maxHeight = 'none';
                    el.style.height = String(el.scrollHeight) + 'px';
                    el.scrollTop = 0;
                  }

                  document.documentElement.style.overflow = 'visible';
                  document.body.style.overflow = 'visible';
                  document.documentElement.style.height = String(Math.max(
                    document.documentElement.scrollHeight,
                    document.body.scrollHeight,
                    window.innerHeight
                  )) + 'px';
                }
                """
            )
            await asyncio.sleep(0.3)
            return
        except PlaywrightError as exc:
            if not is_navigation_race(exc):
                raise
            await wait_for_page(page, 10000)


def is_navigation_race(exc: PlaywrightError) -> bool:
    message = str(exc).lower()
    return (
        "execution context was destroyed" in message
        or "most likely because of a navigation" in message
        or "cannot find context with specified id" in message
        or "target closed" in message
    )


def looks_like_login(url: str, text: str) -> bool:
    parsed = urlparse(url)
    if parsed.scheme == "data":
        url_hint = ""
    else:
        url_hint = f"{parsed.netloc} {parsed.path} {parsed.query[:300]}"
    haystack = f"{url_hint}\n{text[:4000]}".lower()
    return any(re.search(pattern, haystack) for pattern in LOGIN_PATTERNS)


async def maybe_prompt_for_login(
    page,
    timeout_ms: int,
    prompt_enabled: bool,
    wait_seconds: int,
    allow_login_capture: bool,
) -> None:
    text = await page_text(page)
    if not looks_like_login(page.url, text):
        return

    print("Login screen detected.", file=sys.stderr)

    if wait_seconds > 0:
        print(
            f"Waiting up to {wait_seconds}s for manual login in the Chrome window...",
            file=sys.stderr,
        )
        deadline = asyncio.get_running_loop().time() + wait_seconds
        while asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(3)
            await wait_for_page(page, min(timeout_ms, 10000))
            text = await all_frames_text(page)
            if not looks_like_login(page.url, text):
                print("Login appears complete. Continuing capture.", file=sys.stderr)
                return
        print("Login still detected after wait period.", file=sys.stderr)

    if not prompt_enabled:
        if allow_login_capture:
            print("Continuing because --allow-login-capture is set.", file=sys.stderr)
            return
        raise LoginRequired(
            "Run with --headed and complete manual login before capture."
        )

    print(
        "Log in manually in the Chrome window, then press Enter here to continue...",
        file=sys.stderr,
    )
    await asyncio.to_thread(input)
    await wait_for_page(page, timeout_ms)
    text = await all_frames_text(page)
    if looks_like_login(page.url, text) and not allow_login_capture:
        raise LoginRequired("Login still detected after manual prompt.")


async def rendered_documents(page) -> list[tuple[str, str]]:
    documents = []
    for frame in page.frames:
        try:
            html = await frame.content()
        except PlaywrightError as exc:
            if is_navigation_race(exc):
                await asyncio.sleep(0.5)
                try:
                    html = await frame.content()
                except PlaywrightError:
                    continue
            else:
                continue

        text = BeautifulSoup(html, "html.parser").get_text(" ", strip=True)
        if not text:
            continue
        label = frame.url or "about:blank"
        documents.append((label, html))
    return documents


def asset_urls_from_documents(documents: list[tuple[str, str]]) -> list[str]:
    urls = []
    seen = set()
    for doc_url, html in documents:
        soup = BeautifulSoup(html, "html.parser")
        for tag in soup.find_all(["img", "video", "source"]):
            for attr in ["src", "poster"]:
                value = tag.get(attr)
                if not value or value.startswith("data:") or value.startswith("blob:"):
                    continue
                absolute = urljoin(doc_url, value)
                if absolute not in seen:
                    seen.add(absolute)
                    urls.append(absolute)
    return urls


async def download_assets(context, urls: list[str], assets_dir: Path) -> dict[str, str]:
    assets_dir.mkdir(parents=True, exist_ok=True)
    asset_map: dict[str, str] = {}
    for index, url in enumerate(urls, start=1):
        try:
            response = await context.request.get(url, timeout=30000)
            if not response.ok:
                continue
            body = await response.body()
            if not body:
                continue
            content_type = (response.headers.get("content-type") or "").split(";")[0].strip()
            ext = extension_for_asset(url, content_type)
            digest = hashlib.sha1(url.encode("utf-8")).hexdigest()[:10]
            filename = f"asset-{index:03d}-{digest}{ext}"
            path = assets_dir / filename
            path.write_bytes(body)
            asset_map[url] = f"assets/{filename}"
        except Exception:
            continue
    if asset_map:
        print(f"Downloaded {len(asset_map)} image/video assets.", file=sys.stderr)
    return asset_map


def extension_for_asset(url: str, content_type: str) -> str:
    parsed = urlparse(url)
    suffix = Path(parsed.path).suffix.lower()
    if suffix and re.fullmatch(r"\.[a-z0-9]{2,5}", suffix):
        return suffix
    if content_type:
        guessed = mimetypes.guess_extension(content_type)
        if guessed:
            return ".jpg" if guessed == ".jpe" else guessed
    return ".bin"


async def export_current_page(
    context,
    page,
    paths: CapturePaths,
    title: str,
    args: argparse.Namespace,
) -> None:
    await scroll_all_containers(page)
    await scroll_full_page(page)

    await annotate_markers_for_capture(page)
    marker_contexts = await capture_marker_contexts(page, paths.assets_dir)

    documents = await rendered_documents(page)
    asset_urls = asset_urls_from_documents(documents)
    asset_map = await download_assets(context, asset_urls, paths.assets_dir)
    markdown = documents_to_markdown(
        documents, page.url, title, asset_map, marker_contexts
    )
    paths.markdown.write_text(markdown, encoding="utf-8")

    if args.markdown_only or args.split_course_navigation:
        return

    html = await page.content()
    paths.html.write_text(html, encoding="utf-8")
    await prepare_full_page_layout_for_capture(page)
    await page.pdf(path=str(paths.pdf), print_background=True, format="A4")
    await capture_best_screenshot(page, paths.screenshot)


async def capture_course_navigation_items(
    context,
    page,
    args: argparse.Namespace,
    out_dir: Path,
) -> list[CapturePaths]:
    items = await detect_course_navigation_items(page)
    if not items:
        print(
            "Course Navigation not detected; exporting the current page only.",
            file=sys.stderr,
        )
        title = args.title or await safe_title(page, args.url)
        await export_current_page(context, page, build_paths(out_dir), title, args)
        return [build_paths(out_dir)]

    course_title = None
    if len(items) > 1 and is_course_root_title(items[0].title):
        course_title = items[0].title
        out_dir = out_dir / safe_filename(course_title)
        out_dir.mkdir(parents=True, exist_ok=True)
        items = [
            CourseNavItem(index=item.index, title=item.title)
            for item in items[1:]
        ]

    print(f"Detected {len(items)} Course Navigation items.", file=sys.stderr)
    exported: list[CapturePaths] = []

    for item in items:
        print(f"Capturing Course Navigation item {item.index + 1}: {item.title}", file=sys.stderr)
        await page.goto(args.url, wait_until="domcontentloaded", timeout=args.timeout_ms)
        await wait_for_page(page, args.timeout_ms)
        await wait_for_content_ready(
            page, max(3, args.settle_seconds // 3), args.min_text_chars, args.timeout_ms
        )
        clicked = await click_course_navigation_item(page, item.title, item.index)
        if not clicked:
            print(f"Could not click Course Navigation item: {item.title}", file=sys.stderr)
            continue

        await wait_for_page(page, args.timeout_ms)
        await wait_for_content_ready(
            page, args.settle_seconds, args.min_text_chars, args.timeout_ms
        )
        if not args.no_expand_interactive:
            await expand_interactive_content(page)
            await wait_for_content_ready(
                page,
                max(3, args.settle_seconds // 2),
                args.min_text_chars,
                args.timeout_ms,
            )

        paths = build_named_paths(out_dir, item.title)
        await export_current_page(context, page, paths, item.title, args)
        exported.append(paths)

    index_path = out_dir / "Course Navigation.md"
    index_lines = [f"# {course_title or 'Course Navigation'}", ""]
    for paths in exported:
        index_lines.append(f"- [{paths.markdown.stem}]({paths.out_dir.name}/{paths.markdown.name})")
    index_path.write_text("\n".join(index_lines).rstrip() + "\n", encoding="utf-8")
    print(f"Course index: {index_path}", file=sys.stderr)
    return exported


async def detect_course_navigation_items(page) -> list[CourseNavItem]:
    for frame in page.frames:
        try:
            raw_items = await frame.evaluate(
                """
                () => {
                  const clean = (value) => String(value || '').replace(/\\s+/g, ' ').trim();
                  const visible = (el) => {
                    const rect = el.getBoundingClientRect();
                    const style = window.getComputedStyle(el);
                    return rect.width > 0 && rect.height > 0 && style.visibility !== 'hidden' && style.display !== 'none';
                  };
                  const all = Array.from(document.querySelectorAll('h1,h2,h3,h4,h5,h6,[role="heading"],nav,aside,section,div,span,button'));
                  const heading = all.find((el) => /\\bcourse\\s+navigation\\b/i.test(clean(el.innerText || el.textContent)) && visible(el));
                  if (!heading) return [];

                  const container = heading.closest('nav,aside,section,[role="navigation"],[class*="nav" i],[class*="sidebar" i],[class*="drawer" i]') || heading.parentElement;
                  if (!container) return [];

                  const candidates = Array.from(container.querySelectorAll('a,button,[role="button"],[role="treeitem"],[role="menuitem"],[tabindex]'));
                  const items = [];
                  const seen = new Set();
                  for (const el of candidates) {
                    if (!visible(el)) continue;
                    const text = clean(el.innerText || el.textContent || el.getAttribute('aria-label') || el.getAttribute('title'));
                    if (!text || seen.has(text)) continue;
                    if (/\\bcourse\\s+navigation\\b/i.test(text)) continue;
                    if (/^(previous|next|back|close|menu|contents?|collapse|expand)$/i.test(text)) continue;
                    if (text.length < 3 || text.length > 120) continue;
                    seen.add(text);
                    items.push(text);
                  }
                  return items.slice(0, 80);
                }
                """
            )
        except PlaywrightError:
            continue
        items = []
        seen_titles = set()
        for raw_index, title in enumerate(raw_items or []):
            cleaned = clean_text(title)
            if not is_course_navigation_title(cleaned) or cleaned in seen_titles:
                continue
            seen_titles.add(cleaned)
            items.append(CourseNavItem(index=raw_index, title=cleaned))
        if items:
            return items
    return []


def is_course_navigation_title(title: str) -> bool:
    if not title:
        return False
    normalized = title.lower()
    noisy_patterns = [
        r"top of page",
        r"skip to lesson",
        r"close navigation",
        r"open search",
        r"play video",
        r"video transcript",
        r"zoom image",
        r"start(?: again)?$",
        r"last step",
        r"submit",
        r"take again",
        r"go to website",
        r"click to flip",
        r"front of card",
        r"back of card",
        r"\bmarker\b",
        r"not viewed",
        r"\bstep\s+\d+\b",
        r"\b\d+\s+\d+\s+\d+\b",
        r"^lesson\s+\d+\s+-",
    ]
    if any(re.search(pattern, normalized) for pattern in noisy_patterns):
        return False
    if len(title) > 80:
        return False
    words = title.split()
    if len(words) > 8:
        return False
    if "," in title:
        return False
    generic_content_tabs = {
        "extract",
        "load",
        "transform",
        "window functions",
        "load transformed data",
        "data loading",
        "data transformations",
        "scheduled tasks",
        "event-driven processing",
        "amazon redshift automation",
    }
    if normalized in generic_content_tabs:
        return False
    return True


def is_course_root_title(title: str) -> bool:
    normalized = title.lower()
    lesson_titles = {
        "introduction",
        "assessment",
        "conclusion",
    }
    if normalized in lesson_titles:
        return False
    if re.search(r"\b(solution|course|module|building)\b", normalized):
        return True
    return len(title.split()) >= 4


async def click_course_navigation_item(page, title: str, index: int) -> bool:
    for frame in page.frames:
        try:
            clicked = await frame.evaluate(
                """
                ({ title, index }) => {
                  const clean = (value) => String(value || '').replace(/\\s+/g, ' ').trim();
                  const visible = (el) => {
                    const rect = el.getBoundingClientRect();
                    const style = window.getComputedStyle(el);
                    return rect.width > 0 && rect.height > 0 && style.visibility !== 'hidden' && style.display !== 'none';
                  };
                  const all = Array.from(document.querySelectorAll('h1,h2,h3,h4,h5,h6,[role="heading"],nav,aside,section,div,span,button'));
                  const heading = all.find((el) => /\\bcourse\\s+navigation\\b/i.test(clean(el.innerText || el.textContent)) && visible(el));
                  if (!heading) return false;
                  const container = heading.closest('nav,aside,section,[role="navigation"],[class*="nav" i],[class*="sidebar" i],[class*="drawer" i]') || heading.parentElement;
                  if (!container) return false;
                  const candidates = Array.from(container.querySelectorAll('a,button,[role="button"],[role="treeitem"],[role="menuitem"],[tabindex]'))
                    .filter(visible)
                    .filter((el) => {
                      const text = clean(el.innerText || el.textContent || el.getAttribute('aria-label') || el.getAttribute('title'));
                      return text && !/\\bcourse\\s+navigation\\b/i.test(text) && !/^(previous|next|back|close|menu|contents?|collapse|expand)$/i.test(text);
                    });
                  const exact = candidates.find((el) => clean(el.innerText || el.textContent || el.getAttribute('aria-label') || el.getAttribute('title')) === title);
                  const target = exact || candidates[index];
                  if (!target) return false;
                  target.scrollIntoView({ block: 'center', inline: 'center' });
                  target.click();
                  return true;
                }
                """,
                {"title": title, "index": index},
            )
        except PlaywrightError as exc:
            if is_navigation_race(exc):
                await asyncio.sleep(0.5)
            continue
        if clicked:
            return True
    return False


async def capture(args: argparse.Namespace) -> CapturePaths:
    out_dir = Path(args.out).expanduser().resolve()
    profile_dir = Path(args.profile_dir).expanduser().resolve()
    if args.delete_orphans and out_dir.exists():
        if out_dir == out_dir.anchor or len(out_dir.parts) < 3:
            raise ValueError(f"Refusing to delete unsafe output directory: {out_dir}")
        shutil.rmtree(out_dir)
    paths = build_paths(out_dir)

    async with async_playwright() as playwright:
        context = await playwright.chromium.launch_persistent_context(
            user_data_dir=str(profile_dir),
            channel="chrome",
            headless=not args.headed,
            accept_downloads=True,
            viewport={"width": 1440, "height": 1100},
        )
        page = context.pages[0] if context.pages else await context.new_page()
        page.set_default_timeout(args.timeout_ms)

        try:
            await page.goto(args.url, wait_until="domcontentloaded", timeout=args.timeout_ms)
            await wait_for_page(page, args.timeout_ms)
            await maybe_prompt_for_login(
                page,
                args.timeout_ms,
                args.prompt_login,
                args.wait_login_seconds,
                args.allow_login_capture,
            )
            await wait_for_content_ready(
                page, args.settle_seconds, args.min_text_chars, args.timeout_ms
            )
            if not args.no_expand_interactive and not args.split_course_navigation:
                await expand_interactive_content(page)
                await wait_for_content_ready(
                    page, max(3, args.settle_seconds // 2), args.min_text_chars, args.timeout_ms
                )
            if args.split_course_navigation:
                await capture_course_navigation_items(context, page, args, out_dir)
            else:
                title = args.title or await safe_title(page, args.url)
                await export_current_page(context, page, paths, title, args)
        finally:
            await context.close()

    return paths


async def safe_title(page, url: str) -> str:
    try:
        title = (await page.title()).strip()
    except Exception:
        title = ""
    if title:
        return title
    parsed = urlparse(url)
    return parsed.netloc or url


def documents_to_markdown(
    documents: list[tuple[str, str]],
    url: str,
    title: str,
    asset_map: dict[str, str] | None = None,
    marker_contexts: list[tuple[str, str, str]] | None = None,
) -> str:
    sections = []
    seen = set()
    asset_map = asset_map or {}
    marker_asset_map = {
        number: (rel_path, clean_marker_label(label))
        for number, rel_path, label in (marker_contexts or [])
    }
    for doc_url, html in documents:
        body = html_to_markdown_body(html, doc_url, asset_map, marker_asset_map)
        normalized = clean_text(body)
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        sections.append((doc_url, body))

    if not sections:
        return f"# {clean_text(title)}\n\nSource: {url}\n"

    output = [f"# {clean_text(title)}", "", f"Source: {url}", ""]
    for index, (doc_url, body) in enumerate(sections, start=1):
        if index > 1:
            output.extend([f"## Frame {index}: {doc_url}", ""])
        output.append(body)
        output.append("")
    return "\n".join(output).rstrip() + "\n"


def html_to_markdown_body(
    html: str,
    base_url: str,
    asset_map: dict[str, str] | None = None,
    marker_asset_map: dict[str, tuple[str, str]] | None = None,
) -> str:
    soup = BeautifulSoup(html, "html.parser")
    for selector in [
        "script",
        "style",
        "noscript",
        "[data-capture-marker-overlay]",
        "iframe",
        "nav",
        "header",
        "footer",
        "aside",
        "form",
    ]:
        for node in soup.select(selector):
            node.decompose()

    main = (
        soup.find("article")
        or soup.find("main")
        or soup.find(attrs={"role": "main"})
        or soup.body
        or soup
    )
    body = render_children(main, base_url, asset_map or {}, marker_asset_map or {}).strip()
    body = collapse_marker_duplicate_labels(body)
    body = relocate_streaming_tab_headings(body)
    body = format_compact_flashcard_items(body)
    body = normalize_batch_flashcard_numbering(body)
    body = format_knowledge_checks(body)
    body = normalize_nested_heading_structure(body)
    body = split_inline_bold_leads(body)
    body = re.sub(r"\n{3,}", "\n\n", body)
    return body


def render_children(
    node: Tag,
    base_url: str = "",
    asset_map: dict[str, str] | None = None,
    marker_asset_map: dict[str, tuple[str, str]] | None = None,
) -> str:
    return "".join(
        render_node(child, base_url, asset_map or {}, marker_asset_map or {})
        for child in node.children
    )


def render_node(
    node,
    base_url: str = "",
    asset_map: dict[str, str] | None = None,
    marker_asset_map: dict[str, tuple[str, str]] | None = None,
) -> str:
    tick = chr(96)
    asset_map = asset_map or {}
    marker_asset_map = marker_asset_map or {}
    if isinstance(node, NavigableString):
        return clean_inline(str(node))
    if not isinstance(node, Tag):
        return ""

    name = node.name.lower()
    if name in {"h1", "h2", "h3", "h4", "h5", "h6"}:
        level = normalize_heading_level(node, int(name[1]))
        text = clean_text(node.get_text(" ", strip=True))
        return f"\n\n{'#' * level} {text}\n\n" if text else ""
    if name == "p":
        text = render_children(node, base_url, asset_map, marker_asset_map).strip()
        return f"\n\n{text}\n\n" if text else ""
    if name == "br":
        return "\n"
    if name in {"strong", "b"}:
        text = render_children(node, base_url, asset_map, marker_asset_map).strip()
        return f"**{text}**" if text else ""
    if name in {"em", "i"}:
        text = render_children(node, base_url, asset_map, marker_asset_map).strip()
        return f"*{text}*" if text else ""
    if name == "code":
        text = node.get_text("", strip=False)
        if node.find_parent("pre"):
            return text
        return f"{tick}{text.strip()}{tick}" if text.strip() else ""
    if name == "pre":
        text = node.get_text("", strip=False).strip("\n")
        return f"\n\n{tick * 3}\n{text}\n{tick * 3}\n\n" if text else ""
    if name == "a":
        text = clean_text(node.get_text(" ", strip=True))
        href = node.get("href")
        if text and href and not href.startswith("#"):
            return f"[{text}]({urljoin(base_url, href)})"
        return text
    if name == "img":
        alt = clean_text(node.get("alt", ""))
        src = node.get("src", "")
        title = clean_text(node.get("title", ""))
        if not src:
            return f"![{alt or title}]" if (alt or title) else ""
        caption = alt or title or "image"
        absolute = urljoin(base_url, src)
        target = asset_map.get(absolute, absolute)
        return f"\n\n![{caption}]({target})\n\n"
    if name == "video":
        label = clean_text(node.get("aria-label", "") or node.get("title", "") or "video")
        src = node.get("src")
        if not src:
            source = node.find("source")
            src = source.get("src") if source else ""
        poster = node.get("poster")
        parts = []
        if poster:
            poster_url = urljoin(base_url, poster)
            parts.append(f"![{label} poster]({asset_map.get(poster_url, poster_url)})")
        if src:
            video_url = urljoin(base_url, src)
            parts.append(f"[Video: {label}]({asset_map.get(video_url, video_url)})")
        return "\n\n" + "\n\n".join(parts) + "\n\n" if parts else ""
    if name == "svg":
        title_node = node.find("title")
        label = clean_text(
            node.get("aria-label", "")
            or node.get("title", "")
            or node.get("data-testid", "")
            or (title_node.get_text(" ", strip=True) if title_node else "")
            or node.get_text(" ", strip=True)
        )
        if not useful_icon_label(label):
            return ""
        return f" [{label}] "
    if name == "canvas":
        label = clean_text(node.get("aria-label", "") or node.get("title", ""))
        return f" [{label or 'canvas content'}] "
    if name == "button":
        text = render_children(node, base_url, asset_map, marker_asset_map).strip() or clean_text(
            node.get("aria-label", "") or node.get("title", "")
        )
        text = clean_button_text(text)
        marker_number = node.get("data-capture-marker-number")
        marker_label = clean_marker_label(node.get("data-capture-marker-label", ""))
        if marker_number:
            rel_path, asset_label = marker_asset_map.get(marker_number, ("", ""))
            label = clean_marker_label(marker_label or text or asset_label)
            if not label:
                return ""
            caption = f"Marker {marker_number}" + (f": {label}" if label else "")
            image = f'<img src="{rel_path}" alt="{caption}" width="360">\n\n' if rel_path else ""
            marker_text = f"**Marker {marker_number}:** {label}" if label else f"**Marker {marker_number}**"
            return f"\n\n{image}{marker_text}\n\n"
        if is_content_selector_button(node, text):
            level = content_selector_heading_level(node)
            return f"\n\n{'#' * level} {text}\n\n"
        return f"\n\n**Button:** {text}\n\n" if text else ""
    if name in {"ul", "ol"}:
        ordered = name == "ol"
        lines = []
        for index, li in enumerate(node.find_all("li", recursive=False), start=1):
            content = render_children(li, base_url, asset_map, marker_asset_map).strip()
            content = strip_leading_bullet_glyphs(content)
            content = re.sub(r"\n+", "\n  ", content)
            marker = f"{index}." if ordered else "-"
            if content:
                lines.append(f"{marker} {content}")
        return "\n\n" + "\n".join(lines) + "\n\n" if lines else ""
    if name == "blockquote":
        text = render_children(node, base_url, asset_map, marker_asset_map).strip()
        quoted = "\n".join(f"> {line}" for line in text.splitlines() if line.strip())
        return f"\n\n{quoted}\n\n" if quoted else ""
    if name == "table":
        return render_table(node)
    if name in {"div", "section", "span", "main", "article", "body"}:
        return render_children(node, base_url, asset_map, marker_asset_map)
    return render_children(node, base_url, asset_map, marker_asset_map)


def render_table(table: Tag) -> str:
    rows = []
    for tr in table.find_all("tr"):
        cells = [clean_text(cell.get_text(" ", strip=True)) for cell in tr.find_all(["th", "td"])]
        if cells:
            rows.append(cells)
    if not rows:
        return ""

    width = max(len(row) for row in rows)
    padded = [row + [""] * (width - len(row)) for row in rows]
    header = padded[0]
    sep = ["---"] * width
    lines = [
        "| " + " | ".join(header) + " |",
        "| " + " | ".join(sep) + " |",
    ]
    for row in padded[1:]:
        lines.append("| " + " | ".join(row) + " |")
    return "\n\n" + "\n".join(lines) + "\n\n"


def clean_inline(text: str) -> str:
    cleaned = re.sub(r"\bclick\s+to\s+flip\s*(?=back\s+of\s+card\b)", " ", text, flags=re.I)
    cleaned = re.sub(r"\bclick\s+to\s+flip\b", " ", cleaned, flags=re.I)
    cleaned = re.sub(r"\b(?:front|back)\s+of\s+card\b", " ", cleaned, flags=re.I)
    return re.sub(r"\s+", " ", cleaned)


def clean_text(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


STREAMING_TAB_ANCHORS = {
    "Kinesis Data Streams": re.compile(
        r"^Amazon Redshift supports streaming ingestion from Amazon Kinesis Data Streams\b"
    ),
    "Amazon Data Firehose": re.compile(
        r"^Amazon Data Firehose integrates with Amazon S3 and Amazon Redshift\b"
    ),
    "Amazon MSK": re.compile(
        r"^Amazon MSK is a fully managed service\b"
    ),
}


def relocate_streaming_tab_headings(markdown: str) -> str:
    lines = markdown.splitlines()
    heading_levels: dict[str, int] = {}
    output: list[str] = []

    for line in lines:
        match = re.fullmatch(r"(#{2,6})\s+(.+)", line.strip())
        if match and match.group(2) in STREAMING_TAB_ANCHORS:
            heading_levels[match.group(2)] = len(match.group(1))
            continue
        output.append(line)

    if not heading_levels:
        return markdown

    relocated: list[str] = []
    inserted: set[str] = set()
    for line in output:
        stripped = line.strip()
        for label, anchor in STREAMING_TAB_ANCHORS.items():
            if label not in heading_levels or label in inserted:
                continue
            if anchor.search(stripped):
                if relocated and relocated[-1].strip():
                    relocated.append("")
                relocated.append(f"{'#' * heading_levels[label]} {label}")
                relocated.append("")
                inserted.add(label)
                break
        relocated.append(line)

    return "\n".join(relocated)


def format_compact_flashcard_items(markdown: str) -> str:
    service_labels = {
        "Amazon S3",
        "AWS Glue",
        "AWS EMR",
        "Amazon EMR",
        "AWS DMS",
        "AWS Lambda",
    }

    def repl(match: re.Match[str]) -> str:
        number, alt, src, title, body = match.groups()
        title = clean_text(title)
        if title not in service_labels:
            return match.group(0)
        alt = clean_text(alt)
        body = clean_text(body)
        return (
            f'{number}. **{title}**\n\n'
            f'   <img src="{src}" alt="{alt}" width="240">\n\n'
            f'   {body}\n'
        )

    return re.sub(
        r"(?m)^(\d+)\.\s+!\[([^\]]*)\]\(([^)]+)\)\s+\*\*([^*]+)\*\*\s+(.+)$",
        repl,
        markdown,
    )


def normalize_batch_flashcard_numbering(markdown: str) -> str:
    service_labels = {
        "Amazon S3",
        "AWS Glue",
        "AWS EMR",
        "Amazon EMR",
        "AWS DMS",
        "AWS Lambda",
    }
    lines = markdown.splitlines()
    output: list[str] = []
    in_batch = False
    counter = 1
    for line in lines:
        stripped = line.strip()
        if stripped == "## Ingesting batch data in Amazon Redshift":
            in_batch = True
            counter = 1
            output.append(line)
            continue
        if in_batch and stripped.startswith("### Common use cases"):
            in_batch = False
        if in_batch:
            match = re.match(r"^(\d+)\.\s+\*\*([^*]+)\*\*(.*)$", line)
            if match and clean_text(match.group(2)) in service_labels:
                line = f"{counter}. **{clean_text(match.group(2))}**{match.group(3)}"
                counter += 1
        output.append(line)
    return "\n".join(output)


def format_knowledge_checks(markdown: str) -> str:
    if "## Knowledge check" not in markdown:
        return markdown

    lines = markdown.splitlines()
    output: list[str] = []
    i = 0
    in_knowledge = False
    question_number = 1

    while i < len(lines):
        stripped = lines[i].strip()
        if stripped == "## Knowledge check":
            in_knowledge = True
            question_number = 1
            output.append(lines[i])
            i += 1
            continue
        if in_knowledge and stripped.startswith("## ") and stripped != "## Knowledge check":
            in_knowledge = False

        if in_knowledge and stripped.endswith("?"):
            question = stripped
            j = i + 1
            options: list[str] = []
            while j < len(lines):
                candidate = lines[j].strip()
                if not candidate:
                    j += 1
                    continue
                if candidate in {"Correct", "Incorrect"}:
                    break
                if candidate.startswith("#") or candidate.endswith("?"):
                    break
                options.append(candidate)
                j += 1

            if len(options) >= 2 and j < len(lines) and lines[j].strip() in {"Correct", "Incorrect"}:
                result = lines[j].strip()
                j += 1
                feedback_lines: list[str] = []
                while j < len(lines):
                    candidate = lines[j].strip()
                    if not candidate:
                        j += 1
                        continue
                    if candidate.startswith("****In the next lesson"):
                        break
                    if candidate.startswith("#") or candidate.endswith("?"):
                        break
                    feedback_lines.append(candidate)
                    j += 1

                feedback = clean_text(" ".join(feedback_lines))
                correct = infer_correct_option(options, feedback)
                output.extend(
                    render_knowledge_question(
                        question_number, question, options, correct, result, feedback
                    )
                )
                question_number += 1
                i = j
                continue

        output.append(lines[i])
        i += 1

    return "\n".join(output)


def infer_correct_option(options: list[str], feedback: str) -> str | None:
    normalized_feedback = clean_text(re.sub(r"[*_\\x60]", "", feedback)).lower()
    matches = [
        option
        for option in options
        if clean_text(re.sub(r"[*_\\x60]", "", option)).lower() in normalized_feedback
    ]
    if not matches:
        return None
    return max(matches, key=len)


def render_knowledge_question(
    question_number: int,
    question: str,
    options: list[str],
    correct: str | None,
    result: str,
    feedback: str,
) -> list[str]:
    output = ["", f"### Question {question_number}", "", question, ""]
    for option in options:
        checked = "x" if correct and option == correct else " "
        suffix = " (correct)" if correct and option == correct else ""
        output.append(f"- [{checked}] {option}{suffix}")
    output.extend(["", f"**Result:** {result}"])
    if feedback:
        output.extend(["", f"**Feedback:** {feedback}"])
    output.append("")
    return output


def normalize_nested_heading_structure(markdown: str) -> str:
    child_heading_labels = {
        "extract",
        "load",
        "transform",
        "window functions",
        "load transformed data",
        "data loading",
        "data transformations",
        "scheduled tasks",
        "event-driven processing",
        "amazon redshift automation",
    }
    parent_heading_hints = {
        "an example",
        "introduction",
        "elt workflow",
        "stored procedure",
        "workflow",
    }
    lines = markdown.splitlines()
    output: list[str] = []
    active_parent_level: int | None = None

    for line in lines:
        match = re.match(r"^(#{1,6})\s+(.+?)\s*$", line)
        if not match:
            output.append(line)
            continue

        level = len(match.group(1))
        title = clean_text(match.group(2)).strip("*")
        normalized = title.lower()

        if active_parent_level is not None and level <= active_parent_level and normalized not in child_heading_labels:
            active_parent_level = None

        if normalized in parent_heading_hints or "elt workflow" in normalized:
            active_parent_level = level
            output.append(line)
            continue

        if normalized in child_heading_labels and active_parent_level is not None:
            level = min(active_parent_level + 1, 6)
            output.append(f"{'#' * level} {title}")
            continue

        if re.match(r"^step\s+\d+\b", normalized) and active_parent_level is not None:
            level = min(active_parent_level + 1, 6)
            output.append(f"{'#' * level} {title}")
            continue

        output.append(line)

    return "\n".join(output)


def split_inline_bold_leads(markdown: str) -> str:
    markdown = re.sub(r"\*\*([^*]+)\*\*\*\*\(([^)]+)\)\*\*", r"**\1 (\2)**", markdown)
    markdown = re.sub(r"(?m)^(\*\*[^*]+\*\*)(\S.+)$", r"\1\n\n\2", markdown)
    service_names = [
        "AWS Glue",
        "AWS DMS",
        "AWS Lambda",
        "Amazon S3",
        "Amazon EMR",
        "Amazon Redshift",
    ]
    pattern = re.compile(
        r"(?m)^\*\*(" + "|".join(re.escape(name) for name in service_names) + r")\*\*\s+(\S.+)$"
    )
    markdown = pattern.sub(r"**\1**\n\n\2", markdown)
    markdown = re.sub(r"(?m)^(\*\*[^*]{3,120}\*\*)\n(?!\n|#|[-*]\s|\d+\.\s|!|<img)(\S)", r"\1\n\n\2", markdown)
    return markdown


def normalize_heading_level(node: Tag, level: int) -> int:
    text = clean_text(node.get_text(" ", strip=True)).lower()
    secondary_tab_headings = {
        "scalability",
        "performance",
        "cost-effectiveness",
        "cost effectiveness",
        "integration",
        "security",
    }
    container = node.find_parent(
        attrs={
            "class": re.compile(
                r"(card|tab|panel|accordion|carousel|slide|flip)", re.I
            )
        }
    )
    if text in secondary_tab_headings or (container and level < 3):
        return 3
    return level


def strip_leading_bullet_glyphs(text: str) -> str:
    cleaned = clean_text(text)
    cleaned = re.sub(r"^(?:[•·●▪◦]\s*)+", "", cleaned)
    cleaned = re.sub(r"^(?:[-*+]\s*)?[•·●▪◦]\s+", "", cleaned)
    return clean_text(cleaned)


def is_content_selector_button(node: Tag, text: str) -> bool:
    if not text:
        return False
    normalized = clean_text(text).lower()
    service_tab_labels = {
        "kinesis data streams",
        "amazon data firehose",
        "amazon msk",
    }
    if normalized in service_tab_labels:
        return True
    if node.get("role") == "tab" or node.get("aria-controls"):
        return True
    classes = " ".join(node.get("class") or [])
    parent_classes = " ".join((node.parent.get("class") if isinstance(node.parent, Tag) else None) or [])
    return bool(re.search(r"\b(tab|tabs|tablist|pill|selector)\b", f"{classes} {parent_classes}", re.I))


def content_selector_heading_level(node: Tag) -> int:
    parent_level = nearest_preceding_heading_level(node)
    return min((parent_level or 2) + 1, 6)


def nearest_preceding_heading_level(node: Tag) -> int | None:
    current: Tag | None = node
    while current and isinstance(current, Tag):
        sibling = current.previous_sibling
        while sibling:
            if isinstance(sibling, Tag):
                found = last_heading_level(sibling)
                if found:
                    return found
            sibling = sibling.previous_sibling
        current = current.parent if isinstance(current.parent, Tag) else None
    return None


def last_heading_level(node: Tag) -> int | None:
    if re.fullmatch(r"h[1-6]", node.name or ""):
        return normalize_heading_level(node, int(node.name[1]))
    headings = node.find_all(re.compile(r"^h[1-6]$"))
    if not headings:
        return None
    last = headings[-1]
    return normalize_heading_level(last, int(last.name[1]))


def useful_icon_label(label: str) -> bool:
    normalized = clean_text(label).lower()
    if not normalized:
        return False
    noisy = {
        "icon",
        "x",
        "x mark",
        "close",
        "close icon",
        "hidden",
        "visible",
        "visibility",
        "button",
        "svg",
    }
    if normalized in noisy:
        return False
    if len(normalized) <= 2 and not normalized.isdigit():
        return False
    return True


def clean_button_text(text: str) -> str:
    cleaned = clean_text(text)
    cleaned = re.sub(
        r"\[(?:icon|x mark|close|hidden|visible)\]", "", cleaned, flags=re.I
    )
    cleaned = re.sub(r"\b(?:visible|hidden)\b", "", cleaned, flags=re.I)
    cleaned = re.sub(r"\bclick\s+to\s+flip\b", "", cleaned, flags=re.I)
    cleaned = re.sub(r"\b(?:front|back)\s+of\s+card\b", "", cleaned, flags=re.I)
    cleaned = clean_text(cleaned)
    noisy = {
        "icon",
        "x",
        "x mark",
        "close",
        "button",
        "click to flip",
        "zoom image",
        "start",
        "next",
        "previous",
        "submit",
        "take again",
        "play",
        "play video",
        "pause",
        "mute",
        "unmute",
        "fullscreen",
        "captions",
        "picture-in-picture",
        "playback rate 1x",
    }
    if re.fullmatch(r"\d+", cleaned):
        return ""
    return "" if cleaned.lower() in noisy else cleaned


def clean_marker_label(text: str) -> str:
    cleaned = clean_button_text(text)
    cleaned = re.sub(r"\b(?:zoom\s+image|select|click)\b", "", cleaned, flags=re.I)
    cleaned = clean_text(cleaned.strip(" :-"))
    noisy = {"marker", "hotspot", "pin", "point"}
    return "" if cleaned.lower() in noisy else cleaned


def collapse_marker_duplicate_labels(markdown: str) -> str:
    lines = markdown.splitlines()
    output: list[str] = []
    marker_label: str | None = None

    for line in lines:
        stripped = line.strip()
        match = re.fullmatch(r"\*\*Marker \d+:\*\* (.+)", stripped)
        if match:
            marker_label = clean_text(match.group(1)).strip("*")
            output.append(line)
            continue

        duplicate = clean_text(stripped).strip("*")
        if marker_label and duplicate == marker_label:
            marker_label = None
            continue

        if stripped:
            marker_label = None
        output.append(line)

    return "\n".join(output)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        paths = asyncio.run(capture(args))
    except LoginRequired as exc:
        print(f"Login required: {exc}", file=sys.stderr)
        return 2
    if args.split_course_navigation:
        out_dir = Path(args.out).expanduser().resolve()
        indexes = sorted(out_dir.glob("**/Course Navigation.md"))
        print(f"Course split output: {out_dir}")
        for index in indexes:
            print(f"Course index: {index}")
        return 0
    print(f"Markdown: {paths.markdown}")
    print(f"PDF: {paths.pdf}")
    print(f"Screenshot: {paths.screenshot}")
    print(f"HTML: {paths.html}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
