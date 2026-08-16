"""Safe reconciliation of locally cached platform wishlists."""

import hashlib
import re

from sqlmodel import Session, select

from app.models import Book, Purchase, WishlistItem

_MISSING_IDENTIFIERS = {"", "none", "null", "unknown", "unknown_isbn", "undefined"}


def _normalized_title(value: str | None) -> str:
    """Use titles as a fallback because Kobo ProductId is not always an ISBN."""
    return "".join(
        character.casefold()
        for character in (value or "")
        if character.isalnum()
    )


def _title_identity_variants(value: str | None) -> set[str]:
    """Return conservative title variants used only for cross-platform identity."""

    normalized = _normalized_title(value)
    if not normalized:
        return set()

    variants = {normalized}
    without_series = normalized.replace("系列", "")
    if len(without_series) >= 8:
        variants.add(without_series)
    return variants


def _titles_are_same_book(left: str | None, right: str | None) -> bool:
    """Match exact titles or a distinctive title followed by a subtitle."""

    left_text = (left or "").strip()
    right_text = (right or "").strip()
    left_normalized = _normalized_title(left_text)
    right_normalized = _normalized_title(right_text)
    if not left_normalized or not right_normalized:
        return False
    if _title_identity_variants(left_text) & _title_identity_variants(right_text):
        return True

    shorter, longer = sorted(
        (left_text, right_text),
        key=lambda value: len(_normalized_title(value)),
    )
    if len(_normalized_title(shorter)) < 5:
        return False
    return any(
        longer.startswith(f"{shorter}{separator}")
        for separator in ("：", ":", "（", "(", " ")
    )


def _owned_title_matches(left: str | None, right: str | None) -> bool:
    normalized = _normalized_title(left)
    return (
        len(normalized) >= 4
        and normalized == _normalized_title(right)
    )


def _normalized_identifier(value: object) -> str:
    identifier = str(value or "").strip()
    if identifier.casefold() in _MISSING_IDENTIFIERS:
        return ""

    # ISBNs are commonly returned with spaces or hyphens by one endpoint and
    # without them by another.  Only collapse separators when the result looks
    # like an ISBN; platform product IDs must otherwise remain untouched.
    compact = re.sub(r"[\s-]", "", identifier).upper()
    if re.fullmatch(r"(?:\d{9}[\dX]|\d{13})", compact):
        return compact
    return identifier


def deduplicate_remote_books(
    remote_books: list[dict],
    platform: str,
) -> list[dict]:
    """Return one stable record per remote book.

    A missing product ID must never become the shared ``UNKNOWN_ISBN`` primary
    key.  Use a deterministic, platform-scoped title key instead so unrelated
    books cannot overwrite each other on import.
    """
    deduplicated: list[dict] = []
    seen_identifiers: set[str] = set()
    seen_fallback_titles: set[str] = set()

    for book in remote_books:
        title = str(book.get("title") or "").strip()
        if not title:
            continue

        identifier = _normalized_identifier(book.get("isbn"))
        normalized_title = _normalized_title(title)
        if identifier:
            if identifier in seen_identifiers:
                continue
            seen_identifiers.add(identifier)
        else:
            if not normalized_title or normalized_title in seen_fallback_titles:
                continue
            seen_fallback_titles.add(normalized_title)
            digest = hashlib.sha256(normalized_title.encode("utf-8")).hexdigest()[:24]
            identifier = f"{platform}:title:{digest}"

        deduplicated.append({**book, "isbn": identifier, "title": title})

    return deduplicated


def remove_stale_synced_wishlist_items(
    db: Session,
    user_id: str,
    platform: str,
    remote_books: list[dict],
) -> int:
    """Remove only confirmed-synced items absent from a successful remote import.

    Pending/failed items are deliberately retained: they may be waiting for an
    add/remove action and must not disappear merely because a remote import ran.
    """
    remote_identifiers = {
        str(book.get("isbn") or "").strip()
        for book in remote_books
    }
    remote_titles = {
        _normalized_title(str(book.get("title") or ""))
        for book in remote_books
    }
    remote_titles.discard("")

    synced_items = db.exec(
        select(WishlistItem).where(
            WishlistItem.user_id == user_id,
            WishlistItem.platform == platform,
            WishlistItem.sync_status == "synced",
        )
    ).all()

    removed = 0
    for item in synced_items:
        book = db.get(Book, item.isbn)
        title = _normalized_title(book.title if book else "")
        platform_book_id = item.platform_book_id or item.isbn
        if (
            platform_book_id in remote_identifiers
            or (title and title in remote_titles)
        ):
            continue
        db.delete(item)
        removed += 1
    return removed


def _merge_missing_book_metadata(target: Book, source: Book | None) -> None:
    """Preserve useful metadata when a platform-specific book is canonicalized."""

    if source is None or source is target:
        return
    missing_authors = {None, "", "未知作者"}
    missing_categories = {None, "", "Unkown", "未分類"}
    if target.author in missing_authors and source.author not in missing_authors:
        target.author = source.author
    if (
        target.category in missing_categories
        and source.category not in missing_categories
    ):
        target.category = source.category
    if not target.cover_url and source.cover_url:
        target.cover_url = source.cover_url


