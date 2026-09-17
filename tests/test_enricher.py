"""Tests for OpenSky-based flight-number enrichment."""

from datetime import datetime, timedelta, timezone

import pytest

import enricher
from enricher import (
    callsign_to_flight_number,
    iata_to_icao,
    icao_to_iata,
    _match_and_update,
    _propagate_flight_numbers,
    _unenriched_airports,
)
from db import db_session, get_or_create_route, insert_flight_schedule

PT = enricher.PT


# ── IATA ↔ ICAO mapping ───────────────────────────────────────────────────────

def test_iata_to_icao_us_default():
    assert iata_to_icao("SFO") == "KSFO"


def test_iata_to_icao_international_override():
    assert iata_to_icao("CUN") == "MMUN"
    assert iata_to_icao("SJD") == "MMSD"


def test_icao_to_iata_us_default():
    assert icao_to_iata("KLAS") == "LAS"


def test_icao_to_iata_override():
    assert icao_to_iata("MMUN") == "CUN"


def test_icao_to_iata_bad_input():
    assert icao_to_iata("") is None
    assert icao_to_iata("ABC") is None       # wrong length
    assert icao_to_iata("ZZZZ") is None       # non-K, non-override


def test_iata_icao_roundtrip():
    for code in ("SFO", "LAS", "CUN", "SJD"):
        assert icao_to_iata(iata_to_icao(code)) == code


# ── callsign parsing ──────────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "callsign,expected",
    [
        ("FFT1234", "F9 1234"),
        ("  FFT0089 ", "F9 89"),   # leading zeros stripped, whitespace trimmed
        ("FFTA1", "F9 1"),          # non-digit chars ignored
    ],
)
def test_callsign_to_flight_number(callsign, expected):
    assert callsign_to_flight_number(callsign) == expected


@pytest.mark.parametrize("callsign", ["", None, "UAL123", "FFT", "FFTABC"])
def test_callsign_to_flight_number_rejected(callsign):
    assert callsign_to_flight_number(callsign) is None


# ── _unenriched_airports ──────────────────────────────────────────────────────

def test_unenriched_airports_lists_origins_in_window(test_db):
    future = (datetime.now(PT) + timedelta(days=5)).isoformat()
    far_future = (datetime.now(PT) + timedelta(days=120)).isoformat()
    with db_session(test_db) as conn:
        r1 = get_or_create_route(conn, "SFO", "LAS", 1)
        r2 = get_or_create_route(conn, "SFO", "DEN", 1)
        # in-window nonstop, unenriched → included
        insert_flight_schedule(conn, r1, future, future, 90, 0, "outbound", "2026-04-06")
        # outside days_forward window → excluded
        insert_flight_schedule(conn, r2, far_future, far_future, 120, 0, "outbound", "2026-04-06")

    with db_session(test_db) as conn:
        airports = _unenriched_airports(conn, days_forward=60)

    assert airports == ["SFO"]


def test_unenriched_airports_skips_connections_and_enriched(test_db):
    future = (datetime.now(PT) + timedelta(days=5)).isoformat()
    with db_session(test_db) as conn:
        r = get_or_create_route(conn, "SJC", "ATL", 0)
        # 1-stop flight → excluded (enrichment is nonstop-only)
        insert_flight_schedule(conn, r, future, future, 300, 1, "outbound", "2026-04-06")

    with db_session(test_db) as conn:
        airports = _unenriched_airports(conn, days_forward=60)

    assert airports == []


# ── _match_and_update ─────────────────────────────────────────────────────────

def _opensky_row(callsign, dep_icao, arr_icao, dep_pt):
    return {
        "callsign": callsign,
        "estDepartureAirport": dep_icao,
        "estArrivalAirport": arr_icao,
        "firstSeen": int(dep_pt.astimezone(timezone.utc).timestamp()),
    }


def test_match_and_update_writes_flight_number(test_db):
    dep = PT.localize(datetime(2026, 4, 24, 18, 30))
    with db_session(test_db) as conn:
        r = get_or_create_route(conn, "SFO", "ATL", 1)
        sid = insert_flight_schedule(conn, r, dep.isoformat(), dep.isoformat(),
                                     240, 0, "outbound", "2026-04-20")

    row = _opensky_row("FFT1500", "KSFO", "KATL",
                       PT.localize(datetime(2026, 4, 24, 18, 35)))  # 5 min off → within ±15
    with db_session(test_db) as conn:
        assert _match_and_update(conn, row) == 1

    with db_session(test_db) as conn:
        fn = conn.execute(
            "SELECT flight_number FROM flight_schedules WHERE schedule_id = ?", (sid,)
        ).fetchone()["flight_number"]
    assert fn == "F9 1500"


