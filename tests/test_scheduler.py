"""Tests for the catch-up dispatcher."""

from unittest.mock import patch

import scheduler
from scheduler import dispatch, _acquire_lock, _release_lock
from db import db_session


def _batch_result(schedule_id, found=True, price="$150", error=None):
    return {
        "schedule_id": schedule_id,
        "flight_found": found,
        "price": price if found else None,
        "price_cents": 15000 if found else None,
        "num_frontier_results": 3,
        "matched_flight": {"origin": "SFO"} if found else None,
        "error": error,
        "duration_ms": 12,
    }


def test_acquire_lock_skipped_in_ci(monkeypatch):
    monkeypatch.setenv("CI", "1")
    assert _acquire_lock() is None
    _release_lock(None)  # no-op, should not raise


def test_dispatch_no_flights_due(env_db, monkeypatch):
    monkeypatch.setenv("CI", "1")  # skip file lock
    with patch("scheduler.batch_check_flights") as mock_check:
        dispatch()
        mock_check.assert_not_called()


def test_dispatch_checks_and_records(seeded_db, monkeypatch):
    monkeypatch.setenv("CI", "1")
    monkeypatch.setenv("GOWILD_DB_PATH", seeded_db)
    monkeypatch.setattr(scheduler, "RATE_LIMIT_SECONDS", 0)

    # Freeze "now" to the reference time used by the seeded_db fixture.
    ref = "2026-04-11T00:00:00-07:00"

    class _FrozenDatetime(scheduler.datetime):
        @classmethod
        def now(cls, tz=None):
            return cls.fromisoformat(ref)

    monkeypatch.setattr(scheduler, "datetime", _FrozenDatetime)

    def fake_batch(origin, dest, date_str, flight_dicts, max_stops=None):
        return [_batch_result(f["schedule_id"]) for f in flight_dicts]

    with patch("scheduler.batch_check_flights", side_effect=fake_batch) as mock_check:
        dispatch()
        assert mock_check.called

    # The two due flights (LAS, DEN) should now have T-24h availability_checks.
    with db_session(seeded_db) as conn:
        rows = conn.execute(
            "SELECT schedule_id FROM availability_checks WHERE check_type='T-24h' "
            "AND checked_at = ?",
            (ref,),
        ).fetchall()
    checked_ids = {r["schedule_id"] for r in rows}
    assert {1, 2} <= checked_ids


def test_dispatch_logs_errors_from_checker(seeded_db, monkeypatch):
    monkeypatch.setenv("CI", "1")
    monkeypatch.setenv("GOWILD_DB_PATH", seeded_db)
    monkeypatch.setattr(scheduler, "RATE_LIMIT_SECONDS", 0)

    ref = "2026-04-11T00:00:00-07:00"

    class _FrozenDatetime(scheduler.datetime):
        @classmethod
        def now(cls, tz=None):
            return cls.fromisoformat(ref)

    monkeypatch.setattr(scheduler, "datetime", _FrozenDatetime)

    def fake_batch(origin, dest, date_str, flight_dicts, max_stops=None):
        return [_batch_result(f["schedule_id"], found=False, error="boom")
                for f in flight_dicts]

    with patch("scheduler.batch_check_flights", side_effect=fake_batch):
        dispatch()

    with db_session(seeded_db) as conn:
        err = conn.execute(
            "SELECT COUNT(*) AS c FROM check_log WHERE level='ERROR'"
        ).fetchone()["c"]
        failed = conn.execute(
            "SELECT COUNT(*) AS c FROM availability_checks WHERE search_success=0"
        ).fetchone()["c"]
    assert err >= 1
    assert failed >= 1
