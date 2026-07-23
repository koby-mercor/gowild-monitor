"""Tests for weekly flight-schedule seeding."""

from datetime import datetime
from unittest.mock import patch

import seeder
from seeder import _is_nonstop_route, seed_week
from db import db_session
from gowild_search import FlightOption


def test_is_nonstop_route_true():
    assert _is_nonstop_route("SFO", "LAS") is True


def test_is_nonstop_route_false():
    assert _is_nonstop_route("SFO", "BOS") is False
    assert _is_nonstop_route("OAK", "LAS") is False  # OAK has no Frontier service


def _flight(dep_dt, dep="8:00 AM on Mon, Apr 6", arr="9:00 PM on Sat, Apr 11",
            dur="2 hr 30 min", stops=0, price="$120"):
    return FlightOption(
        origin="SFO", destination="LAS", departure=dep, arrival=arr,
        duration=dur, stops=stops, price=price,
        dep_dt=dep_dt,
        arr_dt=dep_dt.replace(hour=(dep_dt.hour + 2) % 24),
    )


def test_seed_week_inserts_schedules(env_db, monkeypatch):
    """seed_week should insert a distinct schedule per (route, departure, direction)."""
    # Keep the run tiny: monitor just one origin with one destination.
    monkeypatch.setattr(seeder, "MONITORED_ROUTES", {"SFO": ["LAS"]})
    monkeypatch.setattr(seeder, "RATE_LIMIT_SECONDS", 0)

    # Return a flight whose departure date matches the search date so each
    # search yields a distinct schedule row rather than colliding on UNIQUE.
    def fake_search(origin, dest, date_str, max_stops=None):
        dep = datetime.strptime(date_str, "%Y-%m-%d").replace(hour=8)
        return [_flight(dep)]

    with patch("seeder.search_flights", side_effect=fake_search):
        stats = seed_week("2026-04-06")

    # 1 route pair × (7 outbound + 9 return) dates, one flight each.
    assert stats["routes_checked"] == 16
    assert stats["flights_found"] == 16
    assert stats["flights_inserted"] == 16

    with db_session(env_db) as conn:
        count = conn.execute("SELECT COUNT(*) AS c FROM flight_schedules").fetchone()["c"]
    assert count == 16


def test_seed_week_counts_search_errors(env_db, monkeypatch):
    monkeypatch.setattr(seeder, "MONITORED_ROUTES", {"SFO": ["LAS"]})
    monkeypatch.setattr(seeder, "RATE_LIMIT_SECONDS", 0)

    with patch("seeder.search_flights", side_effect=Exception("rate limited")):
        stats = seed_week("2026-04-06")

    assert stats["errors"] == 16
    assert stats["flights_inserted"] == 0


def test_seed_week_skips_flights_without_dep_dt(env_db, monkeypatch):
    monkeypatch.setattr(seeder, "MONITORED_ROUTES", {"SFO": ["LAS"]})
    monkeypatch.setattr(seeder, "RATE_LIMIT_SECONDS", 0)

    bad = _flight(datetime(2026, 4, 6, 8, 0))
    bad.dep_dt = None
    with patch("seeder.search_flights", return_value=[bad]):
        stats = seed_week("2026-04-06")

    assert stats["flights_found"] == 0
    assert stats["flights_inserted"] == 0
