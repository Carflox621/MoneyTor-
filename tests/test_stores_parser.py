from decimal import Decimal
from pathlib import Path

import pytest

from app.services.stores import SUPPORTED_STORES, ParseError, parse_books, parse_price_text

FIXTURES = Path(__file__).parent / "fixtures" / "books_toscrape"


def _page(price_html: str, extra: str = "", title: str = "Libro") -> str:
    return (
        f'<html><body><div class="product_main"><h1>{title}</h1>{price_html}</div>{extra}'
        "</body></html>"
    )


def test_real_fixture_page():
    html = (FIXTURES / "product_ok.html").read_text(encoding="utf-8")
    obs = parse_books(html)
    assert obs.price == Decimal("51.77")
    assert obs.currency == "GBP"
    assert obs.price_raw == "£51.77"
    assert obs.name == "A Light in the Attic"


def test_price_is_decimal_not_float():
    obs = parse_books(_page('<p class="price_color">£53.74</p>'))
    assert isinstance(obs.price, Decimal)
    assert str(obs.price) == "53.74"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("£0.01", "0.01"), ("£1,299.50", "1299.50"), ("£1299.50", "1299.50"), ("£ 10.00", "10.00")],
)
def test_valid_price_texts(raw, expected):
    assert parse_price_text(raw) == (Decimal(expected), "GBP")


@pytest.mark.parametrize(
    "raw",
    [
        "£abc",
        "£",
        "",
        "51.77",
        "£51.7",
        "£51.777",
        "£51,77",
        "£1,29.50",
        "£1,2345.00",
        "£0.00",
        "£99999999999.99",  # > Numeric(12,2)
        "£51.77 £60.00",
    ],
)
def test_invalid_price_texts_raise_parse_error(raw):
    with pytest.raises(ParseError) as exc:
        parse_price_text(raw)
    assert exc.value.code == "parse_error"


@pytest.mark.parametrize("raw", ["$51.77", "€51.77", "Â£51.77", "MXN 51.77"])
def test_unknown_or_mojibake_currency_is_not_guessed(raw):
    with pytest.raises(ParseError) as exc:
        parse_price_text(raw)
    assert exc.value.code == "unsupported_currency"


def test_missing_price_element():
    with pytest.raises(ParseError) as exc:
        parse_books(_page(""))
    assert exc.value.code == "parse_error"


def test_two_price_elements_is_ambiguous():
    with pytest.raises(ParseError):
        parse_books(_page('<p class="price_color">£1.00</p><p class="price_color">£2.00</p>'))


def test_price_outside_product_main_is_ignored():
    html = _page("", extra='<p class="price_color">£9.99</p>')
    with pytest.raises(ParseError):
        parse_books(html)


@pytest.mark.parametrize("html", ["", "<<<not html", "<html></html>", "\x00\x01"])
def test_garbage_html_fails_loudly(html):
    with pytest.raises(ParseError):
        parse_books(html)


def test_empty_or_oversized_name_becomes_none():
    price = '<p class="price_color">£1.00</p>'
    assert parse_books(_page(price, title="")).name is None
    assert parse_books(_page(price, title="x" * 201)).name is None
    assert parse_books(_page(price, title="  Hola  ")).name == "Hola"


def test_supported_stores_registry():
    assert set(SUPPORTED_STORES) == {"books.toscrape.com"}
    assert SUPPORTED_STORES["books.toscrape.com"] is parse_books


def test_negative_price_is_rejected():
    with pytest.raises(ParseError):
        parse_price_text("£-5.00")
