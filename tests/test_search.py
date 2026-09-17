"""Tests for search_flights error propagation and result parsing."""

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from gowild_search import search_flights


def _flight(name, departure="6:00 PM on Fri, Apr 10", arrival="8:30 PM on Fri, Apr 10",
            duration="2 hr 30 min", stops=0, price="$120"):
    return SimpleNamespace(
        name=name, departure=departure, arrival=arrival,
        duration=duration, stops=stops, price=price,
    )


@patch("gowild_search.get_flights")
def test_search_propagates_scraper_errors(mock_get_flights):
    """A scraper failure must propagate, not be swallowed into an empty list,
    so callers can distinguish 'no availability' from 'search failed'."""
    mock_get_flights.side_effect = Exception("rate limited")

    with pytest.raises(Exception, match="rate limited"):
        search_flights("SFO", "LAS", "2026-04-10")


@patch("gowild_search.get_flights")
def test_search_returns_empty_when_no_frontier(mock_get_flights):
    """A successful search with no Frontier results returns an empty list."""
    mock_get_flights.return_value = SimpleNamespace(
        flights=[_flight("United"), _flight("Delta")]
    )

    assert search_flights("SFO", "LAS", "2026-04-10") == []


@patch("gowild_search.get_flights")
def test_search_returns_frontier_flights(mock_get_flights):
    """Frontier flights are parsed and returned on a successful search."""
    mock_get_flights.return_value = SimpleNamespace(
        flights=[_flight("Frontier"), _flight("United")]
    )

    results = search_flights("SFO", "LAS", "2026-04-10")

    assert len(results) == 1
    assert results[0].origin == "SFO"
    assert results[0].destination == "LAS"
    assert results[0].price == "$120"
