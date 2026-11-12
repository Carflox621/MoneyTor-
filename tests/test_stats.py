"""GET /products/{id}/stats: exactitud, monedas, empates y consistencia de la lectura."""

import random
import threading
from datetime import UTC, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal

import pytest
from sqlalchemy import text

T0 = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)


def at(minutes):
    return T0 + timedelta(minutes=minutes)


def stats(client, pid, **params):
    response = client.get(f"/products/{pid}/stats", params=params)
    assert response.status_code == 200, response.text
    return response.json()


def block(client, pid, currency="GBP", **params):
    (found,) = [b for b in stats(client, pid, **params)["by_currency"] if b["currency"] == currency]
    return found


def seed_series(seed, pid, prices, currency="GBP"):
    for i, price in enumerate(prices):
        seed.observation(pid, price, at(i), currency)


# ---------- casos base ----------


def test_empty_history_has_no_blocks(client, new_product):
    pid = new_product()
    assert stats(client, pid) == {"product_id": pid, "by_currency": []}


def test_unknown_product_is_404(client):
    response = client.get("/products/999/stats")
    assert response.status_code == 404
    assert response.json() == {"detail": "Producto no encontrado"}


@pytest.mark.parametrize("bad_id", ["0", "abc", "2147483648"])
def test_invalid_id_is_422(client, bad_id):
    assert client.get(f"/products/{bad_id}/stats").status_code == 422


def test_single_observation_has_zero_change(client, new_product, seed):
    pid = new_product()
    seed.observation(pid, "51.77", at(0))
    b = block(client, pid)
    assert b["count"] == 1
    assert b["min"] == b["max"] == b["first"] == b["latest"] == "51.77"
    assert b["change"] == "0.00" and b["change_pct"] == "0.00"
    assert b["first_observed_at"] == b["latest_observed_at"]


def test_known_series_matches_hand_calculation(client, new_product, seed):
    pid = new_product()
    seed_series(seed, pid, ["51.77", "53.74", "49.99", "49.99", "52.00"])
    b = block(client, pid)
    assert b["count"] == 5
    assert (b["min"], b["max"]) == ("49.99", "53.74")
    assert (b["first"], b["latest"]) == ("51.77", "52.00")
    assert b["change"] == "0.23"
    assert b["change_pct"] == "0.44"  # 0.23 / 51.77 * 100 = 0.4442...
    assert datetime.fromisoformat(b["max_observed_at"]) == at(1)
    assert datetime.fromisoformat(b["first_observed_at"]) == at(0)
    assert datetime.fromisoformat(b["latest_observed_at"]) == at(4)
    # El mínimo se repite (t2 y t3): se informa la ocurrencia MÁS RECIENTE.
    assert datetime.fromisoformat(b["min_observed_at"]) == at(3)


@pytest.mark.parametrize(
    ("prices", "change", "pct"),
    [
        (["60.00", "45.00"], "-15.00", "-25.00"),  # variación negativa
        (["10.00", "12.00", "10.00"], "0.00", "0.00"),  # sube y vuelve: cero
        (["50.00", "55.00"], "5.00", "10.00"),  # positiva
        (["3.00", "3.01"], "0.01", "0.33"),
        (["8.00", "8.01"], "0.01", "0.13"),  # 0.125 -> 0.13 (mitad hacia arriba)
        (["8.00", "7.99"], "-0.01", "-0.13"),  # -0.125 -> -0.13 (simétrico, no a par)
    ],
)
def test_change_and_percentage(client, new_product, seed, prices, change, pct):
    pid = new_product()
    seed_series(seed, pid, prices)
    b = block(client, pid)
    assert (b["change"], b["change_pct"]) == (change, pct)


def test_amounts_are_exact_decimals_not_floats(client, new_product, seed):
    pid = new_product()
    seed_series(seed, pid, ["0.01", "9999999999.99"])
    response = client.get(f"/products/{pid}/stats")
    b = response.json()["by_currency"][0]
    assert b["latest"] == "9999999999.99" and b["change"] == "9999999999.98"
    assert b["change_pct"] == "99999999999800.00"  # exacto: un float perdería precisión
    for key in ("min", "max", "first", "latest", "change", "change_pct"):
        assert isinstance(b[key], str)
    assert '"min":"0.01"' in response.text.replace(" ", "")


# ---------- empates de fecha ----------


