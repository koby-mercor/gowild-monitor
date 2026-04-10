"""Tests for the catch-up query logic."""

from db import db_session, get_unchecked_flights_past_t24h


def test_catchup_query_returns_correct_flights(seeded_db):
    """Catch-up query should return only flights where T-24h has passed,
    not already checked, and not too stale."""
    now_iso = "2026-04-11T00:00:00-07:00"

    with db_session(seeded_db) as conn:
        flights = get_unchecked_flights_past_t24h(conn, now_iso, max_staleness_hours=6.0)

    schedule_ids = [f["schedule_id"] for f in flights]

    # Should include: LAS (22h away, 2h stale) and DEN (23.5h away, 0.5h stale)
    assert 1 in schedule_ids, "SFO->LAS should be included (T-24h passed 2h ago)"
    assert 2 in schedule_ids, "SFO->DEN should be included (T-24h passed 0.5h ago)"

    # Should NOT include:
    assert 3 not in schedule_ids, "SFO->ATL should be excluded (T-24h not yet passed)"
    assert 4 not in schedule_ids, "SFO->PHX should be excluded (too stale, 9h after T-24h)"
    assert 5 not in schedule_ids, "SFO->LAX should be excluded (already checked)"


def test_catchup_query_orders_by_freshness(seeded_db):
    """Freshest flights (closest to T-24h) should come first."""
    now_iso = "2026-04-11T00:00:00-07:00"

    with db_session(seeded_db) as conn:
        flights = get_unchecked_flights_past_t24h(conn, now_iso, max_staleness_hours=6.0)

    schedule_ids = [f["schedule_id"] for f in flights]
    # DEN (23.5h = freshest, T-24h was 0.5h ago) should come before LAS (22h = 2h stale)
    den_idx = schedule_ids.index(2)
    las_idx = schedule_ids.index(1)
    assert den_idx < las_idx, "DEN (fresher) should come before LAS (staler)"


def test_catchup_query_includes_return_flights(test_db):
    """Catch-up query should check both outbound AND return flights."""
    from db import get_or_create_route, insert_flight_schedule

    with db_session(test_db) as conn:
        r = get_or_create_route(conn, "LAS", "SFO", 1)
        insert_flight_schedule(conn, r, "2026-04-11T23:00:00-07:00", "2026-04-12T01:00:00-07:00",
                               120, 0, "return", "2026-04-10")

    now_iso = "2026-04-11T00:00:00-07:00"
    with db_session(test_db) as conn:
        flights = get_unchecked_flights_past_t24h(conn, now_iso, max_staleness_hours=6.0)

    assert len(flights) == 1
    assert flights[0]["origin"] == "LAS"
    assert flights[0]["destination"] == "SFO"
