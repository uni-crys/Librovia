from __future__ import annotations

import hashlib
from collections import defaultdict

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlmodel import Session, select

from app.database import engine, get_session
from app.models import Book, Purchase, WishlistItem
from app.observability import run_sync_job
from app.services.kobo_worker import (
    add_to_kobo_wishlist,
    import_kobo_wishlist_to_db,
    remove_from_kobo_wishlist,
)
from app.services.metadata_pipeline import (
    fetch_and_clean_metadata,
    is_valid_isbn,
    normalize_isbn,
)
from app.services.readmoo_worker import (
    add_to_readmoo_wishlist,
    import_readmoo_wishlist_to_db,
    remove_from_readmoo_wishlist,
)

router = APIRouter(tags=["Wishlist"])
SUPPORTED_PLATFORMS = {"readmoo", "kobo"}
RETRYABLE_WISHLIST_STATUSES = {"pending", "failed", "auth_expired"}


class WishlistCreate(BaseModel):
    user_id: str
    query: str = Field(
        min_length=1,
        description="ISBN 或書名",
    )


class WishlistTransfer(BaseModel):
    user_id: str
    isbns: list[str] = Field(min_length=1)
    platforms: list[str] = Field(min_length=1)


def _stable_title_identifier(title: str) -> str:
    digest = hashlib.sha256(title.casefold().encode("utf-8")).hexdigest()[:16]
    return f"title_{digest}"


def _has_known_author(metadata: dict) -> bool:
    author = str(metadata.get("author") or "").strip()
    return bool(
        author
        and author.casefold() not in {"未知作者", "unknown", "unkown"}
    )


def _has_known_category(metadata: dict) -> bool:
    category = str(
        metadata.get("standard_category")
        or metadata.get("category")
        or ""
    ).strip()
    return bool(
        category
        and category.casefold() not in {"未分類", "unknown", "unkown"}
    )


async def _refine_title_metadata_by_isbn(
    metadata: dict,
    resolved_isbn: str,
    original_title: str,
) -> dict:
    """Retry an incomplete title match by exact ISBN without merging editions."""
    if (
        not is_valid_isbn(resolved_isbn)
        or (_has_known_author(metadata) and _has_known_category(metadata))
    ):
        return metadata

    refined = await fetch_and_clean_metadata(
        isbn=resolved_isbn,
        raw_title=metadata.get("title") or original_title,
        author=(
            metadata.get("author")
            if _has_known_author(metadata)
            else None
        ),
    )
    refined_is_exact_edition = (
        refined.get("source")
        and refined.get("isbn_valid")
        and normalize_isbn(refined.get("isbn", "")) == resolved_isbn
    )
    if not refined_is_exact_edition:
        return metadata

    author_regressed = (
        _has_known_author(metadata)
        and not _has_known_author(refined)
    )
    category_regressed = (
        _has_known_category(metadata)
        and not _has_known_category(refined)
    )
    improved = (
        _has_known_author(refined) and not _has_known_author(metadata)
    ) or (
        _has_known_category(refined) and not _has_known_category(metadata)
    )
    if improved and not author_regressed and not category_regressed:
        return refined
    return metadata


async def _enrich_incomplete_wishlist_books(
    books: list[Book],
    session: Session,
) -> None:
    changed = False
    for book in books:
        current = {
            "author": book.author,
            "category": book.category,
        }
        if _has_known_author(current) and _has_known_category(current):
            continue

        valid_isbn = is_valid_isbn(book.isbn)
        metadata = await fetch_and_clean_metadata(
            isbn=book.isbn if valid_isbn else "",
            raw_title=book.title,
            author=book.author if _has_known_author(current) else None,
        )
        if not metadata.get("source"):
            continue
        if valid_isbn and not (
            metadata.get("isbn_valid")
            and normalize_isbn(metadata.get("isbn", "")) == book.isbn
        ):
            continue

        if not _has_known_author(current) and _has_known_author(metadata):
            book.author = metadata["author"]
            changed = True
        if not _has_known_category(current) and _has_known_category(metadata):
            book.category = (
                metadata.get("standard_category")
                or metadata["category"]
            )
            changed = True
        if not book.cover_url and metadata.get("cover_url"):
            book.cover_url = metadata["cover_url"]
            changed = True
        session.add(book)

    if changed:
        session.commit()


