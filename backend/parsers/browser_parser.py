"""
Browser-based parser using Playwright headless Chromium.
Renders pages like a real browser — bypasses SSR/cookie restrictions.
Supports: Douyin, Kuaishou, Xiaohongshu (video + image posts).
Uses saved cookies for authenticated access when available.
"""

import re
import json
import asyncio
import sys
import os
from playwright.async_api import async_playwright

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from cookie_store import get_cookie

PLATFORM_MAP = {
    "douyin": ["douyin.com", "iesdouyin.com"],
    "kuaishou": ["kuaishou.com", "gifshow.com", "chenzhongtech.com"],
    "xiaohongshu": ["xiaohongshu.com", "xhslink.com"],
}

# Singleton browser instance
_browser = None
_playwright = None
_lock = asyncio.Lock()


def detect_platform(url: str) -> str:
    for platform, domains in PLATFORM_MAP.items():
        if any(d in url for d in domains):
            return platform
    return "unknown"


def _clean_url(raw: str) -> str:
    raw = raw.strip()
    m = re.search(r'(https?://[^\s<>"\']+)', raw)
    return m.group(1) if m else raw


async def _get_browser():
    global _browser, _playwright
    async with _lock:
        if _browser is None or not _browser.is_connected():
            _playwright = await async_playwright().start()
            _browser = await _playwright.chromium.launch(headless=True)
    return _browser


