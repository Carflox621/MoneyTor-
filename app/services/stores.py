"""Tiendas soportadas y extracción de precios (funciones puras: sin red ni BD).

Alta de una tienda = añadir su host a SUPPORTED_STORES con su función de parseo. Esa
allowlist es también la defensa SSRF principal: solo se hacen peticiones a estos hosts.

books.toscrape.com (revisado el 2026-11-12): sandbox pensado para ser scrapeado
("We love being scraped!"); /robots.txt responde 404 (sin reglas publicadas); HTML
servido sin JavaScript; los precios están asignados al azar y no cambian.
"""

import re
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal

from bs4 import BeautifulSoup

MAX_NAME_LENGTH = 200
MAX_PRICE = Decimal("9999999999.99")  # Numeric(12,2)

# Símbolo -> código ISO 4217. Un símbolo desconocido es un error, no una suposición.
CURRENCY_SYMBOLS = {"£": "GBP"}

_PRICE_TEXT = re.compile(r"(\D+?)\s*(\d[\d,]*\.\d{2})")
_AMOUNT = re.compile(r"(?:\d{1,3}(?:,\d{3})+|\d+)\.\d{2}")


class ParseError(Exception):
    """La página se obtuvo, pero no contiene un precio fiable."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class Observation:
    price: Decimal
    currency: str
    price_raw: str
    name: str | None


def parse_price_text(raw: str) -> tuple[Decimal, str]:
    """'£1,299.50' -> (Decimal('1299.50'), 'GBP'). Lanza ParseError ante cualquier duda."""
    match = _PRICE_TEXT.fullmatch(raw)
    if match is None:
        raise ParseError("parse_error", "el texto del precio no tiene el formato esperado")
    symbol, amount = match.groups()
    currency = CURRENCY_SYMBOLS.get(symbol)
    if currency is None:
        raise ParseError("unsupported_currency", "símbolo de moneda no soportado")
    if _AMOUNT.fullmatch(amount) is None:
        raise ParseError("parse_error", "la cantidad no tiene el formato esperado")
    price = Decimal(amount.replace(",", ""))
    if not 0 < price <= MAX_PRICE:
        raise ParseError("parse_error", "el precio está fuera de rango")
    return price, currency


def parse_books(html: str) -> Observation:
    soup = BeautifulSoup(html, "html.parser")
    prices = soup.select("div.product_main p.price_color")
    if len(prices) != 1:
        raise ParseError("parse_error", f"se esperaba 1 elemento de precio, hay {len(prices)}")
    price_raw = prices[0].get_text(strip=True)
    price, currency = parse_price_text(price_raw)

    heading = soup.select_one("div.product_main h1")
    name = heading.get_text(strip=True) if heading else ""
    if not name or len(name) > MAX_NAME_LENGTH:
        name = None
    return Observation(price=price, currency=currency, price_raw=price_raw, name=name)


SUPPORTED_STORES: dict[str, Callable[[str], Observation]] = {
    "books.toscrape.com": parse_books,
}
