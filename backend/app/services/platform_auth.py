import asyncio
import json
import os
import re
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from playwright.async_api import Error as PlaywrightError, async_playwright
from sqlmodel import Session, select

from app.database import engine
from app.models import PlatformSession


BASE_DIR = Path(__file__).resolve().parent.parent.parent
SUPPORTED_PLATFORMS = {"readmoo", "kobo"}
LOGIN_TIMEOUT_SECONDS = 180
READMOO_LOGIN_SETTLE_SECONDS = 8
READMOO_BROWSER_CHANNEL = os.getenv(
    "READMOO_BROWSER_CHANNEL",
    "",
).strip()
READMOO_BROWSER_PROXY = os.getenv(
    "READMOO_BROWSER_PROXY",
    "",
).strip()
_SAFE_USER_ID = re.compile(r"^[A-Za-z0-9_.-]+$")


class PlatformLoginTimeout(Exception):
    pass


class PlatformLoginBlocked(Exception):
    pass


async def launch_readmoo_browser(playwright, *, headless: bool):
    """Launch bundled Chromium or an explicitly configured stable channel."""
    launch_options = dict(
        headless=headless,
        args=["--disable-blink-features=AutomationControlled"],
    )
    if READMOO_BROWSER_CHANNEL:
        launch_options["channel"] = READMOO_BROWSER_CHANNEL
    if READMOO_BROWSER_PROXY:
        launch_options["proxy"] = {"server": READMOO_BROWSER_PROXY}
    return await playwright.chromium.launch(**launch_options)


def get_platform_auth_cookies(
    cookies: list[dict],
    platform: str,
) -> list[dict]:
    if platform == "readmoo":
        return [
            cookie for cookie in cookies
            if (
                cookie.get("name") in {
                    "oauth_token",
                    "oauth_refresh_token",
                    "readmoo",
                }
                or (
                    str(cookie.get("name", "")).startswith(
                        "CognitoIdentityServiceProvider."
                    )
                    and str(cookie.get("name", "")).endswith(
                        (".accessToken", ".idToken", ".refreshToken")
                    )
                )
            )
            and "readmoo.com" in str(cookie.get("domain", ""))
        ]
    if platform == "kobo":
        return [
            cookie for cookie in cookies
            if cookie.get("name") in {"KoboSession", "session"}
            and (
                "kobo.com" in str(cookie.get("domain", ""))
                or "kobobooks.com" in str(cookie.get("domain", ""))
            )
        ]
    return []


def _belongs_to_platform(host: str, platform: str) -> bool:
    host = host.lstrip(".").lower()
    if platform == "readmoo":
        return host == "readmoo.com" or host.endswith(".readmoo.com")
    if platform == "kobo":
        return (
            host == "kobo.com"
            or host.endswith(".kobo.com")
            or host == "kobobooks.com"
            or host.endswith(".kobobooks.com")
        )
    return False