def _serialize_wishlist(
    items: list[WishlistItem],
    books_by_isbn: dict[str, Book],
) -> list[dict]:
    grouped: dict[str, list[WishlistItem]] = defaultdict(list)
    for item in items:
        grouped[item.isbn].append(item)

    result = []
    for isbn, platform_items in grouped.items():
        book = books_by_isbn.get(isbn)
        result.append({
            "isbn": isbn,
            "title": book.title if book else f"未命名書籍 ({isbn})",
            "author": book.author if book else "未知作者",
            "cover_url": book.cover_url if book else None,
            "category": book.category if book else "未分類",
            "updated_at": max(item.updated_at for item in platform_items),
            "platforms": [
                {
                    "platform": item.platform.lower(),
                    "status": item.sync_status,
                    "updated_at": item.updated_at,
                }
                for item in sorted(
                    platform_items,
                    key=lambda row: row.platform,
                )
            ],
        })
    return sorted(result, key=lambda row: row["title"])


async def _retry_wishlist_additions(
    user_id: str,
    platform: str,
    add_worker,
) -> dict[str, int]:
    """Retry local additions after a successful remote snapshot import."""
    with Session(engine) as db:
        candidates = db.exec(
            select(WishlistItem).where(
                WishlistItem.user_id == user_id,
                WishlistItem.platform == platform,
                WishlistItem.sync_status.in_(RETRYABLE_WISHLIST_STATUSES),
            )
        ).all()
        candidate_isbns = [item.isbn for item in candidates]

    for isbn in candidate_isbns:
        await add_worker(user_id, isbn)

    counts = {
        "attempted": len(candidate_isbns),
        "synced": 0,
        "not_available": 0,
        "failed": 0,
    }
    if not candidate_isbns:
        return counts

    with Session(engine) as db:
        retried_items = db.exec(
            select(WishlistItem).where(
                WishlistItem.user_id == user_id,
                WishlistItem.platform == platform,
                WishlistItem.isbn.in_(candidate_isbns),
            )
        ).all()
        for item in retried_items:
            status = item.sync_status
            if status == "synced":
                counts["synced"] += 1
            elif status == "not_available":
                counts["not_available"] += 1
            else:
                counts["failed"] += 1
    return counts


@router.post("/import")
async def trigger_wishlist_import(
    user_id: str = Query(..., description="使用者 ID"),
):
    # Run sequentially: each platform uses Chromium and a 4 GB VPS should not
    # launch both browser sessions at once.  The response preserves the exact
    # platform outcome so the UI never mistakes a failed login for an empty list.
    results = []
    for platform, importer, add_worker in (
        ("readmoo", import_readmoo_wishlist_to_db, add_to_readmoo_wishlist),
        ("kobo", import_kobo_wishlist_to_db, add_to_kobo_wishlist),
    ):
        result = await run_sync_job(
            "wishlist_import",
            platform,
            importer,
            user_id,
        )
        if result["status"] == "success":
            result = {
                **result,
                "retry": await _retry_wishlist_additions(
                    user_id,
                    platform,
                    add_worker,
                ),
            }
        results.append(result)
    statuses = {result["platform"]: result["status"] for result in results}
    blocked = [
        result["platform"] for result in results
        if result["status"] == "blocked"
    ]
    needs_login = [
        result["platform"] for result in results
        if result["status"] == "auth_required"
    ]
    failures = [
        result["platform"] for result in results
        if result["status"] == "parser_error"
    ]

    if blocked:
        message = f"{', '.join(blocked)} 被平台安全驗證拒絕，已停止同步"
    elif needs_login:
        message = f"{', '.join(needs_login)} 需要重新登入後才能同步"
    elif failures:
        message = f"{', '.join(failures)} 的待購清單頁面無法解析"
    else:
        message = "兩個平台的待購清單同步完成"

    return {
        "message": message,
        "results": results,
        "statuses": statuses,
        "needs_login": needs_login,
        "blocked": blocked,
    }


