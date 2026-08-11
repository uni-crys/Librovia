from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta

from sqlmodel import Session, select

from app.database import engine
from app.models import Book, MetadataJob, Purchase, WishlistItem
from app.services.library_metadata import book_metadata_is_incomplete
from app.services.metadata_matching import (
    MetadataMatchAction,
    apply_metadata_decision,
    apply_platform_snapshot,
    decide_metadata_match,
    metadata_book_values,
)
from app.services.metadata_pipeline import fetch_and_clean_metadata, normalize_text

LOGGER = logging.getLogger("librovia.metadata_queue")
MAX_ATTEMPTS = 3
_processor_lock = asyncio.Lock()


def _titles_are_distinctive_extensions(
    left: str | None,
    right: str | None,
) -> bool:
    left_text = str(left or "").strip()
    right_text = str(right or "").strip()
    left_normalized = normalize_text(left_text)
    right_normalized = normalize_text(right_text)
    if not left_normalized or not right_normalized:
        return False
    if left_normalized == right_normalized:
        return True

    shorter, longer = sorted(
        (left_text, right_text),
        key=lambda value: len(normalize_text(value)),
    )
    if len(normalize_text(shorter)) < 4:
        return False
    return any(
        longer.startswith(f"{shorter}{separator}")
        for separator in ("：", ":", "（", "(", " ")
    )


def _find_unique_platform_rekey_candidate(
    purchases: list[Purchase],
    books_by_isbn: dict[str, Book | None],
    *,
    platform: str,
    remote_platform_ids: set[str],
    raw_title: str,
    platform_author: str,
) -> Purchase | None:
    normalized_author = normalize_text(platform_author)
    if not normalized_author or platform_author == "未知作者":
        return None

    candidates = []
    for purchase in purchases:
        if purchase.platform != platform:
            continue
        if str(purchase.platform_book_id or "") in remote_platform_ids:
            continue
        book = books_by_isbn.get(purchase.isbn)
        if book is None:
            continue
        if normalize_text(book.author) != normalized_author:
            continue
        if not _titles_are_distinctive_extensions(book.title, raw_title):
            continue
        candidates.append(purchase)
    return candidates[0] if len(candidates) == 1 else None


def _canonical_isbn_by_platform_id(
    purchases: list[Purchase],
) -> dict[str, str]:
    return {
        str(purchase.platform_book_id).strip(): purchase.isbn
        for purchase in purchases
        if purchase.platform_book_id
    }


def _enqueue_job(
    db: Session,
    *,
    user_id: str,
    platform: str,
    platform_book_id: str,
    raw_identifier: str,
    raw_title: str,
    crawler_cover: str | None,
) -> bool:
    job = db.exec(
        select(MetadataJob).where(
            MetadataJob.user_id == user_id,
            MetadataJob.platform == platform,
            MetadataJob.platform_book_id == platform_book_id,
        )
    ).first()
    created = job is None
    if job is None:
        job = MetadataJob(
            user_id=user_id,
            platform=platform,
            platform_book_id=platform_book_id,
            raw_identifier=raw_identifier,
            raw_title=raw_title,
            crawler_cover=crawler_cover,
        )
    else:
        job.raw_identifier = raw_identifier
        job.raw_title = raw_title
        job.crawler_cover = crawler_cover
        if job.status == "completed":
            # A completed job whose book is still incomplete should not be
            # silently reset by every platform sync. Surface it for an
            # explicit manual retry instead.
            job.status = "manual_review"
            job.attempts = max(job.attempts, MAX_ATTEMPTS)
            job.next_retry_at = None
            job.last_error_type = "IncompleteMetadata"
        job.updated_at = datetime.utcnow()
    db.add(job)
    return created or job.status == "pending"


def _complete_existing_job(
    db: Session,
    *,
    user_id: str,
    platform: str,
    platform_book_id: str,
) -> None:
    job = db.exec(
        select(MetadataJob).where(
            MetadataJob.user_id == user_id,
            MetadataJob.platform == platform,
            MetadataJob.platform_book_id == platform_book_id,
        )
    ).first()
    if job is None or job.status == "completed":
        return
    job.status = "completed"
    job.result = "platform_snapshot"
    job.last_error_type = None
    job.next_retry_at = None
    job.updated_at = datetime.utcnow()
    db.add(job)


