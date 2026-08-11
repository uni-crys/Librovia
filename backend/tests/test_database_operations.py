import asyncio
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, patch

from sqlalchemy import create_engine, inspect
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine as create_sqlmodel_engine, select

from app import models  # noqa: F401
from app.config import Settings
from app.database import run_migrations
from app.database_admin import create_backup, restore_backup, verify_sqlite
from app.models import Book, MetadataJob, Purchase
from app.services import library_import_queue
from app.services.library_import_queue import (
    process_metadata_queue,
    retry_incomplete_metadata_jobs,
    stage_library_snapshot,
)


class ProductionSettingsTests(unittest.TestCase):
    def test_production_requires_a_strong_sync_token(self):
        with self.assertRaisesRegex(ValueError, "READMOO_SYNC_TOKEN"):
            Settings(
                _env_file=None,
                ENVIRONMENT="production",
                READMOO_SYNC_TOKEN="short",
                CORS_ORIGINS="https://librovia.example",
            )

    def test_production_rejects_localhost_cors(self):
        with self.assertRaisesRegex(ValueError, "localhost"):
            Settings(
                _env_file=None,
                ENVIRONMENT="production",
                READMOO_SYNC_TOKEN="x" * 32,
                CORS_ORIGINS="http://localhost:3000",
            )


