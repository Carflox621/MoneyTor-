"""Seguridad del fetcher. Todo con MockTransport y resolvedor simulado: sin red real."""

import gzip

import httpx2
import pytest

from app.config import settings
from app.services.fetcher import FetchError, fetch_html, is_public_ip

URL = "https://books.toscrape.com/catalogue/a-light-in-the-attic_1000/index.html"
PUBLIC = lambda host: ["13.248.0.10"]  # noqa: E731


def make_client(handler, seen=None):
    def wrapped(request):
        if seen is not None:
            seen.append(request)
        return handler(request)

    return httpx2.Client(transport=httpx2.MockTransport(wrapped), trust_env=False)


def ok(request):
    return httpx2.Response(200, text="<html>ok £1</html>", headers={"content-type": "text/html"})


def code_of(url, client, resolver=PUBLIC):
    with pytest.raises(FetchError) as exc:
        fetch_html(url, client, resolver)
    return exc.value.code


def test_success_forces_https_and_sends_identifying_headers():
    seen = []
    result = fetch_html(URL.replace("https://", "http://"), make_client(ok, seen), PUBLIC)
    assert result.html == "<html>ok £1</html>"
    assert result.http_status == 200
    request = seen[0]
    assert request.url.scheme == "https"
    assert request.url.host == "books.toscrape.com"
    assert request.headers["user-agent"] == settings.USER_AGENT
    assert "MoneyTor" in request.headers["user-agent"]
    assert "cookie" not in request.headers and "authorization" not in request.headers


@pytest.mark.parametrize(
    "url",
    [
        "https://evil.example.com/x",
        "https://books.toscrape.com.evil.com/x",
        "https://evil.com/books.toscrape.com",
        "https://127.0.0.1/x",
        "https://169.254.169.254/latest/meta-data",
        "https://[::1]/x",
        "https://books.toscrape.com:8443/x",
        "http://books.toscrape.com:8080/x",
        "ftp://books.toscrape.com/x",
        "file:///etc/passwd",
    ],
)
def test_destinations_outside_allowlist_never_reach_network(url):
    seen = []
    assert code_of(url, make_client(ok, seen)) == "blocked_destination"
    assert seen == []


@pytest.mark.parametrize(
    "address",
    [
        "127.0.0.1",
        "10.0.0.5",
        "172.16.0.1",
        "192.168.1.1",
        "169.254.169.254",
        "0.0.0.0",
        "100.64.0.1",
        "::1",
        "fd00::1",
        "fe80::1",
        "::ffff:127.0.0.1",
        "::ffff:10.0.0.1",
        "::ffff:169.254.169.254",
    ],
)
def test_dns_resolving_to_non_public_address_is_blocked(address):
    seen = []
    assert code_of(URL, make_client(ok, seen), lambda h: [address]) == "blocked_destination"
    assert seen == []


def test_mixed_public_and_private_resolution_is_blocked():
    assert code_of(URL, make_client(ok), lambda h: ["13.248.0.10", "127.0.0.1"]) == (
        "blocked_destination"
    )


def test_unparseable_address_is_blocked():
    assert code_of(URL, make_client(ok), lambda h: ["not-an-ip"]) == "blocked_destination"


@pytest.mark.parametrize("resolver_result", [[], OSError("nxdomain")])
def test_dns_failure_or_empty_resolution(resolver_result):
    def resolver(host):
        if isinstance(resolver_result, Exception):
            raise resolver_result
        return resolver_result

    assert code_of(URL, make_client(ok), resolver) == "fetch_failed"


def test_public_ip_helper():
    assert is_public_ip("8.8.8.8")
    assert is_public_ip("::ffff:8.8.8.8")
    assert not is_public_ip("::ffff:127.0.0.1")
    assert not is_public_ip("fe80::1%eth0")


@pytest.mark.parametrize("status", [301, 302, 307, 308])
def test_redirects_are_never_followed(status):
    seen = []

    def handler(request):
        return httpx2.Response(status, headers={"location": "http://169.254.169.254/"})

    assert code_of(URL, make_client(handler, seen)) == "redirect_not_allowed"
    assert len(seen) == 1  # solo la petición original, jamás el destino del Location


