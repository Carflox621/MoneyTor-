from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    CHAR,
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    Numeric,
    SmallInteger,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class Product(Base):
    __tablename__ = "products"
    __table_args__ = (
        CheckConstraint("char_length(url) <= 2048", name="url_max_length"),
        CheckConstraint("store <> ''", name="store_not_empty"),
        CheckConstraint("name IS NULL OR char_length(name) BETWEEN 1 AND 200", name="name_length"),
        CheckConstraint("target_price IS NULL OR target_price > 0", name="target_price_positive"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    url: Mapped[str] = mapped_column(Text, unique=True)
    name: Mapped[str | None] = mapped_column(Text)
    store: Mapped[str] = mapped_column(Text)
    target_price: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class CheckRun(Base):
    """Un intento de revisión de precio (exitoso o fallido)."""

    __tablename__ = "check_runs"
    __table_args__ = (
        CheckConstraint("status IN ('success', 'error')", name="status_valid"),
        CheckConstraint(
            "(status = 'success') = (error_code IS NULL)", name="error_code_matches_status"
        ),
        CheckConstraint(
            "error_message IS NULL OR char_length(error_message) <= 500",
            name="error_message_length",
        ),
        CheckConstraint("char_length(requested_url) <= 2048", name="requested_url_max_length"),
        CheckConstraint("finished_at >= started_at", name="finished_after_started"),
        CheckConstraint("duration_ms >= 0", name="duration_non_negative"),
        # Permite la FK compuesta de price_history (observación y ejecución comparten producto).
        UniqueConstraint("id", "product_id", name="uq_check_runs_id_product_id"),
        Index("ix_check_runs_product_id_started_at", "product_id", text("started_at DESC")),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id", ondelete="RESTRICT"))
    status: Mapped[str] = mapped_column(Text)
    error_code: Mapped[str | None] = mapped_column(Text)
    error_message: Mapped[str | None] = mapped_column(Text)
    http_status: Mapped[int | None] = mapped_column(SmallInteger)
    requested_url: Mapped[str] = mapped_column(Text)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    finished_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    duration_ms: Mapped[int] = mapped_column(Integer)


class PriceHistory(Base):
    """Una observación de precio válida, ligada 1:1 a su ejecución exitosa."""

    __tablename__ = "price_history"
    __table_args__ = (
        ForeignKeyConstraint(
            ["check_run_id", "product_id"],
            ["check_runs.id", "check_runs.product_id"],
            name="fk_price_history_check_run_product",
        ),
        CheckConstraint("price > 0", name="price_positive"),
        CheckConstraint("currency ~ '^[A-Z]{3}$'", name="currency_iso"),
        CheckConstraint("char_length(price_raw) <= 100", name="price_raw_length"),
        CheckConstraint("char_length(source_url) <= 2048", name="source_url_max_length"),
        Index(
            "ix_price_history_product_id_observed_at",
            "product_id",
            text("observed_at DESC"),
            text("id DESC"),
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id", ondelete="RESTRICT"))
    check_run_id: Mapped[int] = mapped_column(BigInteger, unique=True)
    price: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    currency: Mapped[str] = mapped_column(CHAR(3))
    price_raw: Mapped[str] = mapped_column(Text)
    source_url: Mapped[str] = mapped_column(Text)
    observed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
