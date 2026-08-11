import os
import asyncio
import logging
from pathlib import Path
from difflib import SequenceMatcher
from urllib.parse import quote
from playwright.async_api import async_playwright
from sqlmodel import Session, select
from app.database import engine
from app.models import Book, Purchase
from app.services.library_import_queue import stage_library_snapshot
from app.services.metadata_pipeline import (
    is_valid_isbn,
    normalize_isbn,
    normalize_text,
    parse_readmoo_detail,
    parse_readmoo_search,
    split_classification,
)
from app.services.library_navigation import (
    is_readmoo_dashboard_url,
    is_readmoo_library_url,
    wait_for_stable_route,
)
from app.services.wishlist_reconciliation import deduplicate_remote_books
from app.services.platform_auth import (
    get_platform_auth_cookies,
    get_platform_state_path,
    launch_readmoo_browser,
    save_platform_storage_state,
    set_platform_session_status,
)

BASE_DIR = Path(__file__).resolve().parent.parent.parent
IS_HEADLESS = os.getenv("PLAYWRIGHT_HEADLESS", "False").lower() == "true"
LOGGER = logging.getLogger("librovia.readmoo_library")
READMOO_DETAIL_CONCURRENCY = 2


def _readmoo_api_cover(cover: object) -> str | None:
    if not isinstance(cover, dict):
        return None
    for size in ("large", "medium", "small"):
        value = cover.get(size)
        if isinstance(value, dict) and value.get("href"):
            return str(value["href"]).strip()
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def parse_readmoo_library_api(payload: object) -> dict[str, dict]:
    """Extract non-sensitive book metadata from Readmoo's library response."""

    if not isinstance(payload, dict):
        return {}
    included = payload.get("included")
    if not isinstance(included, list):
        return {}
    resources = {
        (str(item.get("type")), str(item.get("id"))): item
        for item in included
        if isinstance(item, dict) and item.get("type") and item.get("id")
    }
    result: dict[str, dict] = {}
    for item in included:
        if not isinstance(item, dict) or item.get("type") != "books":
            continue
        platform_id = str(item.get("id") or "").strip()
        attributes = item.get("attributes")
        relationships = item.get("relationships")
        if not platform_id or not isinstance(attributes, dict):
            continue
        relationships = relationships if isinstance(relationships, dict) else {}

        category_names = []
        for relationship_name in ("top_main_category", "categories"):
            relationship = relationships.get(relationship_name)
            data = relationship.get("data") if isinstance(relationship, dict) else None
            references = data if isinstance(data, list) else [data]
            for reference in references:
                if not isinstance(reference, dict):
                    continue
                resource = resources.get(
                    (str(reference.get("type")), str(reference.get("id")))
                )
                category_attributes = (
                    resource.get("attributes")
                    if isinstance(resource, dict)
                    else None
                )
                name = (
                    category_attributes.get("name")
                    if isinstance(category_attributes, dict)
                    else None
                )
                if name and str(name).strip() not in category_names:
                    category_names.append(str(name).strip())

        _, _, standard_category = split_classification(category_names)
        raw_isbn = str(attributes.get("isbn") or "").strip()
        metadata = {
            "title": str(attributes.get("title") or "").strip(),
            "platform_author": str(attributes.get("author") or "").strip(),
            "cover_url": _readmoo_api_cover(attributes.get("cover")),
        }
        if standard_category != "未分類":
            metadata["platform_category"] = standard_category
        if is_valid_isbn(raw_isbn):
            metadata["metadata_identifier"] = normalize_isbn(raw_isbn)
        result[platform_id] = {
            key: value for key, value in metadata.items() if value
        }
    return result


def _merge_readmoo_api_metadata(
    remote_books: list[dict],
    api_metadata: dict[str, dict],
) -> int:
    by_title = {
        normalize_text(metadata.get("title", "")): metadata
        for metadata in api_metadata.values()
        if normalize_text(metadata.get("title", ""))
    }
    enriched = 0
    for item in remote_books:
        metadata = api_metadata.get(str(item.get("isbn") or "").strip())
        if metadata is None:
            metadata = by_title.get(normalize_text(item.get("title", "")))
        if metadata is None:
            continue
        for key in (
            "metadata_identifier",
            "platform_author",
            "platform_category",
            "cover_url",
        ):
            if metadata.get(key):
                item[key] = metadata[key]
        enriched += 1
    return enriched