def stage_library_snapshot(
    db: Session,
    *,
    user_id: str,
    platform: str,
    remote_books: list[dict],
    limit: int | None = None,
) -> dict[str, int]:
    """Persist raw platform data quickly and enqueue incomplete metadata."""

    purchases = db.exec(
        select(Purchase).where(
            Purchase.user_id == user_id,
            Purchase.platform == platform,
        )
    ).all()
    isbn_by_platform_id = _canonical_isbn_by_platform_id(purchases)
    purchases_by_platform_id = {
        str(purchase.platform_book_id): purchase
        for purchase in purchases
        if purchase.platform_book_id
    }
    books_by_isbn = {
        purchase.isbn: db.get(Book, purchase.isbn)
        for purchase in purchases
    }
    remote_platform_ids = {
        str(item.get("isbn") or "").strip()
        for item in remote_books
    }
    new_books = 0
    updated_books = 0
    queued_jobs = 0
    staged_books = 0

    for item in remote_books:
        if limit is not None and staged_books >= limit:
            break
        staged_books += 1
        platform_book_id = str(item["isbn"]).strip()
        raw_title = str(item.get("title") or "未知書名").strip()
        platform_author = str(
            item.get("platform_author") or "未知作者"
        ).strip()
        purchase = purchases_by_platform_id.get(platform_book_id)
        rekeyed_purchase = False
        if purchase is None:
            purchase = _find_unique_platform_rekey_candidate(
                purchases,
                books_by_isbn,
                platform=platform,
                remote_platform_ids=remote_platform_ids,
                raw_title=raw_title,
                platform_author=platform_author,
            )
            if purchase is not None:
                old_platform_book_id = str(purchase.platform_book_id or "")
                purchases_by_platform_id.pop(old_platform_book_id, None)
                purchase.platform_book_id = platform_book_id
                purchases_by_platform_id[platform_book_id] = purchase
                db.add(purchase)
                rekeyed_purchase = True
        raw_identifier = str(
            (purchase.isbn if rekeyed_purchase else None)
            or item.get("metadata_identifier")
            or isbn_by_platform_id.get(platform_book_id)
            or platform_book_id
        ).strip()
        platform_category = str(
            item.get("platform_category") or "未分類"
        ).strip()
        crawler_cover = item.get("cover_url")
        book = db.get(Book, raw_identifier)
        if book is None:
            book = Book(
                isbn=raw_identifier,
                title=raw_title,
                author=platform_author,
                cover_url=crawler_cover,
                category=platform_category,
            )
            db.add(book)
            db.flush()
        elif apply_platform_snapshot(
            book,
            platform_book_id=platform_book_id,
            raw_title=raw_title,
            crawler_cover=crawler_cover,
        ):
            db.add(book)
            updated_books += 1
        elif (
            _titles_are_distinctive_extensions(book.title, raw_title)
            and len(raw_title) > len(book.title)
        ):
            book.title = raw_title
            db.add(book)
            updated_books += 1
        if crawler_cover and not book.cover_url:
            book.cover_url = crawler_cover
            db.add(book)
        if (
            platform_author != "未知作者"
            and (not book.author or book.author == "未知作者")
        ):
            book.author = platform_author
            db.add(book)
        if (
            platform_category != "未分類"
            and (
                platform == "readmoo"
                or book.category in {"未分類", "Unkown"}
            )
        ):
            book.category = platform_category
            db.add(book)

        if purchase is None:
            purchase = Purchase(
                user_id=user_id,
                platform=platform,
                platform_book_id=platform_book_id,
                isbn=book.isbn,
            )
            db.add(purchase)
            purchases_by_platform_id[platform_book_id] = purchase
            new_books += 1
        elif purchase.isbn != book.isbn and item.get("metadata_identifier"):
            previous_isbn = purchase.isbn
            previous_book = db.get(Book, previous_isbn)
            if previous_book is not None:
                if (
                    (not book.author or book.author == "未知作者")
                    and previous_book.author
                    and previous_book.author != "未知作者"
                ):
                    book.author = previous_book.author
                if (
                    (
                        not book.cover_url
                        or book.cover_url == "/images/openbook.png"
                    )
                    and previous_book.cover_url
                    and previous_book.cover_url != "/images/openbook.png"
                ):
                    book.cover_url = previous_book.cover_url
                if (
                    book.category in {"未分類", "Unkown"}
                    and previous_book.category
                    and previous_book.category not in {"未分類", "Unkown"}
                ):
                    book.category = previous_book.category
                db.add(book)
            purchase.isbn = book.isbn
            db.add(purchase)
            db.flush()
            still_used = db.exec(
                select(Purchase).where(Purchase.isbn == previous_isbn)
            ).first() or db.exec(
                select(WishlistItem).where(
                    WishlistItem.isbn == previous_isbn
                )
            ).first()
            if previous_book is not None and not still_used:
                db.delete(previous_book)

        if book_metadata_is_incomplete(book):
            queued_jobs += int(_enqueue_job(
                db,
                user_id=user_id,
                platform=platform,
                platform_book_id=platform_book_id,
                raw_identifier=book.isbn,
                raw_title=raw_title,
                crawler_cover=crawler_cover,
            ))
        else:
            _complete_existing_job(
                db,
                user_id=user_id,
                platform=platform,
                platform_book_id=platform_book_id,
            )

    db.commit()
    return {
        "new_books": new_books,
        "updated_books": updated_books,
        "metadata_jobs": queued_jobs,
    }


