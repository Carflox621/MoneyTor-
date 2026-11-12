"""Obtención HTTP con defensas SSRF. No conoce tiendas concretas ni la base de datos.

Controles (todos antes de enviar datos al destino):
1. El host debe estar en la allowlist (SUPPORTED_STORES).
2. Siempre HTTPS y puerto 443, aunque la URL guardada sea http.
3. DNS: se resuelve el host y se rechaza si ALGUNA dirección no es pública.
4. Sin redirecciones (cualquier 3xx es un error) y sin proxies/variables de entorno.
5. Tope de tamaño sobre bytes ya descomprimidos, plazo total y tipo text/html.

Riesgo residual aceptado: entre la resolución DNS de (3) y la conexión real podría cambiar
la respuesta DNS (DNS rebinding). El host es fijo y público; fijar la IP queda como mejora.
"""

import ipaddress
import socket
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from urllib.parse import urlsplit, urlunsplit

import httpx2

from app.config import settings
from app.services.stores import SUPPORTED_STORES

Resolver = Callable[[str], list[str]]


class FetchError(Exception):
    """Fallo controlado. `message` es seguro de mostrar: nunca incluye contenido del destino."""

    def __init__(self, code: str, message: str, http_status: int | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.http_status = http_status


@dataclass(frozen=True)
class FetchResult:
    html: str
    final_url: str
    http_status: int


def default_resolver(host: str) -> list[str]:
    infos = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    return [str(info[4][0]) for info in infos]


def is_public_ip(address: str) -> bool:
    ip = ipaddress.ip_address(address.split("%", 1)[0])
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped  # ::ffff:127.0.0.1 es loopback aunque venga como IPv6
    return ip.is_global


def build_request_url(url: str) -> str:
    """Valida host/puerto contra la allowlist y devuelve la URL https a pedir."""
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    if host not in SUPPORTED_STORES:
        raise FetchError("blocked_destination", "el host no está en la lista de tiendas soportadas")
    if parts.scheme not in ("http", "https") or parts.port not in (None, 80, 443):
        raise FetchError("blocked_destination", "esquema o puerto no permitido")
    return urlunsplit(("https", host, parts.path or "/", parts.query, ""))


def _assert_public_destination(host: str, resolver: Resolver) -> None:
    try:
        addresses = resolver(host)
    except OSError as exc:
        raise FetchError("fetch_failed", "no se pudo resolver el host") from exc
    if not addresses:
        raise FetchError("fetch_failed", "no se pudo resolver el host")
    try:
        public = all(is_public_ip(a) for a in addresses)
    except ValueError as exc:
        raise FetchError("blocked_destination", "dirección de destino no válida") from exc
    if not public:
        raise FetchError("blocked_destination", "el host resuelve a una dirección no pública")


def fetch_html(
    url: str, client: httpx2.Client, resolver: Resolver = default_resolver
) -> FetchResult:
    request_url = build_request_url(url)
    _assert_public_destination(urlsplit(request_url).hostname or "", resolver)

    max_bytes = settings.MAX_RESPONSE_BYTES
    deadline = time.monotonic() + settings.HTTP_TOTAL_TIMEOUT
    timeout = httpx2.Timeout(settings.HTTP_TIMEOUT, connect=settings.HTTP_CONNECT_TIMEOUT)
    headers = {"User-Agent": settings.USER_AGENT, "Accept": "text/html"}
    try:
        with client.stream(
            "GET", request_url, headers=headers, timeout=timeout, follow_redirects=False
        ) as response:
            status = response.status_code
            if 300 <= status < 400:
                raise FetchError(
                    "redirect_not_allowed", "el destino respondió con redirección", status
                )
            if status != 200:
                raise FetchError("upstream_http_error", f"el destino respondió {status}", status)
            content_type = response.headers.get("content-type", "").split(";")[0].strip().lower()
            if content_type != "text/html":
                raise FetchError("bad_content_type", "el destino no devolvió text/html", status)
            declared = response.headers.get("content-length", "")
            if declared.isdigit() and int(declared) > max_bytes:
                raise FetchError("too_large", "la respuesta excede el tamaño máximo", status)
            body = bytearray()
            for chunk in response.iter_bytes():
                body += chunk
                if len(body) > max_bytes:
                    raise FetchError("too_large", "la respuesta excede el tamaño máximo", status)
                if time.monotonic() > deadline:
                    raise FetchError("timeout", "se agotó el plazo de la petición", status)
    except FetchError:
        raise
    except httpx2.TimeoutException as exc:
        raise FetchError("timeout", "se agotó el plazo de la petición") from exc
    except httpx2.HTTPError as exc:
        raise FetchError("fetch_failed", "falló la conexión con el destino") from exc

    # Content-Type sin charset; el HTML declara UTF-8. Decodificación estricta (evita "Â£").
    try:
        html = bytes(body).decode("utf-8")
    except UnicodeDecodeError as exc:
        raise FetchError("parse_error", "la respuesta no es UTF-8 válido", status) from exc
    return FetchResult(html=html, final_url=request_url, http_status=status)


def make_client() -> httpx2.Client:
    """Cliente sin proxies ni variables de entorno (un proxy resolvería DNS por su cuenta)."""
    return httpx2.Client(trust_env=False, follow_redirects=False)


def get_http_client() -> Iterator[httpx2.Client]:
    """Dependencia de FastAPI: un cliente por petición (sin cookies compartidas)."""
    client = make_client()
    try:
        yield client
    finally:
        client.close()


def get_resolver() -> Resolver:
    return default_resolver