def test_identical_dates_use_id_as_tiebreaker(client, new_product, seed):
    pid = new_product()
    for price in ("10.00", "20.00", "15.00"):  # misma fecha, ids crecientes
        seed.observation(pid, price, at(5))
    b = block(client, pid)
    assert b["first"] == "10.00"  # menor id entre las más antiguas
    assert b["latest"] == "15.00"  # mayor id entre las más recientes
    assert (b["min"], b["max"]) == ("10.00", "20.00")


def test_identical_dates_and_prices_are_stable(client, new_product, seed):
    pid = new_product()
    for _ in range(3):
        seed.observation(pid, "5.00", at(0))
    b = block(client, pid)
    assert b["count"] == 3 and b["change"] == "0.00"
    assert stats(client, pid) == stats(client, pid)  # determinista


# ---------- monedas ----------


def test_one_block_per_currency_never_mixed(client, new_product, seed):
    pid = new_product()
    seed.observation(pid, "100.00", at(0), "GBP")
    seed.observation(pid, "10.00", at(1), "EUR")
    seed.observation(pid, "110.00", at(2), "GBP")
    seed.observation(pid, "12.00", at(3), "EUR")
    seed.observation(pid, "7.00", at(4), "USD")
    blocks = stats(client, pid)["by_currency"]
    assert [b["currency"] for b in blocks] == ["EUR", "GBP", "USD"]  # orden alfabético
    eur, gbp, usd = blocks
    assert (eur["count"], eur["min"], eur["max"], eur["change_pct"]) == (
        2,
        "10.00",
        "12.00",
        "20.00",
    )
    assert (gbp["count"], gbp["min"], gbp["max"], gbp["change_pct"]) == (
        2,
        "100.00",
        "110.00",
        "10.00",
    )
    assert (usd["count"], usd["change"], usd["change_pct"]) == (1, "0.00", "0.00")


# ---------- filtros ----------


def test_from_to_recompute_inclusively(client, new_product, seed):
    pid = new_product()
    seed_series(seed, pid, ["10.00", "20.00", "5.00", "40.00"])  # a 0, 1, 2 y 3 min
    b = block(client, pid, **{"from": at(1).isoformat(), "to": at(2).isoformat()})
    assert b["count"] == 2
    assert (b["first"], b["latest"], b["min"], b["max"]) == ("20.00", "5.00", "5.00", "20.00")
    assert (b["change"], b["change_pct"]) == ("-15.00", "-75.00")
    assert stats(client, pid, to=at(-1).isoformat())["by_currency"] == []


def test_filters_validate_like_history(client, new_product):
    pid = new_product()
    url = f"/products/{pid}/stats"
    assert client.get(url, params={"from": "2026-01-01T00:00:00"}).status_code == 422
    assert client.get(url, params={"to": "2026-01-01"}).status_code == 422
    inverted = {"from": at(5).isoformat(), "to": at(1).isoformat()}
    assert client.get(url, params=inverted).status_code == 422


def test_stats_are_isolated_per_product(client, new_product, seed):
    a, b = new_product(), new_product()
    seed_series(seed, a, ["10.00", "20.00"])
    seed_series(seed, b, ["99.00"])
    assert block(client, a)["count"] == 2 and block(client, a)["max"] == "20.00"
    assert block(client, b)["count"] == 1 and block(client, b)["max"] == "99.00"


# ---------- verificación cruzada contra un cálculo independiente ----------


def expected_stats(rows):
    """Recalcula en Python (Decimal) a partir del historial completo: oráculo independiente."""
    out = {}
    for currency in sorted({r["currency"] for r in rows}):
        group = [r for r in rows if r["currency"] == currency]
        key = lambda r: (r["observed_at"], r["id"])  # noqa: E731
        oldest, newest = min(group, key=key), max(group, key=key)
        prices = [Decimal(r["price"]) for r in group]
        low = max((r for r in group if Decimal(r["price"]) == min(prices)), key=key)
        high = max((r for r in group if Decimal(r["price"]) == max(prices)), key=key)
        change = Decimal(newest["price"]) - Decimal(oldest["price"])
        pct = (change / Decimal(oldest["price"]) * 100).quantize(Decimal("0.01"), ROUND_HALF_UP)
        out[currency] = {
            "count": len(group),
            "min": min(prices),
            "min_at": low["observed_at"],
            "max": max(prices),
            "max_at": high["observed_at"],
            "first": Decimal(oldest["price"]),
            "latest": Decimal(newest["price"]),
            "change": change,
            "change_pct": pct,
        }
    return out