def _record_job_failure(
    job: MetadataJob,
    error_type: str,
    *,
    now: datetime | None = None,
) -> None:
    failed_at = now or datetime.utcnow()
    job.last_error_type = error_type
    job.updated_at = failed_at
    if job.attempts >= MAX_ATTEMPTS:
        job.status = "manual_review"
        job.next_retry_at = None
        return
    job.status = "failed"
    job.next_retry_at = failed_at + timedelta(
        hours=24 * (2 ** max(0, job.attempts - 1))
    )


def _apply_job_result(job_id: int, metadata: dict) -> bool:
    with Session(engine) as db:
        job = db.get(MetadataJob, job_id)
        if job is None:
            return False
        purchase = db.exec(
            select(Purchase).where(
                Purchase.user_id == job.user_id,
                Purchase.platform == job.platform,
                Purchase.platform_book_id == job.platform_book_id,
            )
        ).first()
        if purchase is None:
            raise LookupError("purchase_missing")

        source_book = db.get(Book, purchase.isbn)
        decision = decide_metadata_match(
            identifier=job.raw_identifier,
            raw_title=job.raw_title,
            metadata=metadata,
        )
        target_isbn = (
            decision.canonical_isbn
            if (
                decision.action == MetadataMatchAction.CANONICALIZE
                and decision.canonical_isbn
            )
            else purchase.isbn
        )
        target_book = db.get(Book, target_isbn)
        if target_book is None:
            values = metadata_book_values(
                decision,
                raw_title=job.raw_title,
                crawler_cover=job.crawler_cover,
                metadata=metadata,
            )
            target_book = Book(
                isbn=target_isbn,
                title=str(values["title"] or job.raw_title),
                author=str(values["author"] or "未知作者"),
                cover_url=values["cover_url"],
                category=str(values["category"] or "未分類"),
            )
        else:
            apply_metadata_decision(
                target_book,
                decision,
                raw_title=job.raw_title,
                crawler_cover=job.crawler_cover,
                metadata=metadata,
                overwrite_with_trusted=True,
            )
        db.add(target_book)
        db.flush()
        purchase.isbn = target_isbn
        db.add(purchase)

        if source_book and source_book.isbn != target_isbn:
            still_used = db.exec(
                select(Purchase).where(Purchase.isbn == source_book.isbn)
            ).first() or db.exec(
                select(WishlistItem).where(WishlistItem.isbn == source_book.isbn)
            ).first()
            if not still_used:
                db.delete(source_book)

        job.result = decision.action.value
        if book_metadata_is_incomplete(target_book):
            _record_job_failure(job, "IncompleteMetadata")
        else:
            job.status = "completed"
            job.last_error_type = None
            job.next_retry_at = None
            job.updated_at = datetime.utcnow()
        db.add(job)
        db.commit()
        return job.status == "completed"


