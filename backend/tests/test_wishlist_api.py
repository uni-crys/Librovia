import asyncio
import json
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, patch

from fastapi import BackgroundTasks, HTTPException
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from app.api import auth, readmoo_replication
from app.services import platform_auth
from app.services.kobo_library_worker import (
    _canonical_isbn_by_platform_id,
    _kobo_detail_is_needed,
    extract_kobo_book_id,
    extract_kobo_detail_metadata,
    extract_kobo_public_metadata,
    extract_kobo_tracked_links,
    map_kobo_category,
)
from app.services.library_navigation import (
    is_kobo_home_url,
    is_kobo_library_url,
    is_readmoo_dashboard_url,
    is_readmoo_library_url,
)
from app.services.metadata_matching import (
    MetadataMatchAction,
    apply_platform_snapshot,
    decide_metadata_match,
    metadata_book_values,
)
from app.services.readmoo_library_worker import (
    _canonical_isbn_by_platform_id as readmoo_canonical_isbn_by_platform_id,
    _merge_readmoo_api_metadata,
    parse_readmoo_library_api,
)
from app.services.wishlist_reconciliation import (
    deduplicate_remote_books,
    remove_stale_synced_wishlist_items,
    upsert_remote_wishlist_books,
)
from app.api.wishlist import (
    WishlistCreate,
    WishlistTransfer,
    add_to_wishlist,
    get_wishlist,
    trigger_wishlist_import,
    transfer_to_library,
)
from app.models import Book, PlatformSession, Purchase, WishlistItem


class WishlistApiTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        SQLModel.metadata.create_all(self.engine)

    def test_add_by_title_creates_book_and_two_platform_items(self):
        metadata = {
            "source": "readmoo",
            "title": "測試書名",
            "author": "測試作者",
            "category": "文學小說",
            "cover_url": "https://example.test/cover.jpg",
            "identifiers": ["9789571078304"],
        }
        with (
            Session(self.engine) as session,
            patch(
                "app.api.wishlist.fetch_and_clean_metadata",
                AsyncMock(return_value=metadata),
            ),
        ):
            result = asyncio.run(add_to_wishlist(
                WishlistCreate(user_id="reader", query="測試書名"),
                BackgroundTasks(),
                session,
            ))
            items = session.exec(select(WishlistItem)).all()
            book = session.get(Book, "9789571078304")

        self.assertEqual(result["book"]["isbn"], "9789571078304")
        self.assertEqual(book.title, "測試書名")
        self.assertEqual(
            {item.platform for item in items},
            {"kobo", "readmoo"},
        )

    def test_wishlist_is_grouped_into_book_cards(self):
        with Session(self.engine) as session:
            session.add(Book(
                isbn="book-a",
                title="同一本書",
                author="作者",
                category="文學小說",
            ))
            session.add_all([
                WishlistItem(
                    user_id="reader",
                    isbn="book-a",
                    platform="kobo",
                    sync_status="pending",
                ),
                WishlistItem(
                    user_id="reader",
                    isbn="book-a",
                    platform="readmoo",
                    sync_status="synced",
                ),
            ])
            session.commit()
            result = asyncio.run(get_wishlist(
                user_id="reader",
                session=session,
            ))

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["title"], "同一本書")
        self.assertEqual(len(result[0]["platforms"]), 2)

    def test_remote_import_removes_only_stale_synced_platform_items(self):
        with Session(self.engine) as session:
            session.add_all([
                Book(isbn="gone", title="已從遠端刪除", category="未分類"),
                Book(isbn="keep", title="仍在遠端", category="未分類"),
                Book(isbn="pending", title="等待同步", category="未分類"),
            ])
            session.add_all([
                WishlistItem(
                    user_id="reader", isbn="gone", platform="kobo",
                    sync_status="synced",
                ),
                WishlistItem(
                    user_id="reader", isbn="keep", platform="kobo",
                    sync_status="synced",
                ),
                WishlistItem(
                    user_id="reader", isbn="pending", platform="kobo",
                    sync_status="pending",
                ),
                WishlistItem(
                    user_id="reader", isbn="gone", platform="readmoo",
                    sync_status="synced",
                ),
            ])
            session.commit()

            removed = remove_stale_synced_wishlist_items(
                session,
                "reader",
                "kobo",
                [{"isbn": "keep", "title": "仍在遠端"}],
            )
            session.commit()
            items = session.exec(select(WishlistItem)).all()

        self.assertEqual(removed, 1)
        self.assertEqual(
            {(item.isbn, item.platform) for item in items},
            {("keep", "kobo"), ("pending", "kobo"), ("gone", "readmoo")},
        )

    def test_remote_import_deduplicates_equivalent_isbn_formats(self):
        books = deduplicate_remote_books(
            [
                {"isbn": "978-957-10-7830-4", "title": "第一筆"},
                {"isbn": " 9789571078304 ", "title": "重複資料"},
            ],
            "kobo",
        )

        self.assertEqual(
            books,
            [{"isbn": "9789571078304", "title": "第一筆"}],
        )

    def test_readmoo_library_api_extracts_and_merges_book_metadata(self):
        payload = {
            "included": [
                {
                    "type": "categories",
                    "id": "3",
                    "attributes": {"name": "文學小說"},
                },
                {
                    "type": "books",
                    "id": "210407586000101",
                    "attributes": {
                        "title": "測試書",
                        "author": "測試作者",
                        "isbn": "9789571078304",
                        "cover": {
                            "small": {"href": "https://example.test/s.jpg"},
                            "large": {"href": "https://example.test/l.jpg"},
                        },
                    },
                    "relationships": {
                        "top_main_category": {
                            "data": {"type": "categories", "id": "3"}
                        }
                    },
                },
            ]
        }

        metadata = parse_readmoo_library_api(payload)
        remote_books = [{
            "isbn": "210407586000101",
            "title": "測試書",
            "cover_url": None,
        }]
        enriched = _merge_readmoo_api_metadata(remote_books, metadata)

        self.assertEqual(enriched, 1)
        self.assertEqual(
            remote_books[0],
            {
                "isbn": "210407586000101",
                "metadata_identifier": "9789571078304",
                "title": "測試書",
                "platform_author": "測試作者",
                "platform_category": "文學小說",
                "cover_url": "https://example.test/l.jpg",
            },
        )

    def test_remote_wishlist_filters_exact_title_already_owned(self):
        with Session(self.engine) as session:
            session.add(Book(
                isbn="owned-product",
                title="森林之神",
                category="文學小說",
            ))
            session.add(Purchase(
                user_id="reader",
                platform="kobo",
                platform_book_id="owned-product",
                isbn="owned-product",
            ))
            session.commit()

            result = upsert_remote_wishlist_books(
                session,
                user_id="reader",
                platform="kobo",
                remote_books=[{
                    "isbn": "wishlist-product",
                    "title": "森林之神",
                }],
            )
            session.commit()
            items = session.exec(select(WishlistItem)).all()

        self.assertEqual(result["owned_filtered"], 1)
        self.assertEqual(items, [])

    def test_remote_wishlist_merges_distinctive_subtitle_across_platforms(self):
        with Session(self.engine) as session:
            session.add(Book(
                isbn="readmoo-product",
                title="變臉的緬甸",
                category="未分類",
            ))
            session.add(WishlistItem(
                user_id="reader",
                platform="readmoo",
                platform_book_id="readmoo-product",
                isbn="readmoo-product",
                sync_status="synced",
            ))
            session.commit()

            upsert_remote_wishlist_books(
                session,
                user_id="reader",
                platform="kobo",
                remote_books=[{
                    "isbn": "kobo-product",
                    "title": "變臉的緬甸：一個由血、夢想和黃金構成的國度",
                }],
            )
            session.commit()
            items = session.exec(select(WishlistItem)).all()
            books = session.exec(select(Book)).all()

        self.assertEqual({item.isbn for item in items}, {"readmoo-product"})
        self.assertEqual(
            {
                (item.platform, item.platform_book_id)
                for item in items
            },
            {
                ("readmoo", "readmoo-product"),
                ("kobo", "kobo-product"),
            },
        )
        self.assertEqual(len(books), 1)
        self.assertIn("一個由血", books[0].title)

    def test_short_generic_title_is_not_merged_by_prefix(self):
        with Session(self.engine) as session:
            session.add(Book(
                isbn="short-product",
                title="鯨",
                category="未分類",
            ))
            session.add(WishlistItem(
                user_id="reader",
                platform="readmoo",
                platform_book_id="short-product",
                isbn="short-product",
                sync_status="synced",
            ))
            session.commit()

            upsert_remote_wishlist_books(
                session,
                user_id="reader",
                platform="kobo",
                remote_books=[{
                    "isbn": "different-product",
                    "title": "鯨：海洋紀實",
                }],
            )
            session.commit()
            items = session.exec(select(WishlistItem)).all()

        self.assertEqual(
            {item.isbn for item in items},
            {"short-product", "different-product"},
        )

    def test_missing_remote_ids_get_distinct_stable_keys(self):
        books = deduplicate_remote_books(
            [
                {"isbn": "UNKNOWN_ISBN", "title": "甲書"},
                {"isbn": None, "title": "乙書"},
                {"isbn": "", "title": " 甲書 "},
            ],
            "readmoo",
        )

        self.assertEqual(len(books), 2)
        self.assertNotEqual(books[0]["isbn"], books[1]["isbn"])
        self.assertTrue(books[0]["isbn"].startswith("readmoo:title:"))
        self.assertEqual(
            books,
            deduplicate_remote_books(
                [
                    {"isbn": None, "title": "甲書"},
                    {"isbn": "UNKNOWN_ISBN", "title": "乙書"},
                ],
                "readmoo",
            ),
        )

    def test_kobo_platform_id_resolves_to_existing_canonical_isbn(self):
        purchases = [
            Purchase(
                user_id="reader",
                platform="kobo",
                platform_book_id="kobo-product-uuid",
                isbn="9786263901438",
            ),
        ]

        self.assertEqual(
            _canonical_isbn_by_platform_id(purchases),
            {"kobo-product-uuid": "9786263901438"},
        )

    def test_kobo_book_id_parser_reads_detail_label(self):
        self.assertEqual(
            extract_kobo_book_id(
                "出版者：麥浩斯\n書籍ID：9786267558935\n語言：中文"
            ),
            "9786267558935",
        )

    def test_kobo_book_id_parser_reads_structured_isbn(self):
        self.assertEqual(
            extract_kobo_book_id(
                "",
                ['{"@type":"Book","isbn":"9786267558935"}'],
            ),
            "9786267558935",
        )

    def test_kobo_book_id_parser_rejects_invalid_checksum(self):
        self.assertIsNone(
            extract_kobo_book_id("書籍 ID：9786267558934")
        )

    def test_kobo_detail_metadata_uses_explicit_category_not_json_genre(self):
        metadata = extract_kobo_detail_metadata(
            "書籍ID：9786267558935",
            [json.dumps({
                "@type": "Book",
                "isbn": "9786267558935",
                "author": [{"name": "伊恩・安德森"}],
                "image": "https://example.test/kobo-cover.jpg",
                "genre": "Kobo 不可信分類",
            })],
            ["社會科學"],
        )

        self.assertEqual(metadata["isbn"], "9786267558935")
        self.assertEqual(metadata["author"], "伊恩・安德森")
        self.assertEqual(
            metadata["cover_url"],
            "https://example.test/kobo-cover.jpg",
        )
        self.assertEqual(metadata["category"], "人文社科")

    def test_kobo_category_prefers_store_parent_and_refines_nonfiction(self):
        self.assertEqual(
            map_kobo_category(["小說與文學", "心理學"]),
            "文學小說",
        )
        self.assertEqual(
            map_kobo_category(["非小說", "科學與自然", "科學"]),
            "自然科普",
        )
        self.assertEqual(
            map_kobo_category(["企業與金融", "經濟學"]),
            "商業理財",
        )
        self.assertEqual(
            map_kobo_category(["非小說", "健康與幸福", "鍛鍊"]),
            "醫療保健",
        )
        self.assertEqual(
            map_kobo_category(["非小說", "電腦"]),
            "電腦資訊",
        )
        self.assertEqual(
            map_kobo_category(["非小說", "健康與幸福", "心理學"]),
            "心理勵志",
        )
        self.assertEqual(
            map_kobo_category(["非小說", "健康與幸福", "自助"]),
            "心理勵志",
        )
        self.assertEqual(
            map_kobo_category(["非小說", "健康與幸福", "健康"]),
            "醫療保健",
        )
        self.assertEqual(
            map_kobo_category(["非小說", "健康與幸福", "醫學"]),
            "醫療保健",
        )
        self.assertEqual(
            map_kobo_category(["非小說", "參考與語言", "法律"]),
            "人文社科",
        )
        self.assertEqual(
            map_kobo_category(["非小說", "參考與語言", "外國語言"]),
            "語言學習",
        )
        self.assertEqual(
            map_kobo_category(["非小說", "參考與語言", "研究輔助"]),
            "考試用書",
        )
        self.assertEqual(
            map_kobo_category(
                ["非小說", "參考與語言", "參考", "科學與自然"]
            ),
            "自然科普",
        )

    def test_kobo_detail_refresh_skips_persisted_complete_book(self):
        purchase = Purchase(
            user_id="reader",
            platform="kobo",
            platform_book_id="product-id",
            isbn="9789571078304",
        )
        complete = Book(
            isbn="9789571078304",
            title="完整書",
            author="作者",
            cover_url="https://example.test/cover.jpg",
            category="文學小說",
        )
        incomplete = Book(
            isbn="9789571078304",
            title="缺分類",
            author="作者",
            cover_url="https://example.test/cover.jpg",
            category="未分類",
        )

        self.assertFalse(_kobo_detail_is_needed(purchase, complete))
        purchase.isbn = "unresolved-kobo-product-id"
        self.assertTrue(_kobo_detail_is_needed(purchase, complete))
        purchase.detail_attempts = 1
        purchase.detail_status = "cooldown"
        purchase.detail_next_retry_at = datetime.utcnow() + timedelta(hours=24)
        self.assertFalse(_kobo_detail_is_needed(purchase, complete))
        purchase.detail_next_retry_at = datetime.utcnow() - timedelta(seconds=1)
        self.assertTrue(_kobo_detail_is_needed(purchase, complete))
        purchase.detail_attempts = 3
        purchase.detail_status = "manual_review"
        self.assertFalse(_kobo_detail_is_needed(purchase, complete))
        purchase.detail_attempts = 0
        purchase.detail_status = "pending"
        purchase.detail_next_retry_at = None
        self.assertTrue(_kobo_detail_is_needed(purchase, incomplete))
        self.assertTrue(_kobo_detail_is_needed(None, None))

    def test_kobo_tracked_links_parse_lazy_category_and_author_html(self):
        html = """
        <a href="/tw/zh/ebooks/literary-2"
           data-track-info='{"description":"category","totalBooks":0}'>
          文學
        </a>
        <a class="contributor-name"
           data-track-info='{"description":"authorSearch","author":"Shuang-zi Yang"}'>
          Shuang-zi Yang
        </a>
        <a href="/tw/zh/ebooks/psychology"
           data-track-info='{"description":"category","totalBooks":0}'>
          心理學
        </a>
        """

        tracked = extract_kobo_tracked_links(html)

        self.assertEqual(tracked["authors"], ["Shuang-zi Yang"])
        self.assertEqual(tracked["categories"], ["文學", "心理學"])
        self.assertEqual(map_kobo_category(["文學"]), "文學小說")
        self.assertEqual(map_kobo_category(["語言文學"]), "文學小說")
        self.assertEqual(map_kobo_category(["心理學"]), "心理勵志")

    def test_kobo_public_page_extracts_book_id_author_and_category(self):
        html = """
        <h1>Rewire-神經可塑性</h1>
        <a class="contributor-name"
           data-track-info='{"description":"authorSearch","author":"妮可．維諾拉"}'>
          妮可．維諾拉
        </a>
        <a data-track-info='{"description":"category"}'>心理學</a>
        <div class="bookitem-secondary-metadata">
          <h2>電子書詳細資料</h2>
          <ul><li>書籍ID：<span>9786263106635</span></li></ul>
        </div>
        """

        metadata = extract_kobo_public_metadata(html)

        self.assertEqual(metadata["isbn"], "9786263106635")
        self.assertEqual(metadata["author"], "妮可．維諾拉")
        self.assertEqual(metadata["category"], "心理勵志")

    def test_library_navigation_requires_expected_platform_route(self):
        self.assertTrue(
            is_readmoo_dashboard_url(
                "https://read.readmoo.com/#/dashboard"
            )
        )
        self.assertTrue(
            is_readmoo_library_url("https://read.readmoo.com/#/library")
        )
        self.assertFalse(
            is_readmoo_library_url("https://read.readmoo.com/#/dashboard")
        )
        self.assertTrue(is_kobo_home_url("https://www.kobo.com/tw/zh/"))
        self.assertTrue(
            is_kobo_library_url(
                "https://www.kobo.com/tw/zh/library/books"
            )
        )

    def test_readmoo_platform_id_resolves_to_canonical_eisbn(self):
        purchases = [
            Purchase(
                user_id="reader",
                platform="readmoo",
                platform_book_id="17818597",
                isbn="9786267747308",
            ),
        ]

        self.assertEqual(
            readmoo_canonical_isbn_by_platform_id(purchases),
            {"17818597": "9786267747308"},
        )

    def test_metadata_decision_rejects_short_title_expansion(self):
        decision = decide_metadata_match(
            identifier="17818597",
            raw_title="鯨",
            metadata={
                "title": "鯨滅",
                "author": "陳建佐",
                "confidence": 0.7,
                "source": "google_books",
                "identifiers": ["9789863267386"],
            },
        )

        self.assertEqual(decision.action, MetadataMatchAction.REJECT)
        self.assertIn("short_title_conflict", decision.reasons)
        values = metadata_book_values(
            decision,
            raw_title="鯨",
            crawler_cover="https://readmoo.test/whale.jpg",
            metadata={"title": "鯨滅", "author": "陳建佐"},
        )
        self.assertEqual(values["title"], "鯨")
        self.assertEqual(values["author"], "未知作者")
        self.assertEqual(
            values["cover_url"],
            "https://readmoo.test/whale.jpg",
        )

    def test_metadata_decision_enriches_matching_long_title(self):
        decision = decide_metadata_match(
            identifier="7060939",
            raw_title="Python入門教室",
            metadata={
                "title": "Python入門教室：8堂基礎課程",
                "author": "大澤文孝",
                "confidence": 0.7,
                "source": "google_books",
                "identifiers": ["9789864769315"],
            },
        )

        self.assertEqual(
            decision.action,
            MetadataMatchAction.ENRICH_ONLY,
        )
        self.assertIsNone(decision.canonical_isbn)

    def test_metadata_decision_rejects_missing_sequel_number(self):
        decision = decide_metadata_match(
            identifier="14286274",
            raw_title="也許你該找人聊聊（二版）",
            metadata={
                "title": "也許你該找人聊聊２（二版）",
                "author": "蘿蕊・葛利布",
                "confidence": 0.9,
                "source": "google_books",
                "identifiers": ["9786267244913"],
            },
        )

        self.assertEqual(decision.action, MetadataMatchAction.REJECT)
        self.assertTrue(decision.evidence.volume_conflict)
        self.assertIn("volume_conflict", decision.reasons)

    def test_metadata_decision_accepts_same_sequel_number_and_edition(self):
        decision = decide_metadata_match(
            identifier="readmoo-product-id",
            raw_title="也許你該找人聊聊２",
            raw_author="蘿蕊・葛利布",
            metadata={
                "title": "也許你該找人聊聊2：心理師教你大膽修訂自己的人生故事！（二版）",
                "author": "蘿蕊・葛利布",
                "confidence": 0.9,
                "source": "google_books",
                "identifiers": ["9786267244913"],
            },
        )

        self.assertFalse(decision.evidence.volume_conflict)
        self.assertEqual(decision.action, MetadataMatchAction.CANONICALIZE)

    def test_platform_snapshot_repairs_unresolved_title_and_cover(self):
        book = Book(
            isbn="14286274",
            title="也許你該找人聊聊２（二版）",
            author="蘿蕊・葛利布",
            cover_url="https://metadata.test/wrong.jpg",
            category="心理勵志",
        )

        changed = apply_platform_snapshot(
            book,
            platform_book_id="14286274",
            raw_title="也許你該找人聊聊（二版）",
            crawler_cover="https://readmoo.test/original.jpg",
        )

        self.assertTrue(changed)
        self.assertEqual(book.title, "也許你該找人聊聊（二版）")
        self.assertEqual(
            book.cover_url,
            "https://readmoo.test/original.jpg",
        )

    def test_platform_snapshot_does_not_overwrite_canonical_isbn(self):
        book = Book(
            isbn="9786267244913",
            title="也許你該找人聊聊２（二版）",
            author="蘿蕊・葛利布",
            cover_url="https://metadata.test/canonical.jpg",
            category="心理勵志",
        )

        changed = apply_platform_snapshot(
            book,
            platform_book_id="14286275",
            raw_title="不可信的暫時標題",
            crawler_cover="https://readmoo.test/raw.jpg",
        )

        self.assertFalse(changed)
        self.assertEqual(book.title, "也許你該找人聊聊２（二版）")

    def test_platform_snapshot_does_not_replace_cover_with_placeholder(self):
        book = Book(
            isbn="14286275",
            title="也許你該找人聊聊２（二版）",
            author="蘿蕊・葛利布",
            cover_url="https://metadata.test/sequel.jpg",
            category="心理勵志",
        )

        changed = apply_platform_snapshot(
            book,
            platform_book_id="14286275",
            raw_title="也許你該找人聊聊2",
            crawler_cover="/images/openbook.png",
        )

        self.assertTrue(changed)
        self.assertEqual(book.title, "也許你該找人聊聊2")
        self.assertEqual(
            book.cover_url,
            "https://metadata.test/sequel.jpg",
        )

    def test_metadata_decision_canonicalizes_exact_isbn(self):
        decision = decide_metadata_match(
            identifier="9786267747308",
            raw_title="鯨",
            metadata={
                "isbn": "9786267747308",
                "isbn_valid": True,
                "title": "鯨",
                "author": "千明官",
                "confidence": 0.87,
                "source": "ncl",
                "identifiers": ["9786267747308"],
            },
        )

        self.assertEqual(
            decision.action,
            MetadataMatchAction.CANONICALIZE,
        )
        self.assertEqual(decision.canonical_isbn, "9786267747308")

    def test_metadata_decision_requires_two_fields_for_new_canonical_isbn(self):
        decision = decide_metadata_match(
            identifier="readmoo-product-id",
            raw_title="完整長篇書名",
            raw_author="測試作者",
            metadata={
                "title": "完整長篇書名",
                "author": "測試作者",
                "contributors": [
                    {"name": "測試作者", "role": "作者"},
                ],
                "confidence": 0.9,
                "source": "ncl",
                "identifiers": ["9786267747308"],
            },
        )

        self.assertEqual(
            decision.action,
            MetadataMatchAction.CANONICALIZE,
        )
        self.assertEqual(decision.canonical_isbn, "9786267747308")

    def test_metadata_decision_rejects_conflicting_isbn(self):
        decision = decide_metadata_match(
            identifier="9786267747308",
            raw_title="鯨",
            metadata={
                "title": "另一個版本",
                "confidence": 0.9,
                "source": "ncl",
                "identifiers": ["9789863267386"],
            },
        )

        self.assertEqual(decision.action, MetadataMatchAction.REJECT)
        self.assertTrue(decision.evidence.isbn_conflict)

    def test_local_readmoo_snapshot_upserts_without_cookie_data(self):
        payload = readmoo_replication.ReadmooSnapshotPayload(
            user_id="reader",
            books=[
                readmoo_replication.ReadmooBookPayload(
                    isbn="owned-book",
                    title="本機書櫃書籍",
                    author="本機作者",
                    category="文學小說",
                    platform_book_id="readmoo-product-1",
                ),
            ],
            wishlist_synced=True,
            wishlist=[
                readmoo_replication.ReadmooBookPayload(
                    isbn="wanted-book",
                    title="本機待購書籍",
                    category="人文社科",
                ),
            ],
        )
        with Session(self.engine) as session:
            session.add_all([
                Book(isbn="stale-book", title="舊待購", category="未分類"),
                WishlistItem(
                    user_id="reader",
                    isbn="stale-book",
                    platform="readmoo",
                    sync_status="synced",
                ),
                WishlistItem(
                    user_id="reader",
                    isbn="stale-book",
                    platform="kobo",
                    sync_status="synced",
                ),
            ])
            session.commit()
            with patch.object(readmoo_replication, "set_platform_session_status"):
                result = readmoo_replication.apply_readmoo_snapshot(
                    session,
                    payload,
                )
            purchases = session.exec(select(Purchase)).all()
            wish_items = session.exec(select(WishlistItem)).all()

        self.assertEqual(result["purchases_added"], 1)
        self.assertEqual(result["wishlist_removed"], 1)
        self.assertEqual(
            {(purchase.isbn, purchase.platform) for purchase in purchases},
            {("owned-book", "readmoo")},
        )
        self.assertEqual(
            {(item.isbn, item.platform) for item in wish_items},
            {("wanted-book", "readmoo"), ("stale-book", "kobo")},
        )

    def test_add_by_title_refines_missing_fields_with_resolved_isbn(self):
        first_match = {
            "source": "readmoo",
            "title": "待補資料書籍",
            "author": "未知作者",
            "category": "未分類",
            "standard_category": "未分類",
            "identifiers": ["9789571078304"],
        }
        exact_match = {
            "source": "ncl",
            "isbn": "9789571078304",
            "isbn_valid": True,
            "title": "待補資料書籍",
            "author": "完整作者",
            "category": "文學小說",
            "standard_category": "文學小說",
            "identifiers": ["9789571078304"],
        }
        metadata_lookup = AsyncMock(
            side_effect=[first_match, exact_match],
        )

        with (
            Session(self.engine) as session,
            patch(
                "app.api.wishlist.fetch_and_clean_metadata",
                metadata_lookup,
            ),
        ):
            result = asyncio.run(add_to_wishlist(
                WishlistCreate(user_id="reader", query="待補資料書籍"),
                BackgroundTasks(),
                session,
            ))

        self.assertEqual(metadata_lookup.await_count, 2)
        self.assertEqual(result["book"]["author"], "完整作者")
        self.assertEqual(result["book"]["category"], "文學小說")

    def test_get_wishlist_enriches_title_only_imported_book(self):
        metadata = {
            "source": "readmoo",
            "isbn": "",
            "isbn_valid": False,
            "title": "遠端匯入書籍",
            "author": "遠端作者",
            "category": "人文社科",
            "standard_category": "人文社科",
            "cover_url": "https://example.test/imported.jpg",
        }
        with (
            Session(self.engine) as session,
            patch(
                "app.api.wishlist.fetch_and_clean_metadata",
                AsyncMock(return_value=metadata),
            ),
        ):
            session.add(Book(
                isbn="remote-platform-id",
                title="遠端匯入書籍",
                author="未知作者",
                category="未分類",
            ))
            session.add(WishlistItem(
                user_id="reader",
                isbn="remote-platform-id",
                platform="readmoo",
                sync_status="synced",
            ))
            session.commit()

            result = asyncio.run(get_wishlist(
                user_id="reader",
                session=session,
            ))

        self.assertEqual(result[0]["author"], "遠端作者")
        self.assertEqual(result[0]["category"], "人文社科")
        self.assertEqual(
            result[0]["cover_url"],
            "https://example.test/imported.jpg",
        )

    def test_bulk_transfer_rejects_two_platforms(self):
        with Session(self.engine) as session:
            with self.assertRaises(HTTPException) as raised:
                asyncio.run(transfer_to_library(
                    WishlistTransfer(
                        user_id="reader",
                        isbns=["book-a", "book-b"],
                        platforms=["kobo", "readmoo"],
                    ),
                    BackgroundTasks(),
                    session,
                ))
        self.assertEqual(raised.exception.status_code, 400)

    def test_bulk_transfer_to_one_platform_creates_purchases(self):
        with Session(self.engine) as session:
            for isbn in ("book-a", "book-b"):
                session.add(Book(
                    isbn=isbn,
                    title=isbn,
                    category="文學小說",
                ))
                session.add(WishlistItem(
                    user_id="reader",
                    isbn=isbn,
                    platform="readmoo",
                    sync_status="synced",
                ))
            session.commit()

            asyncio.run(transfer_to_library(
                WishlistTransfer(
                    user_id="reader",
                    isbns=["book-a", "book-b"],
                    platforms=["kobo"],
                ),
                BackgroundTasks(),
                session,
            ))
            purchases = session.exec(select(Purchase)).all()
            wishlist_items = session.exec(select(WishlistItem)).all()

        self.assertEqual(
            {(row.isbn, row.platform) for row in purchases},
            {("book-a", "kobo"), ("book-b", "kobo")},
        )
        self.assertEqual(wishlist_items, [])


