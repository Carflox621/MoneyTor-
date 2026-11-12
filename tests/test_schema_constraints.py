"""Las restricciones de check_runs / price_history las impone PostgreSQL, no la aplicación."""

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError, ProgrammingError

RUN_SQL = text(
    "INSERT INTO check_runs (product_id, status, error_code, requested_url, finished_at,"
    " duration_ms)"
    " VALUES (:pid, :status, :code, 'https://x.test/p', now(), :dur) RETURNING id"
)
HIST_SQL = text(
    "INSERT INTO price_history (product_id, check_run_id, price, currency, price_raw, source_url)"
    " VALUES (:pid, :rid, :price, :cur, '£1.00', 'https://x.test/p')"
)


@pytest.fixture()
def conn(session_factory):
    with session_factory() as session:
        session.execute(
            text("INSERT INTO products (url, store) VALUES ('https://a.test/1', 'a.test')")
        )
        session.execute(
            text("INSERT INTO products (url, store) VALUES ('https://b.test/2', 'b.test')")
        )
        session.commit()
        yield session
        session.rollback()


def _run(conn, pid=1, status="success", code=None, dur=5):
    return conn.execute(RUN_SQL, {"pid": pid, "status": status, "code": code, "dur": dur}).scalar()


def test_valid_success_run_and_observation(conn):
    rid = _run(conn)
    conn.execute(HIST_SQL, {"pid": 1, "rid": rid, "price": "10.50", "cur": "GBP"})
    conn.commit()


@pytest.mark.parametrize(
    ("status", "code"),
    [("success", "timeout"), ("error", None), ("weird", None), ("running", "x")],
)
def test_status_and_error_code_must_be_coherent(conn, status, code):
    with pytest.raises(IntegrityError):
        _run(conn, status=status, code=code)


def test_negative_duration_rejected(conn):
    with pytest.raises(IntegrityError):
        _run(conn, dur=-1)


def test_finished_before_started_rejected(conn):
    with pytest.raises(IntegrityError):
        conn.execute(
            text(
                "INSERT INTO check_runs (product_id, status, requested_url, started_at,"
                " finished_at, duration_ms) VALUES (1, 'success', 'https://x.test', now(),"
                " now() - interval '1 hour', 0)"
            )
        )


@pytest.mark.parametrize(
    ("price", "cur"), [("0", "GBP"), ("-1", "GBP"), ("1", "gbp"), ("1", "GB"), ("1", "G1P")]
)
def test_invalid_price_or_currency_rejected(conn, price, cur):
    rid = _run(conn)
    # CHAR(3) rechaza >3 caracteres con error de datos; el resto son CHECK.
    with pytest.raises((IntegrityError, ProgrammingError)):
        conn.execute(HIST_SQL, {"pid": 1, "rid": rid, "price": price, "cur": cur})


def test_observation_must_belong_to_same_product_as_run(conn):
    rid = _run(conn, pid=1)
    with pytest.raises(IntegrityError):
        conn.execute(HIST_SQL, {"pid": 2, "rid": rid, "price": "1", "cur": "GBP"})


def test_one_observation_per_run(conn):
    rid = _run(conn)
    conn.execute(HIST_SQL, {"pid": 1, "rid": rid, "price": "1", "cur": "GBP"})
    with pytest.raises(IntegrityError):
        conn.execute(HIST_SQL, {"pid": 1, "rid": rid, "price": "2", "cur": "GBP"})


def test_cannot_delete_product_with_runs(conn):
    _run(conn)
    conn.commit()
    with pytest.raises(IntegrityError):
        conn.execute(text("DELETE FROM products WHERE id = 1"))
