"""Validación y normalización conservadora de URLs de producto.

La URL normalizada es la identidad del producto (columna UNIQUE). Principio: ante la
duda se CONSERVA información. Un duplicado se limpia después; una URL que apunta a
otro producto corrompe el historial de forma silenciosa.
"""

import ipaddress
import re
from urllib.parse import quote, unquote, urlsplit, urlunsplit

MAX_URL_LENGTH = 2048
ALLOWED_SCHEMES = {"http", "https"}
DEFAULT_PORTS = {"http": 80, "https": 443}

# Solo parámetros con especificación pública y dueño conocido (ver documento de la
# etapa 1). `ref` y cualquier otro nombre genérico se conservan.
TRACKING_PARAMS = {"gclid", "fbclid"}
TRACKING_PREFIXES = ("utm_",)

_UNRESERVED = set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~")
_PERCENT = re.compile(r"%([0-9A-Fa-f]{2})")


def _normalize_percent_encoding(text: str) -> str:
    """Hex en mayúsculas y decodifica solo caracteres 'unreserved' (%7E -> ~)."""

    def repl(match: re.Match[str]) -> str:
        char = chr(int(match.group(1), 16))
        return char if char in _UNRESERVED else f"%{match.group(1).upper()}"

    return _PERCENT.sub(repl, text)


def _encode(text: str) -> str:
    # Codifica caracteres no ASCII/espacios sin tocar los % ya presentes.
    return _normalize_percent_encoding(quote(text, safe="/%:@!$&'()*+,;=-._~?"))


def _is_tracking(name: str) -> bool:
    lowered = unquote(name).lower()
    return lowered in TRACKING_PARAMS or lowered.startswith(TRACKING_PREFIXES)


def _clean_host(hostname: str) -> str:
    host = hostname.rstrip(".").lower()
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        ip = None
    if ip is not None:
        if not ip.is_global:
            raise ValueError("la URL apunta a una dirección IP no pública")
        return f"[{ip}]" if ip.version == 6 else str(ip)

    if host.startswith("www."):
        host = host[4:]
    if host == "localhost" or host.endswith(".localhost"):
        raise ValueError("la URL apunta a un host local")
    try:
        host = host.encode("idna").decode("ascii")
    except UnicodeError as exc:
        raise ValueError("el nombre de host no es válido") from exc
    if "." not in host or not re.fullmatch(r"[a-z0-9.-]+", host) or ".." in host:
        raise ValueError("el nombre de host no es válido")
    return host


def normalize_url(url: str) -> str:
    """Devuelve la URL normalizada o lanza ValueError con un motivo legible."""
    url = url.strip()
    if not url:
        raise ValueError("la URL no puede estar vacía")
    if len(url) > MAX_URL_LENGTH:
        raise ValueError(f"la URL excede {MAX_URL_LENGTH} caracteres")
    if re.search(r"\s", url):
        raise ValueError("la URL no puede contener espacios")

    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError as exc:
        raise ValueError("la URL no es válida") from exc

    scheme = parts.scheme.lower()
    if scheme not in ALLOWED_SCHEMES:
        raise ValueError("solo se admiten URLs http o https")
    if not parts.hostname:
        raise ValueError("la URL no tiene host")
    if parts.username is not None or parts.password is not None:
        raise ValueError("la URL no puede incluir credenciales")

    host = _clean_host(parts.hostname)
    netloc = host if port is None or port == DEFAULT_PORTS[scheme] else f"{host}:{port}"

    path = _encode(parts.path)
    if path.endswith("/"):
        path = path.rstrip("/")

    # Se conservan todos los parámetros salvo el tracking conocido. Se ordenan por
    # nombre (orden estable) para que ?a=1&b=2 y ?b=2&a=1 sean la misma identidad.
    pairs = [p for p in parts.query.split("&") if p and not _is_tracking(p.split("=", 1)[0])]
    pairs.sort(key=lambda p: p.split("=", 1)[0])
    query = _encode("&".join(pairs))

    normalized = urlunsplit((scheme, netloc, path, query, ""))
    if len(normalized) > MAX_URL_LENGTH:
        raise ValueError(f"la URL excede {MAX_URL_LENGTH} caracteres")
    return normalized


def store_from_url(url: str) -> str:
    """Hostname (sin puerto ni `www.`) de una URL ya normalizada."""
    hostname = urlsplit(url).hostname
    if not hostname:
        raise ValueError("la URL no tiene host")
    return hostname