def _readmoo_items_needing_metadata(
    user_id: str,
    remote_books: list[dict],
) -> list[dict]:
    with Session(engine) as db:
        purchases = db.exec(
            select(Purchase).where(
                Purchase.user_id == user_id,
                Purchase.platform == "readmoo",
            )
        ).all()
        isbn_by_platform_id = {
            str(purchase.platform_book_id): purchase.isbn
            for purchase in purchases
            if purchase.platform_book_id
        }
        result = []
        for item in remote_books:
            isbn = isbn_by_platform_id.get(str(item["isbn"]))
            book = db.get(Book, isbn) if isbn else None
            item_has_metadata = (
                bool(item.get("platform_author"))
                and bool(item.get("platform_category"))
                and bool(item.get("cover_url"))
                and item.get("cover_url") != "/images/openbook.png"
            )
            if (
                not item_has_metadata
                and (
                    book is None
                    or not book.author
                    or book.author == "未知作者"
                    or book.category in {"未分類", "Unkown"}
                    or not book.cover_url
                    or book.cover_url == "/images/openbook.png"
                )
            ):
                result.append(item)
        return result


async def enrich_readmoo_public_metadata(
    browser,
    remote_books: list[dict],
) -> int:
    """Use an isolated, unauthenticated context for public product metadata."""

    if not remote_books:
        return 0
    public_context = await browser.new_context(
        viewport={"width": 1280, "height": 800},
        user_agent=(
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/122.0.0.0 Safari/537.36"
        ),
    )
    semaphore = asyncio.Semaphore(READMOO_DETAIL_CONCURRENCY)
    source_blocked = asyncio.Event()

    async def enrich(item: dict) -> bool:
        async with semaphore:
            if source_blocked.is_set():
                return False
            page = await public_context.new_page()
            try:
                raw_title = str(item["title"])
                response = await page.goto(
                    "https://readmoo.com/search/keyword"
                    f"?q={quote(raw_title)}&kw=&page=1",
                    wait_until="domcontentloaded",
                    timeout=30000,
                )
                if response is not None and response.status == 403:
                    source_blocked.set()
                    LOGGER.warning(
                        "Readmoo public metadata source blocked",
                        extra={
                            "platform": "readmoo",
                            "result": "http_403",
                        },
                    )
                    return False
                candidates = parse_readmoo_search(await page.content())
                candidate = max(
                    candidates,
                    key=lambda value: SequenceMatcher(
                        None,
                        normalize_text(raw_title),
                        normalize_text(value.title),
                    ).ratio(),
                    default=None,
                )
                if candidate is None:
                    return False
                similarity = SequenceMatcher(
                    None,
                    normalize_text(raw_title),
                    normalize_text(candidate.title),
                ).ratio()
                if similarity < 0.72 or not candidate.detail_url:
                    return False
                await page.goto(
                    candidate.detail_url,
                    wait_until="domcontentloaded",
                    timeout=30000,
                )
                detail = parse_readmoo_detail(await page.content())
                contributors = detail.get("contributors") or candidate.contributors
                author = next(
                    (
                        contributor.name
                        for contributor in contributors
                        if contributor.role == "作者"
                    ),
                    None,
                )
                categories = detail.get("raw_categories") or candidate.raw_categories
                _, _, standard_category = split_classification(categories)
                identifiers = detail.get("identifiers") or candidate.identifiers
                identifier = next(
                    (
                        normalize_isbn(value)
                        for value in identifiers
                        if is_valid_isbn(value)
                    ),
                    None,
                )
                if author:
                    item["platform_author"] = author
                if standard_category != "未分類":
                    item["platform_category"] = standard_category
                if identifier:
                    item["metadata_identifier"] = identifier
                if candidate.cover_url:
                    item["cover_url"] = candidate.cover_url
                return bool(author or identifier or standard_category != "未分類")
            except Exception:
                LOGGER.warning(
                    "Readmoo public detail metadata unavailable",
                    extra={"platform": "readmoo", "result": "detail_failed"},
                )
                return False
            finally:
                await page.close()

    try:
        results = await asyncio.gather(*(enrich(item) for item in remote_books))
        return sum(results)
    finally:
        await public_context.close()


