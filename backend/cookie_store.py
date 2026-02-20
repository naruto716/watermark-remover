"""
Simple file-based cookie store for platform authentication.
Stores cookies per-platform so parsers can use authenticated sessions.
"""

import json
import os

COOKIE_FILE = os.path.join(os.path.dirname(__file__), "cookies.json")


def load_cookies() -> dict:
    """Load all platform cookies. Returns {platform: cookie_string}."""
    if not os.path.exists(COOKIE_FILE):
        return {}
    try:
        with open(COOKIE_FILE, "r") as f:
            return json.load(f)
    except (json.JSONDecodeError, IOError):
        return {}


def save_cookie(platform: str, cookie: str) -> None:
    """Save cookie string for a platform."""
    cookies = load_cookies()
    cookies[platform] = cookie.strip()
    with open(COOKIE_FILE, "w") as f:
        json.dump(cookies, f, ensure_ascii=False, indent=2)


def get_cookie(platform: str) -> str:
    """Get cookie string for a platform."""
    return load_cookies().get(platform, "")


def clear_cookie(platform: str) -> None:
    """Clear cookie for a platform."""
    cookies = load_cookies()
    cookies.pop(platform, None)
    with open(COOKIE_FILE, "w") as f:
        json.dump(cookies, f, ensure_ascii=False, indent=2)
