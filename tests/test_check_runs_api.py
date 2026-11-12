"""GET /products/{id}/check-runs: ejecuciones exitosas y fallidas."""

from datetime import UTC, datetime, timedelta

import httpx2
import pytest

from app.config import settings

T0 = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)


def at(minutes):
    return T0 + timedelta(minutes=minutes)


def ids(response):
    return [item["id"] for item in response.json()]


def test_empty_is_200_empty_list(client, new_product):
    pid = new_product()
    assert client.get(f"/products/{pid}/check-runs").json() == []


def test_unknown_product_is_404(client):
    response = client.get("/products/999/check-runs")
    assert response.status_code == 404
    assert response.json() == {"detail": "Producto no encontrado"}


def test_success_and_failed_runs_with_their_fields(client, new_product, seed):
    pid = new_product()
    ok = seed.run(pid, at(0))
    bad = seed.run(
        pid, at(5), status="error", error_code="timeout", http_status=None, msg="se agotó el plazo"
    )
    items = client.get(f"/products/{pid}/check-runs").json()
    assert [i["id"] for i in items] == [bad, ok]  # más reciente primero
    failed, success = items
    assert failed["status"] == "error"
    assert failed["error_code"] == "timeout"
    assert failed["error_message"] == "se agotó el plazo"
    assert failed["http_status"] is None
    assert success["status"] == "success"
    assert success["error_code"] is None and success["error_message"] is None
    assert success["http_status"] == 200 and success["duration_ms"] == 5
    assert "requested_url" not in success


def test_status_filter(client, new_product, seed):
    pid = new_product()
    s1 = seed.run(pid, at(0))
    e1 = seed.run(pid, at(1), status="error", error_code="parse_error")
    s2 = seed.run(pid, at(2))
    e2 = seed.run(pid, at(3), status="error", error_code="upstream_http_error", http_status=503)
    base = f"/products/{pid}/check-runs"
    assert ids(client.get(base, params={"status": "success"})) == [s2, s1]
    assert ids(client.get(base, params={"status": "error"})) == [e2, e1]
    assert ids(client.get(base)) == [e2, s2, e1, s1]


@pytest.mark.parametrize("value", ["", "ok", "ERROR", "running"])
def test_invalid_status_is_422(client, new_product, value):
    pid = new_product()
    assert client.get(f"/products/{pid}/check-runs", params={"status": value}).status_code == 422


def test_identical_dates_ordered_by_id_desc(client, new_product, seed):
    pid = new_product()
    a, b, c = (seed.run(pid, at(7)) for _ in range(3))
    assert ids(client.get(f"/products/{pid}/check-runs")) == [c, b, a]


def test_pagination(client, new_product, seed):
    pid = new_product()
    expected = [seed.run(pid, at(i)) for i in range(5)][::-1]
    base = f"/products/{pid}/check-runs"
    assert ids(client.get(base, params={"limit": 2})) == expected[:2]
    assert ids(client.get(base, params={"limit": 2, "offset": 2})) == expected[2:4]
    assert ids(client.get(base, params={"limit": 2, "offset": 4})) == expected[4:]
    assert client.get(base, params={"offset": 99}).json() == []
    for bad in ({"limit": 0}, {"limit": 101}, {"offset": -1}):
        assert client.get(base, params=bad).status_code == 422


def test_runs_are_isolated_per_product(client, new_product, seed):
    a, b = new_product(), new_product()
    run_a = seed.run(a, at(0))
    run_b = seed.run(b, at(0), status="error", error_code="timeout")
    assert ids(client.get(f"/products/{a}/check-runs")) == [run_a]
    assert ids(client.get(f"/products/{b}/check-runs")) == [run_b]


def test_end_to_end_real_failed_and_successful_checks_are_listed(
    client, upstream, new_product, monkeypatch
):
    """Las ejecuciones que produce POST /check (fallida y luego exitosa) se ven en el listado."""
    from pathlib import Path

    page = (Path(__file__).parent / "fixtures" / "books_toscrape" / "product_ok.html").read_bytes()
    monkeypatch.setattr(settings, "CHECK_MIN_INTERVAL_SECONDS", 0)
    pid = new_product()

    upstream.handler = lambda r: httpx2.Response(503, headers={"content-type": "text/html"})
    assert client.post(f"/products/{pid}/check").status_code == 502
    upstream.handler = lambda r: httpx2.Response(
        200, content=page, headers={"content-type": "text/html"}
    )
    assert client.post(f"/products/{pid}/check").status_code == 201

    items = client.get(f"/products/{pid}/check-runs").json()
    assert [(i["status"], i["error_code"], i["http_status"]) for i in items] == [
        ("success", None, 200),
        ("error", "upstream_http_error", 503),
    ]
    # y el historial de precios solo contiene la ejecución exitosa
    history = client.get(f"/products/{pid}/history").json()
    assert len(history) == 1 and history[0]["check_run_id"] == items[0]["id"]
