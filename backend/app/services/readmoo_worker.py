# app/services/readmoo_worker.py
import os
import urllib.parse
from pathlib import Path
from playwright.async_api import Error as PlaywrightError, async_playwright
from sqlmodel import Session, select
from app.database import engine
from app.models import WishlistItem, Book, PlatformSession
from app.services.platform_auth import (
    get_platform_auth_cookies,
    launch_readmoo_browser,
    save_platform_storage_state,
    set_platform_session_status,
    verify_readmoo_storefront_session,
)
from app.services.wishlist_reconciliation import (
    deduplicate_remote_books,
    upsert_remote_wishlist_books,
)

BASE_DIR = Path(__file__).resolve().parent.parent.parent

# Readmoo rejects the authenticated reader site from a headless Chromium
# context.  Keep this aligned with readmoo_library_worker: Xvfb/noVNC provides
# the display in production, while PLAYWRIGHT_HEADLESS=true remains available
# for an explicit local diagnostic.
IS_HEADLESS = os.getenv("PLAYWRIGHT_HEADLESS", "False").lower() == "true"


async def _readmoo_wishlist_button_active(wishlist_btn) -> bool:
    return bool(await wishlist_btn.evaluate("""el => (
        el.classList.contains('active') ||
        el.classList.contains('mo-heart-fill') ||
        el.getAttribute('aria-pressed') === 'true' ||
        Boolean(el.querySelector('.mo-heart-fill'))
    )"""))


async def _wait_for_readmoo_wishlist_state(
    page,
    wishlist_btn,
    expected_active: bool,
    attempts: int = 20,
) -> bool:
    """Confirm the storefront actually persisted the requested heart state."""
    for _ in range(attempts):
        if await _readmoo_wishlist_button_active(wishlist_btn) == expected_active:
            return True
        await page.wait_for_timeout(500)
    return False

def get_user_state_path(user_id: str) -> Path:
    return BASE_DIR / "user_profiles" / user_id / "readmoo" / "state.json"

