import os
import asyncio
import json
import logging
import re
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urljoin
from urllib.parse import urlsplit
from bs4 import BeautifulSoup
import httpx
from playwright.async_api import async_playwright
from sqlmodel import Session, select
from app.database import engine
from app.models import Book, Purchase
from app.services.library_import_queue import stage_library_snapshot
from app.services.metadata_pipeline import (
    is_valid_isbn,
    normalize_isbn,
    split_classification,
)
from app.services.library_navigation import (
    is_kobo_home_url,
    is_kobo_library_url,
    wait_for_stable_route,
)
from app.services.wishlist_reconciliation import deduplicate_remote_books
from app.services.platform_auth import (
    get_platform_auth_cookies,
    get_platform_state_path,
    save_platform_storage_state,
    set_platform_session_status,
)

BASE_DIR = Path(__file__).resolve().parent.parent.parent
IS_HEADLESS = os.getenv("PLAYWRIGHT_HEADLESS", "True").lower() == "true"
LOGGER = logging.getLogger("librovia.kobo_library")
KOBO_DETAIL_CONCURRENCY = 3
KOBO_DETAIL_MAX_ATTEMPTS = 3
KOBO_DETAIL_RETRY_BASE_HOURS = 24
KOBO_CATEGORY_ALIASES = {
    "企業與金融": "商業理財",
    "傳記與回憶錄": "人文社科",
    "兒童": "文學小說",
    "語言文學": "文學小說",
    "小說與文學": "文學小說",
    "愛情": "文學小說",
    "期刊": "生活風格",
    "漫畫、圖畫小說和漫畫": "漫畫/圖文",
    "漫畫、圖像小說與連環漫畫": "漫畫/圖文",
    "神秘與懸疑": "文學小說",
    "科幻小說與奇幻小說": "文學小說",
    "青少年 - YA": "文學小說",
    # 「非小說」本身過於籠統，刻意不映射；由下一層分類決定。
    "健康與幸福": "醫療保健",
    "健康": "醫療保健",
    "鍛鍊": "醫療保健",
    "參考與語言": "人文社科",
    "娛樂": "藝術設計",
    "宗教與靈性": "人文社科",
    "家庭與園藝": "生活風格",
    "家庭與關係": "心理勵志",
    "旅行": "旅遊觀光",
    "社會與文化研究": "人文社科",
    "科學與自然": "自然科普",
    "藝術與建築": "藝術設計",
    "電腦": "電腦資訊",
}

KOBO_HIERARCHICAL_CATEGORY_ALIASES = {
    ("健康與幸福", "心理學"): "心理勵志",
    ("健康與幸福", "自助"): "心理勵志",
    ("健康與幸福", "健康"): "醫療保健",
    ("健康與幸福", "醫學"): "醫療保健",
    ("參考與語言", "法律"): "人文社科",
    ("參考與語言", "外國語言"): "語言學習",
    ("參考與語言", "研究輔助"): "考試用書",
}


class KoboLibrarySnapshotIncomplete(RuntimeError):
    """Raised when a paginated Kobo library page did not finish loading."""


async def _require_kobo_library_items(page, page_num: int) -> None:
    try:
        await page.wait_for_selector(
            "li.item-wrapper.book, .book-item",
            timeout=15000,
        )
    except Exception as error:
        raise KoboLibrarySnapshotIncomplete(
            f"Kobo 書櫃第 {page_num} 頁載入逾時，本次未寫入不完整快照"
        ) from error


def map_kobo_category(category_names: list[str] | None) -> str:
    categories = [
        str(category_name).strip()
        for category_name in category_names or []
        if str(category_name).strip()
    ]
    for (parent, child), mapped in KOBO_HIERARCHICAL_CATEGORY_ALIASES.items():
        if parent in categories and child in categories:
            if categories.index(parent) < categories.index(child):
                return mapped

    # Kobo's plain 「參考」 branch mixes unrelated subjects. Treat it as a
    # weak signal and prefer any additional, more specific category.
    if "參考與語言" in categories and "參考" in categories:
        specific_categories = [
            category
            for category in categories
            if category not in {"非小說", "參考與語言", "參考"}
        ]
        for category in specific_categories:
            if mapped := KOBO_CATEGORY_ALIASES.get(category):
                return mapped
        specific_mapping = split_classification(specific_categories)[2]
        if specific_mapping != "未分類":
            return specific_mapping
        return "人文社科"

    for category in categories:
        if mapped := KOBO_CATEGORY_ALIASES.get(category):
            return mapped
    return split_classification(categories)[2]


