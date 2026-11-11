from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from threading import Barrier

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app.main import app

URL = "https://tienda.com/producto"


def create(client, **payload):
    payload.setdefault("url", URL)
    return client.post("/products", json=payload)


# ---------- POST /products ----------


def test_create_happy_path(client):
    r = create(
        client,
        url="https://WWW.Tienda.com/producto/?utm_source=fb",
        name="  Cafetera ",
        target_price="1299.00",
    )
    assert r.status_code == 201
    body = r.json()
    assert body["id"] == 1
    assert body["url"] == URL
    assert body["store"] == "tienda.com"
    assert body["name"] == "Cafetera"
    assert body["target_price"] == "1299.00"
    assert datetime.fromisoformat(body["created_at"]).tzinfo is not None  # con zona horaria
    assert client.get("/products/1").json() == body  # POST y GET devuelven lo mismo


def test_create_minimal_payload(client):
    r = client.post("/products", json={"url": URL})
    assert r.status_code == 201
    assert r.json()["name"] is None
    assert r.json()["target_price"] is None


def test_duplicate_returns_409_with_existing_id(client):
    first = create(client).json()
    r = create(client, url=URL + "?utm_source=x#frag")
    assert r.status_code == 409
    assert r.json()["detail"]["existing_id"] == first["id"]


def test_ref_param_makes_a_different_product(client):
    assert create(client).status_code == 201
    assert create(client, url=URL + "?ref=abc").status_code == 201


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"url": "no-es-una-url"},
        {"url": ""},
        {"url": "ftp://tienda.com/p"},
        {"url": "http://localhost/p"},
        {"url": "http://169.254.169.254/x"},
        {"url": "https://u:p@tienda.com/p"},
        {"url": URL, "target_price": "abc"},
        {"url": URL, "target_price": 0},
        {"url": URL, "target_price": -5},
        {"url": URL, "target_price": "1299.999"},
        {"url": URL, "target_price": "NaN"},
        {"url": URL, "target_price": "Infinity"},
        {"url": URL, "target_price": "1e999"},
        {"url": URL, "target_price": "12345678901.00"},  # >12 dígitos
        {"url": URL, "name": "x" * 201},
        {"url": URL, "precio": 5},  # campo desconocido
    ],
)
def test_create_invalid_payload_returns_422(client, payload):
    assert client.post("/products", json=payload).status_code == 422


def test_invalid_requests_do_not_persist(client):
    create(client, url="ftp://x.com/p")
    assert client.get("/products").json() == []


def test_explicit_null_name_and_price(client):
    r = client.post("/products", json={"url": URL, "name": None, "target_price": None})
    assert r.status_code == 201
    assert r.json()["name"] is None


def test_unrelated_integrity_error_is_not_reported_as_409(client, monkeypatch):
    """Solo el UNIQUE de url es un 409; otros IntegrityError deben seguir siendo errores."""
    from sqlalchemy.orm import Session

    class FakeOrig(Exception):
        class diag:  # noqa: N801 (imita psycopg.errors.Diagnostic)
            constraint_name = "ck_products_otra"

    def failing_commit(self):
        raise IntegrityError("INSERT", {}, FakeOrig())

    monkeypatch.setattr(Session, "commit", failing_commit)
    # Se propaga (no se convierte en 409) y no deja filas.
    with pytest.raises(IntegrityError):
        client.post("/products", json={"url": "https://x.com/otra"})
    monkeypatch.undo()
    assert client.get("/products").json() == []


def test_blank_name_becomes_null(client):
    assert create(client, name="   ").json()["name"] is None


def test_target_price_accepts_number_and_boundaries(client):
    assert (
        create(client, url="https://a.com/1", target_price=10.5).json()["target_price"] == "10.50"
    )
    assert create(client, url="https://a.com/2", target_price="0.01").status_code == 201
    assert create(client, url="https://a.com/3", target_price="9999999999.99").status_code == 201


def test_malformed_json_returns_422(client):
    r = client.post("/products", content="{no-json", headers={"content-type": "application/json"})
    assert r.status_code == 422


def test_concurrent_duplicates_yield_single_row(session_factory, client):
    """El UNIQUE de la BD (no un SELECT previo) debe resolver la condición de carrera."""
    workers = 8
    barrier = Barrier(workers)

    def post(_):
        with TestClient(app) as c:
            barrier.wait()
            return c.post("/products", json={"url": "https://race.com/p"}).status_code

    with ThreadPoolExecutor(workers) as pool:
        codes = list(pool.map(post, range(workers)))

    assert sorted(codes) == [201] + [409] * (workers - 1)
    assert len(client.get("/products").json()) == 1


# ---------- GET /products ----------


def test_list_empty(client):
    r = client.get("/products")
    assert r.status_code == 200
    assert r.json() == []


def test_list_ordered_and_paginated(client):
    for i in range(5):
        create(client, url=f"https://t.com/p{i}")
    ids = [p["id"] for p in client.get("/products").json()]
    assert ids == [1, 2, 3, 4, 5]
    page = client.get("/products", params={"limit": 2, "offset": 2}).json()
    assert [p["id"] for p in page] == [3, 4]
    assert client.get("/products", params={"offset": 99}).json() == []


@pytest.mark.parametrize("params", [{"limit": 0}, {"limit": 101}, {"offset": -1}, {"limit": "x"}])
def test_list_invalid_pagination_returns_422(client, params):
    assert client.get("/products", params=params).status_code == 422


# ---------- GET /products/{id} ----------


def test_get_by_id(client):
    created = create(client).json()
    r = client.get(f"/products/{created['id']}")
    assert r.status_code == 200
    assert r.json() == created


def test_get_missing_returns_404(client):
    assert client.get("/products/999").status_code == 404


@pytest.mark.parametrize("pid", ["abc", "0", "-1", "99999999999"])
def test_get_invalid_id_returns_422(client, pid):
    assert client.get(f"/products/{pid}").status_code == 422


# ---------- Restricciones en PostgreSQL (defensa en profundidad) ----------


@pytest.mark.parametrize(
    ("values", "constraint"),
    [
        ("'https://a.com/1', 'a.com', 0", "ck_products_target_price_positive"),
        ("'https://a.com/2', '', NULL", "ck_products_store_not_empty"),
    ],
)
def test_database_rejects_invalid_rows(session_factory, values, constraint):
    with session_factory() as db, pytest.raises(IntegrityError, match=constraint):
        db.execute(text(f"INSERT INTO products (url, store, target_price) VALUES ({values})"))
        db.commit()


def test_database_enforces_unique_url_and_name_length(session_factory):
    with session_factory() as db:
        db.execute(text("INSERT INTO products (url, store) VALUES ('https://a.com/u', 'a.com')"))
        db.commit()
        with pytest.raises(IntegrityError, match="uq_products_url"):
            db.execute(
                text("INSERT INTO products (url, store) VALUES ('https://a.com/u', 'a.com')")
            )
            db.commit()
        db.rollback()
        with pytest.raises(IntegrityError, match="ck_products_name_length"):
            db.execute(
                text(
                    "INSERT INTO products (url, store, name) "
                    "VALUES ('https://a.com/n', 'a.com', '')"
                )
            )
            db.commit()