async def _execute_readmoo_wishlist_action(user_id: str, isbn: str, action: str):
    state_file_path = get_user_state_path(user_id)
    
    if not state_file_path.exists():
        print("[Readmoo Worker] 找不到憑證檔")
        _update_sync_status(user_id, isbn, "auth_expired")
        return

    book_title = None
    with Session(engine) as db:
        book = db.get(Book, isbn)
        if book:
            book_title = book.title

    async with async_playwright() as p:
        browser = await launch_readmoo_browser(p, headless=IS_HEADLESS)
        context = await browser.new_context(
            storage_state=str(state_file_path),
            viewport={"width": 1280, "height": 800}
        )
        page = await context.new_page()

        try:
            print(f"[Readmoo Worker] 開始處理待購動作: {action}")

            # 1. 前往首頁並檢查憑證是否過期
            await page.goto("https://readmoo.com/", wait_until="domcontentloaded", timeout=20000)
            await page.wait_for_timeout(2000)

            login_btn_count = await page.locator("a:has-text('登入'), button:has-text('登入')").locator("visible=true").count()
            if login_btn_count > 0:
                print("[Readmoo Worker] 偵測到憑證已過期！")
                _update_sync_status(user_id, isbn, "auth_expired")
                
                with Session(engine) as db:
                    session_record = db.exec(
                        select(PlatformSession).where(
                            PlatformSession.user_id == user_id,
                            PlatformSession.platform == "readmoo"
                        )
                    ).first()
                    if session_record:
                        session_record.status = "expired"
                        db.commit()
                return

            async def perform_homepage_search(keyword: str) -> str | None:
                print(f"[Readmoo Worker] 前往 Readmoo 首頁準備搜尋...")
                await page.goto("https://readmoo.com/", wait_until="domcontentloaded", timeout=20000)
                await page.wait_for_timeout(2000)

                search_input = page.locator("input[name='kw']:visible, input[type='search']:visible, input[placeholder*='搜尋']:visible").first
                
                if await search_input.count() == 0:
                    print(f"[Readmoo Worker] 找不到首頁的搜尋輸入框")
                    return None
                    
                print("[Readmoo Worker] 於搜尋框鍵入查詢")
                await search_input.click()
                await search_input.fill("")
                await search_input.type(keyword, delay=50)
                
                search_icon = page.locator("i.mo-search").locator("visible=true").first
                if await search_icon.count() > 0:
                    print(f"[Readmoo Worker] 點擊搜尋圖示送出（執行雙擊）")
                    await search_icon.click(force=True)
                    await page.wait_for_timeout(800)
                    await search_icon.click(force=True)
                else:
                    print(f"[Readmoo Worker] 未找到搜尋圖示，使用 Enter 送出")
                    await search_input.press("Enter")

                print(f"[Readmoo Worker] 等待搜尋結果...")
                await page.wait_for_timeout(3000)

                if book_title:
                    short_title = book_title[:4].strip()
                    selector = f"a[title*='{short_title}'], img[title*='{short_title}']"
                else:
                    selector = "a.product-link, img.js-lazy-image"

                try:
                    await page.wait_for_selector(selector, timeout=10000)
                except Exception:
                    print(f"[Readmoo Worker] 搜尋結果載入逾時或無符合項目")
                    return None

                target_element = page.locator(selector).first
                if await target_element.count() > 0:
                    href = await target_element.evaluate("el => el.tagName.toLowerCase() === 'a' ? el.href : el.closest('a').href")
                    if href:
                        return href if href.startswith("http") else f"https://readmoo.com{href}"
                return None

            target_url = await perform_homepage_search(isbn)

            if not target_url and book_title:
                print("[Readmoo Worker] ISBN 無結果，切換至純書名搜尋")
                target_url = await perform_homepage_search(book_title.strip())

            if target_url:
                print("[Readmoo Worker] 解析到書籍 URL，準備進入內頁")
                await page.goto(target_url, wait_until="domcontentloaded", timeout=15000)
                
                print(f"[Readmoo Worker] 等待內頁待購清單按鈕渲染...")
                try:
                    await page.wait_for_selector("button:has-text('待購清單')", timeout=10000)
                except Exception:
                    pass

                wishlist_btn = page.locator("button:has-text('待購清單')").locator("visible=true").first

                if await wishlist_btn.count() > 0:
                    print(f"[Readmoo Worker] 找到待購清單按鈕，準備判斷是否已在清單中...")
                    await page.wait_for_timeout(2500)

                    is_already_in_wishlist = (
                        await _readmoo_wishlist_button_active(wishlist_btn)
                    )

                    if action == "add":
                        if is_already_in_wishlist:
                            print(f"[Readmoo Worker] 該書籍已經在待購清單中 (愛心已填滿)。")
                            _update_sync_status(user_id, isbn, "synced")
                        else:
                            print(f"[Readmoo Worker] 執行點擊「加入待購清單」...")
                            await wishlist_btn.click(force=True)
                            if await _wait_for_readmoo_wishlist_state(
                                page,
                                wishlist_btn,
                                True,
                            ):
                                print(f"[Readmoo Worker] 成功加入待購清單。")
                                _update_sync_status(user_id, isbn, "synced")
                            else:
                                print("[Readmoo Worker] 點擊後未確認待購狀態已更新")
                                _update_sync_status(user_id, isbn, "failed")
                            
                    elif action == "remove":
                        if is_already_in_wishlist:
                            print(f"[Readmoo Worker] 執行再次點擊以「移除待購清單」...")
                            await wishlist_btn.click(force=True)
                            if await _wait_for_readmoo_wishlist_state(
                                page,
                                wishlist_btn,
                                False,
                            ):
                                print(f"[Readmoo Worker] 成功移除待購清單。")
                                _update_sync_status(user_id, isbn, "removed")
                            else:
                                print("[Readmoo Worker] 點擊後未確認待購狀態已移除")
                                _update_sync_status(user_id, isbn, "failed")
                        else:
                            print(f"[Readmoo Worker] 該書籍原本就不在待購清單中，無須移除。")
                            _update_sync_status(user_id, isbn, "removed")
                else:
                    print(f"[Readmoo Worker] 進入內頁後找不到待購清單按鈕")
                    _update_sync_status(user_id, isbn, "failed")
            else:
                print(f"[Readmoo Worker] 在 Readmoo 平台上找不到目標書籍")
                _update_sync_status(user_id, isbn, "not_available")

        except Exception as e:
            print(f"[Readmoo Worker] 執行過程發生例外錯誤: {e}")
            _update_sync_status(user_id, isbn, "failed")
        finally:
            await browser.close()

