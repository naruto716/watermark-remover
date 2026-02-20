"""
Aggregator parser — uses multiple free third-party APIs as fallback chain.

Supports: Douyin, Kuaishou, Xiaohongshu, and many more platforms.
Handles both video and image (图集) content.
"""

import re
import httpx

MOBILE_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) "
    "Version/17.0 Mobile/15E148 Safari/604.1"
)

# Platform detection
PLATFORM_MAP = {
    "douyin": ["douyin.com", "iesdouyin.com"],
    "kuaishou": ["kuaishou.com", "gifshow.com", "chenzhongtech.com"],
    "xiaohongshu": ["xiaohongshu.com", "xhslink.com"],
    "bilibili": ["bilibili.com", "b23.tv"],
    "weibo": ["weibo.com", "weibo.cn"],
    "pipixia": ["pipix.com", "h5.pipix.com"],
    "tiktok": ["tiktok.com", "vm.tiktok.com"],
}


def detect_platform(url: str) -> str:
    for platform, domains in PLATFORM_MAP.items():
        if any(d in url for d in domains):
            return platform
    return "unknown"


def _clean_url(raw: str) -> str:
    """Extract the first URL from share text."""
    raw = raw.strip()
    m = re.search(r'(https?://[^\s<>"\']+)', raw)
    return m.group(1) if m else raw


