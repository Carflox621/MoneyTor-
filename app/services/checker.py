"""Orquesta una revisión de precio. Es la única pieza que escribe check_runs / price_history.

Conexiones: cada revisión usa UNA sola conexión del pool. Es la del advisory lock de
PostgreSQL por producto (a nivel de sesión), y una Session ORM ligada a esa misma conexión
lee y escribe con ella. Así el lock vive exactamente en la conexión que lo tomó (un lock de
sesión pertenece a una conexión) y ninguna petición retiene una conexión mientras pide otra
(sin hold-and-wait: con el pool agotado las peticiones se encolan, no se bloquean entre sí).
La sesión de la petición (`db`) libera su conexión ANTES de pedir la del lock.

Concurrencia: si otra revisión del mismo producto está en curso, la segunda se rechaza al
instante; si la anterior terminó hace menos de CHECK_MIN_INTERVAL_SECONDS, también.

Atomicidad: la ejecución exitosa, su observación y el nombre del producto se confirman en UNA
transacción. Ante cualquier fallo no se inserta precio: solo queda check_runs(status='error').
"""

import logging
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime

import httpx2
from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session

from app.config import settings
from app.models import CheckRun, PriceHistory, Product
from app.services.fetcher import FetchError, Resolver, default_resolver, fetch_html
from app.services.stores import SUPPORTED_STORES, Observation, ParseError

logger = logging.getLogger(__name__)

LOCK_NAMESPACE = 2  # classid del advisory lock: "revisión de precio"
MAX_MESSAGE_LENGTH = 500


class UnsupportedStore(Exception):
    def __init__(self, store: str) -> None:
        super().__init__(store)
        self.store = store


class CheckRejected(Exception):
    """No se intentó la revisión (no se registra check_run)."""

    def __init__(self, code: str, message: str, retry_after: int) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.retry_after = retry_after


@dataclass(frozen=True)
class CheckOutcome:
    run: CheckRun
    observation: PriceHistory | None  # None si la ejecución falló


def _record_error(
    session: Session,
    product_id: int,
    product_url: str,
    started_at: datetime,
    t0: float,
    code: str,
    message: str,
    http_status: int | None,
) -> CheckRun:
    run = CheckRun(
        product_id=product_id,
        status="error",
        error_code=code,
        error_message=message[:MAX_MESSAGE_LENGTH],
        http_status=http_status,
        requested_url=product_url,
        started_at=started_at,
        finished_at=datetime.now(UTC),
        duration_ms=int((time.monotonic() - t0) * 1000),
    )
    session.add(run)
    session.commit()
    return run


@contextmanager
def _locked_session(engine: Engine, product_id: int) -> Iterator[Session | None]:
    """Sesión ligada a una conexión DEDICADA que sostiene el advisory lock del producto.

    Rinde `None` si otra revisión del producto ya tiene el lock. Al salir (con o sin
    excepción): se cierra la sesión (rollback de lo no confirmado), se libera el lock y se
    devuelve la conexión al pool. Si algo de eso falla, la conexión se invalida: al cerrarse,
    PostgreSQL libera sus locks, así que nunca vuelve al pool con un lock puesto.
    """
    conn = engine.connect()
    acquired = False
    session: Session | None = None
    try:
        acquired = bool(conn.scalar(select(func.pg_try_advisory_lock(LOCK_NAMESPACE, product_id))))
        conn.commit()
        if acquired:
            session = Session(bind=conn, expire_on_commit=False)
        yield session
    finally:
        try:
            if session is not None:
                session.close()
            if acquired:
                conn.rollback()
                conn.scalar(select(func.pg_advisory_unlock(LOCK_NAMESPACE, product_id)))
                conn.commit()
        except Exception:
            logger.exception("no se pudo liberar el lock del producto %s", product_id)
            conn.invalidate()
        finally:
            conn.close()