def _update_sync_status(user_id: str, isbn: str, status: str):
    try:
        with Session(engine) as db:
            statement = select(WishlistItem).where(
                WishlistItem.user_id == user_id,
                WishlistItem.isbn == isbn,
                WishlistItem.platform == "readmoo"
            )
            item = db.exec(statement).first()
            if item:
                item.sync_status = status
                db.commit()
                print(f"[Readmoo Worker] 資料庫狀態已更新為: {status}")
    except Exception as e:
        print(f"[Readmoo Worker] 更新資料庫狀態失敗: {e}")

async def add_to_readmoo_wishlist(user_id: str, isbn: str):
    await _execute_readmoo_wishlist_action(user_id, isbn, action="add")

async def remove_from_readmoo_wishlist(user_id: str, isbn: str):
    await _execute_readmoo_wishlist_action(user_id, isbn, action="remove")

async def _readmoo_import_page_status(page) -> str | None:
    """Return a non-success status when the wishlist page is not usable."""
    try:
        body = (await page.locator("body").inner_text()).casefold()
        cookies = await page.context.cookies()
    except PlaywrightError:
        return "parser_error"

    if any(
        marker in body
        for marker in (
            "max challenge attempts exceeded",
            "challenge attempts exceeded",
            "captcha challenge",
        )
    ):
        return "blocked"

    current_url = page.url.casefold()
    if any(token in current_url for token in ("signin", "login", "oauth2")):
        return "auth_required"

    strong_auth_cookie = any(
        cookie.get("name") in {"oauth_token", "oauth_refresh_token"}
        or (
            str(cookie.get("name", "")).startswith(
                "CognitoIdentityServiceProvider."
            )
            and str(cookie.get("name", "")).endswith(
                (".accessToken", ".idToken", ".refreshToken")
            )
        )
        for cookie in get_platform_auth_cookies(cookies, "readmoo")
    )
    return None if strong_auth_cookie else "auth_required"


async def _readmoo_explicitly_empty(page) -> bool:
    empty_selector = (
        ".cart-empty, .empty-cart, .empty-state, "
        "text=/待購清單.*(?:沒有|尚無|目前無)/"
    )
    try:
        return await page.locator(empty_selector).count() > 0
    except PlaywrightError:
        return False