@router.get("/")
async def get_wishlist(
    user_id: str = Query(..., description="使用者 ID"),
    session: Session = Depends(get_session),
):
    items = session.exec(
        select(WishlistItem)
        .where(WishlistItem.user_id == user_id)
        .order_by(WishlistItem.updated_at.desc())
    ).all()
    if not items:
        return []

    isbns = {item.isbn for item in items}
    books = session.exec(
        select(Book).where(Book.isbn.in_(isbns))
    ).all()
    await _enrich_incomplete_wishlist_books(books, session)
    return _serialize_wishlist(
        items,
        {book.isbn: book for book in books},
    )


@router.post("/")
async def add_to_wishlist(
    item: WishlistCreate,
    background_tasks: BackgroundTasks,
    session: Session = Depends(get_session),
):
    query = item.query.strip()
    normalized = normalize_isbn(query)
    isbn_query = normalized if is_valid_isbn(normalized) else ""
    title_query = None if isbn_query else query

    metadata = await fetch_and_clean_metadata(
        isbn=isbn_query,
        raw_title=title_query,
    )
    if not metadata.get("source"):
        raise HTTPException(
            status_code=404,
            detail="找不到足以確認的書籍資料，請改用完整 ISBN 或更精確的書名",
        )

    identifiers = metadata.get("identifiers") or []
    resolved_isbn = (
        isbn_query
        or next(
            (
                normalize_isbn(value)
                for value in identifiers
                if is_valid_isbn(value)
            ),
            "",
        )
        or _stable_title_identifier(metadata.get("title") or query)
    )
    if title_query:
        metadata = await _refine_title_metadata_by_isbn(
            metadata,
            resolved_isbn,
            title_query,
        )

    book = session.get(Book, resolved_isbn)
    if not book:
        book = Book(
            isbn=resolved_isbn,
            title=metadata.get("title") or query,
            author=metadata.get("author") or "未知作者",
            cover_url=metadata.get("cover_url"),
            category=metadata.get("category") or "未分類",
        )
    else:
        if not book.author or book.author == "未知作者":
            book.author = metadata.get("author") or book.author
        if not book.cover_url:
            book.cover_url = metadata.get("cover_url")
        if not book.category or book.category == "未分類":
            book.category = metadata.get("category") or book.category
    session.add(book)

    existing_items = session.exec(
        select(WishlistItem).where(
            WishlistItem.user_id == item.user_id,
            WishlistItem.isbn == resolved_isbn,
        )
    ).all()
    existing_by_platform = {
        row.platform.lower(): row for row in existing_items
    }
    for platform in sorted(SUPPORTED_PLATFORMS):
        wishlist_item = existing_by_platform.get(platform)
        if wishlist_item:
            wishlist_item.sync_status = "pending"
        else:
            wishlist_item = WishlistItem(
                user_id=item.user_id,
                isbn=resolved_isbn,
                platform=platform,
                sync_status="pending",
            )
        session.add(wishlist_item)
    session.commit()

    background_tasks.add_task(
        add_to_readmoo_wishlist,
        item.user_id,
        resolved_isbn,
    )
    background_tasks.add_task(
        add_to_kobo_wishlist,
        item.user_id,
        resolved_isbn,
    )

    return {
        "message": "已加入待購清單，正在背景同步至 Readmoo 與 Kobo",
        "book": {
            "isbn": resolved_isbn,
            "title": book.title,
            "author": book.author,
            "cover_url": book.cover_url,
            "category": book.category,
            "platforms": [
                {"platform": "kobo", "status": "pending"},
                {"platform": "readmoo", "status": "pending"},
            ],
        },
    }


