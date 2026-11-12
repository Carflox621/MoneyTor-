"""create check_runs and price_history

Revision ID: 0002
Revises: 0001
Create Date: 2026-11-12

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: Union[str, Sequence[str], None] = "0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "check_runs",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("product_id", sa.Integer(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("error_code", sa.Text(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("http_status", sa.SmallInteger(), nullable=True),
        sa.Column("requested_url", sa.Text(), nullable=False),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("duration_ms", sa.Integer(), nullable=False),
        sa.CheckConstraint(
            "status IN ('success', 'error')", name=op.f("ck_check_runs_status_valid")
        ),
        sa.CheckConstraint(
            "(status = 'success') = (error_code IS NULL)",
            name=op.f("ck_check_runs_error_code_matches_status"),
        ),
        sa.CheckConstraint(
            "error_message IS NULL OR char_length(error_message) <= 500",
            name=op.f("ck_check_runs_error_message_length"),
        ),
        sa.CheckConstraint(
            "char_length(requested_url) <= 2048",
            name=op.f("ck_check_runs_requested_url_max_length"),
        ),
        sa.CheckConstraint(
            "finished_at >= started_at", name=op.f("ck_check_runs_finished_after_started")
        ),
        sa.CheckConstraint("duration_ms >= 0", name=op.f("ck_check_runs_duration_non_negative")),
        sa.ForeignKeyConstraint(
            ["product_id"],
            ["products.id"],
            name=op.f("fk_check_runs_product_id_products"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_check_runs")),
        sa.UniqueConstraint("id", "product_id", name="uq_check_runs_id_product_id"),
    )
    op.create_index(
        "ix_check_runs_product_id_started_at",
        "check_runs",
        ["product_id", sa.text("started_at DESC")],
    )

    op.create_table(
        "price_history",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("product_id", sa.Integer(), nullable=False),
        sa.Column("check_run_id", sa.BigInteger(), nullable=False),
        sa.Column("price", sa.Numeric(precision=12, scale=2), nullable=False),
        sa.Column("currency", sa.CHAR(length=3), nullable=False),
        sa.Column("price_raw", sa.Text(), nullable=False),
        sa.Column("source_url", sa.Text(), nullable=False),
        sa.Column(
            "observed_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("price > 0", name=op.f("ck_price_history_price_positive")),
        sa.CheckConstraint("currency ~ '^[A-Z]{3}$'", name=op.f("ck_price_history_currency_iso")),
        sa.CheckConstraint(
            "char_length(price_raw) <= 100", name=op.f("ck_price_history_price_raw_length")
        ),
        sa.CheckConstraint(
            "char_length(source_url) <= 2048", name=op.f("ck_price_history_source_url_max_length")
        ),
        sa.ForeignKeyConstraint(
            ["check_run_id", "product_id"],
            ["check_runs.id", "check_runs.product_id"],
            name="fk_price_history_check_run_product",
        ),
        sa.ForeignKeyConstraint(
            ["product_id"],
            ["products.id"],
            name=op.f("fk_price_history_product_id_products"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_price_history")),
        sa.UniqueConstraint("check_run_id", name=op.f("uq_price_history_check_run_id")),
    )
    op.create_index(
        "ix_price_history_product_id_observed_at",
        "price_history",
        ["product_id", sa.text("observed_at DESC"), sa.text("id DESC")],
    )


def downgrade() -> None:
    op.drop_table("price_history")
    op.drop_table("check_runs")