@pytest.mark.parametrize("seed_value", [1, 2, 3])
def test_stats_match_independent_recomputation_from_history(client, new_product, seed, seed_value):
    rng = random.Random(seed_value)
    pid = new_product()
    for _ in range(60):  # precios repetidos y fechas repetidas a propósito
        price = f"{rng.randint(1, 30)}.{rng.choice(['00', '25', '99'])}"
        seed.observation(pid, price, at(rng.randint(0, 20)), rng.choice(["GBP", "EUR"]))

    history = []
    for offset in (0, 100):
        history += client.get(
            f"/products/{pid}/history", params={"limit": 100, "offset": offset}
        ).json()
    assert len(history) == 60
    expected = expected_stats(history)

    blocks = stats(client, pid)["by_currency"]
    assert [b["currency"] for b in blocks] == sorted(expected)
    for b in blocks:
        want = expected[b["currency"]]
        assert b["count"] == want["count"]
        for key in ("min", "max", "first", "latest", "change", "change_pct"):
            assert Decimal(b[key]) == want[key], (b["currency"], key)
        assert datetime.fromisoformat(b["min_observed_at"]) == datetime.fromisoformat(
            want["min_at"]
        )
        assert datetime.fromisoformat(b["max_observed_at"]) == datetime.fromisoformat(
            want["max_at"]
        )


# ---------- consistencia ante escrituras concurrentes ----------


def test_uncommitted_writes_are_invisible_and_committed_ones_appear(
    client, new_product, seed, session_factory
):
    pid = new_product()
    seed.observation(pid, "10.00", at(0))
    run_sql = text(
        "INSERT INTO check_runs (product_id, status, requested_url, started_at, finished_at,"
        " duration_ms) VALUES (:p, 'success', 'https://x.test', :t, :t, 1) RETURNING id"
    )
    hist_sql = text(
        "INSERT INTO price_history (product_id, check_run_id, price, currency, price_raw,"
        " source_url, observed_at) VALUES (:p, :r, 99.00, 'GBP', 'x', 'https://x.test', :t)"
    )
    with session_factory() as writer:
        run_id = writer.execute(run_sql, {"p": pid, "t": at(1)}).scalar()
        writer.execute(hist_sql, {"p": pid, "r": run_id, "t": at(1)})
        # escritura SIN confirmar: la lectura no la ve (ni a medias)
        assert block(client, pid)["count"] == 1 and block(client, pid)["max"] == "10.00"
        writer.commit()
    b = block(client, pid)  # confirmada: se ve completa
    assert (b["count"], b["max"], b["latest"]) == (2, "99.00", "99.00")


def test_stats_stay_internally_consistent_while_rows_are_being_inserted(
    client, new_product, seed, session_factory
):
    """Un escritor inserta precios estrictamente crecientes (1.00, 1.01, 1.02, ...), uno por
    transacción, mientras varios lectores piden estadísticas. En cualquier instantánea
    coherente de las filas 0..n deben cumplirse relaciones entre campos que un cálculo
    hecho en varias consultas (o con instantáneas distintas) rompería al mezclar un
    `count` de un instante con un `max` de otro:
        latest == max,  latest_observed_at == max_observed_at,  count == (latest - 1.00)/0.01 + 1
    """
    pid = new_product()
    seed.observation(pid, "1.00", at(0))
    stop = threading.Event()
    problems: list[str] = []
    checked = []

    def writer():
        i = 1
        while not stop.is_set() and i < 400:
            seed.observation(pid, Decimal("1.00") + Decimal(i) * Decimal("0.01"), at(i))
            i += 1

    def reader():
        while not stop.is_set():
            response = client.get(f"/products/{pid}/stats")
            if response.status_code != 200:
                problems.append(f"HTTP {response.status_code}")
                continue
            (b,) = response.json()["by_currency"]
            latest, count = Decimal(b["latest"]), b["count"]
            if b["max"] != b["latest"] or b["max_observed_at"] != b["latest_observed_at"]:
                problems.append(f"latest != max: {b}")
            if (latest - Decimal("1.00")) / Decimal("0.01") + 1 != count:
                problems.append(f"count inconsistente: {b}")
            if b["first"] != "1.00" or b["min"] != "1.00" or Decimal(b["change"]) != latest - 1:
                problems.append(f"first/min/change inconsistentes: {b}")
            checked.append(count)

    w = threading.Thread(target=writer)
    readers = [threading.Thread(target=reader) for _ in range(4)]
    for t in [w, *readers]:
        t.start()
    w.join()
    stop.set()
    for t in readers:
        t.join()

    assert problems == []
    assert len(checked) >= 20, "los lectores no llegaron a ejecutarse lo suficiente"
    assert min(checked) < max(checked), "las lecturas no observaron escrituras en curso"
    assert block(client, pid)["count"] == 400  # estado final completo