def extract_kobo_tracked_links(html: str) -> dict[str, object]:
    """Parse category/author tracking links without relying on rendered state."""

    categories: list[str] = []
    authors: list[str] = []
    soup = BeautifulSoup(html or "", "html.parser")
    for link in soup.select("a[data-track-info]"):
        try:
            tracking = json.loads(link.get("data-track-info") or "{}")
        except (json.JSONDecodeError, TypeError):
            continue
        description = tracking.get("description")
        text = link.get_text(" ", strip=True)
        if description == "category" and text:
            categories.append(text)
        elif description == "authorSearch":
            author = str(tracking.get("author") or text).strip()
            if author:
                authors.append(author)
    return {
        "categories": list(dict.fromkeys(categories)),
        "authors": list(dict.fromkeys(authors)),
    }


def extract_kobo_public_metadata(html: str) -> dict[str, str]:
    soup = BeautifulSoup(html or "", "html.parser")
    metadata_section = soup.select_one(".bookitem-secondary-metadata")
    page_text = (
        metadata_section.get_text(" ", strip=True)
        if metadata_section
        else soup.get_text(" ", strip=True)
    )
    tracked = extract_kobo_tracked_links(html)
    metadata = extract_kobo_detail_metadata(
        page_text,
        [
            script.get_text()
            for script in soup.select("script[type='application/ld+json']")
        ],
        list(tracked["categories"]),
    )
    if tracked["authors"]:
        metadata["author"] = "、".join(tracked["authors"])
    return metadata


def _apply_kobo_detail_metadata(item: dict, metadata: dict[str, str]) -> None:
    if metadata.get("isbn"):
        item["metadata_identifier"] = metadata["isbn"]
    if metadata.get("author"):
        item["platform_author"] = metadata["author"]
    if metadata.get("cover_url"):
        item["cover_url"] = metadata["cover_url"]
    if metadata.get("category"):
        item["platform_category"] = metadata["category"]


def extract_kobo_book_id(
    page_text: str,
    structured_data: list[str] | None = None,
) -> str | None:
    """Extract Kobo's ISBN-like Book ID from a product detail page."""

    def valid(value: object) -> str | None:
        normalized = normalize_isbn(str(value or ""))
        return normalized if is_valid_isbn(normalized) else None

    def walk(value: object):
        if isinstance(value, dict):
            for key, child in value.items():
                if key.casefold() in {"isbn", "bookid", "book_id"}:
                    if identifier := valid(child):
                        return identifier
                if identifier := walk(child):
                    return identifier
        elif isinstance(value, list):
            for child in value:
                if identifier := walk(child):
                    return identifier
        return None

    for payload in structured_data or []:
        try:
            if identifier := walk(json.loads(payload)):
                return identifier
        except (json.JSONDecodeError, TypeError):
            continue

    match = re.search(
        r"(?:書籍\s*ID|Book\s*ID|Boek-ID)\s*[:：]\s*"
        r"((?:97[89][\d\s-]{10,20})|(?:\d[\d\s-]{8,16}[\dXx]))",
        page_text or "",
        flags=re.IGNORECASE,
    )
    return valid(match.group(1)) if match else None


def extract_kobo_detail_metadata(
    page_text: str,
    structured_data: list[str] | None = None,
    category_names: list[str] | None = None,
) -> dict[str, str]:
    """Read explicitly labelled Kobo product metadata."""

    result: dict[str, str] = {}

    def author_name(value: object) -> str | None:
        if isinstance(value, str):
            return value.strip() or None
        if isinstance(value, dict):
            return str(value.get("name") or "").strip() or None
        if isinstance(value, list):
            names = [
                name
                for item in value
                if (name := author_name(item))
            ]
            return "、".join(names) or None
        return None

    for payload in structured_data or []:
        try:
            value = json.loads(payload)
        except (json.JSONDecodeError, TypeError):
            continue
        candidates = value if isinstance(value, list) else [value]
        for candidate in candidates:
            if not isinstance(candidate, dict):
                continue
            if author := author_name(candidate.get("author")):
                result.setdefault("author", author)
            image = candidate.get("image")
            if isinstance(image, dict):
                image = image.get("url")
            if isinstance(image, list):
                image = image[0] if image else None
            if image:
                result.setdefault("cover_url", str(image).strip())

    if identifier := extract_kobo_book_id(page_text, structured_data):
        result["isbn"] = identifier
    standard_category = map_kobo_category(category_names)
    if standard_category != "未分類":
        result["category"] = standard_category
    return result