class BrowserParser:
    """Parse social media links using headless Chromium."""

    @classmethod
    def can_handle(cls, url: str) -> bool:
        return any(
            d in url
            for domains in PLATFORM_MAP.values()
            for d in domains
        )

    @classmethod
    async def parse(cls, url: str) -> dict:
        url = _clean_url(url)
        platform = detect_platform(url)

        browser = await _get_browser()
        context = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
                "AppleWebKit/605.1.15 (KHTML, like Gecko) "
                "Version/17.0 Mobile/15E148 Safari/604.1"
            ),
            viewport={"width": 390, "height": 844},
            locale="zh-CN",
        )

        # Inject saved cookies if available
        cookie_str = get_cookie(platform)
        if cookie_str:
            cookies = cls._parse_cookie_string(cookie_str, platform)
            if cookies:
                await context.add_cookies(cookies)

        page = await context.new_page()
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=20000)
            # Wait a bit for JS to hydrate
            await page.wait_for_timeout(3000)

            if platform == "xiaohongshu":
                return await cls._parse_xhs(page, platform)
            elif platform == "douyin":
                return await cls._parse_douyin(page, platform)
            elif platform == "kuaishou":
                return await cls._parse_kuaishou(page, platform)
            else:
                raise ValueError(f"不支持的平台: {platform}")
        finally:
            await context.close()

    @classmethod
    async def _parse_xhs(cls, page, platform: str) -> dict:
        """Parse Xiaohongshu note page."""
        # Extract __INITIAL_STATE__ from the rendered page
        data = await page.evaluate("""() => {
            const state = window.__INITIAL_STATE__;
            if (!state) return null;
            return JSON.parse(JSON.stringify(state));
        }""")

        if data:
            result = cls._extract_xhs_from_state(data, platform)
            if result:
                return result

        # Fallback: scrape from DOM
        return await cls._scrape_xhs_dom(page, platform)

    @classmethod
    def _extract_xhs_from_state(cls, data: dict, platform: str) -> dict | None:
        """Extract from XHS __INITIAL_STATE__."""
        # Try noteData.data path (new structure)
        note_data = data.get("noteData", {}).get("data", {})
        if not note_data:
            # Try note.noteDetailMap path (old structure)
            note_map = data.get("note", {}).get("noteDetailMap", {})
            if note_map:
                first_key = next(iter(note_map), None)
                if first_key:
                    note_data = note_map[first_key].get("note", {})

        if not note_data:
            return None

        title = note_data.get("title", note_data.get("desc", ""))
        if not title:
            return None

        images = []
        image_list = note_data.get("imageList", [])
        if isinstance(image_list, list):
            for img in image_list:
                if isinstance(img, dict):
                    url = (
                        img.get("urlDefault")
                        or img.get("url")
                        or img.get("original")
                        or ""
                    )
                    if url:
                        images.append(url)

        video_url = None
        video_data = note_data.get("video", {})
        if isinstance(video_data, dict) and video_data:
            media = video_data.get("media", {})
            stream = media.get("stream", {})
            for codec in ["h264", "h265", "av1"]:
                streams = stream.get(codec, [])
                if isinstance(streams, list):
                    for s in streams:
                        if isinstance(s, dict):
                            master = s.get("masterUrl", s.get("url", ""))
                            if master:
                                video_url = master
                                break
                    if video_url:
                        break
            if not video_url:
                vkey = video_data.get("originVideoKey", "")
                if vkey:
                    video_url = f"https://sns-video-bd.xhscdn.com/{vkey}"

        content_type = "video" if video_url else "images"
        cover = images[0] if images else ""

        return {
            "title": title or "小红书笔记",
            "cover": cover,
            "video_url": video_url if content_type == "video" else None,
            "images": images if content_type == "images" else [],
            "platform": platform,
            "type": content_type,
        }

    @classmethod
    async def _scrape_xhs_dom(cls, page, platform: str) -> dict:
        """Fallback: scrape XHS data from rendered DOM."""
        result = await page.evaluate("""() => {
            const title = document.querySelector('#detail-title')?.textContent
                || document.querySelector('.title')?.textContent
                || document.querySelector('meta[name="og:title"]')?.content
                || document.title || '';

            // Images
            const imgs = [];
            const imgEls = document.querySelectorAll('.swiper-slide img, .note-image img, .carousel img');
            imgEls.forEach(img => {
                const src = img.src || img.dataset.src || '';
                if (src && !src.includes('avatar') && !src.includes('icon')) {
                    imgs.push(src);
                }
            });

            // Video
            let videoUrl = null;
            const videoEl = document.querySelector('video source, video');
            if (videoEl) {
                videoUrl = videoEl.src || videoEl.querySelector('source')?.src || null;
            }

            return { title, images: imgs, videoUrl };
        }""")

        images = result.get("images", [])
        video_url = result.get("videoUrl")
        content_type = "video" if video_url else "images"

        return {
            "title": result.get("title", "小红书笔记"),
            "cover": images[0] if images else "",
            "video_url": video_url,
            "images": images if content_type == "images" else [],
            "platform": platform,
            "type": content_type,
        }

    @classmethod
    async def _parse_douyin(cls, page, platform: str) -> dict:
        """Parse Douyin video/image page."""
        # Wait for video or image content to load
        await page.wait_for_timeout(2000)

        data = await page.evaluate("""() => {
            // Try to get from SSR state
            const state = window.__INITIAL_STATE__ || window.RENDER_DATA;
            if (state) return { state: JSON.parse(JSON.stringify(state)) };

            // Fallback: scrape DOM
            const title = document.querySelector('.video-info-detail .title')?.textContent
                || document.querySelector('meta[name="description"]')?.content
                || document.title || '';

            let videoUrl = null;
            const videoEl = document.querySelector('video');
            if (videoEl) videoUrl = videoEl.src || null;

            const imgs = [];
            document.querySelectorAll('.swiper-slide img, .image-list img').forEach(img => {
                if (img.src) imgs.push(img.src);
            });

            return { dom: { title, videoUrl, images: imgs } };
        }""")

        if data.get("state"):
            result = cls._extract_douyin_from_state(data["state"], platform)
            if result:
                return result

        dom = data.get("dom", {})
        images = dom.get("images", [])
        video_url = dom.get("videoUrl")
        content_type = "video" if video_url else "images"

        return {
            "title": dom.get("title", "抖音视频"),
            "cover": images[0] if images else "",
            "video_url": video_url,
            "images": images if content_type == "images" else [],
            "platform": platform,
            "type": content_type,
        }

    @classmethod
    def _extract_douyin_from_state(cls, state: dict, platform: str) -> dict | None:
        """Extract from Douyin SSR state."""
        # Navigate various Douyin state structures
        item = None

        # Try common paths
        for key in ["itemInfo", "videoData", "aweme"]:
            if key in state:
                item = state[key]
                if isinstance(item, dict) and "itemStruct" in item:
                    item = item["itemStruct"]
                break

        if not item:
            # Deep search for aweme_id
            item = cls._deep_find(state, "desc", max_depth=4)

        if not item:
            return None

        title = item.get("desc", item.get("title", ""))
        cover = ""
        covers = item.get("video", {}).get("cover", {}).get("url_list", [])
        if covers:
            cover = covers[0]

        # Images
        images_data = item.get("images", [])
        images = []
        if images_data and isinstance(images_data, list):
            for img in images_data:
                if isinstance(img, dict):
                    urls = img.get("url_list", [])
                    if urls:
                        images.append(urls[0])

        if images:
            return {
                "title": title or "抖音图集",
                "cover": cover or (images[0] if images else ""),
                "video_url": None,
                "images": images,
                "platform": platform,
                "type": "images",
            }

        # Video
        video_url = ""
        play_addr = item.get("video", {}).get("play_addr", {})
        urls = play_addr.get("url_list", [])
        if urls:
            video_url = urls[0].replace("playwm", "play")

        return {
            "title": title or "抖音视频",
            "cover": cover,
            "video_url": video_url or None,
            "images": [],
            "platform": platform,
            "type": "video",
        }

    @classmethod
    async def _parse_kuaishou(cls, page, platform: str) -> dict:
        """Parse Kuaishou video/image page."""
        await page.wait_for_timeout(2000)

        data = await page.evaluate("""() => {
            const state = window.__APOLLO_STATE__
                || window.__INITIAL_STATE__
                || window._PAGE_DATA_;
            if (state) return { state: JSON.parse(JSON.stringify(state)) };

            const title = document.querySelector('.video-info .title')?.textContent
                || document.title || '';
            let videoUrl = null;
            const videoEl = document.querySelector('video');
            if (videoEl) videoUrl = videoEl.src || null;

            return { dom: { title, videoUrl, images: [] } };
        }""")

        if data.get("state"):
            result = cls._extract_ks_from_state(data["state"], platform)
            if result:
                return result

        dom = data.get("dom", {})
        return {
            "title": dom.get("title", "快手视频"),
            "cover": "",
            "video_url": dom.get("videoUrl"),
            "images": [],
            "platform": platform,
            "type": "video",
        }

    @classmethod
    def _extract_ks_from_state(cls, state: dict, platform: str) -> dict | None:
        """Extract from Kuaishou state."""
        photo = cls._deep_find(state, "caption", max_depth=5)
        if not photo:
            photo = cls._deep_find(state, "srcNoMark", max_depth=5)
        if not photo:
            return None

        title = photo.get("caption", photo.get("desc", "快手视频"))
        video_url = photo.get("srcNoMark", photo.get("photoUrl", ""))
        cover = photo.get("coverUrl", photo.get("poster", ""))

        images = []
        ext_photos = photo.get("ext_photo_list", photo.get("images", []))
        if ext_photos and isinstance(ext_photos, list) and not video_url:
            for p in ext_photos:
                if isinstance(p, dict):
                    images.append(p.get("cdn_image_url", p.get("url", "")))
            images = [i for i in images if i]

        content_type = "images" if images and not video_url else "video"

        return {
            "title": title,
            "cover": cover,
            "video_url": video_url or None,
            "images": images if content_type == "images" else [],
            "platform": platform,
            "type": content_type,
        }

    @classmethod
    def _deep_find(cls, obj, key: str, max_depth: int = 4, depth: int = 0):
        """Find first dict containing the given key."""
        if depth > max_depth:
            return None
        if isinstance(obj, dict):
            if key in obj:
                return obj
            for v in obj.values():
                r = cls._deep_find(v, key, max_depth, depth + 1)
                if r:
                    return r
        elif isinstance(obj, list):
            for item in obj[:30]:
                r = cls._deep_find(item, key, max_depth, depth + 1)
                if r:
                    return r
        return None

    @classmethod
    def _parse_cookie_string(cls, cookie_str: str, platform: str) -> list[dict]:
        """Convert a cookie header string to Playwright cookie objects."""
        domain_map = {
            "xiaohongshu": ".xiaohongshu.com",
            "douyin": ".douyin.com",
            "kuaishou": ".kuaishou.com",
        }
        domain = domain_map.get(platform, f".{platform}.com")
        cookies = []
        for pair in cookie_str.split(";"):
            pair = pair.strip()
            if "=" in pair:
                name, _, value = pair.partition("=")
                cookies.append({
                    "name": name.strip(),
                    "value": value.strip(),
                    "domain": domain,
                    "path": "/",
                })
        return cookies