async def import_readmoo_wishlist_to_db(user_id: str) -> dict:
    state_file_path = get_user_state_path(user_id)
    if not state_file_path.exists():
        print("[Readmoo Import] 找不到憑證檔，無法同步")
        set_platform_session_status(user_id, "readmoo", "expired")
        return {
            "platform": "readmoo",
            "status": "auth_required",
            "books": 0,
            "message": "找不到 Readmoo 登入憑證，請重新登入",
        }

    async with async_playwright() as p:
        browser = await launch_readmoo_browser(p, headless=IS_HEADLESS)
        context = await browser.new_context(storage_state=str(state_file_path))
        page = await context.new_page()

        remote_books = []

        try:
            print(f"[Readmoo Import] 開始同步遠端清單...")

            # The cart belongs to the Readmoo storefront. Confirm the stored
            # session on the storefront before entering the wishlist route.
            storefront_status = await verify_readmoo_storefront_session(page)
            if storefront_status == "blocked":
                set_platform_session_status(user_id, "readmoo", "blocked")
                print("[Readmoo Import] 官網首頁遭平台安全驗證拒絕，停止同步")
                return {
                    "platform": "readmoo",
                    "status": "blocked",
                    "books": 0,
                    "message": "Readmoo 安全驗證拒絕登入，請暫停重試",
                }
            if storefront_status != "active":
                set_platform_session_status(user_id, "readmoo", "expired")
                print("[Readmoo Import] 官網首頁登入驗證失敗，停止同步")
                return {
                    "platform": "readmoo",
                    "status": "auth_required",
                    "books": 0,
                    "message": "Readmoo 登入憑證已失效，請重新登入",
                }

            await save_platform_storage_state(
                context,
                state_file_path,
                "readmoo",
            )
            print("[Readmoo Import] 已驗證官網登入並更新 state.json")

            await page.goto("https://readmoo.com/checkout/cart#wishlist", wait_until="domcontentloaded", timeout=20000)
            await page.wait_for_timeout(1500)

            page_status = await _readmoo_import_page_status(page)
            if page_status == "blocked":
                set_platform_session_status(user_id, "readmoo", "blocked")
                print("[Readmoo Import] 平台安全驗證拒絕存取，停止同步")
                return {
                    "platform": "readmoo",
                    "status": "blocked",
                    "books": 0,
                    "message": "Readmoo 安全驗證拒絕登入，請暫停重試",
                }
            if page_status == "auth_required":
                # The storefront session was just verified. A login redirect here
                # means the wishlist route no longer accepts that session,
                # not that state.json is necessarily invalid.
                set_platform_session_status(user_id, "readmoo", "parser_error")
                print("[Readmoo Import] 待購頁要求登入，但官網 session 已驗證")
                return {
                    "platform": "readmoo",
                    "status": "parser_error",
                    "books": 0,
                    "message": "Readmoo 待購清單路徑無法使用目前已驗證的登入 session",
                }
            if page_status:
                set_platform_session_status(user_id, "readmoo", "parser_error")
                return {
                    "platform": "readmoo",
                    "status": "parser_error",
                    "books": 0,
                    "message": "無法確認 Readmoo 待購清單頁面狀態",
                }
            
            # 等待清單列表渲染完成
            try:
                await page.wait_for_selector("li.cart-list-item", timeout=10000)
            except PlaywrightError:
                print(f"[Readmoo Import] 等待 cart-list-item 逾時")
            
            await page.wait_for_timeout(2000)

            # 💡 直接以每一個獨立的清單項目 (li.cart-list-item) 作為迴圈單位，避免重複抓取
            book_items = page.locator("li.cart-list-item") 
            count = await book_items.count()
            if count == 0 and not await _readmoo_explicitly_empty(page):
                set_platform_session_status(user_id, "readmoo", "parser_error")
                print("[Readmoo Import] 找不到清單或明確空清單提示，停止同步")
                return {
                    "platform": "readmoo",
                    "status": "parser_error",
                    "books": 0,
                    "message": "Readmoo 頁面沒有可辨識的待購清單資料",
                }
            print(f"[Readmoo Import] 遠端頁面共找到 {count} 個獨立書籍項目")

            for i in range(count):
                item = book_items.nth(i)
                try:
                    # 在每個項目內精準抓取唯一的書名連結
                    title_el = item.locator("a.item-title-link").first
                    title = await title_el.inner_text() if await title_el.count() > 0 else ""
                    href = await title_el.get_attribute("href") if await title_el.count() > 0 else ""
                    
                    isbn = href.split("/")[-1] if href and "book/" in href else "UNKNOWN_ISBN"
                except PlaywrightError as error:
                    print(f"[Readmoo Import] 第 {i + 1} 筆資料解析失敗: {error}")
                    title = ""
                    isbn = "UNKNOWN_ISBN"

                if title and title.strip():
                    remote_books.append({"isbn": str(isbn).strip(), "title": title.strip()})

            remote_books = deduplicate_remote_books(remote_books, "readmoo")
            print(f"[Readmoo Import] 確認同步的書籍數: {len(remote_books)}")

            with Session(engine) as db:
                reconciliation = upsert_remote_wishlist_books(
                    db,
                    user_id=user_id,
                    platform="readmoo",
                    remote_books=remote_books,
                )
                db.commit()
            set_platform_session_status(user_id, "readmoo", "active")
            print(
                "[Readmoo Import] 資料庫同步完成！"
                f" 已移除 {reconciliation['removed']} 筆遠端不存在的同步項目"
            )
            return {
                "platform": "readmoo",
                "status": "success",
                "books": len(remote_books),
                "removed": reconciliation["removed"],
                "owned_filtered": reconciliation["owned_filtered"],
                "message": f"Readmoo 待購清單同步完成（{len(remote_books)} 本）",
            }

        except Exception as e:
            print(f"[Readmoo Import] 同步過程發生錯誤: {e}")
            set_platform_session_status(user_id, "readmoo", "parser_error")
            return {
                "platform": "readmoo",
                "status": "parser_error",
                "books": 0,
                "message": "Readmoo 待購清單同步失敗",
            }
        finally:
            await browser.close()