async def enrich_kobo_book_ids(context, remote_books: list[dict]) -> int:
    """Visit Kobo detail pages with bounded concurrency and attach ISBNs."""

    semaphore = asyncio.Semaphore(KOBO_DETAIL_CONCURRENCY)
    public_client = httpx.AsyncClient(
        follow_redirects=True,
        timeout=20.0,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/122.0.0.0 Safari/537.36"
            ),
            "Accept-Language": "zh-TW,zh;q=0.9,en;q=0.7",
        },
    )

    async def enrich(item: dict) -> bool:
        detail_url = item.get("detail_url")
        if not detail_url:
            return False
        async with semaphore:
            try:
                response = await public_client.get(detail_url)
                public_metadata = (
                    extract_kobo_public_metadata(response.text)
                    if response.status_code == 200
                    else {}
                )
                _apply_kobo_detail_metadata(item, public_metadata)
                LOGGER.info(
                    "Kobo public detail fetched",
                    extra={
                        "platform": "kobo",
                        "result": (
                            "success"
                            if public_metadata.get("isbn")
                            else "incomplete"
                        ),
                        "status": response.status_code,
                        "path": urlsplit(str(response.url)).path,
                    },
                )
                if public_metadata.get("isbn"):
                    return True
            except httpx.HTTPError as error:
                LOGGER.warning(
                    "Kobo public detail unavailable",
                    extra={
                        "platform": "kobo",
                        "result": "transport_failed",
                        "error": type(error).__name__,
                    },
                )
            detail_page = await context.new_page()
            try:
                await detail_page.goto(
                    detail_url,
                    wait_until="domcontentloaded",
                    timeout=30000,
                )
                # Kobo lazily mounts the lower product widgets. The category
                # links may not exist until the viewport approaches the
                # metadata section.
                for scroll_ratio in (0.55, 1.0):
                    await detail_page.evaluate(
                        "(ratio) => window.scrollTo("
                        "0, document.body.scrollHeight * ratio)",
                        scroll_ratio,
                    )
                    await detail_page.wait_for_timeout(500)
                try:
                    await detail_page.wait_for_selector(
                        "a[data-track-info*='category']",
                        state="attached",
                        timeout=5000,
                    )
                except Exception:
                    # Some products genuinely have no category widget. Continue
                    # with ISBN/author/cover extraction after the bounded wait.
                    pass
                metadata_section = detail_page.locator(
                    ".bookitem-secondary-metadata"
                ).first
                page_text = (
                    await metadata_section.inner_text()
                    if await metadata_section.count() > 0
                    else await detail_page.locator("body").inner_text()
                )
                category_names = []
                category_links = detail_page.locator("a[data-track-info]")
                for index in range(await category_links.count()):
                    category_link = category_links.nth(index)
                    try:
                        tracking = json.loads(
                            await category_link.get_attribute(
                                "data-track-info"
                            )
                            or "{}"
                        )
                    except json.JSONDecodeError:
                        continue
                    if tracking.get("description") != "category":
                        continue
                    category_name = (await category_link.inner_text()).strip()
                    if category_name:
                        category_names.append(category_name)
                tracked_links = extract_kobo_tracked_links(
                    await detail_page.content()
                )
                category_names.extend(tracked_links["categories"])
                metadata = extract_kobo_detail_metadata(
                    page_text,
                    await detail_page.locator(
                        "script[type='application/ld+json']"
                    ).all_text_contents(),
                    category_names,
                )
                authors = list(tracked_links["authors"])
                if not metadata.get("author"):
                    author_elements = detail_page.locator(
                        "[itemprop='author'], "
                        ".bookitem-contributor a, "
                        ".contributor-name"
                    )
                    authors.extend(
                        value.strip()
                        for value in await author_elements.all_text_contents()
                        if value.strip()
                    )
                if authors:
                    metadata["author"] = "、".join(dict.fromkeys(authors))
                if not metadata.get("cover_url"):
                    image_meta = detail_page.locator(
                        "meta[property='og:image']"
                    ).first
                    if await image_meta.count() > 0:
                        metadata["cover_url"] = (
                            await image_meta.get_attribute("content") or ""
                        )
                _apply_kobo_detail_metadata(item, metadata)
                if metadata.get("isbn"):
                    return True
            except Exception:
                LOGGER.warning(
                    "Kobo detail metadata unavailable",
                    extra={"platform": "kobo", "result": "detail_failed"},
                )
            finally:
                await detail_page.close()
        return False

    try:
        results = await asyncio.gather(*(enrich(item) for item in remote_books))
        return sum(results)
    finally:
        await public_client.aclose()


