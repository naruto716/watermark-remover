"""
Xiaohongshu (小红书) watermark-free image/video parser.

Flow: share link → follow redirects → extract note_id → parse page data → extract media URLs.
"""

import re
import json
import httpx

MOBILE_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) "
    "Version/17.0 Mobile/15E148 Safari/604.1"
)

PC_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)


class XiaohongshuParser:
    """Parse Xiaohongshu share links to extract watermark-free media."""

    @classmethod
    def can_handle(cls, url: str) -> bool:
        return any(
            p in url
            for p in ["xiaohongshu.com", "xhslink.com"]
        )

    @classmethod
    async def parse(cls, url: str) -> dict:
        async with httpx.AsyncClient(
            follow_redirects=True,
            timeout=15.0,
            headers={
                "User-Agent": PC_UA,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            },
        ) as client:
            # Follow redirects to get the actual note page
            resp = await client.get(url)
            final_url = str(resp.url)
            html = resp.text

            note_id = cls._extract_note_id(final_url, url)
            if not note_id:
                note_id = cls._extract_note_id_from_html(html)

            if not note_id:
                raise ValueError("无法从链接中提取小红书笔记ID")

            return cls._parse_page(html, note_id)

    @classmethod
    def _extract_note_id(cls, *urls: str) -> str | None:
        for url in urls:
            # /explore/noteId or /discovery/item/noteId
            m = re.search(r"/(?:explore|discovery/item|item)/([a-f0-9]{24})", url)
            if m:
                return m.group(1)
            # note_id param
            m = re.search(r"note_id=([a-f0-9]{24})", url)
            if m:
                return m.group(1)
            # xhslink short URL won't have it, need redirect
        return None

    @classmethod
    def _extract_note_id_from_html(cls, html: str) -> str | None:
        m = re.search(r'"noteId"\s*:\s*"([a-f0-9]{24})"', html)
        if m:
            return m.group(1)
        m = re.search(r'"id"\s*:\s*"([a-f0-9]{24})"', html)
        if m:
            return m.group(1)
        return None

    @classmethod
    def _parse_page(cls, html: str, note_id: str) -> dict:
        """Extract note data from the page's embedded SSR state."""
        title = "小红书笔记"
        cover = ""
        video_url = None
        images = []
        content_type = "images"

        # XHS embeds data in window.__INITIAL_STATE__ or similar
        state_data = None
        for pattern in [
            r'window\.__INITIAL_STATE__\s*=\s*({.+?})\s*</script>',
            r'window\.__INITIAL_SSR_STATE__\s*=\s*({.+?})\s*</script>',
        ]:
            m = re.search(pattern, html, re.DOTALL)
            if m:
                try:
                    raw = m.group(1)
                    # XHS sometimes uses undefined as value
                    raw = raw.replace("undefined", "null")
                    state_data = json.loads(raw)
                    break
                except json.JSONDecodeError:
                    continue

        if state_data:
            result = cls._extract_from_state(state_data, note_id)
            if result:
                return result

        # Fallback: regex extraction
        # Title
        m = re.search(r'<title>([^<]+)</title>', html)
        if m:
            title = m.group(1).split(" - ")[0].strip()

        m = re.search(r'"desc"\s*:\s*"([^"]*)"', html)
        if m:
            title = m.group(1) or title

        # Images - XHS uses specific CDN patterns
        img_patterns = [
            r'"url"\s*:\s*"(https?://sns-webpic-qc[^"]+)"',
            r'"url"\s*:\s*"(https?://ci\.xiaohongshu\.com/[^"]+)"',
            r'"urlDefault"\s*:\s*"(https?://[^"]+)"',
        ]
        for ip in img_patterns:
            found = re.findall(ip, html)
            if found:
                # Deduplicate while preserving order
                seen = set()
                for u in found:
                    clean = u.replace("\\u002F", "/")
                    if clean not in seen:
                        seen.add(clean)
                        images.append(clean)
                break

        # Video
        m = re.search(r'"originVideoKey"\s*:\s*"([^"]+)"', html)
        if m:
            video_key = m.group(1)
            video_url = f"https://sns-video-bd.xhscdn.com/{video_key}"
            content_type = "video"
        else:
            for vp in [
                r'"url"\s*:\s*"(https?://sns-video[^"]+)"',
                r'"masterUrl"\s*:\s*"(https?://[^"]+\.mp4[^"]*)"',
            ]:
                m = re.search(vp, html)
                if m:
                    video_url = m.group(1).replace("\\u002F", "/")
                    content_type = "video"
                    break

        if images:
            cover = images[0]

        return {
            "title": title,
            "cover": cover,
            "video_url": video_url,
            "images": images if content_type == "images" else [],
            "platform": "xiaohongshu",
            "type": content_type,
        }

    @classmethod
    def _extract_from_state(cls, data: dict, note_id: str) -> dict | None:
        """Extract from structured SSR state."""
        note = None

        # Navigate XHS state structure
        # Common paths: data.note.noteDetailMap[noteId].note
        note_map = (
            data.get("note", {}).get("noteDetailMap", {})
            or data.get("noteDetail", {}).get("noteDetailMap", {})
        )

        if note_map:
            detail = note_map.get(note_id, {})
            note = detail.get("note", detail)

        if not note:
            # Try flat search
            note = cls._find_note(data, note_id)

        if not note or not isinstance(note, dict):
            return None

        title = note.get("title", note.get("desc", "小红书笔记"))
        note_type = note.get("type", "")

        # Images
        images = []
        image_list = note.get("imageList", note.get("images", []))
        if isinstance(image_list, list):
            for img in image_list:
                if isinstance(img, dict):
                    # Prefer original/large size
                    url = (
                        img.get("urlDefault")
                        or img.get("url")
                        or img.get("original")
                        or ""
                    )
                    if url:
                        images.append(url.replace("\\u002F", "/"))

        cover = images[0] if images else ""

        # Video
        video_url = None
        content_type = "images"
        video_data = note.get("video", {})
        if isinstance(video_data, dict) and video_data:
            # Get the best quality video
            media = video_data.get("media", {})
            stream = media.get("stream", {})

            # Try h264 streams first
            for quality in ["h264", "h265", "av1"]:
                streams = stream.get(quality, [])
                if isinstance(streams, list):
                    for s in streams:
                        if isinstance(s, dict):
                            master = s.get("masterUrl", s.get("url", ""))
                            if master:
                                video_url = master.replace("\\u002F", "/")
                                break
                    if video_url:
                        break

            if not video_url:
                # Fallback
                vkey = video_data.get("originVideoKey", "")
                if vkey:
                    video_url = f"https://sns-video-bd.xhscdn.com/{vkey}"

            if video_url:
                content_type = "video"

        return {
            "title": title,
            "cover": cover,
            "video_url": video_url,
            "images": images if content_type == "images" else [],
            "platform": "xiaohongshu",
            "type": content_type,
        }

    @classmethod
    def _find_note(cls, obj, note_id: str, depth: int = 0) -> dict | None:
        if depth > 8:
            return None
        if isinstance(obj, dict):
            if obj.get("noteId") == note_id or obj.get("id") == note_id:
                if "title" in obj or "desc" in obj or "imageList" in obj:
                    return obj
            for v in obj.values():
                result = cls._find_note(v, note_id, depth + 1)
                if result:
                    return result
        elif isinstance(obj, list):
            for item in obj[:50]:  # limit to avoid huge lists
                result = cls._find_note(item, note_id, depth + 1)
                if result:
                    return result
        return None