def test_match_and_update_rejects_out_of_tolerance(test_db):
    dep = PT.localize(datetime(2026, 4, 24, 18, 30))
    with db_session(test_db) as conn:
        r = get_or_create_route(conn, "SFO", "ATL", 1)
        insert_flight_schedule(conn, r, dep.isoformat(), dep.isoformat(),
                               240, 0, "outbound", "2026-04-20")

    row = _opensky_row("FFT1500", "KSFO", "KATL",
                       PT.localize(datetime(2026, 4, 24, 19, 30)))  # 60 min off
    with db_session(test_db) as conn:
        assert _match_and_update(conn, row) == 0


def test_match_and_update_ignores_non_frontier(test_db):
    row = _opensky_row("UAL200", "KSFO", "KATL", PT.localize(datetime(2026, 4, 24, 18, 30)))
    with db_session(test_db) as conn:
        assert _match_and_update(conn, row) == 0


def test_match_and_update_no_matching_schedule(test_db):
    row = _opensky_row("FFT1500", "KSFO", "KATL", PT.localize(datetime(2026, 4, 24, 18, 30)))
    with db_session(test_db) as conn:
        assert _match_and_update(conn, row) == 0


# ── _propagate_flight_numbers ─────────────────────────────────────────────────

def test_propagate_carries_flight_number_forward(test_db):
    # Past enriched Friday 18:45 SFO->ATL, future unenriched Friday 18:50 SFO->ATL.
    past_fri = PT.localize(datetime(2020, 1, 3, 18, 45))       # a Friday in the past
    future_fri = PT.localize(datetime(2100, 1, 1, 18, 50))     # also a Friday, far future
    assert past_fri.weekday() == 4 and future_fri.weekday() == 4

    with db_session(test_db) as conn:
        r = get_or_create_route(conn, "SFO", "ATL", 1)
        past_sid = insert_flight_schedule(conn, r, past_fri.isoformat(), past_fri.isoformat(),
                                          240, 0, "outbound", "2019-12-30")
        conn.execute("UPDATE flight_schedules SET flight_number = 'F9 999' WHERE schedule_id = ?",
                     (past_sid,))
        future_sid = insert_flight_schedule(conn, r, future_fri.isoformat(), future_fri.isoformat(),
                                            240, 0, "outbound", "2099-12-28")

    with db_session(test_db) as conn:
        propagated = _propagate_flight_numbers(conn, verbose=False)
        assert propagated == 1
        fn = conn.execute(
            "SELECT flight_number FROM flight_schedules WHERE schedule_id = ?", (future_sid,)
        ).fetchone()["flight_number"]
    assert fn == "F9 999"


def test_propagate_skips_different_day_of_week(test_db):
    past_fri = PT.localize(datetime(2020, 1, 3, 18, 45))       # Friday
    future_sat = PT.localize(datetime(2100, 1, 2, 18, 45))     # Saturday
    assert past_fri.weekday() == 4 and future_sat.weekday() == 5

    with db_session(test_db) as conn:
        r = get_or_create_route(conn, "SFO", "ATL", 1)
        past_sid = insert_flight_schedule(conn, r, past_fri.isoformat(), past_fri.isoformat(),
                                          240, 0, "outbound", "2019-12-30")
        conn.execute("UPDATE flight_schedules SET flight_number = 'F9 999' WHERE schedule_id = ?",
                     (past_sid,))
        insert_flight_schedule(conn, r, future_sat.isoformat(), future_sat.isoformat(),
                               240, 0, "outbound", "2099-12-28")

    with db_session(test_db) as conn:
        assert _propagate_flight_numbers(conn, verbose=False) == 0


# ── enrich_schedules top-level (credential handling) ──────────────────────────

def test_enrich_schedules_skips_without_credentials(monkeypatch):
    monkeypatch.delenv("OPENSKY_CLIENT_ID", raising=False)
    monkeypatch.delenv("OPENSKY_CLIENT_SECRET", raising=False)
    monkeypatch.setattr(enricher, "_load_credentials",
                        lambda: (_ for _ in ()).throw(RuntimeError("no creds")))
    stats = enricher.enrich_schedules(verbose=False)
    assert stats["skipped"] is True
    assert stats["matched"] == 0