async def save_platform_storage_state(
    context,
    state_path: Path,
    platform: str,
) -> dict:
    state = await context.storage_state()
    state["cookies"] = [
        cookie for cookie in state.get("cookies", [])
        if _belongs_to_platform(str(cookie.get("domain", "")), platform)
    ]
    state["origins"] = [
        origin for origin in state.get("origins", [])
        if _belongs_to_platform(
            urlparse(str(origin.get("origin", ""))).hostname or "",
            platform,
        )
    ]
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(
        json.dumps(state, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    return state


def get_platform_state_path(user_id: str, platform: str) -> Path:
    if platform not in SUPPORTED_PLATFORMS:
        raise ValueError("不支援的電子書平台")
    if not _SAFE_USER_ID.fullmatch(user_id):
        raise ValueError("使用者 ID 格式無效")
    return BASE_DIR / "user_profiles" / user_id / platform / "state.json"


def set_platform_session_status(
    user_id: str,
    platform: str,
    status: str,
) -> None:
    with Session(engine) as db:
        record = db.exec(
            select(PlatformSession).where(
                PlatformSession.user_id == user_id,
                PlatformSession.platform == platform,
            )
        ).first()
        if record is None:
            record = PlatformSession(
                user_id=user_id,
                platform=platform,
                status=status,
                updated_at=datetime.utcnow(),
            )
            db.add(record)
        else:
            record.status = status
            record.updated_at = datetime.utcnow()
        db.commit()


async def _is_logged_in(page, platform: str) -> bool:
    try:
        cookies = await page.context.cookies()
        auth_cookies = get_platform_auth_cookies(cookies, platform)

        if platform == "readmoo":
            strong_auth_cookie = any(
                cookie.get("name") in {
                    "oauth_token",
                    "oauth_refresh_token",
                }
                or (
                    str(cookie.get("name", "")).startswith(
                        "CognitoIdentityServiceProvider."
                    )
                    and str(cookie.get("name", "")).endswith(
                        (
                            ".accessToken",
                            ".idToken",
                            ".refreshToken",
                        )
                    )
                )
                for cookie in auth_cookies
            )
            if not strong_auth_cookie:
                return False

        current_url = page.url.lower()
        if any(
            token in current_url
            for token in ("signin", "login", "oauth2")
        ):
            return False

        if platform == "kobo":
            if "www.kobo.com/" not in current_url:
                return False
            return bool(await page.evaluate("""
                async () => {
                    try {
                        const response = await fetch(
                            "/tw/zh/account",
                            {
                                credentials: "include",
                                redirect: "follow"
                            }
                        );
                        return response.ok
                            && !/(signin|login|authorize)/i.test(
                                response.url
                            );
                    } catch {
                        return false;
                    }
                }
            """))

        login_visible = await page.locator(
            "a:has-text('登入'):visible, "
            "button:has-text('登入'):visible"
        ).count()
        return bool(auth_cookies) and login_visible == 0
    except PlaywrightError:
        # OAuth redirects can destroy the execution context between polls.
        return False


async def _readmoo_login_is_blocked(page) -> bool:
    try:
        body_text = (await page.locator("body").inner_text()).casefold()
    except PlaywrightError:
        return False
    return any(
        marker in body_text
        for marker in (
            "max challenge attempts exceeded",
            "challenge attempts exceeded",
            "captcha challenge",
        )
    )


async def verify_readmoo_reader_session(page) -> str:
    """Validate the post-login reader session before persisting state.json."""
    try:
        await page.goto(
            "https://read.readmoo.com/#/dashboard",
            wait_until="domcontentloaded",
            timeout=30000,
        )
        await page.wait_for_timeout(3000)
    except PlaywrightError:
        return "auth_required"

    if await _readmoo_login_is_blocked(page):
        return "blocked"
    if any(
        token in page.url.casefold()
        for token in ("signin", "login", "oauth2")
    ):
        return "auth_required"
    try:
        auth_cookies = get_platform_auth_cookies(
            await page.context.cookies(),
            "readmoo",
        )
        login_visible = await page.locator(
            "a:has-text('登入'):visible, button:has-text('登入'):visible"
        ).count()
        body_text = (await page.locator("body").inner_text()).strip()
    except PlaywrightError:
        return "auth_required"
    return "active" if (
        auth_cookies
        and login_visible == 0
        and len(body_text) > 20
    ) else "auth_required"


def _is_readmoo_dashboard_url(url: str) -> bool:
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


def _readmoo_storefront_callback_completed(page) -> bool:
    parsed = urlparse(page.url)
    hostname = (parsed.hostname or "").casefold()
    callback_key = parse_qs(parsed.query).get("key", [])
    return (
        _is_readmoo_dashboard_url(page.url)
        or (
            hostname in {"readmoo.com", "www.readmoo.com"}
            and "true" not in {value.casefold() for value in callback_key}
        )
    )


def _select_login_page(context, current_page, platform: str):
    """Follow a platform login when OAuth opens or redirects a new tab."""
    pages = [page for page in context.pages if not page.is_closed()]
    if platform == "readmoo":
        for candidate in reversed(pages):
            if _is_readmoo_dashboard_url(candidate.url):
                return candidate
    if current_page in pages:
        return current_page
    return pages[-1] if pages else current_page


async def verify_readmoo_storefront_session(
    page,
    *,
    navigate: bool = True,
) -> str:
    """Check the storefront before a wishlist import uses its cart route."""
    if navigate:
        try:
            await page.goto(
                "https://readmoo.com/",
                wait_until="domcontentloaded",
                timeout=30000,
            )
            await page.wait_for_timeout(3000)
        except PlaywrightError:
            return "auth_required"

    if await _readmoo_login_is_blocked(page):
        return "blocked"
    if any(
        token in page.url.casefold()
        for token in ("signin", "login", "oauth2")
    ):
        return "auth_required"
    try:
        auth_cookies = get_platform_auth_cookies(
            await page.context.cookies(),
            "readmoo",
        )
        login_visible = await page.locator(
            "a:has-text('登入'):visible, button:has-text('登入'):visible"
        ).count()
        body_text = (await page.locator("body").inner_text()).strip()
    except PlaywrightError:
        return "auth_required"
    return "active" if auth_cookies and login_visible == 0 and len(body_text) > 20 else "auth_required"


async def login_and_save_platform_state(
    user_id: str,
    platform: str,
) -> dict:
    state_path = get_platform_state_path(user_id, platform)
    state_path.parent.mkdir(parents=True, exist_ok=True)

    start_url = (
        "https://readmoo.com/"
        if platform == "readmoo"
        else "https://www.kobo.com/tw/zh"
    )

    async with async_playwright() as playwright:
        browser = await launch_readmoo_browser(playwright, headless=False)
        context = await browser.new_context(
            viewport={"width": 1280, "height": 800},
        )
        page = await context.new_page()
        status_recorded = False
        readmoo_cookie_seen_at: float | None = None

        try:
            await page.goto(
                start_url,
                wait_until="domcontentloaded",
                timeout=30000,
            )
            await page.wait_for_timeout(2000)

            deadline = asyncio.get_running_loop().time() + LOGIN_TIMEOUT_SECONDS
            while asyncio.get_running_loop().time() < deadline:
                page = _select_login_page(context, page, platform)
                if (
                    platform == "readmoo"
                    and await _readmoo_login_is_blocked(page)
                ):
                    raise PlatformLoginBlocked(
                        "Readmoo 拒絕此登入安全驗證，請停止重試並稍後再試"
                    )
                # Never navigate while the user is completing OAuth/login in
                # the visible browser.  Readmoo may require several attempts;
                # only leave its homepage after the authenticated state has
                # stayed stable for a short period.
                if not await _is_logged_in(page, platform):
                    readmoo_cookie_seen_at = None
                    await asyncio.sleep(1)
                    continue

                if platform == "readmoo":
                    now = asyncio.get_running_loop().time()
                    if readmoo_cookie_seen_at is None:
                        readmoo_cookie_seen_at = now
                        await asyncio.sleep(1)
                        continue
                    if now - readmoo_cookie_seen_at < READMOO_LOGIN_SETTLE_SECONDS:
                        await asyncio.sleep(1)
                        continue

                    # OAuth may expose cookies before Readmoo completes its
                    # `key=true` callback. Stay on the visible page until the
                    # browser naturally returns to the real storefront URL.
                    if not _readmoo_storefront_callback_completed(page):
                        await asyncio.sleep(1)
                        continue

                    # 1. Confirm that the storefront has consumed the login
                    # cookies, then 2. confirm that the reader subdomain can
                    # use the same browser context before saving it.
                    storefront_status = await verify_readmoo_storefront_session(
                        page,
                        navigate=False,
                    )
                    if storefront_status == "blocked":
                        raise PlatformLoginBlocked(
                            "Readmoo 拒絕此登入安全驗證，請停止重試並稍後再試"
                        )
                    if storefront_status != "active":
                        await asyncio.sleep(1)
                        continue

                    reader_status = await verify_readmoo_reader_session(page)
                    if reader_status == "blocked":
                        raise PlatformLoginBlocked(
                            "Readmoo 拒絕此登入安全驗證，請停止重試並稍後再試"
                        )
                    if reader_status != "active":
                        await asyncio.sleep(1)
                        continue

                state = await save_platform_storage_state(
                    context,
                    state_path,
                    platform,
                )
                if not state.get("cookies"):
                    raise RuntimeError("登入完成，但沒有取得可儲存的 Cookie")
                set_platform_session_status(user_id, platform, "active")
                status_recorded = True
                platform_label = "Readmoo" if platform == "readmoo" else "Kobo"
                return {
                    "status": "success",
                    "platform": platform,
                    "cookie_count": len(state["cookies"]),
                    "message": f"{platform_label} 登入憑證已更新",
                }
                await asyncio.sleep(1)

            raise PlatformLoginTimeout(
                "等待登入逾時，請重新操作並在三分鐘內完成登入"
            )
        except PlatformLoginBlocked:
            set_platform_session_status(user_id, platform, "blocked")
            status_recorded = True
            raise
        except PlatformLoginTimeout:
            set_platform_session_status(user_id, platform, "expired")
            status_recorded = True
            raise
        except Exception:
            set_platform_session_status(user_id, platform, "expired")
            status_recorded = True
            raise
        finally:
            # Also covers client disconnects/cancellation during login.
            if not status_recorded:
                set_platform_session_status(user_id, platform, "expired")
            await browser.close()
