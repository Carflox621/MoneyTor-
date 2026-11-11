from datetime import datetime
from decimal import Decimal
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.urls import normalize_url


class ProductCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    url: str
    name: str | None = None
    # Decimal exacto, nunca float. Máx. 12 dígitos en total, 2 decimales (Numeric(12,2)).
    target_price: Annotated[
        Decimal | None,
        Field(default=None, gt=0, max_digits=12, decimal_places=2, allow_inf_nan=False),
    ]

    @field_validator("url")
    @classmethod
    def _normalize_url(cls, value: str) -> str:
        return normalize_url(value)  # ValueError -> 422

    @field_validator("target_price")
    @classmethod
    def _two_decimals(cls, value: Decimal | None) -> Decimal | None:
        # Misma representación que devolverá PostgreSQL (Numeric(12,2)): "10.50".
        return None if value is None else value.quantize(Decimal("0.01"))

    @field_validator("name")
    @classmethod
    def _clean_name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        if not value:
            return None
        if len(value) > 200:
            raise ValueError("el nombre excede 200 caracteres")
        return value


class ProductOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    url: str
    name: str | None
    store: str
    target_price: Decimal | None
    created_at: datetime
