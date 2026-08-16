# app/services/kobo_worker.py
import asyncio
import os
import urllib.parse
import json
import re
from pathlib import Path
from playwright.async_api import Error as PlaywrightError, async_playwright
from sqlmodel import Session, select
from app.database import engine
from app.models import WishlistItem, Book, PlatformSession
from app.services.platform_auth import (
    launch_kobo_browser,
    set_platform_session_status,
)
from app.services.wishlist_reconciliation import (
    deduplicate_remote_books,
    upsert_remote_wishlist_books,
)

BASE_DIR = Path(__file__).resolve().parent.parent.parent

# 透過環境變數控制，預設開啟隱藏模式
IS_HEADLESS = os.getenv("PLAYWRIGHT_HEADLESS", "True").lower() == "true"
KOBO_HUMAN_VERIFICATION_TIMEOUT_MS = 180000
KOBO_SEARCH_RESULT_SELECTOR = (
    ".item-detail a[href*='/ebook/'], "
    "article a[href*='/ebook/'], "
    "li a[href*='/ebook/'], "
    "a.title[href*='/ebook/'], "
    "h2 a[href*='/ebook/'], "
    "a[href*='/ebook/']"
)


class KoboHumanVerificationTimeout(RuntimeError):
    pass


async def _kobo_wishlist_button_active(wishlist_btn) -> bool:
    btn_html = await wishlist_btn.evaluate("el => el.outerHTML")
    btn_text = (await wishlist_btn.inner_text()).strip()
    return bool(
        "移除" in btn_text
        or "remove-from-wishlist" in btn_html
        or "已在" in btn_text
        or 'aria-pressed="true"' in btn_html
    )


async def _wait_for_kobo_wishlist_state(
    page,
    wishlist_btn,
    expected_active: bool,
    attempts: int = 20,
) -> bool:
    for _ in range(attempts):
        if await _kobo_wishlist_button_active(wishlist_btn) == expected_active:
            return True
        await page.wait_for_timeout(500)
    return False

def get_user_state_path(user_id: str) -> Path:
    return BASE_DIR / "user_profiles" / user_id / "kobo" / "state.json"