def _canonical_isbn_by_platform_id(
    purchases: list[Purchase],
) -> dict[str, str]:
    """Map Kobo's stable product ID to the canonical local book key."""
    return {
        str(purchase.platform_book_id).strip(): purchase.isbn
        for purchase in purchases
        if str(purchase.platform_book_id or "").strip()
    }


def _kobo_detail_is_complete(
    purchase: Purchase | None,
    book: Book | None,
) -> bool:
    if purchase is None or book is None:
        return False
    return (
        is_valid_isbn(purchase.isbn)
        and bool(book.author)
        and book.author != "未知作者"
        and bool(book.cover_url)
        and book.cover_url != "/images/openbook.png"
        and book.category not in {"未分類", "Unknown", "Unkown"}
    )


def _kobo_detail_is_needed(
    purchase: Purchase | None,
    book: Book | None,
    *,
    now: datetime | None = None,
) -> bool:
    if _kobo_detail_is_complete(purchase, book):
        return False
    if purchase is None or book is None:
        return True
    if (
        purchase.detail_status == "manual_review"
        or purchase.detail_attempts >= KOBO_DETAIL_MAX_ATTEMPTS
    ):
        return False
    check_time = now or datetime.utcnow()
    return (
        purchase.detail_next_retry_at is None
        or purchase.detail_next_retry_at <= check_time
    )


def _record_kobo_detail_attempts(
    user_id: str,
    attempted_items: list[dict],
    *,
    now: datetime | None = None,
) -> None:
    if not attempted_items:
        return
    attempted_ids = {str(item["isbn"]) for item in attempted_items}
    attempted_at = now or datetime.utcnow()
    with Session(engine) as db:
        purchases = db.exec(
            select(Purchase).where(
                Purchase.user_id == user_id,
                Purchase.platform == "kobo",
                Purchase.platform_book_id.in_(attempted_ids),
            )
        ).all()
        for purchase in purchases:
            book = db.get(Book, purchase.isbn)
            if _kobo_detail_is_complete(purchase, book):
                purchase.detail_attempts = 0
                purchase.detail_status = "complete"
                purchase.detail_last_attempt_at = attempted_at
                purchase.detail_next_retry_at = None
                db.add(purchase)
                continue
            purchase.detail_attempts += 1
            purchase.detail_last_attempt_at = attempted_at
            if purchase.detail_attempts >= KOBO_DETAIL_MAX_ATTEMPTS:
                purchase.detail_status = "manual_review"
                purchase.detail_next_retry_at = None
            else:
                delay_hours = min(
                    KOBO_DETAIL_RETRY_BASE_HOURS
                    * (2 ** (purchase.detail_attempts - 1)),
                    7 * 24,
                )
                purchase.detail_status = "cooldown"
                purchase.detail_next_retry_at = (
                    attempted_at + timedelta(hours=delay_hours)
                )
            db.add(purchase)
        db.commit()


def _kobo_items_needing_detail(
    user_id: str,
    remote_books: list[dict],
    *,
    force_refresh: bool = False,
) -> list[dict]:
    if force_refresh:
        return list(remote_books)
    with Session(engine) as db:
        purchases = db.exec(
            select(Purchase).where(
                Purchase.user_id == user_id,
                Purchase.platform == "kobo",
            )
        ).all()
        by_platform_id = {
            str(purchase.platform_book_id): purchase
            for purchase in purchases
            if purchase.platform_book_id
        }
        return [
            item
            for item in remote_books
            if (
                _kobo_detail_is_needed(
                    purchase := by_platform_id.get(str(item["isbn"])),
                    db.get(Book, purchase.isbn) if purchase else None,
                )
            )
        ]


def get_user_state_path(user_id: str) -> Path:
    return get_platform_state_path(user_id, "kobo")

