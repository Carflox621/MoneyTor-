import pytest

from app.urls import normalize_url, store_from_url


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        # Ejemplo del documento de planificación
        (
            "https://WWW.Tienda.com/producto/?utm_source=fb&id=42#reviews",
            "https://tienda.com/producto?id=42",
        ),
        # esquema/host en minúsculas, www, fragmento
        ("HTTP://Tienda.COM/Producto", "http://tienda.com/Producto"),  # el path conserva mayúsculas
        ("https://www.tienda.com/p#frag", "https://tienda.com/p"),
        # barras finales y raíz
        ("https://tienda.com/", "https://tienda.com"),
        ("https://tienda.com", "https://tienda.com"),
        ("https://tienda.com/a/b/", "https://tienda.com/a/b"),
        # puertos
        ("https://tienda.com:443/p", "https://tienda.com/p"),
        ("http://tienda.com:80/p", "http://tienda.com/p"),
        ("https://tienda.com:8443/p", "https://tienda.com:8443/p"),
        # tracking conocido, incluso en mayúsculas
        ("https://t.com/p?UTM_Source=x&utm_medium=y&gclid=1&fbclid=2", "https://t.com/p"),
        ("https://t.com/p?utm_source=x", "https://t.com/p"),
        # `ref`, `id` y demás SE CONSERVAN
        ("https://t.com/p?ref=abc", "https://t.com/p?ref=abc"),
        ("https://t.com/p?id=42&ref=abc&utm_campaign=z", "https://t.com/p?id=42&ref=abc"),
        # orden de parámetros estable
        ("https://t.com/p?b=2&a=1", "https://t.com/p?a=1&b=2"),
        ("https://t.com/p?a=1&b=2", "https://t.com/p?a=1&b=2"),
        # parámetros repetidos: se conserva su orden relativo
        ("https://t.com/p?x=2&x=1", "https://t.com/p?x=2&x=1"),
        # query vacío y parámetros sin valor
        ("https://t.com/p?", "https://t.com/p"),
        ("https://t.com/p?a&b=", "https://t.com/p?a&b="),
        # percent-encoding
        ("https://t.com/%7Eusuario/a%2fb", "https://t.com/~usuario/a%2Fb"),
        ("https://t.com/p con", None),  # espacios: inválido
        # IDN -> punycode
        ("https://bücher.de/libro", "https://xn--bcher-kva.de/libro"),
        # espacios alrededor
        ("   https://t.com/p   ", "https://t.com/p"),
        # host con punto final
        ("https://t.com./p", "https://t.com/p"),
        # IP pública literal permitida
        ("http://8.8.8.8/p", "http://8.8.8.8/p"),
    ],
)
def test_normalize_url(raw, expected):
    if expected is None:
        with pytest.raises(ValueError):
            normalize_url(raw)
    else:
        assert normalize_url(raw) == expected


def test_ref_is_preserved():
    assert "ref=" in normalize_url("https://t.com/p?ref=promo")


@pytest.mark.parametrize(
    "raw",
    [
        "https://t.com/p?utm_source=x&b=2&a=1#f",
        "HTTPS://WWW.T.com:443/a/b/?ref=1&id=2",
        "https://bücher.de/%7Efoo/",
        "https://t.com/p?x=2&x=1",
    ],
)
def test_normalize_is_idempotent(raw):
    once = normalize_url(raw)
    assert normalize_url(once) == once


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "   ",
        "no-es-una-url",
        "tienda.com/producto",
        "ftp://tienda.com/p",
        "javascript:alert(1)",
        "file:///etc/passwd",
        "data:text/html,hola",
        "https://",
        "https:///p",
        "https://user:pass@tienda.com/p",
        "https://user@tienda.com/p",
        "http://localhost/p",
        "http://app.localhost/p",
        "http://127.0.0.1/p",
        "http://10.0.0.5/p",
        "http://192.168.1.1/p",
        "http://172.16.0.1/p",
        "http://169.254.169.254/latest/meta-data",
        "http://[::1]/p",
        "http://[fd00::1]/p",
        "http://intranet/p",  # sin punto: host no público
        "https://tienda.com:99999/p",
        "https://tienda.com:abc/p",
        "https://tien da.com/p",
        "https://t.com/" + "a" * 3000,
    ],
)
def test_normalize_rejects_invalid_or_unsafe(raw):
    with pytest.raises(ValueError):
        normalize_url(raw)


def test_rejects_idna_label_too_long():
    with pytest.raises(ValueError, match="host"):
        normalize_url("https://" + "a" * 64 + ".com/p")


def test_rejects_url_that_grows_beyond_limit_when_encoded():
    # 400 caracteres no ASCII (<2048) se codifican a ~2400 caracteres.
    with pytest.raises(ValueError, match="excede"):
        normalize_url("https://t.com/" + "é" * 400)


def test_store_from_url_requires_host():
    with pytest.raises(ValueError):
        store_from_url("https:///sin-host")


def test_store_from_url():
    assert store_from_url(normalize_url("https://WWW.Tienda.com/p")) == "tienda.com"
    assert store_from_url("https://tienda.com:8443/p") == "tienda.com"