@pytest.mark.parametrize("status", [404, 403, 429, 500, 503, 204])
def test_non_200_is_upstream_error_with_status(status):
    client = make_client(lambda r: httpx2.Response(status, headers={"content-type": "text/html"}))
    with pytest.raises(FetchError) as exc:
        fetch_html(URL, client, PUBLIC)
    assert exc.value.code == "upstream_http_error"
    assert exc.value.http_status == status


@pytest.mark.parametrize("ctype", ["application/json", "text/plain", "", "image/png"])
def test_content_type_must_be_html(ctype):
    client = make_client(lambda r: httpx2.Response(200, text="x", headers={"content-type": ctype}))
    assert code_of(URL, client) == "bad_content_type"


def test_content_type_with_charset_is_accepted():
    client = make_client(
        lambda r: httpx2.Response(
            200, text="x", headers={"content-type": "text/html; charset=UTF-8"}
        )
    )
    assert fetch_html(URL, client, PUBLIC).html == "x"


def test_declared_content_length_over_limit_is_rejected(monkeypatch):
    monkeypatch.setattr(settings, "MAX_RESPONSE_BYTES", 100)
    client = make_client(
        lambda r: httpx2.Response(
            200, content=b"x" * 10, headers={"content-type": "text/html", "content-length": "5000"}
        )
    )
    assert code_of(URL, client) == "too_large"


def test_streamed_body_over_limit_is_rejected(monkeypatch):
    monkeypatch.setattr(settings, "MAX_RESPONSE_BYTES", 1000)

    def handler(request):
        def body():
            for _ in range(50):
                yield b"x" * 100

        return httpx2.Response(200, content=body(), headers={"content-type": "text/html"})

    assert code_of(URL, make_client(handler)) == "too_large"


def test_gzip_bomb_is_capped_after_decompression(monkeypatch):
    monkeypatch.setattr(settings, "MAX_RESPONSE_BYTES", 10_000)
    bomb = gzip.compress(b"a" * 5_000_000)
    assert len(bomb) < 10_000  # pequeño en el cable, enorme al descomprimir

    def handler(request):
        return httpx2.Response(
            200, content=bomb, headers={"content-type": "text/html", "content-encoding": "gzip"}
        )

    assert code_of(URL, make_client(handler)) == "too_large"


def test_timeout_is_reported_as_timeout():
    def handler(request):
        raise httpx2.ReadTimeout("slow", request=request)

    assert code_of(URL, make_client(handler)) == "timeout"


def test_total_deadline_stops_slow_drip(monkeypatch):
    monkeypatch.setattr(settings, "HTTP_TOTAL_TIMEOUT", -1.0)

    def handler(request):
        return httpx2.Response(
            200, content=iter([b"a", b"b"]), headers={"content-type": "text/html"}
        )

    assert code_of(URL, make_client(handler)) == "timeout"


def test_connection_errors_do_not_leak_internal_details():
    def handler(request):
        raise httpx2.ConnectError("connect to 10.1.2.3:443 failed", request=request)

    with pytest.raises(FetchError) as exc:
        fetch_html(URL, make_client(handler), PUBLIC)
    assert exc.value.code == "fetch_failed"
    assert "10.1.2.3" not in exc.value.message


def test_invalid_utf8_is_rejected():
    client = make_client(
        lambda r: httpx2.Response(200, content=b"\xa351.77", headers={"content-type": "text/html"})
    )
    assert code_of(URL, client) == "parse_error"


def test_error_messages_never_include_upstream_body():
    secret = "SECRET-INTERNAL-BODY"
    client = make_client(
        lambda r: httpx2.Response(500, text=secret, headers={"content-type": "text/html"})
    )
    with pytest.raises(FetchError) as exc:
        fetch_html(URL, client, PUBLIC)
    assert secret not in exc.value.message
