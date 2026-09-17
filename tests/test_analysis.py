"""Tests for the reporting/analysis layer.

These functions render Rich tables and query the DB via get_connection(),
which resolves GOWILD_DB_PATH. We exercise both the empty-data early return
and the populated path, asserting on captured console output rather than
pixel-perfect table formatting.
"""

import analysis
from db import db_session, get_or_create_route, insert_flight_schedule, insert_availability_check


def _seed_checks(db_path):
    """Two weeks of T-24h checks for SFO<->LAS, both directions, mostly available."""
    with db_session(db_path) as conn:
        out = get_or_create_route(conn, "SFO", "LAS", 1)
        ret = get_or_create_route(conn, "LAS", "SFO", 1)
        for week, wk_of in enumerate(["2026-04-06", "2026-04-13"]):
            o_sid = insert_flight_schedule(
                conn, out, f"2026-04-{10 + week * 7}T20:00:00-07:00",
                f"2026-04-{10 + week * 7}T22:00:00-07:00", 120, 0, "outbound", wk_of)
            r_sid = insert_flight_schedule(
                conn, ret, f"2026-04-{12 + week * 7}T09:00:00-07:00",
                f"2026-04-{12 + week * 7}T11:00:00-07:00", 120, 0, "return", wk_of)
            insert_availability_check(
                conn, o_sid, "T-24h", "2026-04-09T20:00:00-07:00", 23.5,
                flight_found=True, price="$150", price_cents=15000, num_results=3)
            insert_availability_check(
                conn, o_sid, "T-23h", "2026-04-09T21:00:00-07:00", 23.0,
                flight_found=True, price="$150", price_cents=15000, num_results=3)
            insert_availability_check(
                conn, r_sid, "T-24h", "2026-04-11T09:00:00-07:00", 23.5,
                flight_found=True, price="$140", price_cents=14000, num_results=2)


# ── empty-data early returns ──────────────────────────────────────────────────

def test_availability_rate_empty(env_db, capsys):
    analysis.availability_rate_by_route()
    assert "No T-24h data yet" in capsys.readouterr().out


def test_change_report_empty(env_db, capsys):
    analysis.availability_change_t24_to_t23()
    assert "No paired" in capsys.readouterr().out


def test_route_detail_empty(env_db, capsys):
    analysis.route_detail("SFO", "LAS")
    assert "No data for SFO->LAS" in capsys.readouterr().out


def test_confidence_report_empty(env_db, capsys):
    analysis.confidence_report(min_weeks=2)
    assert "Not enough data" in capsys.readouterr().out


def test_safe_destinations_empty(env_db, capsys):
    analysis.safe_destinations()
    assert "No destinations meet" in capsys.readouterr().out


# ── populated paths ───────────────────────────────────────────────────────────

def test_availability_rate_populated(env_db, capsys):
    _seed_checks(env_db)
    analysis.availability_rate_by_route(min_samples=1)
    out = capsys.readouterr().out
    assert "Availability Rate" in out
    assert "SFO->LAS" in out


def test_change_report_populated(env_db, capsys):
    _seed_checks(env_db)
    analysis.availability_change_t24_to_t23()
    out = capsys.readouterr().out
    assert "T-24h vs T-23h" in out


def test_route_detail_populated(env_db, capsys):
    _seed_checks(env_db)
    analysis.route_detail("SFO", "LAS")
    out = capsys.readouterr().out
    assert "Check History" in out


def test_status_summary_runs(env_db, capsys):
    _seed_checks(env_db)
    analysis.status_summary()
    out = capsys.readouterr().out
    assert "GoWild Monitor Status" in out
    assert "Recent Checks" in out


def test_status_summary_no_checks(env_db, capsys):
    analysis.status_summary()
    out = capsys.readouterr().out
    assert "No checks recorded yet" in out


def test_confidence_report_populated(env_db, capsys):
    _seed_checks(env_db)
    analysis.confidence_report(min_weeks=2)
    out = capsys.readouterr().out
    assert "Route Confidence" in out


def test_safe_destinations_populated(env_db, capsys):
    _seed_checks(env_db)
    analysis.safe_destinations(min_pct=50.0, min_weeks=2)
    out = capsys.readouterr().out
    assert "Safe GoWild Destinations" in out
    assert "LAS" in out
