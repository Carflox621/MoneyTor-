from alembic import command
from sqlalchemy import inspect, text

from tests.conftest import _alembic_config


def test_downgrade_and_upgrade_roundtrip(engine, test_db_url):
    cfg = _alembic_config(test_db_url)
    try:
        command.downgrade(cfg, "base")
        tables = inspect(engine).get_table_names()
        assert not {"products", "check_runs", "price_history"} & set(tables)
        command.upgrade(cfg, "head")
        inspector = inspect(engine)
        assert {"products", "check_runs", "price_history"} <= set(inspector.get_table_names())
        uniques = {u["name"] for u in inspector.get_unique_constraints("products")}
        assert "uq_products_url" in uniques
    finally:
        command.upgrade(cfg, "head")


def test_downgrade_0002_keeps_products(engine, test_db_url):
    cfg = _alembic_config(test_db_url)
    try:
        command.downgrade(cfg, "0001")
        tables = set(inspect(engine).get_table_names())
        assert "products" in tables
        assert not {"check_runs", "price_history"} & tables
    finally:
        command.upgrade(cfg, "head")


def test_new_tables_have_expected_constraints_and_indexes(engine):
    inspector = inspect(engine)
    runs_uniques = {u["name"] for u in inspector.get_unique_constraints("check_runs")}
    assert "uq_check_runs_id_product_id" in runs_uniques
    history_uniques = {u["name"] for u in inspector.get_unique_constraints("price_history")}
    assert "uq_price_history_check_run_id" in history_uniques
    fks = {fk["name"] for fk in inspector.get_foreign_keys("price_history")}
    assert "fk_price_history_check_run_product" in fks
    assert "ix_check_runs_product_id_started_at" in {
        i["name"] for i in inspector.get_indexes("check_runs")
    }
    assert "ix_price_history_product_id_observed_at" in {
        i["name"] for i in inspector.get_indexes("price_history")
    }


def _snapshot(engine):
    with engine.connect() as conn:
        rows = conn.execute(text("SELECT * FROM products ORDER BY id")).all()
        columns = [c["name"] for c in inspect(conn).get_columns("products")]
    return columns, [tuple(r) for r in rows]


def test_migration_0002_preserves_existing_products_data(engine, test_db_url):
    """Datos creados en la revisión 0001 sobreviven intactos a upgrade y downgrade de 0002.

    Solo corre contra `moneytor_test` (conftest aborta si TEST_DATABASE_URL apunta a otra).
    """
    cfg = _alembic_config(test_db_url)
    try:
        command.downgrade(cfg, "0001")
        with engine.begin() as conn:
            conn.execute(text("TRUNCATE products RESTART IDENTITY"))
            conn.execute(
                text(
                    "INSERT INTO products (url, name, store, target_price, created_at) VALUES"
                    " ('https://a.test/1', 'Uno', 'a.test', 10.50, '2026-01-02 03:04:05+00'),"
                    " ('https://b.test/2', NULL, 'b.test', NULL, '2026-02-03 04:05:06+00')"
                )
            )
        before = _snapshot(engine)
        assert len(before[1]) == 2

        command.upgrade(cfg, "0002")
        assert _snapshot(engine) == before  # upgrade no altera columnas ni filas

        with engine.begin() as conn:  # y las tablas nuevas funcionan con esos productos
            run_id = conn.execute(
                text(
                    "INSERT INTO check_runs (product_id, status, requested_url, finished_at,"
                    " duration_ms) VALUES (1, 'success', 'https://a.test/1', now(), 1)"
                    " RETURNING id"
                )
            ).scalar()
            conn.execute(
                text(
                    "INSERT INTO price_history (product_id, check_run_id, price, currency,"
                    " price_raw, source_url) VALUES (1, :rid, 9.99, 'GBP', '£9.99', 'https://a.test/1')"
                ),
                {"rid": run_id},
            )

        command.downgrade(cfg, "0001")  # borra solo las tablas nuevas
        assert _snapshot(engine) == before
        assert {"check_runs", "price_history"}.isdisjoint(inspect(engine).get_table_names())

        command.upgrade(cfg, "0002")  # y vuelve a subir sin tocar los productos
        assert _snapshot(engine) == before
    finally:
        command.upgrade(cfg, "head")
        with engine.begin() as conn:
            conn.execute(text("TRUNCATE price_history, check_runs, products RESTART IDENTITY"))
