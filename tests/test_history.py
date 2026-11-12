"""GET /products/{id}/history: orden, paginación, filtros de fecha y aislamiento."""

from datetime import UTC, datetime, timedelta

import pytest

T0 = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)


def at(minutes):
    return T0 + timedelta(minutes=minutes)


def ids(response):
    return [item["id"] for item in response.json()]


def test_empty_history_is_200_empty_list(client, new_product):
    pid = new_product()
    response = client.get(f"/products/{pid}/history")
    assert response.status_code == 200
    assert response.json() == []


def test_unknown_product_is_404(client):
    response = client.get("/products/999/history")
    assert response.status_code == 404
    assert response.json() == {"detail": "Producto no encontrado"}


@pytest.mark.parametrize("bad_id", ["0", "-1", "abc", "2147483648"])
def test_invalid_product_id_is_422(client, bad_id):
    assert client.get(f"/products/{bad_id}/history").status_code == 422


def test_item_fields_and_decimal_as_text(client, new_product, seed):
    pid = new_product()
    obs_id = seed.observation(pid, "51.77", at(0))
    (item,) = client.get(f"/products/{pid}/history").json()
    assert item["id"] == obs_id
    assert item["price"] == "51.77"  # texto exacto, nunca float
    assert item["currency"] == "GBP"
    assert item["price_raw"] == "GBP 51.77"
    assert item["source_url"] == "https://books.toscrape.com/p/1"
    assert {"check_run_id", "observed_at"} <= set(item)


def test_order_is_newest_first(client, new_product, seed):
    pid = new_product()
    a = seed.observation(pid, "1.00", at(0))
    c = seed.observation(pid, "3.00", at(20))  # insertado antes que b, pero más reciente
    b = seed.observation(pid, "2.00", at(10))
    assert ids(client.get(f"/products/{pid}/history")) == [c, b, a]


def test_identical_dates_are_ordered_by_id_desc(client, new_product, seed):
    pid = new_product()
    first, second, third = (seed.observation(pid, p, at(5)) for p in ("1.00", "2.00", "3.00"))
    assert ids(client.get(f"/products/{pid}/history")) == [third, second, first]


def test_pagination_pages_reassemble_the_full_history(client, new_product, seed):
    pid = new_product()
    expected = [seed.observation(pid, f"{i + 1}.00", at(i)) for i in range(7)][::-1]
    pages = [
        ids(client.get(f"/products/{pid}/history", params={"limit": 3, "offset": off}))
        for off in (0, 3, 6)
    ]
    assert [len(p) for p in pages] == [3, 3, 1]
    assert sum(pages, []) == expected
    assert client.get(f"/products/{pid}/history", params={"limit": 3, "offset": 7}).json() == []
    assert client.get(f"/products/{pid}/history", params={"offset": 1000}).json() == []


def test_default_limit_is_50(client, new_product, seed):
    pid = new_product()
    for i in range(55):
        seed.observation(pid, "1.00", at(i))
    assert len(client.get(f"/products/{pid}/history").json()) == 50


@pytest.mark.parametrize("params", [{"limit": 0}, {"limit": 101}, {"limit": "x"}, {"offset": -1}])
def test_invalid_pagination_is_422(client, new_product, params):
    pid = new_product()
    assert client.get(f"/products/{pid}/history", params=params).status_code == 422


# ---------- filtros de fecha ----------


@pytest.fixture()
def timeline(client, new_product, seed):
    """Observaciones a 0, 10, 20 y 30 minutos de T0."""
    pid = new_product()
    obs = {m: seed.observation(pid, f"{m + 1}.00", at(m)) for m in (0, 10, 20, 30)}
    return pid, obs


def get_ids(client, pid, **params):
    return ids(client.get(f"/products/{pid}/history", params=params))


def test_from_and_to_are_inclusive(client, timeline):
    pid, obs = timeline
    got = get_ids(client, pid, **{"from": at(10).isoformat(), "to": at(20).isoformat()})
    assert got == [obs[20], obs[10]]  # ambos extremos incluidos


def test_only_from_and_only_to(client, timeline):
    pid, obs = timeline
    assert get_ids(client, pid, **{"from": at(20).isoformat()}) == [obs[30], obs[20]]
    assert get_ids(client, pid, to=at(10).isoformat()) == [obs[10], obs[0]]


def test_filter_with_no_matches_is_empty_list(client, timeline):
    pid, _ = timeline
    assert get_ids(client, pid, **{"from": at(11).isoformat(), "to": at(19).isoformat()}) == []


def test_timezones_are_honoured(client, timeline):
    pid, obs = timeline
    # 12:10 UTC expresado en otras zonas: mismo instante, mismo resultado (extremo incluido).
    for stamp in ("2026-01-01T07:10:00-05:00", "2026-01-01T13:10:00+01:00", "2026-01-01T12:10:00Z"):
        assert get_ids(client, pid, **{"from": stamp, "to": stamp}) == [obs[10]], stamp
    # Un segundo después del instante ya no coincide.
    assert get_ids(client, pid, **{"from": "2026-01-01T07:10:01-05:00"}) == [obs[30], obs[20]]


@pytest.mark.parametrize(
    "stamp", ["2026-01-01T12:00:00", "2026-01-01", "ayer", "2026-13-01T00:00Z"]
)
def test_dates_without_timezone_or_invalid_are_422(client, timeline, stamp):
    pid, _ = timeline
    assert client.get(f"/products/{pid}/history", params={"from": stamp}).status_code == 422
    assert client.get(f"/products/{pid}/history", params={"to": stamp}).status_code == 422


def test_unencoded_plus_offset_is_rejected_not_misread(client, timeline):
    """'+' sin codificar llega como espacio: se rechaza en lugar de interpretar mal la hora."""
    pid, _ = timeline
    response = client.get(f"/products/{pid}/history?from=2026-01-01T13:10:00+01:00")
    assert response.status_code == 422


def test_from_after_to_is_422(client, timeline):
    pid, _ = timeline
    response = client.get(
        f"/products/{pid}/history", params={"from": at(20).isoformat(), "to": at(10).isoformat()}
    )
    assert response.status_code == 422
    assert "posterior" in response.text


# ---------- aislamiento ----------


def test_history_only_contains_the_requested_product(client, new_product, seed):
    a, b = new_product(), new_product()
    in_a = [seed.observation(a, "1.00", at(1)), seed.observation(a, "2.00", at(2))]
    in_b = [seed.observation(b, "9.00", at(1))]
    assert sorted(get_ids(client, a)) == sorted(in_a)
    assert get_ids(client, b) == in_b
