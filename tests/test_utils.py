"""Tests for the shared helper utilities."""

from datetime import datetime

from utils import circular_minute_diff, next_weekday, price_str, rate_style, route_label


def test_circular_minute_diff_wraps_midnight():
    assert circular_minute_diff(23 * 60 + 50, 10) == 20  # 23:50 vs 00:10
    assert circular_minute_diff(10, 23 * 60 + 50) == 20  # symmetric
    assert circular_minute_diff(600, 660) == 60          # no wrap needed
    assert circular_minute_diff(120, 120) == 0


def test_next_weekday_same_day_before_cutoff():
    # Wednesday (weekday 2) 09:00 → next Monday (0) is 5 days away
    now = datetime(2026, 4, 22, 9, 0)  # Wed
    assert next_weekday(now, 0).date() == datetime(2026, 4, 27).date()


def test_next_weekday_today_before_and_after_cutoff():
    monday_morning = datetime(2026, 4, 20, 9, 0)  # Mon 09:00
    assert next_weekday(monday_morning, 0).date() == monday_morning.date()

    monday_evening = datetime(2026, 4, 20, 20, 0)  # Mon 20:00, past cutoff
    assert next_weekday(monday_evening, 0).date() == datetime(2026, 4, 27).date()


def test_rate_style_thresholds():
    assert rate_style(90) == "green"
    assert rate_style(75) == "green"
    assert rate_style(60) == "yellow"
    assert rate_style(50) == "yellow"
    assert rate_style(10) == "red"


def test_route_label():
    assert route_label("SFO", "DEN") == "SFO->DEN"


def test_price_str():
    assert price_str(123) == "$123"
    assert price_str(123.9) == "$123"
    assert price_str(0) == "-"
    assert price_str(None) == "-"