def _release_request_connection(db: Session) -> None:
    """Devuelve al pool la conexión que la sesión de la petición abrió con `db.get(...)`.

    Se usa commit() y no rollback(): rollback() expira todas las instancias y obligaría a
    pedir otra conexión al releerlas. Es seguro solo si no hay nada pendiente, y eso se
    exige aquí en lugar de confirmar trabajo ajeno de forma implícita.
    """
    if db.new or db.dirty or db.deleted:
        raise RuntimeError("la sesión tiene cambios pendientes; no se confirmarán implícitamente")
    db.commit()


def run_check(
    db: Session,
    product: Product,
    client: httpx2.Client,
    resolver: Resolver = default_resolver,
) -> CheckOutcome:
    # Se copian los valores antes de soltar la conexión: después `product` no se vuelve a usar.
    product_id, product_url, store = product.id, product.url, product.store
    parse = SUPPORTED_STORES.get(store)
    if parse is None:
        raise UnsupportedStore(store)

    engine = db.get_bind()
    _release_request_connection(db)
    with _locked_session(engine, product_id) as session:
        if session is None:
            raise CheckRejected(
                "check_in_progress", "ya hay una revisión en curso para este producto", 1
            )
        return _run_locked(session, product_id, product_url, client, resolver, parse)


def _run_locked(
    session: Session,
    product_id: int,
    product_url: str,
    client: httpx2.Client,
    resolver: Resolver,
    parse: Callable[[str], Observation],
) -> CheckOutcome:
    interval = settings.CHECK_MIN_INTERVAL_SECONDS
    last = session.scalar(
        select(func.max(CheckRun.started_at)).where(CheckRun.product_id == product_id)
    )
    session.commit()  # termina la transacción de lectura antes de la petición HTTP
    started_at = datetime.now(UTC)
    if last is not None:
        elapsed = (started_at - last).total_seconds()
        if elapsed < interval:
            raise CheckRejected(
                "too_soon",
                f"el producto se revisó hace menos de {interval} s",
                max(1, int(interval - elapsed) + 1),
            )

    t0 = time.monotonic()

    def fail(code: str, message: str, http_status: int | None) -> CheckOutcome:
        run = _record_error(
            session, product_id, product_url, started_at, t0, code, message, http_status
        )
        return CheckOutcome(run, None)

    try:
        fetched = fetch_html(product_url, client, resolver)
    except FetchError as exc:
        return fail(exc.code, exc.message, exc.http_status)
    try:
        observation = parse(fetched.html)
    except ParseError as exc:
        return fail(exc.code, exc.message, fetched.http_status)
    except Exception:  # un fallo inesperado del parser también debe quedar registrado
        logger.exception("error inesperado al extraer el precio del producto %s", product_id)
        return fail("parse_error", "error inesperado al extraer", fetched.http_status)

    try:
        run = CheckRun(
            product_id=product_id,
            status="success",
            http_status=fetched.http_status,
            requested_url=product_url,
            started_at=started_at,
            finished_at=datetime.now(UTC),
            duration_ms=int((time.monotonic() - t0) * 1000),
        )
        session.add(run)
        session.flush()  # obtiene run.id sin confirmar
        history = PriceHistory(
            product_id=product_id,
            check_run_id=run.id,
            price=observation.price,
            currency=observation.currency,
            price_raw=observation.price_raw,
            source_url=fetched.final_url,
        )
        session.add(history)
        product = session.get(Product, product_id)  # fila fresca, en esta misma transacción
        if product is not None and product.name is None and observation.name:
            product.name = observation.name  # nunca pisa un nombre puesto por el usuario
        session.commit()
    except Exception:
        # Todo o nada: ni run de éxito sin precio ni precio sin run.
        logger.exception("no se pudo guardar la observación del producto %s", product_id)
        session.rollback()
        return fail("persist_failed", "no se pudo guardar la observación", None)
    return CheckOutcome(run, history)