async def _first_visible(page, selectors: tuple[str, ...]):
    """Select by semantic priority instead of document order."""
    for selector in selectors:
        matches = page.locator(selector)
        for index in range(await matches.count()):
            locator = matches.nth(index)
            if await locator.is_visible():
                return locator
    return None


def _canonical_isbn_by_platform_id(
    purchases: list[Purchase],
) -> dict[str, str]:
    return {
        str(purchase.platform_book_id).strip(): purchase.isbn
        for purchase in purchases
        if str(purchase.platform_book_id or "").strip()
    }


def get_user_state_path(user_id: str) -> Path:
    return get_platform_state_path(user_id, "readmoo")

async def import_readmoo_library_to_db(user_id: str, limit: int | None = None):
    effective_limit = limit if limit is not None and limit > 0 else None
    state_file_path = get_user_state_path(user_id)
    if not state_file_path.exists():
        set_platform_session_status(user_id, "readmoo", "expired")
        return {
            "platform": "readmoo",
            "status": "auth_required",
            "message": "找不到 Readmoo 登入憑證",
            "new_books": 0,
        }
    
    async with async_playwright() as p:
        browser = await launch_readmoo_browser(p, headless=IS_HEADLESS)
        
        context_kwargs = {
            "viewport": {"width": 1280, "height": 800},
            "storage_state": str(state_file_path),
        }
            
        context = await browser.new_context(**context_kwargs)
        page = await context.new_page()
        library_api_payloads: list[dict] = []
        library_api_tasks: set[asyncio.Task] = set()

        async def capture_library_api_response(response) -> None:
            if (
                "/store/v3/me/library_items" not in response.url
                or response.status != 200
            ):
                return
            try:
                payload = await response.json()
            except Exception:
                return
            if isinstance(payload, dict) and isinstance(
                payload.get("included"),
                list,
            ):
                library_api_payloads.append(payload)

        def schedule_library_api_capture(response) -> None:
            task = asyncio.create_task(capture_library_api_response(response))
            library_api_tasks.add(task)
            task.add_done_callback(library_api_tasks.discard)

        page.on("response", schedule_library_api_capture)

        remote_books = []
        new_books_count = 0
        updated_books_count = 0

        try:
            print(
                "[Readmoo Library Import] 開始同步 Readmoo 已購書櫃..."
                + (
                    f"（測試模式：最多 {effective_limit} 本）"
                    if effective_limit is not None
                    else ""
                )
            )
            
            # 1. 前往閱讀器首頁 / 總覽
            await page.goto(
                "https://next.readmoo.com/read/#/dashboard",
                wait_until="domcontentloaded",
                timeout=30000,
            )
            dashboard_status = await wait_for_stable_route(
                page,
                is_readmoo_dashboard_url,
                timeout_ms=180000,
            )
            if dashboard_status == "blocked":
                set_platform_session_status(user_id, "readmoo", "blocked")
                return {
                    "platform": "readmoo",
                    "status": "blocked",
                    "message": "Readmoo 要求完成人機驗證，已停止同步",
                    "new_books": 0,
                }
            if dashboard_status != "ready":
                set_platform_session_status(user_id, "readmoo", "parser_error")
                return {
                    "platform": "readmoo",
                    "status": "parser_error",
                    "message": "Readmoo 總覽頁仍在重載，已停止同步",
                    "new_books": 0,
                }

            # 2. 自動偵測登入
            current_url = page.url.lower()
            auth_cookies = get_platform_auth_cookies(
                await context.cookies(),
                "readmoo",
            )
            login_visible = await page.locator(
                "a:has-text('登入'):visible, button:has-text('登入'):visible"
            ).count()
            if (
                any(
                    token in current_url
                    for token in ("signin", "login", "oauth2")
                )
                or login_visible > 0
                or not auth_cookies
            ):
                print(
                    "[Readmoo Library Import] 登入憑證已失效"
                )
                set_platform_session_status(user_id, "readmoo", "expired")
                return {
                    "platform": "readmoo",
                    "status": "auth_required",
                    "message": "Readmoo 登入憑證已失效",
                    "new_books": 0,
                }

            set_platform_session_status(user_id, "readmoo", "active")

            await page.wait_for_timeout(2000)

            # 3. 展開「書櫃」accordion。本按鈕只展開選單，不改 URL。
            print("[Readmoo Library Import] 正在確認「書櫃」選單...")
            try:
                bookcase_button = await _first_visible(
                    page,
                    (
                        "button.accordion-button:has-text('書櫃')",
                        "button[aria-expanded]:has-text('書櫃')",
                    ),
                )
                if bookcase_button is None:
                    print(
                        "[Readmoo Library Import] 找不到可見的書櫃入口，"
                        f"目前 URL: {page.url}"
                    )
                    set_platform_session_status(user_id, "readmoo", "parser_error")
                    return {
                        "platform": "readmoo",
                        "status": "parser_error",
                        "message": "Readmoo 總覽頁找不到書櫃入口",
                        "new_books": 0,
                    }
                expanded = (
                    await bookcase_button.get_attribute("aria-expanded")
                ) == "true"
                if not expanded:
                    await bookcase_button.click()
                    await page.wait_for_timeout(500)
                    print("[Readmoo Library Import] ✅ 已展開「書櫃」選單")
                else:
                    print("[Readmoo Library Import] 「書櫃」選單已展開")
            except Exception as e:
                print(f"[Readmoo Library Import] 展開書櫃選單失敗: {e}")
                set_platform_session_status(user_id, "readmoo", "parser_error")
                return {
                    "platform": "readmoo",
                    "status": "parser_error",
                    "message": "Readmoo 無法展開書櫃選單",
                    "new_books": 0,
                }

            # 4. 只有在「書櫃」accordion 展開後，才點擊其中真正導航到
            # next.readmoo.com/read/#/library 的「書籍」連結。
            print("[Readmoo Library Import] 正在點擊「書籍」...")
            try:
                books_btn = await _first_visible(
                    page,
                    (
                        "a[href='#/library']:has-text('書籍')",
                        "a[href='/#/library']:has-text('書籍')",
                        "[role='tab']:has-text('書籍')",
                    ),
                )
                if books_btn is not None:
                    await books_btn.click()
                    print("[Readmoo Library Import] ✅ 已點擊「書籍」，等待 #/library")
                else:
                    print(
                        "[Readmoo Library Import] 書櫃選單內找不到"
                        f"可見的「書籍」分類，目前 URL: {page.url}"
                    )
                    set_platform_session_status(user_id, "readmoo", "parser_error")
                    return {
                        "platform": "readmoo",
                        "status": "parser_error",
                        "message": "Readmoo 書櫃找不到「書籍」分類",
                        "new_books": 0,
                    }
            except Exception as e:
                print(f"[Readmoo Library Import] 切換書籍分類失敗: {e}")
                set_platform_session_status(user_id, "readmoo", "parser_error")
                return {
                    "platform": "readmoo",
                    "status": "parser_error",
                    "message": "Readmoo 無法切換至「書籍」分類",
                    "new_books": 0,
                }

            library_status = await wait_for_stable_route(
                page,
                is_readmoo_library_url,
            )
            if library_status != "ready":
                print(
                    "[Readmoo Library Import] 點擊「書籍」後未穩定進入"
                    f" #/library，狀態: {library_status}，目前 URL: {page.url}"
                )
                status = "blocked" if library_status == "blocked" else "parser_error"
                set_platform_session_status(user_id, "readmoo", status)
                return {
                    "platform": "readmoo",
                    "status": status,
                    "message": "Readmoo 尚未穩定進入書籍頁面，已停止同步",
                    "new_books": 0,
                }
            print("[Readmoo Library Import] ✅ 已穩定進入 #/library 書籍頁面")

            # 5. 展開全部書籍
            print(f"[Readmoo Library Import] 正在展開並載入所有書籍...")
            for _ in range(30):
                await page.evaluate("window.scrollTo(0, document.body.scrollHeight);")
                await page.wait_for_timeout(1000)

                more_btn = page.locator("button:has-text('更多...')").first
                if await more_btn.count() > 0 and await more_btn.is_visible():
                    try:
                        await more_btn.click()
                        print(f"[Readmoo Library Import] 已點擊「更多...」載入更多書籍...")
                        await page.wait_for_timeout(1500)
                    except Exception:
                        break
                else:
                    break

            # 6. 解析書本區塊
            book_items = page.locator(".library-item")
            count = await book_items.count()
            print(f"[Readmoo Library Import] 展開完成，實際找到書本區塊數量: {count}")
            if count == 0:
                set_platform_session_status(user_id, "readmoo", "parser_error")
                return {
                    "platform": "readmoo",
                    "status": "parser_error",
                    "message": "Readmoo 書籍清單尚未完成載入",
                    "new_books": 0,
                }

            for i in range(count):
                item = book_items.nth(i)
                try:
                    title_el = item.locator(".title").first
                    title = ""
                    if await title_el.count() > 0:
                        title = await title_el.get_attribute("title")
                        if not title:
                            title = await title_el.inner_text()

                    img_el = item.locator("img.cover-img").first
                    cover_url = await img_el.get_attribute("src") if await img_el.count() > 0 else ""

                    reader_link = item.locator("a.reader-link").first
                    href = await reader_link.get_attribute("href") if await reader_link.count() > 0 else ""
                    
                    privacy_div = item.locator("div[id^='privacy-']").first
                    privacy_id = ""
                    if await privacy_div.count() > 0:
                        privacy_id = await privacy_div.get_attribute("id")

                    isbn = privacy_id.replace("privacy-", "") if privacy_id else (href.split("/")[-1] if href else "")

                    if title and title.strip():
                        remote_books.append({
                            "isbn": str(isbn).strip(),
                            "title": title.strip(),
                            "cover_url": cover_url.strip() if cover_url else None
                        })
                except Exception as e:
                    print(f"[Readmoo Library Import] 解析第 {i} 本書失敗: {e}")

            remote_books = deduplicate_remote_books(remote_books, "readmoo")
            print(f"[Readmoo Library Import] 成功解析 {len(remote_books)} 本已購書籍")

            # 7. 更新憑證
            if len(remote_books) > 0:
                if library_api_tasks:
                    await asyncio.gather(
                        *tuple(library_api_tasks),
                        return_exceptions=True,
                    )
                api_payload = max(
                    library_api_payloads,
                    key=lambda value: len(value.get("included", [])),
                    default={},
                )
                api_metadata = parse_readmoo_library_api(api_payload)
                api_enriched_count = _merge_readmoo_api_metadata(
                    remote_books,
                    api_metadata,
                )
                print(
                    "[Readmoo Library Import] 官方書櫃 API metadata 合併完成，"
                    f"取得 {api_enriched_count}/{len(remote_books)} 筆"
                )
                state_file_path.parent.mkdir(parents=True, exist_ok=True)
                await save_platform_storage_state(
                    context,
                    state_file_path,
                    "readmoo",
                )
                print(f"[Readmoo Library Import] 💡 已完美自動更新最新憑證至 state.json！")

                # 8. 寫入 DB (透過單一 Session 集中處理)
                with Session(engine) as db:
                    staged = stage_library_snapshot(
                        db,
                        user_id=user_id,
                        platform="readmoo",
                        remote_books=remote_books,
                        limit=effective_limit,
                    )
                new_books_count = staged["new_books"]
                updated_books_count = staged["updated_books"]
                print(
                    "[Readmoo Library Import] 原始書櫃寫入完成，"
                    f"新增 {new_books_count} 本，排入 "
                    f"{staged['metadata_jobs']} 筆 metadata 工作"
                )

            return {
                "platform": "readmoo",
                "status": "success",
                "message": "Readmoo 書櫃同步完成",
                "new_books": new_books_count,
                "updated_books": updated_books_count,
                "remote_books": len(remote_books),
                "metadata_jobs": staged["metadata_jobs"] if remote_books else 0,
            }
        except Exception:
            LOGGER.exception(
                "Readmoo library import failed",
                extra={"platform": "readmoo", "result": "failed"},
            )
            return {
                "platform": "readmoo",
                "status": "failed",
                "message": "Readmoo 書櫃同步失敗，請稍後再試",
                "new_books": new_books_count,
                "updated_books": updated_books_count,
            }
        finally:
            await browser.close()
