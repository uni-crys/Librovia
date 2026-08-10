"""Guarded browser navigation for platform library imports."""

import asyncio
import time
from collections.abc import Callable
from urllib.parse import urlparse

from playwright.async_api import Error as PlaywrightError


BOT_CHALLENGE_MARKERS = (
    "verify you are human",
    "確認您是人類",
    "機器人",
    "captcha challenge",
    "challenge attempts exceeded",
    "access denied",
)


async def page_has_bot_challenge(page) -> bool:
    try:
        body_text = (await page.locator("body").inner_text()).casefold()
    except PlaywrightError:
        return False
    return any(marker in body_text for marker in BOT_CHALLENGE_MARKERS)


async def wait_for_stable_route(
    page,
    matches_route: Callable[[str], bool],
    *,
    timeout_ms: int = 45000,
    stable_polls: int = 3,
) -> str:
    """Wait until redirects/reloads stop and the expected route stays active."""
    deadline = time.monotonic() + timeout_ms / 1000
    last_url = ""
    consecutive = 0

    while time.monotonic() < deadline:
        if await page_has_bot_challenge(page):
            return "blocked"

        try:
            current_url = page.url
            body = (await page.locator("body").inner_text()).strip()
        except PlaywrightError:
            # A navigation destroyed the current execution context.  Wait for
            # the replacement document instead of acting on a stale page.
            consecutive = 0
            await asyncio.sleep(0.5)
            continue

        if (
            matches_route(current_url)
            and current_url == last_url
            and len(body) > 20
        ):
            consecutive += 1
            if consecutive >= stable_polls:
                return "ready"
        else:
            consecutive = 0
        last_url = current_url
        await asyncio.sleep(0.5)

    return "timeout"


def is_readmoo_dashboard_url(url: str) -> bool:
    parsed = urlparse(url)
    hostname = (parsed.hostname or "").casefold()
    fragment = parsed.fragment.casefold()
    return (
        hostname == "read.readmoo.com"
        and fragment.startswith("/dashboard")
    ) or (
        hostname == "next.readmoo.com"
        and parsed.path.rstrip("/").casefold() == "/read"
        and fragment.startswith("/dashboard")
    )


def is_readmoo_library_url(url: str) -> bool:
    parsed = urlparse(url)
    hostname = (parsed.hostname or "").casefold()
    fragment = parsed.fragment.casefold()
    return (
        hostname == "read.readmoo.com"
        and fragment.startswith("/library")
    ) or (
        hostname == "next.readmoo.com"
        and parsed.path.rstrip("/").casefold() == "/read"
        and fragment.startswith("/library")
    )


def is_kobo_home_url(url: str) -> bool:
    normalized = url.casefold().rstrip("/")
    return normalized.endswith("kobo.com/tw/zh")


def is_kobo_library_url(url: str) -> bool:
    return "/tw/zh/library/books" in url.casefold()
