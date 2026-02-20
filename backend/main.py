"""
Watermark-free media download API.

FastAPI backend that parses share links from Douyin, Kuaishou, and Xiaohongshu
to extract watermark-free video and image URLs.
"""

import re
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from slowapi import Limiter
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded

from parsers import DouyinParser, KuaishouParser, XiaohongshuParser, AggregatorParser, BrowserParser
from cookie_store import save_cookie, get_cookie, clear_cookie, load_cookies

# --- Rate limiter ---
limiter = Limiter(key_func=get_remote_address)


@asynccontextmanager
async def lifespan(app: FastAPI):
    yield


app = FastAPI(
    title="去水印下载工具 API",
    version="1.0.0",
    lifespan=lifespan,
)
app.state.limiter = limiter


# --- CORS ---
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# --- Rate limit error handler ---
@app.exception_handler(RateLimitExceeded)
async def rate_limit_handler(request: Request, exc: RateLimitExceeded):
    return JSONResponse(
        status_code=429,
        content={"success": False, "error": "请求过于频繁，请稍后再试"},
    )


# --- Models ---
class ParseRequest(BaseModel):
    url: str


class ParseResponse(BaseModel):
    success: bool
    title: str | None = None
    cover: str | None = None
    video_url: str | None = None
    images: list[str] = []
    platform: str | None = None
    type: str | None = None
    error: str | None = None


class CookieRequest(BaseModel):
    platform: str
    cookie: str = ""


# --- Parsers registry ---
# BrowserParser first (headless Chromium, most reliable — renders like real browser)
# AggregatorParser as fallback (third-party APIs)
# Local parsers as last resort
PARSERS = [BrowserParser, AggregatorParser, DouyinParser, KuaishouParser, XiaohongshuParser]


def _clean_url(raw: str) -> str:
    """Extract the first URL from a share text (may contain extra text)."""
    raw = raw.strip()
    m = re.search(r'(https?://[^\s<>"\']+)', raw)
    return m.group(1) if m else raw


# --- Routes ---
@app.post("/api/parse", response_model=ParseResponse)
@limiter.limit("30/minute")
async def parse_link(body: ParseRequest, request: Request):
    url = _clean_url(body.url)

    if not url.startswith("http"):
        return ParseResponse(success=False, error="请输入有效的链接")

    # Try each matching parser in order (fallback chain)
    last_error = "暂不支持该平台，目前支持：抖音、快手、小红书"
    for parser in PARSERS:
        if not parser.can_handle(url):
            continue
        try:
            result = await parser.parse(url)
            # Validate result has actual content
            if result.get("video_url") or result.get("images"):
                return ParseResponse(success=True, **result)
            # Got a result but no media — try next parser
            last_error = "解析成功但未找到媒体内容"
        except Exception as e:
            last_error = f"{parser.__name__}: {str(e)[:200]}"
            continue

    return ParseResponse(success=False, error=last_error)


@app.get("/api/health")
async def health():
    return {"status": "ok", "platforms": ["douyin", "kuaishou", "xiaohongshu"]}


@app.post("/api/cookies")
async def set_cookie(body: CookieRequest):
    """Save or clear cookies for a platform."""
    platform = body.platform.lower().strip()
    if platform not in ("douyin", "kuaishou", "xiaohongshu"):
        return {"success": False, "error": "不支持的平台"}
    if body.cookie.strip():
        save_cookie(platform, body.cookie)
        return {"success": True, "message": f"{platform} Cookie已保存"}
    else:
        clear_cookie(platform)
        return {"success": True, "message": f"{platform} Cookie已清除"}


@app.get("/api/cookies")
async def get_cookies():
    """Get which platforms have cookies configured."""
    all_cookies = load_cookies()
    return {
        "platforms": {
            p: bool(all_cookies.get(p))
            for p in ["douyin", "kuaishou", "xiaohongshu"]
        }
    }


# --- Serve frontend ---
import os

frontend_dir = os.path.join(os.path.dirname(__file__), "..", "frontend")
if os.path.isdir(frontend_dir):
    app.mount("/", StaticFiles(directory=frontend_dir, html=True), name="frontend")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
