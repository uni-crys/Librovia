from typing import Optional, List
from sqlalchemy import UniqueConstraint
from sqlmodel import SQLModel, Field, Relationship
from datetime import datetime

class Book(SQLModel, table=True):
    """the intergrated books info cache table"""
    __tablename__ = "sttandard_books" # the table name in the database

    isbn:str = Field(primary_key = True)
    title: str = Field(index=True) # 書名
    author: Optional[str] = None # 作者
    cover_url: Optional[str] = None # 封面圖片 URL
    category: str = Field(default="Unkown")
    updated_at: datetime = Field(default_factory=datetime.utcnow) # the last update time of the book info

    purchases: List["Purchase"] = Relationship(back_populates="book")
    whishlist_items: List["WishlistItem"] = Relationship(back_populates="book", sa_relationship_kwargs={"cascade": "delete"})

class Purchase(SQLModel, table=True):
    __tablename__ = "user_purchases" # the table name in the database
    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: str = Field(index=True) # the user id
    platform: str # Readmoo or Kobo currently supported
    platform_book_id: Optional[str] # the book id in the platform, for example, Readmoo's book id or Kobo's book id
    isbn: str = Field(foreign_key="sttandard_books.isbn", index = True)
    detail_attempts: int = Field(default=0)
    detail_status: str = Field(default="pending")
    detail_last_attempt_at: Optional[datetime] = None
    detail_next_retry_at: Optional[datetime] = None
    book: Book = Relationship(back_populates="purchases")

class WishlistItem(SQLModel, table=True):
    __tablename__ = "user_wishlist" # the table name in the database
    __table_args__ = (
        UniqueConstraint(
            "user_id",
            "isbn",
            "platform",
            name="uq_user_wishlist_user_book_platform",
        ),
    )
    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: str = Field(index=True) # the user id
    platform: str
    platform_book_id: Optional[str] = Field(default=None, index=True)

    #the sync status of the wishlist item, for example, "synced" or "not_synced"
    sync_status: str = Field(default="pending")
    updated_at: datetime = Field(default_factory=datetime.utcnow) # the last update time of the wishlist item
    
    isbn: str = Field(foreign_key="sttandard_books.isbn", index=True)
    book: Book = Relationship(back_populates="whishlist_items")

class PlatformSession(SQLModel, table=True):
    """the table to store the platform session info for each user"""
    __tablename__ = "platform_sessions"

    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: str = Field(index=True)
    platform: str
    status: str = Field(default="inactive")
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class MetadataJob(SQLModel, table=True):
    """Durable enrichment work created after a fast platform snapshot."""

    __tablename__ = "metadata_jobs"
    __table_args__ = (
        UniqueConstraint(
            "user_id",
            "platform",
            "platform_book_id",
            name="uq_metadata_job_platform_book",
        ),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: str = Field(index=True)
    platform: str = Field(index=True)
    platform_book_id: str
    raw_identifier: str
    raw_title: str
    crawler_cover: Optional[str] = None
    status: str = Field(default="pending", index=True)
    attempts: int = Field(default=0)
    result: Optional[str] = None
    last_error_type: Optional[str] = None
    next_retry_at: Optional[datetime] = Field(default=None, index=True)
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)