def upsert_remote_wishlist_books(
    db: Session,
    *,
    user_id: str,
    platform: str,
    remote_books: list[dict],
) -> dict[str, int]:
    """Upsert a platform snapshot while preserving its product identifiers."""

    purchases = db.exec(
        select(Purchase).where(Purchase.user_id == user_id)
    ).all()
    owned_books = {
        purchase.isbn: db.get(Book, purchase.isbn)
        for purchase in purchases
    }
    owned_platform_ids = {
        (purchase.platform, purchase.platform_book_id)
        for purchase in purchases
        if purchase.platform_book_id
    }
    wishlist_items = db.exec(
        select(WishlistItem).where(WishlistItem.user_id == user_id)
    ).all()

    imported = 0
    owned_filtered = 0
    for remote in remote_books:
        platform_book_id = str(remote["isbn"]).strip()
        title = str(remote["title"]).strip()
        owned_match = (
            (platform, platform_book_id) in owned_platform_ids
            or any(
                book and _owned_title_matches(book.title, title)
                for book in owned_books.values()
            )
        )
        remote_identity_item = next(
            (
                item
                for item in wishlist_items
                if item.platform == platform
                and (item.platform_book_id or item.isbn) == platform_book_id
            ),
            None,
        )
        # A locally-created row starts with the canonical ISBN and no platform
        # product ID.  When a later platform snapshot returns a UUID/product
        # ID for that same title, reuse the local row instead of creating a
        # second card for the same platform.
        title_matched_item = next(
            (
                item
                for item in wishlist_items
                if item.platform == platform
                and item is not remote_identity_item
                and not item.platform_book_id
                and _titles_are_same_book(
                    (db.get(Book, item.isbn) or Book(
                        isbn=item.isbn,
                        title="",
                    )).title,
                    title,
                )
            ),
            None,
        )
        existing_item = title_matched_item or remote_identity_item
        if owned_match:
            if existing_item is not None:
                db.delete(existing_item)
                wishlist_items.remove(existing_item)
            owned_filtered += 1
            continue

        equivalent_item = next(
            (
                item
                for item in wishlist_items
                if item.platform != platform
                and _titles_are_same_book(
                    (db.get(Book, item.isbn) or Book(
                        isbn=item.isbn,
                        title="",
                    )).title,
                    title,
                )
            ),
            None,
        )
        canonical_isbn = (
            title_matched_item.isbn
            if title_matched_item is not None
            else equivalent_item.isbn
            if equivalent_item is not None
            else remote_identity_item.isbn
            if remote_identity_item is not None
            else platform_book_id
        )
        book = db.get(Book, canonical_isbn)
        if book is None:
            book = Book(isbn=canonical_isbn, title=title)
            db.add(book)
            db.flush()
        elif len(title) > len(book.title):
            book.title = title
            db.add(book)

        if existing_item is None:
            existing_item = next(
                (
                    item
                    for item in wishlist_items
                    if item.platform == platform
                    and item.isbn == canonical_isbn
                ),
                None,
            )

        duplicate_items = [
            item
            for item in wishlist_items
            if item is not existing_item
            and item.platform == platform
            and (
                item.isbn == canonical_isbn
                or (item.platform_book_id or item.isbn) == platform_book_id
            )
        ]
        for duplicate in duplicate_items:
            duplicate_book = db.get(Book, duplicate.isbn)
            _merge_missing_book_metadata(book, duplicate_book)
            db.delete(duplicate)
            wishlist_items.remove(duplicate)
        if duplicate_items:
            db.flush()
            for duplicate in duplicate_items:
                if duplicate.isbn == canonical_isbn:
                    continue
                still_used = any(
                    item.isbn == duplicate.isbn
                    for item in wishlist_items
                ) or any(
                    purchase.isbn == duplicate.isbn
                    for purchase in purchases
                )
                duplicate_book = db.get(Book, duplicate.isbn)
                if duplicate_book is not None and not still_used:
                    db.delete(duplicate_book)

        if existing_item is None:
            existing_item = WishlistItem(
                user_id=user_id,
                isbn=canonical_isbn,
                platform=platform,
                platform_book_id=platform_book_id,
                sync_status="synced",
            )
            wishlist_items.append(existing_item)
        else:
            previous_isbn = existing_item.isbn
            previous_book = db.get(Book, previous_isbn)
            _merge_missing_book_metadata(book, previous_book)
            existing_item.isbn = canonical_isbn
            existing_item.platform_book_id = platform_book_id
            existing_item.sync_status = "synced"
            if previous_isbn != canonical_isbn:
                still_used = any(
                    item is not existing_item and item.isbn == previous_isbn
                    for item in wishlist_items
                ) or any(
                    purchase.isbn == previous_isbn
                    for purchase in purchases
                )
                if previous_book is not None and not still_used:
                    db.delete(previous_book)
        db.add(existing_item)
        imported += 1

    removed = remove_stale_synced_wishlist_items(
        db,
        user_id,
        platform,
        remote_books,
    )
    return {
        "imported": imported,
        "owned_filtered": owned_filtered,
        "removed": removed,
    }
