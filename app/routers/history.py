"""Consulta de historial de precios, estadísticas y ejecuciones. Solo lectura.

Orden determinista en todos los listados: fecha DESC y, ante empate, id DESC.
Los filtros `from`/`to` son inclusivos y exigen zona horaria (un instante sin zona es ambiguo).
Nota: en una URL el `+` de un desplazamiento horario debe ir como `%2B` (o usar `Z`).
"""

from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Annotated, Literal

from fastapi import APIRouter, HTTPException, Path, Query, status
from fastapi.exceptions import RequestValidationError
from pydantic import AwareDatetime
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import aggregate_order_by, array_agg
from sqlalchemy.sql import Select

from app.models import CheckRun, PriceHistory, Product
from app.routers.products import MAX_DB_INT, DbSession
from app.schemas import CheckRunItemOut, CurrencyStatsOut, HistoryItemOut, StatsOut

router = APIRouter(prefix="/products/{product_id}", tags=["history"])

ProductId = Annotated[int, Path(ge=1, le=MAX_DB_INT)]
Limit = Annotated[int, Query(ge=1, le=100)]
Offset = Annotated[int, Query(ge=0)]
# AwareDatetime: pydantic rechaza fechas sin zona horaria con el 422 estándar.
From = Annotated[AwareDatetime | None, Query(alias="from")]
To = Annotated[AwareDatetime | None, Query()]

PCT = Decimal("0.01")


def _require_product(db: DbSession, product_id: int) -> None:
    if db.get(Product, product_id) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Producto no encontrado")


def _check_range(frm: datetime | None, to: datetime | None) -> None:
    if frm is not None and to is not None and frm > to:
        raise RequestValidationError(
            [
                {
                    "type": "value_error",
                    "loc": ("query", "from"),
                    "msg": "'from' no puede ser posterior a 'to'",
                    "input": frm.isoformat(),
                }
            ]
        )


def _observation_filters(stmt: Select, product_id: int, frm, to) -> Select:
    stmt = stmt.where(PriceHistory.product_id == product_id)
    if frm is not None:
        stmt = stmt.where(PriceHistory.observed_at >= frm)
    if to is not None:
        stmt = stmt.where(PriceHistory.observed_at <= to)
    return stmt


@router.get("/history", response_model=list[HistoryItemOut])
def get_history(
    product_id: ProductId,
    db: DbSession,
    limit: Limit = 50,
    offset: Offset = 0,
    from_: From = None,
    to: To = None,
) -> list[PriceHistory]:
    _check_range(from_, to)
    _require_product(db, product_id)
    stmt = _observation_filters(select(PriceHistory), product_id, from_, to)
    stmt = stmt.order_by(PriceHistory.observed_at.desc(), PriceHistory.id.desc())
    return list(db.scalars(stmt.limit(limit).offset(offset)))


@router.get("/check-runs", response_model=list[CheckRunItemOut])
def get_check_runs(
    product_id: ProductId,
    db: DbSession,
    limit: Limit = 50,
    offset: Offset = 0,
    run_status: Annotated[Literal["success", "error"] | None, Query(alias="status")] = None,
) -> list[CheckRun]:
    _require_product(db, product_id)
    stmt = select(CheckRun).where(CheckRun.product_id == product_id)
    if run_status is not None:
        stmt = stmt.where(CheckRun.status == run_status)
    stmt = stmt.order_by(CheckRun.started_at.desc(), CheckRun.id.desc())
    return list(db.scalars(stmt.limit(limit).offset(offset)))


def _first(column, *order):
    """Primer valor de `column` según `order` (array_agg ordenado, índice 1 de PostgreSQL)."""
    return array_agg(aggregate_order_by(column, *order))[1]


@router.get("/stats", response_model=StatsOut)
def get_stats(
    product_id: ProductId,
    db: DbSession,
    from_: From = None,
    to: To = None,
) -> StatsOut:
    _check_range(from_, to)
    _require_product(db, product_id)

    p = PriceHistory
    oldest_first = (p.observed_at.asc(), p.id.asc())
    newest_first = (p.observed_at.desc(), p.id.desc())
    # UNA sola sentencia SELECT: PostgreSQL le da una única instantánea de los datos
    # (aislamiento READ COMMITTED por defecto), así que count, min, max, primero y último
    # son siempre coherentes entre sí aunque otra transacción inserte observaciones al
    # mismo tiempo. Agrupa por moneda: nunca se mezclan ni se convierten monedas.
    stmt = _observation_filters(
        select(
            p.currency,
            func.count().label("count"),
            func.min(p.price).label("min"),
            _first(p.observed_at, p.price.asc(), *newest_first).label("min_observed_at"),
            func.max(p.price).label("max"),
            _first(p.observed_at, p.price.desc(), *newest_first).label("max_observed_at"),
            _first(p.price, *oldest_first).label("first"),
            _first(p.observed_at, *oldest_first).label("first_observed_at"),
            _first(p.price, *newest_first).label("latest"),
            _first(p.observed_at, *newest_first).label("latest_observed_at"),
        ),
        product_id,
        from_,
        to,
    )
    rows = db.execute(stmt.group_by(p.currency).order_by(p.currency)).all()

    by_currency = []
    for row in rows:
        change = row.latest - row.first  # Decimal exacto; el CHECK price > 0 evita /0
        pct = (change / row.first * 100).quantize(PCT, rounding=ROUND_HALF_UP)
        by_currency.append(
            CurrencyStatsOut(**row._mapping, change=change, change_pct=pct)  # type: ignore[arg-type]
        )
    return StatsOut(product_id=product_id, by_currency=by_currency)