async def _execute_kobo_wishlist_action(user_id: str, isbn: str, action: str):
    state_file_path = get_user_state_path(user_id)
    
    if not state_file_path.exists():
        print("[Kobo Worker] 找不到憑證檔")
        _update_sync_status(user_id, isbn, "auth_expired")
        return

    book_title = None
    with Session(engine) as db:
        book = db.get(Book, isbn)
        if book:
            book_title = book.title

    async with async_playwright() as p:
        browser = await launch_kobo_browser(
            p,
            headless=IS_HEADLESS,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--disable-infobars",
                "--start-maximized",
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-web-security"
            ]
        )
        context = await browser.new_context(
            storage_state=str(state_file_path),
            viewport={"width": 1280, "height": 800},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
        )
        await context.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
        page = await context.new_page()

        try:
            print(f"[Kobo Worker] 開始處理待購動作: {action}")

            # 1. 進入首頁檢查是否過期
            await page.goto("https://www.kobo.com/tw/zh", wait_until="domcontentloaded", timeout=20000)
            if not await _wait_for_kobo_human_verification(page):
                _update_sync_status(user_id, isbn, "failed")
                return

            if "/signin" in page.url or await page.locator("a:has-text('登入')").locator("visible=true").count() > 0:
                print("[Kobo Worker] 偵測到憑證已過期！")
                _update_sync_status(user_id, isbn, "auth_expired")
                
                with Session(engine) as db:
                    session_record = db.exec(
                        select(PlatformSession).where(
                            PlatformSession.user_id == user_id,
                            PlatformSession.platform == "kobo"
                        )
                    ).first()
                    if session_record:
                        session_record.status = "expired"
                        db.commit()
                return

            search_url = (
                "https://www.kobo.com/tw/zh/search?query="
                f"{urllib.parse.quote(isbn)}"
            )
            print(f"[Kobo Worker] 進入搜尋頁面...")
            await page.goto(search_url, wait_until="domcontentloaded", timeout=20000)

            if "/ebook/" not in page.url:
                print(f"[Kobo Worker] 停留在搜尋列表，尋找書籍連結...")
                book_link = await _find_kobo_search_result(page, book_title)
                
                if book_link is None and book_title:
                    print(f"[Kobo Worker] ISBN 無結果，改用書名搜尋...")
                    search_url = f"https://www.kobo.com/tw/zh/search?query={urllib.parse.quote(book_title)}"
                    await page.goto(search_url, wait_until="domcontentloaded", timeout=20000)
                    book_link = await _find_kobo_search_result(
                        page,
                        book_title,
                    )
                
                if "/ebook/" not in page.url:
                    if book_link is not None:
                        print(f"[Kobo Worker] 找到搜尋列表中的書籍，進入商品內頁...")
                        if not await _open_kobo_search_result(page, book_link):
                            print("[Kobo Worker] 搜尋結果缺少可用的商品網址")
                            _update_sync_status(user_id, isbn, "failed")
                            return
                        if not await _wait_for_kobo_human_verification(page):
                            _update_sync_status(user_id, isbn, "failed")
                            return
                    else:
                        print(f"[Kobo Worker] 在 Kobo 平台上找不到目標書籍")
                        _update_sync_status(user_id, isbn, "not_available")
                        return
            
            print(f"[Kobo Worker] 已成功進入書籍內頁，準備操作願望清單...")

            print(f"[Kobo Worker] 等待按鈕渲染...")
            try:
                await page.wait_for_selector("text='願望清單'", timeout=10000)
            except Exception:
                pass

            wishlist_btn = page.locator("button:has-text('願望清單'), button:has-text('移除')").locator("visible=true").first

            if await wishlist_btn.count() > 0:
                btn_text = (await wishlist_btn.inner_text()).strip()
                is_already_in_wishlist = (
                    await _kobo_wishlist_button_active(wishlist_btn)
                )
                print(f"[Kobo Worker] 找到願望清單按鈕，當前文字: [{btn_text}]")

                if action == "add":
                    if is_already_in_wishlist:
                        print(f"[Kobo Worker] 該書籍原本就已在 Kobo 願望清單中。")
                        _update_sync_status(user_id, isbn, "synced")
                    else:
                        print(f"[Kobo Worker] 執行點擊「新增至願望清單」...")
                        await wishlist_btn.click(force=True)
                        if await _wait_for_kobo_wishlist_state(
                            page,
                            wishlist_btn,
                            True,
                        ):
                            print(f"[Kobo Worker] 已確認新增至願望清單！")
                            _update_sync_status(user_id, isbn, "synced")
                        else:
                            print("[Kobo Worker] 點擊後未確認願望清單狀態已更新")
                            _update_sync_status(user_id, isbn, "failed")

                elif action == "remove":
                    if is_already_in_wishlist:
                        print(f"[Kobo Worker] 執行點擊「移除」願望清單...")
                        await wishlist_btn.click(force=True)
                        if await _wait_for_kobo_wishlist_state(
                            page,
                            wishlist_btn,
                            False,
                        ):
                            print(f"[Kobo Worker] 已確認從願望清單移除！")
                            _update_sync_status(user_id, isbn, "removed")
                        else:
                            print("[Kobo Worker] 點擊後未確認願望清單狀態已移除")
                            _update_sync_status(user_id, isbn, "failed")
                    else:
                        print(f"[Kobo Worker] 該書籍原本就不在願望清單中，無須移除。")
                        _update_sync_status(user_id, isbn, "removed")
            else:
                print(f"[Kobo Worker] 進入內頁後找不到願望清單按鈕")
                _update_sync_status(user_id, isbn, "failed")

        except Exception as e:
            print(f"[Kobo Worker] 執行過程發生例外錯誤: {e}")
            _update_sync_status(user_id, isbn, "failed")
        finally:
            await browser.close()

def _update_sync_status(user_id: str, isbn: str, status: str):
    try:
        with Session(engine) as db:
            statement = select(WishlistItem).where(
                WishlistItem.user_id == user_id,
                WishlistItem.isbn == isbn,
                WishlistItem.platform == "kobo"
            )
            item = db.exec(statement).first()
            if item:
                item.sync_status = status
                db.commit()
                print(f"[Kobo Worker] 資料庫狀態已更新為: {status}")
    except Exception as e:
        print(f"[Kobo Worker] 更新資料庫狀態失敗: {e}")

# ==========================================
# 對外開放的呼叫介面
# ==========================================
async def add_to_kobo_wishlist(user_id: str, isbn: str):
    await _execute_kobo_wishlist_action(user_id, isbn, action="add")

async def remove_from_kobo_wishlist(user_id: str, isbn: str):
    await _execute_kobo_wishlist_action(user_id, isbn, action="remove")

async def _kobo_session_is_usable(page) -> bool:
    """Check the authenticated account endpoint, not just the presence of cookies."""
    if any(token in page.url.casefold() for token in ("signin", "login", "authorize")):
        return False
    try:
        return bool(await page.evaluate("""
            async () => {
                try {
                    const response = await fetch('/tw/zh/account', {
                        credentials: 'include', redirect: 'follow'
                    });
                    return response.ok
                        && !/(signin|login|authorize)/i.test(response.url);
                } catch {
                    return false;
                }
            }
        """))
    except PlaywrightError:
        return False