async def process_metadata_queue(batch_size: int = 25) -> dict[str, int]:
    """Process durable jobs serially; concurrent wakeups collapse into one."""

    if _processor_lock.locked():
        return {"processed": 0, "failed": 0, "already_running": 1}
    processed = 0
    failed = 0
    async with _processor_lock:
        queue_time = datetime.utcnow()
        with Session(engine) as db:
            candidates = db.exec(
                select(MetadataJob)
                .where(
                    MetadataJob.status.in_(["pending", "failed", "running"]),
                    MetadataJob.attempts < MAX_ATTEMPTS,
                )
                .order_by(MetadataJob.created_at)
            ).all()
            jobs = [
                job
                for job in candidates
                if (
                    job.status in {"pending", "running"}
                    or job.next_retry_at is None
                    or job.next_retry_at <= queue_time
                )
            ][:batch_size]
            job_ids = [job.id for job in jobs if job.id is not None]

        for job_id in job_ids:
            with Session(engine) as db:
                job = db.get(MetadataJob, job_id)
                if job is None:
                    continue
                job.status = "running"
                job.attempts += 1
                job.next_retry_at = None
                job.updated_at = datetime.utcnow()
                db.add(job)
                db.commit()
                raw_identifier = job.raw_identifier
                raw_title = job.raw_title
                purchase = db.exec(
                    select(Purchase).where(
                        Purchase.user_id == job.user_id,
                        Purchase.platform == job.platform,
                        Purchase.platform_book_id == job.platform_book_id,
                    )
                ).first()
                fallback_book = (
                    db.get(Book, purchase.isbn)
                    if purchase is not None
                    else None
                )
                platform_fallback = (
                    {
                        "source": job.platform,
                        "title": fallback_book.title,
                        "author": fallback_book.author,
                        "category": fallback_book.category,
                        "cover_url": fallback_book.cover_url,
                    }
                    if fallback_book is not None
                    else None
                )
            try:
                metadata = await fetch_and_clean_metadata(
                    isbn=raw_identifier,
                    raw_title=raw_title,
                    platform_fallback=platform_fallback,
                )
                if _apply_job_result(job_id, metadata):
                    processed += 1
                else:
                    failed += 1
            except Exception as error:
                failed += 1
                with Session(engine) as db:
                    job = db.get(MetadataJob, job_id)
                    if job:
                        _record_job_failure(job, type(error).__name__)
                        db.add(job)
                        db.commit()
                LOGGER.exception(
                    "Metadata job failed",
                    extra={
                        "metadata_job_id": job_id,
                        "result": "failed",
                    },
                )
    return {"processed": processed, "failed": failed, "already_running": 0}


def metadata_queue_status(user_id: str) -> dict[str, int]:
    with Session(engine) as db:
        jobs = db.exec(
            select(MetadataJob).where(MetadataJob.user_id == user_id)
        ).all()
    counts = {
        "pending": 0,
        "running": 0,
        "completed": 0,
        "failed": 0,
        "manual_review": 0,
    }
    for job in jobs:
        counts[job.status] = counts.get(job.status, 0) + 1
    counts["total"] = len(jobs)
    return counts


def retry_incomplete_metadata_jobs(
    user_id: str,
    *,
    platform: str | None = None,
    platform_book_id: str | None = None,
) -> int:
    """Force incomplete jobs back to pending after an explicit user action."""

    retried = 0
    with Session(engine) as db:
        jobs = db.exec(
            select(MetadataJob).where(MetadataJob.user_id == user_id)
        ).all()
        for job in jobs:
            if platform and job.platform != platform:
                continue
            if platform_book_id and job.platform_book_id != platform_book_id:
                continue
            purchase = db.exec(
                select(Purchase).where(
                    Purchase.user_id == job.user_id,
                    Purchase.platform == job.platform,
                    Purchase.platform_book_id == job.platform_book_id,
                )
            ).first()
            book = db.get(Book, purchase.isbn) if purchase else None
            if book is None or not book_metadata_is_incomplete(book):
                continue
            job.status = "pending"
            job.attempts = 0
            job.result = None
            job.last_error_type = None
            job.next_retry_at = None
            job.updated_at = datetime.utcnow()
            db.add(job)
            retried += 1
        db.commit()
    return retried
