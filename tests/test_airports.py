"""Structural sanity checks for the airport coordinate table."""

from airports import AIRPORTS


def test_home_airports_present():
    for code in ("SFO", "SJC"):
        assert code in AIRPORTS


def test_entries_are_lat_lng_city_triples():
    for code, value in AIRPORTS.items():
        assert isinstance(code, str) and len(code) == 3, f"bad code {code!r}"
        assert len(value) == 3, f"{code} should be (lat, lng, city)"
        lat, lng, city = value
        assert -90 <= lat <= 90, f"{code} latitude out of range: {lat}"
        assert -180 <= lng <= 180, f"{code} longitude out of range: {lng}"
        assert isinstance(city, str) and city, f"{code} missing city name"


def test_codes_are_unique_and_uppercase():
    assert all(code == code.upper() for code in AIRPORTS)
