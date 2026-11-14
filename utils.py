"""Small cross-cutting helpers shared across the GoWild monitor modules."""

from datetime import datetime, timedelta

MINUTES_PER_DAY = 1440


def circular_minute_diff(a_min: int, b_min: int) -> int:
    """Distance in minutes between two times-of-day, wrapping around midnight.

    Both inputs are minutes-since-midnight (0..1439). A flight at 23:50 and one
    at 00:10 are 20 minutes apart, not 1420.
    """
    raw = abs(a_min - b_min)
    return min(raw, MINUTES_PER_DAY - raw)


def next_weekday(now: datetime, target_weekday: int, evening_cutoff_hour: int = 18) -> datetime:
    """Return the datetime of the next occurrence of ``target_weekday``.

    ``target_weekday`` follows ``datetime.weekday()`` (Mon=0 .. Sun=6). If today
    already is that weekday and it is past ``evening_cutoff_hour``, the following
    week's occurrence is returned instead of today's.
    """
    days_ahead = (target_weekday - now.weekday()) % 7
    if days_ahead == 0 and now.hour >= evening_cutoff_hour:
        days_ahead = 7
    return now + timedelta(days=days_ahead)


def rate_style(pct: float) -> str:
    """Rich color style for an availability percentage: green/yellow/red."""
    if pct >= 75:
        return "green"
    if pct >= 50:
        return "yellow"
    return "red"


def route_label(origin: str, destination: str) -> str:
    """Human-readable ``ORIGIN->DEST`` label used in reports."""
    return f"{origin}->{destination}"


def price_str(dollars) -> str:
    """Format a dollar amount as ``$123``, or ``-`` when missing/zero."""
    return f"${int(dollars)}" if dollars else "-"
