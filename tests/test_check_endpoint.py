"""POST /products/{id}/check: contrato, atomicidad y concurrencia (HTTP simulado)."""

import threading
import time
from decimal import Decimal
from pathlib import Path

import httpx2
import pytest
from sqlalchemy import select, text

from app.config import settings
from app.models import CheckRun, PriceHistory, Product
from app.services import stores
from app.services.stores import Observation

PAGE = (Path(__file__).parent / "fixtures" / "books_toscrape" / "product_ok.html").read_bytes()
BOOK_URL = "https://books.toscrape.com/catalogue/a-light-in-the-attic_1000/index.html"
HTML = {"content-type": "text/html"}


def html_ok(request):
    return httpx2.Response(200, content=PAGE, headers=HTML)


@pytest.fixture(autouse=True)
def _no_min_interval(monkeypatch):
    monkeypatch.setattr(settings, "CHECK_MIN_INTERVAL_SECONDS", 0)


@pytest.fixture()
def make_product(session_factory):
    def create(url=BOOK_URL, store="books.toscrape.com", name=None):
        with session_factory() as db:
            product = Product(url=url, store=store, name=name)
            db.add(product)
            db.commit()
            return product.id

    return create


@pytest.fixture()
def db_rows(session_factory):
    def read():
        with session_factory() as db:
            runs = list(db.scalars(select(CheckRun).order_by(CheckRun.id)))
            history = list(db.scalars(select(PriceHistory).order_by(PriceHistory.id)))
            return runs, history

    return read


def advisory_locks(session_factory) -> int:
    with session_factory() as db:
        return db.scalar(text("SELECT count(*) FROM pg_locks WHERE locktype = 'advisory'"))


# ---------- éxito ----------


def test_success_persists_run_and_observation_linked(client, upstream, make_product, db_rows):
    upstream.handler = html_ok
    pid = make_product()
    response = client.post(f"/products/{pid}/check")
    assert response.status_code == 201
    body = response.json()
    assert body["observation"]["price"] == "51.77"
    assert body["observation"]["currency"] == "GBP"
    assert body["observation"]["price_raw"] == "£51.77"
    assert body["observation"]["source_url"] == BOOK_URL
    assert body["check_run"]["status"] == "success"
    assert body["check_run"]["http_status"] == 200

    runs, history = db_rows()
    assert len(runs) == 1 and len(history) == 1
    assert history[0].check_run_id == runs[0].id == body["check_run"]["id"]
    assert history[0].product_id == runs[0].product_id == pid
    assert history[0].price == Decimal("51.77")
    assert runs[0].error_code is None


def test_only_the_expected_request_is_made(client, upstream, make_product):
    upstream.handler = html_ok
    pid = make_product(url=BOOK_URL.replace("https://", "http://"))
    assert client.post(f"/products/{pid}/check").status_code == 201
    assert [(r.method, str(r.url)) for r in upstream.requests] == [("GET", BOOK_URL)]


def test_name_filled_only_when_null(client, upstream, make_product, session_factory):
    upstream.handler = html_ok
    empty = make_product()
    named = make_product(url=BOOK_URL + "?x=1", name="Mi nombre")
    client.post(f"/products/{empty}/check")
    client.post(f"/products/{named}/check")
    with session_factory() as db:
        assert db.get(Product, empty).name == "A Light in the Attic"
        assert db.get(Product, named).name == "Mi nombre"


def test_every_successful_check_adds_an_observation(client, upstream, make_product, db_rows):
    upstream.handler = html_ok
    pid = make_product()
    for _ in range(3):
        assert client.post(f"/products/{pid}/check").status_code == 201
    runs, history = db_rows()
    assert len(runs) == len(history) == 3
    assert len({h.check_run_id for h in history}) == 3


# ---------- validaciones previas ----------


def test_unknown_product_is_404_without_run(client, upstream, db_rows):
    response = client.post("/products/999/check")
    assert response.status_code == 404
    assert db_rows() == ([], [])
    assert upstream.requests == []


@pytest.mark.parametrize("bad_id", ["0", "-1", "abc", "2147483648"])
def test_invalid_id_is_422(client, upstream, bad_id):
    assert client.post(f"/products/{bad_id}/check").status_code == 422