class MigrationTests(unittest.TestCase):
    def test_initial_migration_is_idempotent_for_an_existing_schema(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "migration.sqlite3"
            url = f"sqlite:///{database}"
            SQLModel.metadata.create_all(create_engine(url))
            run_migrations(url)
            run_migrations(url)

            inspector = inspect(create_engine(url))
            self.assertEqual(
                set(inspector.get_table_names()),
                {
                    "alembic_version",
                    "metadata_jobs",
                    "platform_sessions",
                    "sttandard_books",
                    "user_purchases",
                    "user_wishlist",
                },
            )


class BackupTests(unittest.TestCase):
    def test_backup_and_restore_preserve_data(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "ebooks.db"
            with sqlite3.connect(database) as connection:
                connection.execute("CREATE TABLE sample (value TEXT NOT NULL)")
                connection.execute("INSERT INTO sample VALUES ('before')")

            backup, checksum = create_backup(database, root / "backups")
            self.assertEqual(len(checksum), 64)
            verify_sqlite(backup)

            with sqlite3.connect(database) as connection:
                connection.execute("UPDATE sample SET value = 'after'")

            safety_backup = restore_backup(backup, database)
            self.assertIsNotNone(safety_backup)
            with sqlite3.connect(database) as connection:
                value = connection.execute("SELECT value FROM sample").fetchone()[0]
            self.assertEqual(value, "before")

            with sqlite3.connect(safety_backup) as connection:
                value = connection.execute("SELECT value FROM sample").fetchone()[0]
            self.assertEqual(value, "after")

    def test_backup_retention_removes_oldest_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "ebooks.db"
            with sqlite3.connect(database) as connection:
                connection.execute("CREATE TABLE sample (value TEXT)")
            backup_dir = root / "backups"
            old = backup_dir / "librovia-20000101T000000Z.sqlite3"
            backup_dir.mkdir()
            old.write_bytes(database.read_bytes())

            create_backup(database, backup_dir, keep=1)
            self.assertFalse(old.exists())
            self.assertEqual(len(list(backup_dir.glob("librovia-*.sqlite3"))), 1)


class MetadataQueueTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_sqlmodel_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        SQLModel.metadata.create_all(self.engine)

    def test_staging_persists_raw_book_before_metadata_lookup(self):
        with Session(self.engine) as db:
            result = stage_library_snapshot(
                db,
                user_id="reader",
                platform="readmoo",
                remote_books=[{
                    "isbn": "platform-123",
                    "title": "原始平台書名",
                    "cover_url": "https://example.test/cover.jpg",
                }],
            )
            book = db.get(Book, "platform-123")
            purchase = db.exec(select(Purchase)).one()
            job = db.exec(select(MetadataJob)).one()

        self.assertEqual(result["new_books"], 1)
        self.assertEqual(result["metadata_jobs"], 1)
        self.assertEqual(book.title, "原始平台書名")
        self.assertEqual(purchase.isbn, "platform-123")
        self.assertEqual(job.status, "pending")

    def test_staging_accepts_platform_author_and_trusted_category(self):
        with Session(self.engine) as db:
            result = stage_library_snapshot(
                db,
                user_id="reader",
                platform="readmoo",
                remote_books=[{
                    "isbn": "readmoo-product",
                    "title": "公開商品頁書籍",
                    "platform_author": "平台作者",
                    "platform_category": "心理勵志",
                    "cover_url": "https://example.test/cover.jpg",
                }],
            )
            book = db.get(Book, "readmoo-product")

        self.assertEqual(book.author, "平台作者")
        self.assertEqual(book.category, "心理勵志")
        self.assertEqual(result["metadata_jobs"], 0)

    def test_processor_canonicalizes_purchase_and_completes_job(self):
        with Session(self.engine) as db:
            stage_library_snapshot(
                db,
                user_id="reader",
                platform="readmoo",
                remote_books=[{
                    "isbn": "0306406152",
                    "title": "精準書名",
                    "cover_url": None,
                }],
            )
        metadata = {
            "source": "readmoo",
            "confidence": 0.95,
            "title": "精準書名",
            "author": "作者",
            "category": "文學小說",
            "standard_category": "文學小說",
            "cover_url": "https://example.test/cover.jpg",
            "identifiers": ["9780306406157"],
            "isbn": "9780306406157",
            "isbn_valid": True,
        }

        with (
            patch.object(library_import_queue, "engine", self.engine),
            patch.object(
                library_import_queue,
                "fetch_and_clean_metadata",
                AsyncMock(return_value=metadata),
            ),
        ):
            result = asyncio.run(process_metadata_queue())

        with Session(self.engine) as db:
            purchase = db.exec(select(Purchase)).one()
            job = db.exec(select(MetadataJob)).one()
            canonical_book = db.get(Book, "9780306406157")
            raw_book = db.get(Book, "0306406152")

        self.assertEqual(result["processed"], 1)
        self.assertEqual(purchase.isbn, "9780306406157")
        self.assertEqual(job.status, "completed")
        self.assertIsNotNone(canonical_book)
        self.assertIsNone(raw_book)

    def test_staging_prefers_detail_isbn_over_existing_platform_uuid(self):
        with Session(self.engine) as db:
            db.add(Book(
                isbn="kobo-product-uuid",
                title="平台原始書名",
                author="既有作者",
                cover_url="https://example.test/existing.jpg",
                category="文學小說",
            ))
            db.add(Purchase(
                user_id="reader",
                platform="kobo",
                platform_book_id="kobo-product-uuid",
                isbn="kobo-product-uuid",
            ))
            db.commit()

            result = stage_library_snapshot(
                db,
                user_id="reader",
                platform="kobo",
                remote_books=[{
                    "isbn": "kobo-product-uuid",
                    "metadata_identifier": "9786267558935",
                    "title": "舌尖上的香料史",
                    "cover_url": None,
                }],
            )
            purchase = db.exec(select(Purchase)).one()

        self.assertEqual(result["new_books"], 0)
        self.assertEqual(purchase.platform_book_id, "kobo-product-uuid")
        self.assertEqual(purchase.isbn, "9786267558935")
        with Session(self.engine) as db:
            self.assertIsNone(db.get(Book, "kobo-product-uuid"))
            migrated = db.get(Book, "9786267558935")
            self.assertEqual(migrated.author, "既有作者")
            self.assertEqual(migrated.category, "文學小說")
            self.assertEqual(
                migrated.cover_url,
                "https://example.test/existing.jpg",
            )

    def test_readmoo_category_overrides_kobo_but_not_the_reverse(self):
        with Session(self.engine) as db:
            stage_library_snapshot(
                db,
                user_id="reader",
                platform="kobo",
                remote_books=[{
                    "isbn": "shared-book",
                    "title": "跨平台書",
                    "platform_category": "人文社科",
                }],
            )
            stage_library_snapshot(
                db,
                user_id="reader",
                platform="readmoo",
                remote_books=[{
                    "isbn": "shared-book",
                    "title": "跨平台書",
                    "platform_category": "文學小說",
                }],
            )
            stage_library_snapshot(
                db,
                user_id="reader",
                platform="kobo",
                remote_books=[{
                    "isbn": "shared-book",
                    "title": "跨平台書",
                    "platform_category": "商業理財",
                }],
            )
            book = db.get(Book, "shared-book")

        self.assertEqual(book.category, "文學小說")

    def test_staging_rekeys_transferred_purchase_by_title_and_author(self):
        with Session(self.engine) as db:
            db.add(Book(
                isbn="9786267686706",
                title=(
                    "盛世之鑰: 為何開放的社會更強大?"
                    "從七個黃金時代看文明興衰的真相"
                ),
                author="約翰‧諾貝里",
                category="人文社科",
            ))
            db.add(Purchase(
                user_id="reader",
                platform="readmoo",
                platform_book_id="9786267686706",
                isbn="9786267686706",
            ))
            db.commit()

            result = stage_library_snapshot(
                db,
                user_id="reader",
                platform="readmoo",
                remote_books=[{
                    "isbn": "19207523",
                    "metadata_identifier": "9786267686683",
                    "title": "盛世之鑰",
                    "platform_author": "約翰‧諾貝里",
                    "platform_category": "人文社科",
                    "cover_url": "https://example.test/key.jpg",
                }],
            )
            purchases = db.exec(select(Purchase)).all()
            books = db.exec(select(Book)).all()

        self.assertEqual(result["new_books"], 0)
        self.assertEqual(len(purchases), 1)
        self.assertEqual(purchases[0].platform_book_id, "19207523")
        self.assertEqual(purchases[0].isbn, "9786267686706")
        self.assertEqual(len(books), 1)
        self.assertIn("為何開放", books[0].title)
        self.assertEqual(books[0].cover_url, "https://example.test/key.jpg")

    def test_staging_does_not_rekey_same_title_with_different_author(self):
        with Session(self.engine) as db:
            db.add(Book(
                isbn="existing-book",
                title="共同書名：既有副標題",
                author="甲作者",
                category="人文社科",
            ))
            db.add(Purchase(
                user_id="reader",
                platform="readmoo",
                platform_book_id="old-platform-id",
                isbn="existing-book",
            ))
            db.commit()

            result = stage_library_snapshot(
                db,
                user_id="reader",
                platform="readmoo",
                remote_books=[{
                    "isbn": "new-platform-id",
                    "title": "共同書名",
                    "platform_author": "乙作者",
                }],
            )
            purchases = db.exec(select(Purchase)).all()

        self.assertEqual(result["new_books"], 1)
        self.assertEqual(len(purchases), 2)

    def test_staging_does_not_reset_metadata_backoff(self):
        retry_at = datetime.utcnow() + timedelta(hours=12)
        with Session(self.engine) as db:
            db.add(Book(
                isbn="incomplete-book",
                title="缺封面的書",
                author="作者",
                category="人文社科",
            ))
            db.add(Purchase(
                user_id="reader",
                platform="readmoo",
                platform_book_id="readmoo-incomplete",
                isbn="incomplete-book",
            ))
            db.add(MetadataJob(
                user_id="reader",
                platform="readmoo",
                platform_book_id="readmoo-incomplete",
                raw_identifier="incomplete-book",
                raw_title="缺封面的書",
                status="failed",
                attempts=2,
                next_retry_at=retry_at,
            ))
            db.commit()

            result = stage_library_snapshot(
                db,
                user_id="reader",
                platform="readmoo",
                remote_books=[{
                    "isbn": "readmoo-incomplete",
                    "title": "缺封面的書",
                    "platform_author": "作者",
                    "platform_category": "人文社科",
                }],
            )
            job = db.exec(select(MetadataJob)).one()

        self.assertEqual(result["metadata_jobs"], 0)
        self.assertEqual(job.status, "failed")
        self.assertEqual(job.attempts, 2)
        self.assertEqual(job.next_retry_at, retry_at)

    def test_metadata_failures_back_off_then_require_manual_review(self):
        with Session(self.engine) as db:
            stage_library_snapshot(
                db,
                user_id="reader",
                platform="kobo",
                remote_books=[{
                    "isbn": "kobo-incomplete",
                    "title": "缺少資料的書",
                }],
            )

        with (
            patch.object(library_import_queue, "engine", self.engine),
            patch.object(
                library_import_queue,
                "fetch_and_clean_metadata",
                AsyncMock(side_effect=RuntimeError("unavailable")),
            ),
        ):
            for expected_attempt in range(1, 4):
                result = asyncio.run(process_metadata_queue())
                self.assertEqual(result["failed"], 1)
                with Session(self.engine) as db:
                    job = db.exec(select(MetadataJob)).one()
                    self.assertEqual(job.attempts, expected_attempt)
                    if expected_attempt < 3:
                        self.assertEqual(job.status, "failed")
                        self.assertIsNotNone(job.next_retry_at)
                        job.next_retry_at = datetime.utcnow() - timedelta(seconds=1)
                        db.add(job)
                        db.commit()
                    else:
                        self.assertEqual(job.status, "manual_review")
                        self.assertIsNone(job.next_retry_at)

    def test_incomplete_metadata_result_uses_shared_backoff(self):
        with Session(self.engine) as db:
            stage_library_snapshot(
                db,
                user_id="reader",
                platform="readmoo",
                remote_books=[{
                    "isbn": "readmoo-incomplete-result",
                    "title": "仍缺資料的書",
                }],
            )

        with (
            patch.object(library_import_queue, "engine", self.engine),
            patch.object(
                library_import_queue,
                "fetch_and_clean_metadata",
                AsyncMock(return_value={
                    "source": "fallback",
                    "title": "仍缺資料的書",
                }),
            ),
        ):
            result = asyncio.run(process_metadata_queue())

        with Session(self.engine) as db:
            job = db.exec(select(MetadataJob)).one()
        self.assertEqual(result["processed"], 0)
        self.assertEqual(result["failed"], 1)
        self.assertEqual(job.status, "failed")
        self.assertEqual(job.last_error_type, "IncompleteMetadata")
        self.assertIsNotNone(job.next_retry_at)

    def test_manual_retry_resets_only_incomplete_jobs(self):
        with Session(self.engine) as db:
            db.add(Book(
                isbn="manual-book",
                title="待手動補資料",
                author="作者",
                category="人文社科",
            ))
            db.add(Purchase(
                user_id="reader",
                platform="kobo",
                platform_book_id="manual-platform-id",
                isbn="manual-book",
            ))
            db.add(MetadataJob(
                user_id="reader",
                platform="kobo",
                platform_book_id="manual-platform-id",
                raw_identifier="manual-book",
                raw_title="待手動補資料",
                status="manual_review",
                attempts=3,
                last_error_type="IncompleteMetadata",
            ))
            db.commit()

        with patch.object(library_import_queue, "engine", self.engine):
            retried = retry_incomplete_metadata_jobs("reader")

        with Session(self.engine) as db:
            job = db.exec(select(MetadataJob)).one()
        self.assertEqual(retried, 1)
        self.assertEqual(job.status, "pending")
        self.assertEqual(job.attempts, 0)
        self.assertIsNone(job.last_error_type)


if __name__ == "__main__":
    unittest.main()
