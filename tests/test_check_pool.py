"""Ciclo de vida de conexiones de POST /check con un pool diminuto.

Con `pool_size=2, max_overflow=0` cualquier petición que retenga una conexión mientras pide
otra (hold-and-wait) se bloquea hasta `pool_timeout` y falla. El diseño correcto usa UNA sola
conexión por revisión: la del advisory lock, compartida con la sesión que lee y escribe.
"""

import threading
import time
from decimal import Decimal
from pathlib import Path

import httpx2
import pytest
from sqlalchemy import Connection, create_engine, select, text
from sqlalchemy.orm import Session, sessionmaker

from app.db import get_db
from app.main import app
from app.models import CheckRun, PriceHistory, Product
from app.services import stores
from app.services.checker import run_check
from app.services.fetcher import make_client
from app.services.stores import Observation

PAGE = (Path(__file__).parent / "fixtures" / "books_toscrape" / "product_ok.html").read_bytes()
BOOK_URL = "https://books.toscrape.com/catalogue/a-light-in-the-attic_1000/index.html"
HTML = {"content-type": "text/html"}
PUBLIC_DNS = lambda host: ["13.248.0.10"]  # noqa: E731


@pytest.fixture(autouse=True)
def _no_min_interval(monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "CHECK_MIN_INTERVAL_SECONDS", 0)


@pytest.fixture()
def small_engine(test_db_url):
    engine = create_engine(test_db_url, pool_size=2, max_overflow=0, pool_timeout=5)
    yield engine
    engine.dispose()


@pytest.fixture()
def tiny_client(small_engine, client, upstream):
    """La API usa el pool diminuto (el `upstream` ya sustituyó HTTP y DNS)."""
    factory = sessionmaker(small_engine, expire_on_commit=False)

    def override_get_db():
        db = factory()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db
    return client


@pytest.fixture()
def make_products(session_factory):
    def create(n):
        with session_factory() as db:
            products = [
                Product(url=f"{BOOK_URL}?n={i}", store="books.toscrape.com") for i in range(n)
            ]
            db.add_all(products)
            db.commit()
            return [p.id for p in products]

    return create


def advisory_locks(session_factory) -> int:
    with session_factory() as db:
        return db.scalar(text("SELECT count(*) FROM pg_locks WHERE locktype = 'advisory'"))


def run_threads(client, product_ids):
    """Un POST por producto, todos a la vez. Devuelve [(status, json | excepción)]."""
    barrier = threading.Barrier(len(product_ids))
    results = []

    def worker(pid):
        barrier.wait()
        try:
            response = client.post(f"/products/{pid}/check")
            results.append((response.status_code, response.json()))
        except Exception as exc:  # p. ej. QueuePool timeout si hay hold-and-wait
            results.append((None, exc))

    threads = [threading.Thread(target=worker, args=(pid,)) for pid in product_ids]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return results


def ok(request):
    return httpx2.Response(200, content=PAGE, headers=HTML)


# ---------- reproducción del riesgo original y corrección ----------


def test_different_products_do_not_deadlock_on_a_tiny_pool(
    tiny_client, upstream, make_products, small_engine, session_factory
):
    upstream.handler = lambda r: (time.sleep(0.2), ok(r))[1]
    ids = make_products(6)  # 6 revisiones simultáneas con solo 2 conexiones
    started = time.monotonic()
    results = run_threads(tiny_client, ids)
    elapsed = time.monotonic() - started

    assert [r[0] for r in results] == [201] * 6, results  # ninguna falló por el pool
    assert elapsed < 4.5  # se encolan por el pool; nunca esperan al pool_timeout (5 s)
    assert small_engine.pool.checkedout() == 0
    assert advisory_locks(session_factory) == 0
    with session_factory() as db:
        assert len(list(db.scalars(select(PriceHistory)))) == 6


def test_same_product_on_a_tiny_pool_still_runs_once(
    tiny_client, upstream, make_products, small_engine, session_factory
):
    upstream.handler = lambda r: (time.sleep(0.5), ok(r))[1]
    (pid,) = make_products(1)
    results = run_threads(tiny_client, [pid] * 8)

    codes = sorted(r[0] for r in results)
    assert codes == [201] + [429] * 7, results
    assert len(upstream.requests) == 1
    with session_factory() as db:
        assert len(list(db.scalars(select(CheckRun)))) == 1
        assert len(list(db.scalars(select(PriceHistory)))) == 1
    assert small_engine.pool.checkedout() == 0
    assert advisory_locks(session_factory) == 0