def test_unsupported_store_is_422_without_request_or_run(client, upstream, make_product, db_rows):
    pid = make_product(url="https://tienda.example.com/p", store="tienda.example.com")
    response = client.post(f"/products/{pid}/check")
    assert response.status_code == 422
    detail = response.json()["detail"]
    assert detail["code"] == "unsupported_store"
    assert detail["supported"] == ["books.toscrape.com"]
    assert upstream.requests == []
    assert db_rows() == ([], [])


def test_too_soon_is_429_with_retry_after(client, upstream, make_product, db_rows, monkeypatch):
    monkeypatch.setattr(settings, "CHECK_MIN_INTERVAL_SECONDS", 60)
    upstream.handler = html_ok
    pid = make_product()
    assert client.post(f"/products/{pid}/check").status_code == 201
    response = client.post(f"/products/{pid}/check")
    assert response.status_code == 429
    assert response.json()["detail"]["code"] == "too_soon"
    assert 1 <= int(response.headers["retry-after"]) <= 60
    assert len(upstream.requests) == 1
    runs, history = db_rows()
    assert len(runs) == len(history) == 1


def test_failed_runs_also_count_for_the_rate_limit(client, upstream, make_product, monkeypatch):
    monkeypatch.setattr(settings, "CHECK_MIN_INTERVAL_SECONDS", 60)
    upstream.handler = lambda r: httpx2.Response(500, headers=HTML)
    pid = make_product()
    assert client.post(f"/products/{pid}/check").status_code == 502
    assert client.post(f"/products/{pid}/check").status_code == 429


# ---------- fallos del destino: run de error, nunca precio ----------

SECRET = b"<html>SECRET-UPSTREAM-BODY</html>"


def _raise_timeout(request):
    raise httpx2.ReadTimeout("slow", request=request)


@pytest.mark.parametrize(
    ("handler", "http_status", "code", "upstream_status"),
    [
        (
            lambda r: httpx2.Response(404, content=SECRET, headers=HTML),
            502,
            "upstream_http_error",
            404,
        ),
        (
            lambda r: httpx2.Response(503, content=SECRET, headers=HTML),
            502,
            "upstream_http_error",
            503,
        ),
        (
            lambda r: httpx2.Response(302, headers={"location": "http://169.254.169.254/"}),
            502,
            "redirect_not_allowed",
            302,
        ),
        (
            lambda r: httpx2.Response(
                200, content=SECRET, headers={"content-type": "application/json"}
            ),
            502,
            "bad_content_type",
            200,
        ),
        (lambda r: httpx2.Response(200, content=SECRET, headers=HTML), 502, "parse_error", 200),
        (
            lambda r: httpx2.Response(
                200,
                content=b'<div class="product_main"><p class="price_color">$5.00</p></div>',
                headers=HTML,
            ),
            502,
            "unsupported_currency",
            200,
        ),
        (_raise_timeout, 504, "timeout", None),
    ],
)
def test_upstream_failures_record_error_and_no_price(
    client, upstream, make_product, db_rows, handler, http_status, code, upstream_status
):
    upstream.handler = handler
    pid = make_product()
    response = client.post(f"/products/{pid}/check")
    assert response.status_code == http_status
    detail = response.json()["detail"]
    assert detail["code"] == code
    assert b"SECRET-UPSTREAM-BODY" not in response.content  # nunca se filtra el cuerpo del destino

    runs, history = db_rows()
    assert history == []
    assert len(runs) == 1
    assert runs[0].status == "error" and runs[0].error_code == code
    assert runs[0].http_status == upstream_status
    assert detail["check_run_id"] == runs[0].id


def test_private_dns_is_blocked_before_any_request(client, upstream, make_product, db_rows):
    upstream.handler = html_ok
    upstream.dns = ["10.0.0.7"]
    pid = make_product()
    response = client.post(f"/products/{pid}/check")
    assert response.status_code == 502
    assert response.json()["detail"]["code"] == "blocked_destination"
    assert upstream.requests == []
    runs, history = db_rows()
    assert [r.error_code for r in runs] == ["blocked_destination"] and history == []


def test_error_can_be_followed_by_a_success(client, upstream, make_product, db_rows):
    pid = make_product()
    upstream.handler = lambda r: httpx2.Response(500, headers=HTML)
    assert client.post(f"/products/{pid}/check").status_code == 502
    upstream.handler = html_ok
    assert client.post(f"/products/{pid}/check").status_code == 201
    runs, history = db_rows()
    assert [r.status for r in runs] == ["error", "success"] and len(history) == 1


# ---------- atomicidad ----------


