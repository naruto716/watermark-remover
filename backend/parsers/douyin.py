"""
Douyin (抖音) watermark-free video/image parser.

Flow: share link → follow redirects → extract item_id → call detail API → extract media URLs.
"""

import re
import json
import httpx

MOBILE_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) "
    "Version/17.0 Mobile/15E148 Safari/604.1"
)

DETAIL_API = "https://www.iesdouyin.com/web/api/v2/aweme/iteminfo/"


class DouyinParser:
    """Parse Douyin share links to extract watermark-free media."""

    SHARE_PATTERNS = [
        r"v\.douyin\.com/\w+",
        r"www\.douyin\.com/video/(\d+)",
        r"www\.iesdouyin\.com/share/video/(\d+)",
    ]

    @classmethod
    def can_handle(cls, url: str) -> bool:
        return any(
            p in url
            for p in ["douyin.com", "iesdouyin.com"]
        )

    @classmethod
    async def parse(cls, url: str) -> dict:
        async with httpx.AsyncClient(
            follow_redirects=True,
            timeout=15.0,
            headers={"User-Agent": MOBILE_UA},
        ) as client:
            item_id = cls._extract_item_id(url)

            if not item_id:
                # Follow the share link redirect to get the real URL
                resp = await client.get(url)
                final_url = str(resp.url)
                item_id = cls._extract_item_id(final_url)

                if not item_id:
                    # Try extracting from page content
                    item_id = cls._extract_from_html(resp.text)

            if not item_id:
                raise ValueError("无法从链接中提取抖音视频ID")

            return await cls._fetch_detail(client, item_id)

    @classmethod
    def _extract_item_id(cls, url: str) -> str | None:
        # Direct video ID in URL path
        m = re.search(r"/video/(\d+)", url)
        if m:
            return m.group(1)
        m = re.search(r"modal_id=(\d+)", url)
        if m:
            return m.group(1)
        return None

    @classmethod
    def _extract_from_html(cls, html: str) -> str | None:
        # Try to find item ID in rendered page or SSR data
        m = re.search(r'"aweme_id"\s*:\s*"(\d+)"', html)
        if m:
            return m.group(1)
        m = re.search(r"itemId\s*[:=]\s*[\"'](\d+)[\"']", html)
        if m:
            return m.group(1)
        return None

    @classmethod
    async def _fetch_detail(cls, client: httpx.AsyncClient, item_id: str) -> dict:
        resp = await client.get(
            DETAIL_API,
            params={"item_ids": item_id},
            headers={
                "User-Agent": MOBILE_UA,
                "Referer": "https://www.douyin.com/",
            },
        )
        resp.raise_for_status()
        data = resp.json()

        items = data.get("item_list", [])
        if not items:
            raise ValueError("抖音API未返回视频数据，可能链接已失效")

        item = items[0]
        desc = item.get("desc", "抖音视频")
        cover = ""
        covers = item.get("video", {}).get("cover", {}).get("url_list", [])
        if covers:
            cover = covers[0]

        # Check if it's an image post (图集)
        images_data = item.get("images")
        if images_data:
            image_urls = []
            for img in images_data:
                url_list = img.get("url_list", [])
                if url_list:
                    image_urls.append(url_list[0])
            return {
                "title": desc,
                "cover": cover,
                "video_url": None,
                "images": image_urls,
                "platform": "douyin",
                "type": "images",
            }

        # Video: get watermark-free URL
        video_url = ""
        play_addr = item.get("video", {}).get("play_addr", {})
        url_list = play_addr.get("url_list", [])
        if url_list:
            # Replace watermark URL pattern
            video_url = url_list[0].replace("playwm", "play")

        if not video_url:
            # Fallback: try bit_rate list
            bit_rates = item.get("video", {}).get("bit_rate", [])
            if bit_rates:
                best = max(bit_rates, key=lambda x: x.get("bit_rate", 0))
                urls = best.get("play_addr", {}).get("url_list", [])
                if urls:
                    video_url = urls[0].replace("playwm", "play")

        return {
            "title": desc,
            "cover": cover,
            "video_url": video_url or None,
            "images": [],
            "platform": "douyin",
            "type": "video",
        }
