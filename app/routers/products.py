from typing import Annotated

import httpx2
from fastapi import APIRouter, Depends, HTTPException, Path, Query, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import Product
from app.schemas import CheckResultOut, ProductCreate, ProductOut
from app.services.checker import CheckRejected, UnsupportedStore, run_check
from app.services.fetcher import Resolver, get_http_client, get_resolver
from app.services.stores import SUPPORTED_STORES
from app.urls import store_from_url

router = APIRouter(prefix="/products", tags=["products"])

UNIQUE_URL_CONSTRAINT = "uq_products_url"
MAX_DB_INT = 2_147_483_647  # límite de INTEGER en PostgreSQL

DbSession = Annotated[Session, Depends(get_db)]
HttpClient = Annotated[httpx2.Client, Depends(get_http_client)]
DnsResolver = Annotated[Resolver, Depends(get_resolver)]

# Códigos de error de una revisión -> HTTP. Cualquier otro fallo del destino es 502.
CHECK_ERROR_STATUS = {"timeout": 504, "persist_failed": 500}


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


@router.post(
    "/{product_id}/check", response_model=CheckResultOut, status_code=status.HTTP_201_CREATED
)
def check_product(
    product_id: Annotated[int, Path(ge=1, le=MAX_DB_INT)],
    db: DbSession,
    client: HttpClient,
    resolver: DnsResolver,
) -> CheckResultOut:
    product = db.get(Product, product_id)
    if product is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Producto no encontrado")
    try:
        outcome = run_check(db, product, client, resolver)
    except UnsupportedStore as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={
                "code": "unsupported_store",
                "message": "La tienda de este producto no está soportada",
                "store": exc.store,
                "supported": sorted(SUPPORTED_STORES),
            },
        ) from exc
    except CheckRejected as exc:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail={"code": exc.code, "message": exc.message, "retry_after": exc.retry_after},
            headers={"Retry-After": str(exc.retry_after)},
        ) from exc

    if outcome.observation is None:
        run = outcome.run
        raise HTTPException(
            status_code=CHECK_ERROR_STATUS.get(run.error_code, status.HTTP_502_BAD_GATEWAY),
            detail={
                "code": run.error_code,
                "message": run.error_message,
                "check_run_id": run.id,
                "http_status": run.http_status,
            },
        )
    return CheckResultOut(check_run=outcome.run, observation=outcome.observation)
