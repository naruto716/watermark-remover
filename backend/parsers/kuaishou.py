"""
Kuaishou (快手) watermark-free video parser.

Flow: share link → follow redirects → extract photoId → call API → extract video URL.
"""

import re
import json
import httpx

MOBILE_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) "
    "Version/17.0 Mobile/15E148 Safari/604.1"
)


class KuaishouParser:
    """Parse Kuaishou share links to extract watermark-free media."""

    @classmethod
    def can_handle(cls, url: str) -> bool:
        return any(
            p in url
            for p in ["kuaishou.com", "gifshow.com", "chenzhongtech.com"]
        )

    @classmethod
    async def parse(cls, url: str) -> dict:
        async with httpx.AsyncClient(
            follow_redirects=True,
            timeout=15.0,
            headers={"User-Agent": MOBILE_UA},
        ) as client:
            # Follow redirects to get the actual page
            resp = await client.get(url)
            final_url = str(resp.url)
            html = resp.text

            photo_id = cls._extract_photo_id(final_url, html)
            if not photo_id:
                raise ValueError("无法从链接中提取快手视频ID")

            return cls._parse_page_data(html, photo_id)

    @classmethod
    def _extract_photo_id(cls, url: str, html: str) -> str | None:
        # From URL
        m = re.search(r"/short-video/(\w+)", url)
        if m:
            return m.group(1)
        m = re.search(r"photoId=(\w+)", url)
        if m:
            return m.group(1)
        m = re.search(r"/fw/photo/(\w+)", url)
        if m:
            return m.group(1)

        # From page HTML / SSR data
        m = re.search(r'"photoId"\s*:\s*"(\w+)"', html)
        if m:
            return m.group(1)
        m = re.search(r'"photo_id"\s*:\s*"(\w+)"', html)
        if m:
            return m.group(1)
        return None

    @classmethod
    def _parse_page_data(cls, html: str, photo_id: str) -> dict:
        """Extract video info from the page's embedded JSON data."""
        title = "快手视频"
        cover = ""
        video_url = ""
        images = []
        content_type = "video"

        # Try to find Apollo state / SSR data in page
        # Pattern 1: window.__APOLLO_STATE__ or similar
        for pattern in [
            r'window\.__APOLLO_STATE__\s*=\s*({.+?})\s*;?\s*</script>',
            r'window\.__INITIAL_STATE__\s*=\s*({.+?})\s*;?\s*</script>',
            r'<script[^>]*>window\._PAGE_DATA_\s*=\s*({.+?})\s*;?\s*</script>',
        ]:
            m = re.search(pattern, html, re.DOTALL)
            if m:
                try:
                    data = json.loads(m.group(1))
                    result = cls._extract_from_state(data, photo_id)
                    if result:
                        return result
                except (json.JSONDecodeError, KeyError):
                    continue

        # Fallback: regex extraction from HTML
        m = re.search(r'"caption"\s*:\s*"([^"]*)"', html)
        if m:
            title = m.group(1)

        m = re.search(r'"poster"\s*:\s*"(https?://[^"]+)"', html)
        if not m:
            m = re.search(r'"coverUrl"\s*:\s*"(https?://[^"]+)"', html)
        if m:
            cover = m.group(1)

        # Video URL - look for watermark-free patterns
        for vp in [
            r'"srcNoMark"\s*:\s*"(https?://[^"]+)"',
            r'"photoUrl"\s*:\s*"(https?://[^"]+)"',
            r'"url"\s*:\s*"(https?://[^"]*\.mp4[^"]*)"',
            r'"playUrl"\s*:\s*"(https?://[^"]+)"',
        ]:
            m = re.search(vp, html)
            if m:
                video_url = m.group(1).replace("\\u002F", "/")
                break

        # Check for image posts
        img_matches = re.findall(r'"cdn_image_url"\s*:\s*"(https?://[^"]+)"', html)
        if not img_matches:
            img_matches = re.findall(r'"imageUrl"\s*:\s*"(https?://[^"]+)"', html)
        if img_matches and not video_url:
            images = [u.replace("\\u002F", "/") for u in img_matches]
            content_type = "images"

        return {
            "title": title,
            "cover": cover,
            "video_url": video_url or None,
            "images": images,
            "platform": "kuaishou",
            "type": content_type,
        }

    @classmethod
    def _extract_from_state(cls, data: dict, photo_id: str) -> dict | None:
        """Try to extract from structured Apollo/SSR state."""
        # Navigate nested structures - Kuaishou uses various formats
        photo = None

        # Try common paths
        for key in data:
            if isinstance(data[key], dict):
                if "photo" in data[key]:
                    photo = data[key]["photo"]
                    break
                if photo_id in str(data[key]):
                    # Deep search
                    photo = cls._find_photo(data[key], photo_id)
                    if photo:
                        break

        if not photo:
            return None

        title = photo.get("caption", photo.get("desc", "快手视频"))
        cover = ""
        for ck in ["coverUrl", "poster", "webpCoverUrl"]:
            if photo.get(ck):
                cover = photo[ck]
                break

        video_url = photo.get("srcNoMark", photo.get("photoUrl", ""))
        images = []
        content_type = "video"

        ext_photos = photo.get("ext_photo_list", photo.get("images", []))
        if ext_photos and not video_url:
            images = [
                p.get("cdn_image_url", p.get("url", ""))
                for p in ext_photos
                if isinstance(p, dict)
            ]
            content_type = "images"

        return {
            "title": title,
            "cover": cover,
            "video_url": video_url or None,
            "images": images,
            "platform": "kuaishou",
            "type": content_type,
        }

    @classmethod
    def _find_photo(cls, obj: dict, photo_id: str) -> dict | None:
        """Recursively find photo data matching the ID."""
        if isinstance(obj, dict):
            if obj.get("photoId") == photo_id or obj.get("photo_id") == photo_id:
                return obj
            for v in obj.values():
                result = cls._find_photo(v, photo_id)
                if result:
                    return result
        elif isinstance(obj, list):
            for item in obj:
                result = cls._find_photo(item, photo_id)
                if result:
                    return result
        return None