def test_exactly_one_connection_is_held_during_the_http_request(
    tiny_client, upstream, make_products, small_engine
):
    seen = []

    def handler(request):
        seen.append(small_engine.pool.checkedout())
        return ok(request)

    upstream.handler = handler
    (pid,) = make_products(1)
    assert tiny_client.post(f"/products/{pid}/check").status_code == 201
    assert seen == [1]  # solo la conexión del lock/sesión de la revisión
    assert small_engine.pool.checkedout() == 0


@pytest.mark.parametrize(
    "scenario", ["upstream_error", "timeout", "parser_crash", "persist_failed"]
)
def test_connections_and_locks_are_released_on_every_failure_path(
    tiny_client, upstream, make_products, small_engine, session_factory, monkeypatch, scenario
):
    def timeout(request):
        raise httpx2.ReadTimeout("slow", request=request)

    handlers = {
        "upstream_error": lambda r: httpx2.Response(500, headers=HTML),
        "timeout": timeout,
        "parser_crash": ok,
        "persist_failed": ok,
    }
    upstream.handler = handlers[scenario]
    if scenario == "parser_crash":

        def boom(html):
            raise RuntimeError("boom")

        monkeypatch.setitem(stores.SUPPORTED_STORES, "books.toscrape.com", boom)
    if scenario == "persist_failed":
        bad = Observation(Decimal("1.00"), "gbp", "£1.00", None)  # viola ck currency_iso
        monkeypatch.setitem(stores.SUPPORTED_STORES, "books.toscrape.com", lambda html: bad)

    (pid,) = make_products(1)
    assert tiny_client.post(f"/products/{pid}/check").status_code in (500, 502, 504)
    assert small_engine.pool.checkedout() == 0
    assert advisory_locks(session_factory) == 0
    # El pool sigue usable (no quedó ninguna conexión envenenada o retenida).
    upstream.handler = ok
    monkeypatch.setitem(stores.SUPPORTED_STORES, "books.toscrape.com", stores.parse_books)
    assert tiny_client.post(f"/products/{pid}/check").status_code == 201


def test_unexpected_exception_inside_lock_still_releases_everything(
    small_engine, session_factory, make_products, monkeypatch
):
    """Una excepción que escapa de run_check (bug, BD caída…) no filtra conexión ni lock."""
    (pid,) = make_products(1)

    def explode(*args, **kwargs):
        raise RuntimeError("fallo inesperado")

    monkeypatch.setattr("app.services.checker.fetch_html", explode)
    with sessionmaker(small_engine, expire_on_commit=False)() as db:
        product = db.get(Product, pid)
        with make_client() as client, pytest.raises(RuntimeError, match="inesperado"):
            run_check(db, product, client, PUBLIC_DNS)
    assert small_engine.pool.checkedout() == 0
    assert advisory_locks(session_factory) == 0


# ---------- contrato de la sesión que recibe run_check ----------


def test_run_check_refuses_a_session_with_pending_changes(
    small_engine, session_factory, make_products
):
    """No se confirma implícitamente trabajo ajeno al liberar la conexión de la sesión."""
    (pid,) = make_products(1)
    with sessionmaker(small_engine, expire_on_commit=False)() as db:
        product = db.get(Product, pid)
        product.name = "cambio sin confirmar"
        with make_client() as client, pytest.raises(RuntimeError, match="pendientes"):
            run_check(db, product, client, PUBLIC_DNS)
        db.rollback()
    with session_factory() as db:
        assert db.get(Product, pid).name is None  # nada se confirmó
    assert advisory_locks(session_factory) == 0


def test_failed_cleanup_invalidates_the_connection_so_no_lock_leaks(
    small_engine, session_factory, make_products, monkeypatch
):
    """Si la limpieza falla, la conexión se descarta: PostgreSQL libera su lock al cerrarla."""
    (pid,) = make_products(1)
    original_close = Session.close

    def failing_close(self):
        original_close(self)
        if isinstance(self.bind, Connection):  # solo la sesión ligada a la conexión del lock
            raise RuntimeError("fallo al cerrar")

    monkeypatch.setattr(Session, "close", failing_close)
    with sessionmaker(small_engine, expire_on_commit=False)() as db:
        product = db.get(Product, pid)
        monkeypatch.setattr("app.services.checker.fetch_html", lambda *a, **k: 1 / 0)
        with make_client() as client, pytest.raises(ZeroDivisionError):
            run_check(db, product, client, PUBLIC_DNS)
    assert small_engine.pool.checkedout() == 0
    assert advisory_locks(session_factory) == 0  # la conexión invalidada se cerró de verdad
