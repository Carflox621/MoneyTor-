from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Path, Query, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import Product
from app.schemas import ProductCreate, ProductOut
from app.urls import store_from_url

router = APIRouter(prefix="/products", tags=["products"])

UNIQUE_URL_CONSTRAINT = "uq_products_url"
MAX_DB_INT = 2_147_483_647  # límite de INTEGER en PostgreSQL

DbSession = Annotated[Session, Depends(get_db)]


@router.post("", response_model=ProductOut, status_code=status.HTTP_201_CREATED)
def create_product(payload: ProductCreate, db: DbSession) -> Product:
    product = Product(
        url=payload.url,  # ya normalizada por el esquema
        name=payload.name,
        store=store_from_url(payload.url),
        target_price=payload.target_price,
    )
    db.add(product)
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        # Solo la violación del UNIQUE de url es un 409; cualquier otra es un error real.
        constraint = getattr(getattr(exc.orig, "diag", None), "constraint_name", None)
        if constraint != UNIQUE_URL_CONSTRAINT:
            raise
        existing_id = db.scalar(select(Product.id).where(Product.url == payload.url))
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"message": "La URL ya está registrada", "existing_id": existing_id},
        ) from exc
    return product


@router.get("", response_model=list[ProductOut])
def list_products(
    db: DbSession,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[Product]:
    return list(db.scalars(select(Product).order_by(Product.id).limit(limit).offset(offset)))


@router.get("/{product_id}", response_model=ProductOut)
def get_product(product_id: Annotated[int, Path(ge=1, le=MAX_DB_INT)], db: DbSession) -> Product:
    product = db.get(Product, product_id)
    if product is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Producto no encontrado")
    return product