def test_failure_while_saving_observation_rolls_everything_back(
    client, upstream, make_product, db_rows, session_factory, monkeypatch
):
    upstream.handler = html_ok
    # Moneda en minúsculas: el CHECK de PostgreSQL rechaza el INSERT de price_history,
    # DESPUÉS de haberse insertado (sin confirmar) la fila check_runs de éxito.
    bad = Observation(Decimal("1.00"), "gbp", "£1.00", "Nombre que no debe guardarse")
    monkeypatch.setitem(stores.SUPPORTED_STORES, "books.toscrape.com", lambda html: bad)
    pid = make_product()

    response = client.post(f"/products/{pid}/check")
    assert response.status_code == 500
    assert response.json()["detail"]["code"] == "persist_failed"

    runs, history = db_rows()
    assert history == []  # ni observación huérfana
    assert [(r.status, r.error_code) for r in runs] == [
        ("error", "persist_failed")
    ]  # ni éxito sin precio
    with session_factory() as db:
        assert db.get(Product, pid).name is None  # el nombre tampoco se confirmó
    assert advisory_locks(session_factory) == 0


def test_locks_are_released_after_success_and_failure(
    client, upstream, make_product, session_factory
):
    pid = make_product()
    upstream.handler = html_ok
    assert client.post(f"/products/{pid}/check").status_code == 201
    assert advisory_locks(session_factory) == 0
    upstream.handler = lambda r: httpx2.Response(500, headers=HTML)
    assert client.post(f"/products/{pid}/check").status_code == 502
    assert advisory_locks(session_factory) == 0


# ---------- concurrencia ----------


def test_concurrent_checks_of_same_product_run_once(client, upstream, make_product, db_rows):
    def slow(request):
        time.sleep(0.6)  # mantiene la primera revisión "en curso" mientras llegan las demás
        return html_ok(request)

    upstream.handler = slow
    pid = make_product()
    barrier = threading.Barrier(8)
    results: list[tuple[int, dict]] = []

    def worker():
        barrier.wait()
        response = client.post(f"/products/{pid}/check")
        results.append((response.status_code, response.json()))

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    codes = sorted(status for status, _ in results)
    assert codes == [201] + [429] * 7
    assert {b["detail"]["code"] for s, b in results if s == 429} == {"check_in_progress"}
    assert len(upstream.requests) == 1  # una sola petición al destino
    runs, history = db_rows()
    assert len(runs) == len(history) == 1


def test_concurrent_checks_of_different_products_do_not_block_each_other(
    client, upstream, make_product, db_rows
):
    upstream.handler = lambda r: (time.sleep(0.3), html_ok(r))[1]
    ids = [make_product(url=f"{BOOK_URL}?n={i}") for i in range(4)]
    statuses = []

    def worker(pid):
        statuses.append(client.post(f"/products/{pid}/check").status_code)

    threads = [threading.Thread(target=worker, args=(i,)) for i in ids]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert statuses == [201] * 4
    runs, history = db_rows()
    assert len(runs) == len(history) == 4


def test_check_in_progress_from_another_connection_is_rejected(
    client, upstream, make_product, db_rows, session_factory
):
    upstream.handler = html_ok
    pid = make_product()
    with session_factory() as other:  # otra conexión sostiene el lock del producto
        other.execute(text("SELECT pg_advisory_lock(2, :pid)"), {"pid": pid})
        response = client.post(f"/products/{pid}/check")
        assert response.status_code == 429
        assert response.json()["detail"]["code"] == "check_in_progress"
        assert upstream.requests == [] and db_rows() == ([], [])
        other.execute(text("SELECT pg_advisory_unlock(2, :pid)"), {"pid": pid})
    assert client.post(f"/products/{pid}/check").status_code == 201


def test_unexpected_parser_crash_is_recorded_not_lost(
    client, upstream, make_product, db_rows, monkeypatch
):
    upstream.handler = html_ok

    def boom(html):
        raise RuntimeError("bug del parser")

    monkeypatch.setitem(stores.SUPPORTED_STORES, "books.toscrape.com", boom)
    pid = make_product()
    response = client.post(f"/products/{pid}/check")
    assert response.status_code == 502
    assert response.json()["detail"]["code"] == "parse_error"
    assert "bug del parser" not in response.text  # el detalle interno no se expone
    runs, history = db_rows()
    assert [r.error_code for r in runs] == ["parse_error"] and history == []