class AggregatorParser:
    """
    Multi-API fallback parser.
    Tries multiple free APIs in order until one succeeds.
    """

    # API endpoints (free / no-key-required where possible)
    APIS = [
        {
            "name": "jx_api_1",
            "url": "https://api.pearktrue.cn/api/video/",
            "method": "GET",
            "params_key": "url",
            "parse": "_parse_pearktrue",
        },
        {
            "name": "jx_api_2",
            "url": "https://api.xn--7ovq36h.com/api/sp_jx/",
            "method": "GET",
            "params_key": "url",
            "parse": "_parse_generic_v1",
        },
        {
            "name": "jx_api_3",
            "url": "https://api.douyin.wtf/api/hybrid/video_data",
            "method": "GET",
            "params_key": "url",
            "parse": "_parse_douyin_wtf",
        },
    ]

    @classmethod
    def can_handle(cls, url: str) -> bool:
        """Accept any URL — the aggregator APIs support many platforms."""
        return url.startswith("http")

    @classmethod
    async def parse(cls, url: str) -> dict:
        url = _clean_url(url)
        platform = detect_platform(url)
        last_error = "所有解析接口均失败"

        async with httpx.AsyncClient(
            timeout=20.0,
            follow_redirects=True,
            headers={"User-Agent": MOBILE_UA},
        ) as client:
            for api in cls.APIS:
                try:
                    result = await cls._try_api(client, api, url, platform)
                    if result:
                        return result
                except Exception as e:
                    last_error = f"{api['name']}: {str(e)[:100]}"
                    continue

        raise ValueError(f"解析失败 — {last_error}")

    @classmethod
    async def _try_api(
        cls, client: httpx.AsyncClient, api: dict, url: str, platform: str
    ) -> dict | None:
        if api["method"] == "GET":
            resp = await client.get(api["url"], params={api["params_key"]: url})
        else:
            resp = await client.post(api["url"], json={api["params_key"]: url})

        if resp.status_code != 200:
            return None

        data = resp.json()
        parser_method = getattr(cls, api["parse"])
        return parser_method(data, platform)

    # --- Response parsers for different API formats ---

    @classmethod
    def _parse_pearktrue(cls, data: dict, platform: str) -> dict | None:
        """Parse pearktrue API response format."""
        if data.get("code") not in (200, 0, "200"):
            return None

        d = data.get("data", data)
        title = d.get("title", d.get("desc", ""))
        cover = d.get("cover", d.get("thumbnail", ""))
        video_url = d.get("url", d.get("video", d.get("video_url", "")))
        images = d.get("images", d.get("pics", d.get("image_list", [])))

        if not title and not video_url and not images:
            return None

        # Determine type
        if images and isinstance(images, list) and len(images) > 0:
            content_type = "images"
            # Some APIs return image objects instead of strings
            clean_images = []
            for img in images:
                if isinstance(img, str):
                    clean_images.append(img)
                elif isinstance(img, dict):
                    clean_images.append(
                        img.get("url", img.get("url_default", ""))
                    )
            images = [i for i in clean_images if i]
            if not cover and images:
                cover = images[0]
        else:
            content_type = "video"
            images = []

        if content_type == "video" and not video_url:
            return None

        return {
            "title": title or "未知标题",
            "cover": cover,
            "video_url": video_url if content_type == "video" else None,
            "images": images,
            "platform": platform,
            "type": content_type,
        }

    @classmethod
    def _parse_generic_v1(cls, data: dict, platform: str) -> dict | None:
        """Parse generic API format (status/code + data)."""
        status = data.get("status", data.get("code", 0))
        if status not in (101, 200, 0, True, "200", "101"):
            if data.get("success") is not True:
                return None

        d = data.get("data", data)
        title = d.get("title", d.get("work_title", d.get("desc", "")))
        cover = d.get("cover", d.get("work_cover", d.get("thumbnail", "")))
        video_url = d.get("url", d.get("work_url", d.get("video_url", "")))
        images = d.get("images", d.get("pics", d.get("image_list", [])))

        if not title and not video_url and not images:
            return None

        # Normalize images
        if isinstance(images, list) and images:
            clean_images = []
            for img in images:
                if isinstance(img, str):
                    clean_images.append(img)
                elif isinstance(img, dict):
                    clean_images.append(img.get("url", ""))
            images = [i for i in clean_images if i]

        if images:
            content_type = "images"
            if not cover and images:
                cover = images[0]
        else:
            content_type = "video"
            images = []

        if content_type == "video" and not video_url:
            return None

        return {
            "title": title or "未知标题",
            "cover": cover,
            "video_url": video_url if content_type == "video" else None,
            "images": images,
            "platform": platform,
            "type": content_type,
        }

    @classmethod
    def _parse_douyin_wtf(cls, data: dict, platform: str) -> dict | None:
        """Parse douyin.wtf API response."""
        if data.get("code") != 200 and data.get("status") != "success":
            return None

        d = data.get("data", data)
        if not d:
            return None

        title = d.get("desc", d.get("title", ""))
        cover = ""
        video_url = ""
        images = []

        # Cover
        cover_data = d.get("cover", d.get("video", {}).get("cover", {}))
        if isinstance(cover_data, dict):
            urls = cover_data.get("url_list", [])
            cover = urls[0] if urls else ""
        elif isinstance(cover_data, str):
            cover = cover_data

        # Check for images first
        image_data = d.get("images", d.get("image_post_info", {}).get("images", []))
        if image_data and isinstance(image_data, list):
            for img in image_data:
                if isinstance(img, dict):
                    url_list = img.get("url_list", [])
                    if url_list:
                        images.append(url_list[0])
                elif isinstance(img, str):
                    images.append(img)

        if images:
            content_type = "images"
            if not cover:
                cover = images[0]
        else:
            content_type = "video"
            # Video URL
            video_data = d.get("video", {})
            if isinstance(video_data, dict):
                play = video_data.get("play_addr", {})
                urls = play.get("url_list", [])
                if urls:
                    video_url = urls[0].replace("playwm", "play")

                if not video_url:
                    bit_rates = video_data.get("bit_rate", [])
                    if bit_rates:
                        best = max(bit_rates, key=lambda x: x.get("bit_rate", 0))
                        urls = best.get("play_addr", {}).get("url_list", [])
                        if urls:
                            video_url = urls[0].replace("playwm", "play")

        if content_type == "video" and not video_url:
            return None

        return {
            "title": title or "未知标题",
            "cover": cover,
            "video_url": video_url if content_type == "video" else None,
            "images": images,
            "platform": platform,
            "type": content_type,
        }