@router.post("/transfer")
async def transfer_to_library(
    payload: WishlistTransfer,
    background_tasks: BackgroundTasks,
    session: Session = Depends(get_session),
):
    isbns = list(dict.fromkeys(
        value.strip() for value in payload.isbns if value.strip()
    ))
    platforms = list(dict.fromkeys(
        value.strip().lower()
        for value in payload.platforms
        if value.strip()
    ))
    if not isbns:
        raise HTTPException(status_code=400, detail="請至少選擇一本書")
    if (
        not platforms
        or any(platform not in SUPPORTED_PLATFORMS for platform in platforms)
    ):
        raise HTTPException(status_code=400, detail="請選擇有效的購買平台")
    if len(isbns) > 1 and len(platforms) > 1:
        raise HTTPException(
            status_code=400,
            detail="批次移入多本書時只能選擇單一平台",
        )

    wishlist_items = session.exec(
        select(WishlistItem).where(
            WishlistItem.user_id == payload.user_id,
            WishlistItem.isbn.in_(isbns),
        )
    ).all()
    found_isbns = {item.isbn for item in wishlist_items}
    missing = [isbn for isbn in isbns if isbn not in found_isbns]
    if missing:
        raise HTTPException(
            status_code=404,
            detail=f"待購清單中找不到：{', '.join(missing)}",
        )

    existing_purchases = session.exec(
        select(Purchase).where(
            Purchase.user_id == payload.user_id,
            Purchase.isbn.in_(isbns),
            Purchase.platform.in_(platforms),
        )
    ).all()
    existing_keys = {
        (purchase.isbn, purchase.platform.lower())
        for purchase in existing_purchases
    }
    platform_ids = {
        (item.isbn, item.platform.lower()): (
            item.platform_book_id or item.isbn
        )
        for item in wishlist_items
    }
    for isbn in isbns:
        for platform in platforms:
            if (isbn, platform) not in existing_keys:
                session.add(Purchase(
                    user_id=payload.user_id,
                    platform=platform,
                    platform_book_id=platform_ids.get(
                        (isbn, platform),
                        isbn,
                    ),
                    isbn=isbn,
                ))

    removals = [
        (
            item.platform.lower(),
            item.platform_book_id or item.isbn,
        )
        for item in wishlist_items
    ]
    for wishlist_item in wishlist_items:
        session.delete(wishlist_item)
    session.commit()

    removal_workers = {
        "readmoo": remove_from_readmoo_wishlist,
        "kobo": remove_from_kobo_wishlist,
    }
    for platform, platform_book_id in removals:
        background_tasks.add_task(
            removal_workers[platform],
            payload.user_id,
            platform_book_id,
        )

    return {
        "message": f"已將 {len(isbns)} 本書移入我的書櫃",
        "transferred": len(isbns),
        "platforms": platforms,
    }


@router.delete("/{isbn}")
async def remove_from_wishlist(
    isbn: str,
    user_id: str,
    background_tasks: BackgroundTasks,
    session: Session = Depends(get_session),
):
    existing_items = session.exec(
        select(WishlistItem).where(
            WishlistItem.user_id == user_id,
            WishlistItem.isbn == isbn,
        )
    ).all()
    if not existing_items:
        return {"message": "待購清單中找不到該書籍，無需移除"}

    removals = [
        (
            item.platform.lower(),
            item.platform_book_id or item.isbn,
        )
        for item in existing_items
    ]
    for wishlist_item in existing_items:
        session.delete(wishlist_item)
    session.commit()

    removal_workers = {
        "readmoo": remove_from_readmoo_wishlist,
        "kobo": remove_from_kobo_wishlist,
    }
    for platform, platform_book_id in removals:
        background_tasks.add_task(
            removal_workers[platform],
            user_id,
            platform_book_id,
        )
    return {"message": "已從待購清單移除，正在背景同步兩個平台"}
