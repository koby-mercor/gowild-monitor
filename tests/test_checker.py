"""Tests for batch flight checking logic."""

from unittest.mock import patch, MagicMock
from checker import batch_check_flights


def _make_flight_option(dep_hour, dep_minute, price, stops=0):
    """Create a mock FlightOption for testing."""
    mock = MagicMock()
    mock.dep_dt = MagicMock()
    mock.dep_dt.hour = dep_hour
    mock.dep_dt.minute = dep_minute
    mock.price = price
    mock.stops = stops
    mock.origin = "SFO"
    mock.destination = "LAS"
    mock.departure = f"{dep_hour}:{dep_minute:02d} PM"
    mock.arrival = "11:00 PM"
    mock.duration = "2 hr 30 min"
    return mock


@patch("checker.search_flights")
def test_batch_matches_multiple_flights(mock_search):
    """A single search should match results to multiple seeded flights."""
    mock_search.return_value = [
        _make_flight_option(18, 44, "$160"),
        _make_flight_option(20, 14, "$185"),
        _make_flight_option(22, 50, "$142"),
    ]

    flights_to_check = [
        {"schedule_id": 1, "departure_pt": "2026-04-10T18:44:00-07:00"},
        {"schedule_id": 2, "departure_pt": "2026-04-10T20:14:00-07:00"},
        {"schedule_id": 3, "departure_pt": "2026-04-10T21:00:00-07:00"},  # no match
    ]

    results = batch_check_flights("SFO", "LAS", "2026-04-10", flights_to_check)

    assert len(results) == 3
    assert results[0]["flight_found"] is True
    assert results[0]["price"] == "$160"
    assert results[1]["flight_found"] is True
    assert results[1]["price"] == "$185"
    assert results[2]["flight_found"] is False  # 21:00 not in search results


@patch("checker.search_flights")
def test_batch_handles_search_failure(mock_search):
    """If the search raises an exception, all flights get error results."""
    mock_search.side_effect = Exception("rate limited")

    flights_to_check = [
        {"schedule_id": 1, "departure_pt": "2026-04-10T18:44:00-07:00"},
    ]

    results = batch_check_flights("SFO", "LAS", "2026-04-10", flights_to_check)

    assert len(results) == 1
    assert results[0]["flight_found"] is False
    assert results[0]["error"] is not None
    assert "rate limited" in results[0]["error"]


@patch("checker.search_flights")
def test_batch_returns_all_frontier_count(mock_search):
    """Each result should include the total Frontier flight count from the search."""
    mock_search.return_value = [
        _make_flight_option(18, 44, "$160"),
        _make_flight_option(20, 14, "$185"),
        _make_flight_option(22, 0, "$142"),
    ]

    flights_to_check = [
        {"schedule_id": 1, "departure_pt": "2026-04-10T18:44:00-07:00"},
    ]

    results = batch_check_flights("SFO", "LAS", "2026-04-10", flights_to_check)

    assert results[0]["num_frontier_results"] == 3
