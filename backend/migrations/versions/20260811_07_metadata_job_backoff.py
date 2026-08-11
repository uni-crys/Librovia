"""Persist shared metadata queue retry schedules."""

from alembic import op
import sqlalchemy as sa

revision = "20260811_07"
down_revision = "20260810_06"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    columns = {
        column["name"]
        for column in inspector.get_columns("metadata_jobs")
    }
    if "next_retry_at" not in columns:
        op.add_column(
            "metadata_jobs",
            sa.Column("next_retry_at", sa.DateTime(), nullable=True),
        )
    indexes = {
        index["name"]
        for index in sa.inspect(op.get_bind()).get_indexes("metadata_jobs")
    }
    if "ix_metadata_jobs_next_retry_at" not in indexes:
        op.create_index(
            "ix_metadata_jobs_next_retry_at",
            "metadata_jobs",
            ["next_retry_at"],
            unique=False,
        )
    op.execute(
        "UPDATE metadata_jobs SET status = 'manual_review' "
        "WHERE status = 'failed' AND attempts >= 3"
    )


def downgrade() -> None:
    op.drop_index(
        "ix_metadata_jobs_next_retry_at",
        table_name="metadata_jobs",
    )
    with op.batch_alter_table("metadata_jobs") as batch_op:
        batch_op.drop_column("next_retry_at")