async def _kobo_human_verification_visible(page) -> bool:
    try:
        title = (await page.title()).casefold()
        body = (await page.locator("body").inner_text()).casefold()
        challenge_frame_count = await page.locator(
            "iframe[src*='challenge'], iframe[src*='turnstile']"
        ).count()
    except PlaywrightError:
        return False
    return (
        "just a moment" in title
        or "verify you are human" in body
        or "確認您是人類" in body
        or "執行安全性驗證" in body
        or challenge_frame_count > 0
    )


async def _wait_for_kobo_human_verification(page) -> bool:
    """Keep visible Chromium open while the user completes Kobo's CAPTCHA."""
    if not await _kobo_human_verification_visible(page):
        return True

    print(
        "[Kobo Import] 偵測到圖靈驗證；請在 noVNC 內手動勾選，"
        "最多等待三分鐘"
    )
    deadline = (
        asyncio.get_running_loop().time()
        + KOBO_HUMAN_VERIFICATION_TIMEOUT_MS / 1000
    )
    while asyncio.get_running_loop().time() < deadline:
        await page.wait_for_timeout(1000)
        if not await _kobo_human_verification_visible(page):
            await page.wait_for_timeout(1500)
            print("[Kobo Import] 圖靈驗證已完成，繼續同步")
            return True
    print("[Kobo Import] 等待圖靈驗證逾時")
    return False


def _normalize_kobo_search_text(value: str | None) -> str:
    return re.sub(r"[\W_]+", "", str(value or "").casefold())


async def _find_kobo_search_result(page, book_title: str | None = None):
    """Wait for Kobo's client-rendered search cards and return a product link."""
    if not await _wait_for_kobo_human_verification(page):
        raise KoboHumanVerificationTimeout(
            "Kobo human verification did not complete on search page"
        )
    try:
        await page.wait_for_selector(
            KOBO_SEARCH_RESULT_SELECTOR,
            state="attached",
            timeout=20000,
        )
    except Exception:
        return None

    links = page.locator(KOBO_SEARCH_RESULT_SELECTOR)
    count = await links.count()
    if count == 0:
        return None
    if not book_title:
        return links.nth(0)

    expected = _normalize_kobo_search_text(book_title)
    for index in range(count):
        link = links.nth(index)
        card_text = await link.evaluate("""el => {
            const card = el.closest('li, article, .item-detail, .book-item');
            return [
                card?.innerText || '',
                el.innerText || '',
                el.getAttribute('title') || '',
                el.getAttribute('aria-label') || ''
            ].join(' ');
        }""")
        if expected and expected in _normalize_kobo_search_text(card_text):
            return link
    return links.nth(0) if not book_title else None


async def _open_kobo_search_result(page, book_link) -> bool:
    """Navigate by href so hidden duplicate result links cannot break clicks."""
    href = await book_link.get_attribute("href")
    if not href:
        return False
    product_url = urllib.parse.urljoin(page.url, href)
    await page.goto(
        product_url,
        wait_until="domcontentloaded",
        timeout=40000,
    )
    return True


