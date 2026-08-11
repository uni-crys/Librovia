from dotenv import load_dotenv
load_dotenv()
# main.py
import asyncio
import logging
from fastapi import BackgroundTasks, FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware
from apscheduler.schedulers.background import BackgroundScheduler
from contextlib import asynccontextmanager
from app.config import settings
from app.database import init_db
from app.api import auth, books, health, readmoo_replication, wishlist
from app.services.readmoo_worker import import_readmoo_wishlist_to_db
from app.services.readmoo_library_worker import import_readmoo_library_to_db
from app.services.kobo_worker import import_kobo_wishlist_to_db
from app.services.kobo_library_worker import import_kobo_library_to_db
from app.services.metadata_pipeline import close_metadata_client
from app.services.library_import_queue import (
    metadata_queue_status,
    process_metadata_queue,
    retry_incomplete_metadata_jobs,
)
from app.observability import (
    configure_logging,
    request_logging_middleware,
    run_sync_job,
)

configure_logging()
logger = logging.getLogger("librovia.app")

# 初始化背景排程器
scheduler = BackgroundScheduler()

def scheduled_sync_job():
    """
    定時自動同步任務 (例如每 24 小時執行一次)
    由於 Worker 內的 Playwright 函式是非同步 (async) 的，
    因此需透過 asyncio.run 在同步排程中執行它們。
    """
    default_user_id = "default_user"
    for platform, worker in (
        ("readmoo", import_readmoo_wishlist_to_db),
        ("kobo", import_kobo_wishlist_to_db),
    ):
        try:
            asyncio.run(run_sync_job(
                "scheduled_wishlist_import",
                platform,
                worker,
                default_user_id,
            ))
        except Exception:
            # run_sync_job already emitted the exception and structured fields.
            continue


def scheduled_metadata_job():
    asyncio.run(process_metadata_queue())


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    logger.info("Database migrations applied")

    scheduler.add_job(
        scheduled_sync_job,
        "interval",
        hours=24,
        id="scheduled_wishlist_sync",
        replace_existing=True,
    )
    scheduler.add_job(
        scheduled_metadata_job,
        "interval",
        minutes=1,
        id="metadata_queue_processor",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
    scheduler.start()
    logger.info("Background scheduler started", extra={"interval_hours": 24})
    try:
        yield
    finally:
        if scheduler.running:
            scheduler.shutdown()
        await close_metadata_client()
        logger.info("Background scheduler stopped")


app = FastAPI(
    title="Librovia API",
    description="電子書與待購清單自動化管理系統",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.middleware("http")(request_logging_middleware)

# 掛載各模組路由
app.include_router(wishlist.router, prefix="/wishlist", tags=["Wishlist"])
app.include_router(books.router, prefix="/books", tags=["Books"])
app.include_router(auth.router, prefix="/auth", tags=["Auth"])
app.include_router(
    readmoo_replication.router,
    prefix="/internal",
    tags=["Readmoo replication"],
)
app.include_router(health.router)


@app.post("/library/import")
async def import_library(
    user_id: str,
    background_tasks: BackgroundTasks = None,
    limit: int | None = Query(default=None, ge=1, le=50),
):
    results = []
    workers = (
        ("readmoo", import_readmoo_library_to_db),
        ("kobo", import_kobo_library_to_db),
    )
    for platform, worker in workers:
        try:
            result = await run_sync_job(
                "library_import",
                platform,
                worker,
                user_id,
                limit=limit,
            )
            results.append(result or {
                "platform": platform,
                "status": "failed",
                "message": f"{platform} 同步未回傳結果",
                "new_books": 0,
            })
        except Exception:
            results.append({
                "platform": platform,
                "status": "failed",
                "message": f"{platform} 書櫃同步失敗，請稍後再試",
                "new_books": 0,
            })

    needs_auth = [
        result["platform"]
        for result in results
        if result["status"] == "auth_required"
    ]
    failed = [
        result["platform"]
        for result in results
        if result["status"] == "failed"
    ]

    if needs_auth:
        status = "auth_required"
        labels = [
            "Readmoo" if platform == "readmoo" else "Kobo"
            for platform in needs_auth
        ]
        message = f"{'、'.join(labels)} 登入憑證需要更新"
    elif failed:
        status = "partial_failure"
        labels = [
            "Readmoo" if platform == "readmoo" else "Kobo"
            for platform in failed
        ]
        message = f"{'、'.join(labels)} 同步失敗，其他平台結果已保留"
    else:
        status = "success"
        message = "Readmoo 與 Kobo 已購書櫃同步完成"

    metadata_jobs = sum(
        int(result.get("metadata_jobs") or 0)
        for result in results
    )
    if metadata_jobs and background_tasks is not None:
        background_tasks.add_task(process_metadata_queue)

    return {
        "status": status,
        "message": message,
        "needs_auth": needs_auth,
        "results": results,
        "limit_per_platform": limit,
        "metadata_jobs": metadata_jobs,
        "metadata_status": (
            "queued" if metadata_jobs else "not_needed"
        ),
    }


@app.get("/library/metadata-status")
def get_library_metadata_status(user_id: str):
    return metadata_queue_status(user_id)


@app.post("/library/metadata-retry")
async def retry_library_metadata(
    user_id: str,
    background_tasks: BackgroundTasks,
    platform: str | None = None,
    platform_book_id: str | None = None,
):
    retried = retry_incomplete_metadata_jobs(
        user_id,
        platform=platform,
        platform_book_id=platform_book_id,
    )
    if retried:
        background_tasks.add_task(process_metadata_queue)
    return {
        "status": "queued" if retried else "not_needed",
        "retried": retried,
        "message": (
            f"已重新排入 {retried} 筆缺少資料的書籍"
            if retried
            else "目前沒有需要強制重試的書籍"
        ),
    }


@app.get("/")
def root():
    return {"message": "Welcome to Librovia API is running!"}
