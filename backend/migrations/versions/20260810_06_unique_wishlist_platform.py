"""Deduplicate platform wishlist rows and enforce their canonical identity."""

from alembic import op
import sqlalchemy as sa

revision = "20260810_06"
down_revision = "20260729_05"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("UPDATE user_wishlist SET platform = lower(platform)")
    op.execute(
        "DELETE FROM user_wishlist "
        "WHERE id NOT IN ("
        "SELECT max(id) FROM user_wishlist "
        "GROUP BY user_id, isbn, platform"
        ")"
    )
    with op.batch_alter_table("user_wishlist") as batch_op:
        batch_op.create_unique_constraint(
            "uq_user_wishlist_user_book_platform",
            ["user_id", "isbn", "platform"],
        )


def downgrade() -> None:
    with op.batch_alter_table("user_wishlist") as batch_op:
        batch_op.drop_constraint(
            "uq_user_wishlist_user_book_platform",
            type_="unique",
        )