async def import_kobo_wishlist_to_db(user_id: str) -> dict:
    state_file_path = get_user_state_path(user_id)
    if not state_file_path.exists():
        print("[Kobo Import] 找不到憑證檔，無法同步")
        set_platform_session_status(user_id, "kobo", "expired")
        return {
            "platform": "kobo",
            "status": "auth_required",
            "books": 0,
            "message": "找不到 Kobo 登入憑證，請重新登入",
        }

    async with async_playwright() as p:
        browser = await launch_kobo_browser(
            p,
            headless=IS_HEADLESS,
        )
        context = await browser.new_context(
            storage_state=str(state_file_path),
            viewport={"width": 1280, "height": 800},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
        )
        await context.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
        page = await context.new_page()

        remote_books = []
        parsed_wishlist = False

        try:
            print(f"[Kobo Import] 開始同步遠端清單（透過內部 API）...")
            
            # 1. 先前往 Kobo 首頁或任意安全頁面建立 Cookie Session
            await page.goto("https://www.kobo.com/tw/zh", wait_until="domcontentloaded", timeout=20000)
            await page.wait_for_timeout(2000)
            if not await _wait_for_kobo_human_verification(page):
                set_platform_session_status(user_id, "kobo", "blocked")
                return {
                    "platform": "kobo",
                    "status": "blocked",
                    "books": 0,
                    "message": "Kobo 圖靈驗證尚未完成，請使用 noVNC 手動勾選",
                }
            if not await _kobo_session_is_usable(page):
                set_platform_session_status(user_id, "kobo", "expired")
                print("[Kobo Import] 偵測到登入頁或失效憑證，停止同步")
                return {
                    "platform": "kobo",
                    "status": "auth_required",
                    "books": 0,
                    "message": "Kobo 登入憑證已失效，請重新登入",
                }

            # 2. 直接在已經有登入 Cookie 的頁面環境下，用 fetch 呼叫官方的 wishlist fetch API
            print(f"[Kobo Import] 正在請求 Kobo 願望清單 API...")
            api_result = await page.evaluate("""async () => {
                try {
                    const response = await fetch('/tw/zh/account/wishlist/fetch', {
                        method: 'GET',
                        headers: {
                            'Accept': 'application/json, text/plain, */*'
                        }
                    });
                    if (response.ok) {
                        return await response.json();
                    }
                    return null;
                } catch (e) {
                    return null;
                }
            }""")

            if api_result and "Items" in api_result:
                parsed_wishlist = True
                items = api_result.get("Items", [])
                print(f"[Kobo Import] 透過 API 成功取得 {len(items)} 本書")
                
                for item in items:
                    isbn = item.get("ProductId", "UNKNOWN_ISBN")
                    title = item.get("Title", "未知書名")
                    if title and title != "未知書名":
                        remote_books.append({"isbn": str(isbn).strip(), "title": title.strip()})
            else:
                print(f"[Kobo Import] API 回傳資料格式不符或未取得內容，嘗試導向願望清單頁面...")
                # Kobo 首頁可能仍有延遲導向。使用獨立分頁，避免它中斷
                # wishlist 導航；若新頁也出現 CAPTCHA，保留視窗供手動完成。
                page = await context.new_page()
                await page.goto("https://www.kobo.com/tw/zh/account/wishlist", wait_until="domcontentloaded", timeout=20000)
                await page.wait_for_timeout(3000)
                if not await _wait_for_kobo_human_verification(page):
                    set_platform_session_status(user_id, "kobo", "blocked")
                    return {
                        "platform": "kobo",
                        "status": "blocked",
                        "books": 0,
                        "message": "Kobo 圖靈驗證尚未完成，請使用 noVNC 手動勾選",
                    }
                
                wishlist_gizmo = page.locator(".wishlist-page[data-kobo-gizmo='Wishlist']").first
                if await wishlist_gizmo.count() > 0:
                    config_str = await wishlist_gizmo.get_attribute("data-kobo-gizmo-config")
                    if config_str:
                        config_data = json.loads(config_str)
                        parsed_wishlist = "Items" in config_data
                        for item in config_data.get("Items", []):
                            isbn = item.get("ProductId", "UNKNOWN_ISBN")
                            title = item.get("Title", "未知書名")
                            if title and title != "未知書名":
                                remote_books.append({"isbn": str(isbn).strip(), "title": title.strip()})

            if not parsed_wishlist:
                set_platform_session_status(user_id, "kobo", "parser_error")
                print("[Kobo Import] 未取得可辨識的待購清單資料，停止同步")
                return {
                    "platform": "kobo",
                    "status": "parser_error",
                    "books": 0,
                    "message": "Kobo 頁面沒有可辨識的待購清單資料",
                }

            remote_books = deduplicate_remote_books(remote_books, "kobo")
            print(f"[Kobo Import] 確認同步的 Kobo 書籍數: {len(remote_books)}")

            with Session(engine) as db:
                reconciliation = upsert_remote_wishlist_books(
                    db,
                    user_id=user_id,
                    platform="kobo",
                    remote_books=remote_books,
                )
                db.commit()
            set_platform_session_status(user_id, "kobo", "active")
            print(
                "[Kobo Import] 資料庫同步完成！"
                f" 已移除 {reconciliation['removed']} 筆遠端不存在的同步項目"
            )
            return {
                "platform": "kobo",
                "status": "success",
                "books": len(remote_books),
                "removed": reconciliation["removed"],
                "owned_filtered": reconciliation["owned_filtered"],
                "message": f"Kobo 待購清單同步完成（{len(remote_books)} 本）",
            }

        except Exception as e:
            print(f"[Kobo Import] 同步過程發生錯誤: {e}")
            set_platform_session_status(user_id, "kobo", "parser_error")
            return {
                "platform": "kobo",
                "status": "parser_error",
                "books": 0,
                "message": "Kobo 待購清單同步失敗",
            }
        finally:
            await browser.close()
