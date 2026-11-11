from alembic import command
from sqlalchemy import inspect

from tests.conftest import _alembic_config


def test_downgrade_and_upgrade_roundtrip(engine, test_db_url):
    cfg = _alembic_config(test_db_url)
    try:
        command.downgrade(cfg, "base")
        assert "products" not in inspect(engine).get_table_names()
        command.upgrade(cfg, "head")
        inspector = inspect(engine)
        assert "products" in inspector.get_table_names()
        uniques = {u["name"] for u in inspector.get_unique_constraints("products")}
        assert "uq_products_url" in uniques
    finally:
        command.upgrade(cfg, "head")
