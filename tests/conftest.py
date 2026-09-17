"""Shared fixtures for GoWild tests."""

import pytest
from db import init_db, db_session, get_or_create_route, insert_flight_schedule


@pytest.fixture
def test_db(tmp_path):
    """Create a temporary test database with schema initialized."""
    db_path = str(tmp_path / "test.db")
    init_db(db_path)
    return db_path


@pytest.fixture
def env_db(test_db, monkeypatch):
    """Initialized temp DB wired up as the default via GOWILD_DB_PATH.

    Modules that call ``db_session()`` / ``get_connection()`` with no explicit
    path resolve the DB from the ``GOWILD_DB_PATH`` env var, so setting it lets
    us exercise their DB-backed code paths against a throwaway database.
    """
    monkeypatch.setenv("GOWILD_DB_PATH", test_db)
    return test_db


@pytest.fixture
def seeded_db(test_db):
    """Test DB populated with sample flights for catch-up query testing.

    Creates flights at various hours before 'now' to test the catch-up window.
    Reference time: 2026-04-11T00:00:00-07:00 (Fri midnight PT)

    Flights:
      schedule_id 1: SFO->LAS dep 2026-04-11T22:00 PT (22h away — T-24h passed 2h ago)  ← should match
      schedule_id 2: SFO->DEN dep 2026-04-11T23:30 PT (23.5h away — T-24h passed 0.5h ago) ← should match (freshest)
      schedule_id 3: SFO->ATL dep 2026-04-12T06:00 PT (30h away — T-24h not yet passed)  ← should NOT match
      schedule_id 4: SFO->PHX dep 2026-04-11T15:00 PT (15h away — T-24h passed 9h ago)   ← too stale (>6h)
      schedule_id 5: SFO->LAX dep 2026-04-11T23:00 PT (23h away — already checked)       ← should NOT match
    """
    with db_session(test_db) as conn:
        r1 = get_or_create_route(conn, "SFO", "LAS", 1)
        r2 = get_or_create_route(conn, "SFO", "DEN", 1)
        r3 = get_or_create_route(conn, "SFO", "ATL", 0)
        r4 = get_or_create_route(conn, "SFO", "PHX", 1)
        r5 = get_or_create_route(conn, "SFO", "LAX", 1)

        insert_flight_schedule(conn, r1, "2026-04-11T22:00:00-07:00", "2026-04-12T00:30:00-07:00",
                               150, 0, "outbound", "2026-04-06")
        insert_flight_schedule(conn, r2, "2026-04-11T23:30:00-07:00", "2026-04-12T03:00:00-07:00",
                               210, 1, "outbound", "2026-04-06")
        insert_flight_schedule(conn, r3, "2026-04-12T06:00:00-07:00", "2026-04-12T14:00:00-07:00",
                               300, 1, "outbound", "2026-04-06")
        insert_flight_schedule(conn, r4, "2026-04-11T15:00:00-07:00", "2026-04-11T17:00:00-07:00",
                               120, 0, "outbound", "2026-04-06")
        insert_flight_schedule(conn, r5, "2026-04-11T23:00:00-07:00", "2026-04-12T00:30:00-07:00",
                               90, 0, "outbound", "2026-04-06")

        # Mark flight 5 (SFO->LAX) as already checked
        conn.execute(
            """INSERT INTO availability_checks
               (schedule_id, check_type, checked_at, hours_before_dep, flight_found,
                search_success)
               VALUES (5, 'T-24h', '2026-04-11T00:00:00-07:00', 23.0, 1, 1)"""
        )

    return test_db
