"""Fixtures de prueba.

Seguridad: las pruebas ejecutan `downgrade base` y `TRUNCATE`, así que SOLO pueden
correr contra la base `moneytor_test`. Si TEST_DATABASE_URL apunta a otra base, la
sesión de pruebas aborta antes de tocar nada.
"""

import socket

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import sessionmaker

from app.config import settings
from app.db import get_db
from app.main import app

REQUIRED_TEST_DB = "moneytor_test"


@pytest.fixture(autouse=True)
def _no_outbound_network(monkeypatch):
    """Ninguna prueba puede conectarse ni resolver DNS hacia fuera desde Python.

    Se permite solo loopback (el bucle de eventos de asyncio/TestClient usa un socketpair
    local en Windows). PostgreSQL no se ve afectado: libpq abre sus propios sockets.
    """
    local = {"127.0.0.1", "::1", "localhost"}
    real_connect = socket.socket.connect
    real_getaddrinfo = socket.getaddrinfo

    def guarded_connect(self, address):
        # Los sockets AF_UNIX pasan una ruta (str); solo las tuplas (host, puerto) son red.
        if isinstance(address, tuple) and address[0] not in local:
            raise AssertionError(f"las pruebas automatizadas no pueden usar la red: {address!r}")
        return real_connect(self, address)

    def guarded_getaddrinfo(host, *args, **kwargs):
        if host not in local:
            raise AssertionError(f"las pruebas automatizadas no pueden resolver DNS: {host!r}")
        return real_getaddrinfo(host, *args, **kwargs)

    monkeypatch.setattr(socket.socket, "connect", guarded_connect)
    monkeypatch.setattr(socket, "getaddrinfo", guarded_getaddrinfo)


def _alembic_config(url: str) -> Config:
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
    return cfg


@pytest.fixture(scope="session")
def test_db_url() -> str:
    url = settings.TEST_DATABASE_URL
    if not url:
        pytest.exit("TEST_DATABASE_URL no está definida en .env", returncode=2)
    if make_url(url).database != REQUIRED_TEST_DB:
        pytest.exit(f"TEST_DATABASE_URL debe apuntar a la base '{REQUIRED_TEST_DB}'", returncode=2)
    if make_url(url) == make_url(settings.DATABASE_URL):
        pytest.exit("TEST_DATABASE_URL no puede ser igual a DATABASE_URL", returncode=2)
    return url


@pytest.fixture(scope="session")
def engine(test_db_url):
    engine = create_engine(test_db_url)
    # El esquema de prueba sale de las migraciones reales, no de create_all().
    cfg = _alembic_config(test_db_url)
    command.downgrade(cfg, "base")
    command.upgrade(cfg, "head")
    yield engine
    engine.dispose()


@pytest.fixture()
def session_factory(engine):
    yield sessionmaker(engine, expire_on_commit=False)
    with engine.begin() as conn:
        conn.execute(text("TRUNCATE price_history, check_runs, products RESTART IDENTITY"))


@pytest.fixture()
def client(session_factory):
    def override_get_db():
        db = session_factory()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


class FakeUpstream:
    """Destino simulado: `handler` decide la respuesta; `requests` registra lo recibido."""

    def __init__(self) -> None:
        self.requests: list = []
        self.dns: list[str] = ["13.248.0.10"]
        self.handler = None


@pytest.fixture()
def upstream(client):
    """Sustituye el cliente HTTP y el DNS de la API por simulaciones (sin red real)."""
    import httpx2

    from app.services.fetcher import get_http_client, get_resolver

    fake = FakeUpstream()

    def respond(request):
        fake.requests.append(request)
        return fake.handler(request)

    def override_client():
        with httpx2.Client(transport=httpx2.MockTransport(respond), trust_env=False) as c:
            yield c

    app.dependency_overrides[get_http_client] = override_client
    app.dependency_overrides[get_resolver] = lambda: lambda host: fake.dns
    yield fake
