"""Tests for the Google Flights scraper wrapper and its parsing helpers."""

from datetime import datetime
from unittest.mock import MagicMock, patch

import pytest

from gowild_search import (
    FlightOption,
    is_valid_outbound,
    is_valid_return,
    parse_duration_minutes,
    parse_flight_time,
    search_flights,
)


# ── parse_flight_time ─────────────────────────────────────────────────────────

def test_parse_flight_time_pm():
    dt = parse_flight_time("9:00 PM on Fri, Apr 10")
    assert dt == datetime(2026, 4, 10, 21, 0)


def test_parse_flight_time_am_midnight():
    """12:xx AM should map to hour 0."""
    dt = parse_flight_time("12:47 AM on Sat, Apr 11")
    assert dt == datetime(2026, 4, 11, 0, 47)


def test_parse_flight_time_noon():
    """12:xx PM should stay at hour 12."""
    dt = parse_flight_time("12:30 PM on Sun, Jul 5")
    assert dt == datetime(2026, 7, 5, 12, 30)


def test_parse_flight_time_respects_reference_year():
    dt = parse_flight_time("6:15 AM on Mon, Dec 1", reference_year=2030)
    assert dt.year == 2030 and dt.month == 12 and dt.day == 1


def test_parse_flight_time_unparseable_returns_none():
    assert parse_flight_time("sometime tomorrow") is None


def test_parse_flight_time_invalid_date_returns_none():
    """A syntactically valid but calendrically impossible date returns None."""
    assert parse_flight_time("9:00 AM on Wed, Feb 30") is None


# ── parse_duration_minutes ──────────────────────────────────────────────────

@pytest.mark.parametrize(
    "text,expected",
    [
        ("2 hr 47 min", 167),
        ("5 hr", 300),
        ("45 min", 45),
        ("0 hr 0 min", 0),
        ("no numbers here", 0),
    ],
)
def test_parse_duration_minutes(text, expected):
    assert parse_duration_minutes(text) == expected


# ── is_valid_outbound ─────────────────────────────────────────────────────────

def _opt(dep_dt=None, arr_dt=None):
    return FlightOption(
        origin="SFO", destination="LAS", departure="", arrival="",
        duration="", stops=0, price="$100", dep_dt=dep_dt, arr_dt=arr_dt,
    )


def test_outbound_friday_evening_valid():
    fri = datetime(2026, 4, 10)
    assert is_valid_outbound(_opt(dep_dt=datetime(2026, 4, 10, 18, 0)), fri) is True


def test_outbound_friday_afternoon_invalid():
    fri = datetime(2026, 4, 10)
    assert is_valid_outbound(_opt(dep_dt=datetime(2026, 4, 10, 17, 59)), fri) is False


def test_outbound_saturday_morning_valid():
    fri = datetime(2026, 4, 10)
    assert is_valid_outbound(_opt(dep_dt=datetime(2026, 4, 11, 10, 59)), fri) is True


def test_outbound_saturday_late_invalid():
    fri = datetime(2026, 4, 10)
    assert is_valid_outbound(_opt(dep_dt=datetime(2026, 4, 11, 11, 0)), fri) is False


def test_outbound_no_dep_dt_invalid():
    assert is_valid_outbound(_opt(dep_dt=None), datetime(2026, 4, 10)) is False


# ── is_valid_return ───────────────────────────────────────────────────────────

def test_return_before_monday_valid():
    mon = datetime(2026, 4, 13)
    assert is_valid_return(_opt(arr_dt=datetime(2026, 4, 12, 23, 0)), mon) is True


def test_return_monday_early_valid():
    mon = datetime(2026, 4, 13)
    assert is_valid_return(_opt(arr_dt=datetime(2026, 4, 13, 9, 59)), mon) is True


def test_return_monday_late_invalid():
    mon = datetime(2026, 4, 13)
    assert is_valid_return(_opt(arr_dt=datetime(2026, 4, 13, 10, 0)), mon) is False


def test_return_no_arr_dt_invalid():
    assert is_valid_return(_opt(arr_dt=None), datetime(2026, 4, 13)) is False


# ── search_flights ────────────────────────────────────────────────────────────

def _raw_flight(name="Frontier", departure="9:00 PM on Fri, Apr 10",
                arrival="11:30 PM on Fri, Apr 10", duration="2 hr 30 min",
                stops=0, price="$120"):
    m = MagicMock()
    m.name = name
    m.departure = departure
    m.arrival = arrival
    m.duration = duration
    m.stops = stops
    m.price = price
    return m


@patch("gowild_search.get_flights")
def test_search_flights_filters_non_frontier(mock_get_flights):
    result = MagicMock()
    result.flights = [
        _raw_flight(name="Frontier"),
        _raw_flight(name="United Airlines", departure="8:00 AM on Sat, Apr 11"),
    ]
    mock_get_flights.return_value = result

    flights = search_flights("SFO", "LAS", "2026-04-10")

    assert len(flights) == 1
    assert flights[0].origin == "SFO"
    assert flights[0].destination == "LAS"
    assert flights[0].dep_dt == datetime(2026, 4, 10, 21, 0)


@patch("gowild_search.get_flights")
def test_search_flights_dedupes_identical_rows(mock_get_flights):
    result = MagicMock()
    dup = _raw_flight()
    result.flights = [dup, _raw_flight(), _raw_flight(departure="6:00 AM on Sat, Apr 11")]
    mock_get_flights.return_value = result

    flights = search_flights("SFO", "LAS", "2026-04-10")

    # Two of three rows are identical → deduped to a single option, plus the distinct one.
    assert len(flights) == 2


@patch("gowild_search.get_flights")
def test_search_flights_passes_max_stops(mock_get_flights):
    result = MagicMock()
    result.flights = []
    mock_get_flights.return_value = result

    search_flights("SFO", "LAS", "2026-04-10", max_stops=1)

    assert mock_get_flights.call_args.kwargs["max_stops"] == 1


@patch("gowild_search.get_flights")
def test_search_flights_swallows_exceptions(mock_get_flights):
    mock_get_flights.side_effect = RuntimeError("network down")
    assert search_flights("SFO", "LAS", "2026-04-10") == []