class PlatformStatusTests(unittest.TestCase):
    def test_missing_state_requires_update(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            with patch.object(auth, "BASE_DIR", Path(temporary_directory)):
                status = auth._inspect_platform_state(
                    "reader",
                    "kobo",
                    None,
                )

        self.assertEqual(status["status"], "missing")
        self.assertTrue(status["needs_update"])

    def test_session_cookie_requires_recent_active_verification(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            state_dir = (
                Path(temporary_directory)
                / "user_profiles"
                / "reader"
                / "readmoo"
            )
            state_dir.mkdir(parents=True)
            (state_dir / "state.json").write_text(
                (
                    '{"cookies":[{"name":"oauth_token","value":"ok",'
                    '"domain":"read.readmoo.com","expires":-1}]}'
                ),
                encoding="utf-8",
            )
            with patch.object(auth, "BASE_DIR", Path(temporary_directory)):
                active = auth._inspect_platform_state(
                    "reader",
                    "readmoo",
                    PlatformSession(
                        user_id="reader",
                        platform="readmoo",
                        status="active",
                        updated_at=datetime.utcnow(),
                    ),
                )
                unverified = auth._inspect_platform_state(
                    "reader",
                    "readmoo",
                    None,
                )
                expired = auth._inspect_platform_state(
                    "reader",
                    "readmoo",
                    PlatformSession(
                        user_id="reader",
                        platform="readmoo",
                        status="expired",
                        updated_at=datetime.utcnow(),
                    ),
                )

        self.assertEqual(active["status"], "active")
        self.assertFalse(active["needs_update"])
        self.assertEqual(unverified["status"], "unverified")
        self.assertTrue(unverified["needs_update"])
        self.assertEqual(expired["status"], "expired")
        self.assertTrue(expired["needs_update"])

    def test_blocked_session_is_not_reported_as_active(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            state_dir = (
                Path(temporary_directory)
                / "user_profiles"
                / "reader"
                / "readmoo"
            )
            state_dir.mkdir(parents=True)
            (state_dir / "state.json").write_text(
                ('{"cookies":[{"name":"oauth_token","value":"ok",'
                 '"domain":"read.readmoo.com","expires":-1}]}'),
                encoding="utf-8",
            )
            with patch.object(auth, "BASE_DIR", Path(temporary_directory)):
                status = auth._inspect_platform_state(
                    "reader",
                    "readmoo",
                    PlatformSession(
                        user_id="reader",
                        platform="readmoo",
                        status="blocked",
                        updated_at=datetime.utcnow(),
                    ),
                )

        self.assertEqual(status["status"], "blocked")
        self.assertTrue(status["needs_update"])

    def test_remote_readmoo_sync_is_not_reported_as_vps_login(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            with patch.object(auth, "BASE_DIR", Path(temporary_directory)):
                status = auth._inspect_platform_state(
                    "reader",
                    "readmoo",
                    PlatformSession(
                        user_id="reader",
                        platform="readmoo",
                        status="remote_synced",
                        updated_at=datetime.utcnow(),
                    ),
                )

        self.assertEqual(status["status"], "remote_synced")
        self.assertFalse(status["needs_update"])

    def test_tracking_cookies_do_not_count_as_platform_login(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            state_dir = (
                Path(temporary_directory)
                / "user_profiles"
                / "reader"
                / "kobo"
            )
            state_dir.mkdir(parents=True)
            (state_dir / "state.json").write_text(
                (
                    '{"cookies":[{"name":"_ga","value":"tracking",'
                    '"domain":".kobo.com","expires":-1}]}'
                ),
                encoding="utf-8",
            )
            with patch.object(auth, "BASE_DIR", Path(temporary_directory)):
                status = auth._inspect_platform_state(
                    "reader",
                    "kobo",
                    None,
                )

        self.assertEqual(status["status"], "expired")
        self.assertTrue(status["needs_update"])

    def test_login_endpoint_returns_saved_cookie_result(self):
        expected = {
            "status": "success",
            "platform": "readmoo",
            "cookie_count": 3,
            "message": "readmoo 登入憑證已更新",
        }
        with patch.object(
            auth,
            "login_and_save_platform_state",
            AsyncMock(return_value=expected),
        ):
            result = asyncio.run(auth.login_platform("reader", "readmoo"))

        self.assertEqual(result, expected)

    def test_readmoo_callback_must_finish_before_login_redirects(self):
        class FakePage:
            def __init__(self, url):
                self.url = url

        self.assertFalse(platform_auth._readmoo_storefront_callback_completed(
            FakePage("https://www.readmoo.com/?key=true"),
        ))
        self.assertTrue(platform_auth._readmoo_storefront_callback_completed(
            FakePage("https://www.readmoo.com/"),
        ))
        self.assertTrue(platform_auth._readmoo_storefront_callback_completed(
            FakePage("https://read.readmoo.com/#/dashboard"),
        ))

    def test_readmoo_login_follows_dashboard_opened_in_new_tab(self):
        class FakePage:
            def __init__(self, url, closed=False):
                self.url = url
                self.closed = closed

            def is_closed(self):
                return self.closed

        original = FakePage("https://readmoo.com/")
        dashboard = FakePage("https://read.readmoo.com/#/dashboard")
        context = unittest.mock.Mock(pages=[original, dashboard])

        selected = platform_auth._select_login_page(
            context,
            original,
            "readmoo",
        )

        self.assertIs(selected, dashboard)

    def test_login_keeps_original_page_without_readmoo_dashboard(self):
        class FakePage:
            def __init__(self, url):
                self.url = url

            def is_closed(self):
                return False

        original = FakePage("https://readmoo.com/")
        popup = FakePage("https://idp.readmoo.com/oauth2/authorize")
        context = unittest.mock.Mock(pages=[original, popup])

        selected = platform_auth._select_login_page(
            context,
            original,
            "readmoo",
        )

        self.assertIs(selected, original)

    def test_readmoo_uses_bundled_chromium_by_default(self):
        chromium = unittest.mock.Mock()
        chromium.launch = AsyncMock(return_value="browser")
        playwright = unittest.mock.Mock(chromium=chromium)

        with (
            patch.object(platform_auth, "READMOO_BROWSER_CHANNEL", ""),
            patch.object(platform_auth, "READMOO_BROWSER_PROXY", ""),
        ):
            browser = asyncio.run(platform_auth.launch_readmoo_browser(
                playwright,
                headless=False,
            ))

        self.assertEqual(browser, "browser")
        chromium.launch.assert_awaited_once_with(
            headless=False,
            args=["--disable-blink-features=AutomationControlled"],
        )

    def test_readmoo_can_use_installed_chrome_channel(self):
        chromium = unittest.mock.Mock()
        chromium.launch = AsyncMock(return_value="browser")
        playwright = unittest.mock.Mock(chromium=chromium)

        with (
            patch.object(platform_auth, "READMOO_BROWSER_CHANNEL", "chrome"),
            patch.object(
                platform_auth,
                "READMOO_BROWSER_PROXY",
                "socks5://readmoo-vpn:1080",
            ),
        ):
            asyncio.run(platform_auth.launch_readmoo_browser(
                playwright,
                headless=False,
            ))

        chromium.launch.assert_awaited_once_with(
            channel="chrome",
            headless=False,
            args=["--disable-blink-features=AutomationControlled"],
            proxy={"server": "socks5://readmoo-vpn:1080"},
        )

    def test_login_endpoint_returns_403_when_readmoo_is_waf_blocked(self):
        with patch.object(
            auth,
            "login_and_save_platform_state",
            AsyncMock(side_effect=platform_auth.PlatformLoginBlocked("blocked")),
        ):
            with self.assertRaises(HTTPException) as raised:
                asyncio.run(auth.login_platform("reader", "readmoo"))

        self.assertEqual(raised.exception.status_code, 403)

    def test_wishlist_import_returns_platform_outcomes(self):
        with (
            patch(
                "app.api.wishlist.import_readmoo_wishlist_to_db",
                AsyncMock(return_value={
                    "platform": "readmoo",
                    "status": "blocked",
                    "books": 0,
                    "message": "blocked",
                }),
            ),
            patch(
                "app.api.wishlist.import_kobo_wishlist_to_db",
                AsyncMock(return_value={
                    "platform": "kobo",
                    "status": "success",
                    "books": 2,
                    "message": "ok",
                }),
            ),
        ):
            result = asyncio.run(trigger_wishlist_import("reader"))

        self.assertEqual(result["statuses"], {
            "readmoo": "blocked",
            "kobo": "success",
        })
        self.assertEqual(result["blocked"], ["readmoo"])

    def test_saved_state_excludes_unrelated_oauth_cookies(self):
        class FakeContext:
            async def storage_state(self):
                return {
                    "cookies": [
                        {
                            "name": "oauth_token",
                            "domain": "read.readmoo.com",
                            "value": "book-session",
                        },
                        {
                            "name": "SID",
                            "domain": ".google.com",
                            "value": "unrelated",
                        },
                    ],
                    "origins": [],
                }

        with tempfile.TemporaryDirectory() as temporary_directory:
            state_path = Path(temporary_directory) / "state.json"
            state = asyncio.run(platform_auth.save_platform_storage_state(
                FakeContext(),
                state_path,
                "readmoo",
            ))

        self.assertEqual(len(state["cookies"]), 1)
        self.assertEqual(state["cookies"][0]["name"], "oauth_token")


class LibraryImportResultTests(unittest.TestCase):
    def test_auth_required_platform_is_returned_to_frontend(self):
        from main import import_library

        with (
            patch(
                "main.import_readmoo_library_to_db",
                AsyncMock(return_value={
                    "platform": "readmoo",
                    "status": "auth_required",
                    "message": "Readmoo 登入憑證已失效",
                    "new_books": 0,
                }),
            ),
            patch(
                "main.import_kobo_library_to_db",
                AsyncMock(return_value={
                    "platform": "kobo",
                    "status": "success",
                    "message": "Kobo 書櫃同步完成",
                    "new_books": 1,
                }),
            ),
        ):
            result = asyncio.run(import_library("reader", limit=None))

        self.assertEqual(result["status"], "auth_required")
        self.assertEqual(result["needs_auth"], ["readmoo"])
        self.assertEqual(len(result["results"]), 2)


if __name__ == "__main__":
    unittest.main()
