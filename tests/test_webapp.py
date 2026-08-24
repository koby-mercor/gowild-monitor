"""Tests for the Flask dashboard: pure helpers + API endpoints.

The webapp resolves its DB from GOWILD_DB_PATH at request time and runs
init_db() at import. We point GOWILD_DB_PATH at a seeded throwaway DB for the
whole module (set before importing webapp) so no real DB is touched.
"""

import os
import tempfile

import pytest

from db import (
    init_db, db_session, get_or_create_route,
    insert_flight_schedule, insert_availability_check,
)


@pytest.fixture(scope="module")
def client():
    tmpdir = tempfile.mkdtemp()
    db_path = os.path.join(tmpdir, "webapp_test.db")
    prev = os.environ.get("GOWILD_DB_PATH")
    os.environ["GOWILD_DB_PATH"] = db_path

    init_db(db_path)
    with db_session(db_path) as conn:
        out = get_or_create_route(conn, "SFO", "LAS", 1)
        ret = get_or_create_route(conn, "LAS", "SFO", 1)
        # past outbound with a couple of T-24h checks
        o_sid = insert_flight_schedule(
            conn, out, "2020-04-10T20:00:00-07:00", "2020-04-10T22:00:00-07:00",
            120, 0, "outbound", "2020-04-06")
        r_sid = insert_flight_schedule(
            conn, ret, "2020-04-12T09:00:00-07:00", "2020-04-12T11:00:00-07:00",
            120, 0, "return", "2020-04-06")
        insert_availability_check(
            conn, o_sid, "T-24h", "2020-04-09T20:00:00-07:00", 23.5,
            flight_found=True, price="$150", price_cents=15000, num_results=3)
        insert_availability_check(
            conn, r_sid, "T-24h", "2020-04-11T09:00:00-07:00", 23.5,
            flight_found=True, price="$140", price_cents=14000, num_results=2)
        # a future outbound schedule to exercise prediction annotation
        insert_flight_schedule(
            conn, out, "2100-04-16T20:00:00-07:00", "2100-04-16T22:00:00-07:00",
            120, 0, "outbound", "2100-04-12")

    import webapp
    webapp.app.testing = True
    yield webapp.app.test_client()

    if prev is None:
        os.environ.pop("GOWILD_DB_PATH", None)
    else:
        os.environ["GOWILD_DB_PATH"] = prev


# ── pure helpers ──────────────────────────────────────────────────────────────

def test_airports_as_dict_shape():
    import webapp
    d = webapp._airports_as_dict()
    assert "SFO" in d
    assert set(d["SFO"]) == {"lat", "lng", "city"}


def test_null_safe():
    import webapp
    assert webapp._null_safe(None) is None
    assert webapp._null_safe(0) == 0
    assert webapp._null_safe("x") == "x"


def test_parse_pt_weekday_conversion():
    import webapp
    # 2026-04-10 is a Friday → SQLite %w Sun=0..Sat=6 makes Friday = 5
    dow, hour, minute = webapp._parse_pt("2026-04-10T18:30:00-07:00")
    assert (dow, hour, minute) == (5, 18, 30)


def test_parse_pt_bad_input():
    import webapp
    assert webapp._parse_pt("not-a-date") is None


def test_annotate_predictions_uses_past_same_day():
    import webapp
    schedules = [
        # three past Fridays at 18:xx, mixed found/not-found → 2/3 available
        _past("2020-01-03T18:00:00-07:00", found=[True, True]),
        _past("2020-01-10T18:15:00-07:00", found=[False]),
        # future Friday → should get a prediction from the (dow) bucket
        {"departure_pt": "2100-01-01T18:30:00-07:00", "stops": 0,
         "flight_number": None, "checks": []},
    ]
    webapp._annotate_predictions(schedules)
    future = schedules[-1]
    assert future["prediction_samples"] == 3
    assert future["prediction_pct"] == pytest.approx(66.7, abs=0.1)


def _past(dep, found):
    checks = [
        {"check_type": "T-24h", "checked_at": dep, "hours_before_dep": 23.5,
         "flight_found": f, "price": "$100", "num_results": 2}
        for f in found
    ]
    return {"departure_pt": dep, "stops": 0, "flight_number": None, "checks": checks}


# ── API endpoints ─────────────────────────────────────────────────────────────

def test_api_airports(client):
    resp = client.get("/api/airports")
    assert resp.status_code == 200
    assert "SFO" in resp.get_json()


def test_api_routes(client):
    resp = client.get("/api/routes")
    assert resp.status_code == 200
    data = resp.get_json()
    key = {(r["origin"], r["destination"]) for r in data}
    assert ("SFO", "LAS") in key
    sfo_las = next(r for r in data if (r["origin"], r["destination"]) == ("SFO", "LAS"))
    assert sfo_las["outbound"]["pct"] == 100.0


def test_api_route_detail(client):
    resp = client.get("/api/routes/SFO/LAS")
    assert resp.status_code == 200
    data = resp.get_json()
    # includes both outbound (SFO->LAS) and return (LAS->SFO) schedules
    directions = {s["direction"] for s in data}
    assert {"outbound", "return"} <= directions
    # future schedule should carry a prediction annotation
    future = [s for s in data if s["departure_pt"].startswith("2100")]
    assert future and "prediction_pct" in future[0]


def test_api_destinations(client):
    resp = client.get("/api/destinations")
    assert resp.status_code == 200
    data = resp.get_json()
    las = next(d for d in data if d["destination"] == "LAS")
    assert las["confidence"] in {"low", "medium", "high"}
    assert las["stops_label"] == "Nonstop"


def test_api_stats(client):
    resp = client.get("/api/stats")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["total_checks"] == 2
    assert data["destinations"] >= 1
