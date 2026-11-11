from datetime import datetime
from decimal import Decimal

from sqlalchemy import CheckConstraint, DateTime, Numeric, Text, func
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