async def import_kobo_library_to_db(
    user_id: str,
    limit: int | None = None,
    *,
    force_detail_refresh: bool = False,
    detail_platform_book_ids: set[str] | None = None,
):
    effective_limit = limit if limit is not None and limit > 0 else None
    state_file_path = get_user_state_path(user_id)
    if not state_file_path.exists():
        print("[Kobo Library Import] 找不到憑證檔，無法同步已購書櫃")
        set_platform_session_status(user_id, "kobo", "expired")
        return {
            "platform": "kobo",
            "status": "auth_required",
            "message": "找不到 Kobo 登入憑證",
            "new_books": 0,
        }

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=IS_HEADLESS,
            args=["--disable-blink-features=AutomationControlled"]
        )
        context = await browser.new_context(
            storage_state=str(state_file_path),
            viewport={"width": 1280, "height": 800},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
        )
        await context.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
        page = await context.new_page()

        remote_books = []
        new_books_count = 0
        updated_books_count = 0

        try:
            print(
                "[Kobo Library Import] 開始同步 Kobo 已購書櫃..."
                + (
                    f"（測試模式：最多 {effective_limit} 本）"
                    if effective_limit is not None
                    else ""
                )
            )
            # Establish the authenticated storefront session first.  Jumping
            # directly into /library/books is prone to Kobo bot challenges.
            await page.goto(
                "https://www.kobo.com/tw/zh",
                wait_until="domcontentloaded",
                timeout=40000,
            )
            home_status = await wait_for_stable_route(page, is_kobo_home_url)
            if home_status == "blocked":
                set_platform_session_status(user_id, "kobo", "blocked")
                return {
                    "platform": "kobo",
                    "status": "blocked",
                    "message": "Kobo 要求完成人機驗證，請使用 noVNC 手動勾選",
                    "new_books": 0,
                }
            if home_status != "ready":
                set_platform_session_status(user_id, "kobo", "parser_error")
                return {
                    "platform": "kobo",
                    "status": "parser_error",
                    "message": "Kobo 首頁尚未穩定載入，已停止書櫃同步",
                    "new_books": 0,
                }

            auth_cookies = get_platform_auth_cookies(
                await context.cookies(),
                "kobo",
            )
            login_visible = await page.locator(
                "a:has-text('登入'):visible, button:has-text('登入'):visible"
            ).count()
            if (
                "/signin" in page.url.lower()
                or "/login" in page.url.lower()
                or login_visible > 0
                or not auth_cookies
            ):
                print(
                    "[Kobo Library Import] 登入憑證已失效"
                )
                set_platform_session_status(user_id, "kobo", "expired")
                return {
                    "platform": "kobo",
                    "status": "auth_required",
                    "message": "Kobo 登入憑證已失效",
                    "new_books": 0,
                }

            set_platform_session_status(user_id, "kobo", "active")

            await page.goto(
                "https://www.kobo.com/tw/zh/library/books",
                wait_until="domcontentloaded",
                timeout=40000,
            )
            library_status = await wait_for_stable_route(
                page,
                is_kobo_library_url,
            )
            if library_status == "blocked":
                set_platform_session_status(user_id, "kobo", "blocked")
                return {
                    "platform": "kobo",
                    "status": "blocked",
                    "message": "Kobo 書櫃觸發人機驗證，請使用 noVNC 手動勾選",
                    "new_books": 0,
                }
            if library_status != "ready":
                set_platform_session_status(user_id, "kobo", "parser_error")
                return {
                    "platform": "kobo",
                    "status": "parser_error",
                    "message": "Kobo 未能穩定進入書櫃，已停止同步",
                    "new_books": 0,
                }

            page_num = 1
            while True:
                print(f"[Kobo Library Import] 正在爬取第 {page_num} 頁...")
                
                await _require_kobo_library_items(page, page_num)

                await page.wait_for_timeout(3000)

                book_items = page.locator("li.item-wrapper.book, .book-item")
                count = await book_items.count()
                print(f"[Kobo Library Import] 第 {page_num} 頁找到 {count} 個書籍區塊")

                if count == 0:
                    break

                for i in range(count):
                    if (
                        effective_limit is not None
                        and len(remote_books) >= effective_limit
                    ):
                        break
                    item = book_items.nth(i)
                    try:
                        title_el = item.locator("h2.title a, .title a, a.title").first
                        title = await title_el.inner_text() if await title_el.count() > 0 else ""
                        href = (
                            await title_el.get_attribute("href")
                            if await title_el.count() > 0
                            else ""
                        )
                        
                        img_el = item.locator("img.book-image, img.cover-image").first
                        cover_url = await img_el.get_attribute("src") if await img_el.count() > 0 else ""
                        if cover_url and cover_url.startswith("//"):
                            cover_url = "https:" + cover_url

                        track_info_str = await item.get_attribute("data-track-info") or "{}"
                        try:
                            track_data = json.loads(track_info_str)
                        except json.JSONDecodeError:
                            track_data = {}
                        
                        product_id = track_data.get("productId", "UNKNOWN_ID")
                        
                        if product_id == "UNKNOWN_ID":
                            product_id = href.split("/")[-1] if href else ""

                        if title and title.strip():
                            remote_books.append({
                                "isbn": str(product_id).strip(),
                                "title": title.strip(),
                                "cover_url": cover_url.strip() if cover_url else None,
                                "detail_url": (
                                    urljoin("https://www.kobo.com", href)
                                    if href
                                    else None
                                ),
                            })
                    except Exception as e:
                        print(f"[Kobo Library Import] 解析書籍失敗: {e}")

                if (
                    effective_limit is not None
                    and len(remote_books) >= effective_limit
                ):
                    break
                next_btn = page.locator("a.next, .pagination .next, a[rel='next']").first
                if await next_btn.count() > 0 and await next_btn.is_visible():
                    parent_class = await next_btn.locator("..").get_attribute("class") or ""
                    next_class = await next_btn.get_attribute("class") or ""
                    if "disabled" in parent_class or "disabled" in next_class:
                        break
                    
                    print(f"[Kobo Library Import] 準備前往下一頁...")
                    await next_btn.click()
                    page_num += 1
                    await page.wait_for_timeout(4000)
                else:
                    print(f"[Kobo Library Import] 沒有找到下一頁按鈕，爬取結束。")
                    break

            remote_books = deduplicate_remote_books(remote_books, "kobo")
            print(f"[Kobo Library Import] 總共成功解析 {len(remote_books)} 本已購書籍")

            if len(remote_books) > 0:
                detail_books = _kobo_items_needing_detail(
                    user_id,
                    remote_books,
                    force_refresh=force_detail_refresh,
                )
                if detail_platform_book_ids is not None:
                    detail_books = [
                        item
                        for item in detail_books
                        if str(item["isbn"]) in detail_platform_book_ids
                    ]
                resolved_ids = await enrich_kobo_book_ids(
                    context,
                    detail_books,
                )
                print(
                    "[Kobo Library Import] 增量商品頁解析完成，"
                    f"取得 {resolved_ids}/{len(detail_books)} 筆"
                )
                with Session(engine) as db:
                    staged = stage_library_snapshot(
                        db,
                        user_id=user_id,
                        platform="kobo",
                        remote_books=remote_books,
                        limit=effective_limit,
                    )
                _record_kobo_detail_attempts(user_id, detail_books)
                new_books_count = staged["new_books"]
                updated_books_count = staged["updated_books"]
                print(
                    "[Kobo Library Import] 原始書櫃寫入完成，"
                    f"新增 {new_books_count} 本，排入 "
                    f"{staged['metadata_jobs']} 筆 metadata 工作"
                )

            await save_platform_storage_state(
                context,
                state_file_path,
                "kobo",
            )
            return {
                "platform": "kobo",
                "status": "success",
                "message": "Kobo 書櫃同步完成",
                "new_books": new_books_count,
                "updated_books": updated_books_count,
                "remote_books": len(remote_books),
                "metadata_jobs": staged["metadata_jobs"] if remote_books else 0,
            }
        except KoboLibrarySnapshotIncomplete as error:
            LOGGER.warning(
                str(error),
                extra={"platform": "kobo", "result": "incomplete_snapshot"},
            )
            return {
                "platform": "kobo",
                "status": "failed",
                "message": str(error),
                "new_books": 0,
                "updated_books": 0,
            }
        except Exception:
            LOGGER.exception(
                "Kobo library import failed",
                extra={"platform": "kobo", "result": "failed"},
            )
            return {
                "platform": "kobo",
                "status": "failed",
                "message": "Kobo 書櫃同步失敗，請稍後再試",
                "new_books": new_books_count,
                "updated_books": updated_books_count,
            }
        finally:
            await browser.close()
