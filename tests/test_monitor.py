"""Tests for the Typer CLI entry point."""

from unittest.mock import patch

from typer.testing import CliRunner

import monitor
from db import db_session

runner = CliRunner()


def test_init_seeds_routes(env_db, tmp_path, monkeypatch):
    # Keep LOG_DIR out of the repo.
    monkeypatch.setattr(monitor, "LOG_DIR", tmp_path / "logs")
    result = runner.invoke(monitor.app, ["init"])
    assert result.exit_code == 0, result.output
    assert "Database initialized" in result.output

    with db_session(env_db) as conn:
        count = conn.execute("SELECT COUNT(*) AS c FROM routes").fetchone()["c"]
    assert count > 0


def test_dispatch_command_delegates():
    with patch("scheduler.dispatch") as mock_dispatch:
        result = runner.invoke(monitor.app, ["dispatch"])
    assert result.exit_code == 0
    mock_dispatch.assert_called_once()


def test_seed_single_week_delegates():
    with patch("seeder.seed_week", return_value={
        "routes_checked": 5, "flights_found": 10, "flights_inserted": 8, "errors": 0,
    }) as mock_seed:
        result = runner.invoke(monitor.app, ["seed", "2026-04-06"])
    assert result.exit_code == 0
    mock_seed.assert_called_once()
    assert "Inserted: 8" in result.output


def test_seed_multi_week_delegates():
    with patch("seeder.seed_next_n_weeks", return_value={
        "weeks_seeded": 3, "total_inserted": 42,
    }) as mock_seed:
        result = runner.invoke(monitor.app, ["seed", "2026-04-06", "--weeks", "3"])
    assert result.exit_code == 0
    mock_seed.assert_called_once()
    assert "Seeded 3 weeks" in result.output


def test_check_command_reports_found():
    fake_result = {
        "error": None, "flight_found": True, "num_frontier_results": 2,
        "matched_flight": {"departure": "8:00 PM", "arrival": "10:00 PM",
                           "duration": "2 hr", "stops": 0, "price": "$120"},
        "duration_ms": 55,
    }
    with patch("checker.check_flight_availability", return_value=fake_result):
        result = runner.invoke(monitor.app, ["check", "SFO", "LAS", "2026-04-10"])
    assert result.exit_code == 0
    assert "FOUND" in result.output
    assert "Best match" in result.output


def test_check_command_reports_error():
    fake_result = {"error": "rate limited", "flight_found": False,
                   "num_frontier_results": 0, "matched_flight": None, "duration_ms": 5}
    with patch("checker.check_flight_availability", return_value=fake_result):
        result = runner.invoke(monitor.app, ["check", "SFO", "LAS", "2026-04-10"])
    assert result.exit_code == 0
    assert "Error: rate limited" in result.output


def test_enrich_command_delegates():
    with patch("enricher.enrich_schedules", return_value={
        "airports": 2, "http_errors": 0, "matched": 5, "propagated": 3,
    }) as mock_enrich:
        result = runner.invoke(monitor.app, ["enrich"])
    assert result.exit_code == 0
    mock_enrich.assert_called_once()
    assert "directly enriched: 5" in result.output


def test_report_command_delegates():
    with patch("analysis.availability_rate_by_route") as mock_report:
        result = runner.invoke(monitor.app, ["report", "--type", "T-23h"])
    assert result.exit_code == 0
    mock_report.assert_called_once_with("T-23h", 1)


def test_status_command_delegates():
    with patch("analysis.status_summary") as mock_status:
        result = runner.invoke(monitor.app, ["status"])
    assert result.exit_code == 0
    mock_status.assert_called_once()


def test_safe_command_delegates():
    with patch("analysis.safe_destinations") as mock_safe:
        result = runner.invoke(monitor.app, ["safe", "--min-pct", "80", "--min-weeks", "3"])
    assert result.exit_code == 0
    mock_safe.assert_called_once_with(80.0, 3)
