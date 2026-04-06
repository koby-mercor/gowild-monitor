"""Seed flight schedules by searching Google Flights for upcoming weekends."""

import time
import sys
from datetime import datetime, timedelta

import pytz

from config import (
    MONITORED_ROUTES, PACIFIC_TZ, RATE_LIMIT_SECONDS,
    OUTBOUND_EARLIEST_HOUR, OUTBOUND_LATEST_HOUR,
)
from db import db_session, get_or_create_route, insert_flight_schedule
from gowild_search import search_flights, parse_flight_time, parse_duration_minutes

PT = pytz.timezone(PACIFIC_TZ)


def seed_weekend(friday_date: str, max_stops: int = None) -> dict:
    """Seed flight schedules for a specific weekend (outbound only).

    friday_date: 'YYYY-MM-DD' format, must be a Friday.
    Returns: {routes_checked, flights_found, flights_inserted, errors}
    """
    fri_dt = datetime.strptime(friday_date, "%Y-%m-%d")
    sat_dt = fri_dt + timedelta(days=1)
    fri_str = fri_dt.strftime("%Y-%m-%d")
    sat_str = sat_dt.strftime("%Y-%m-%d")

    stats = {"routes_checked": 0, "flights_found": 0, "flights_inserted": 0, "errors": 0}

    # Build route pairs
    route_pairs = []
    for origin, dests in MONITORED_ROUTES.items():
        for dest in dests:
            route_pairs.append((origin, dest))

    total = len(route_pairs) * 2  # Friday + Saturday
    search_num = 0

    with db_session() as conn:
        for origin, dest in route_pairs:
            # Determine if nonstop (in the nonstop list from gowild_search)
            from gowild_search import FRONTIER_NONSTOP
            is_nonstop = 1 if dest in FRONTIER_NONSTOP.get(origin, []) else 0
            route_id = get_or_create_route(conn, origin, dest, is_nonstop)

            for date_str in [fri_str, sat_str]:
                search_num += 1
                day = "Fri" if date_str == fri_str else "Sat"
                sys.stdout.write(f"\r  [{search_num}/{total}] {origin}->{dest} {day}...          ")
                sys.stdout.flush()
                stats["routes_checked"] += 1

                try:
                    flights = search_flights(origin, dest, date_str, max_stops=max_stops)
                except Exception as e:
                    stats["errors"] += 1
                    continue

                for f in flights:
                    if not f.dep_dt:
                        continue

                    # Only keep flights in the outbound window
                    is_friday = date_str == fri_str
                    is_saturday = date_str == sat_str
                    valid = False
                    if is_friday and f.dep_dt.hour >= OUTBOUND_EARLIEST_HOUR:
                        valid = True
                    if is_saturday and f.dep_dt.hour < OUTBOUND_LATEST_HOUR:
                        valid = True
                    if not valid:
                        continue

                    stats["flights_found"] += 1

                    # Convert to Pacific-aware ISO string
                    dep_naive = f.dep_dt
                    dep_pt = PT.localize(dep_naive)
                    dep_iso = dep_pt.isoformat()

                    arr_iso = ""
                    if f.arr_dt:
                        arr_pt = PT.localize(f.arr_dt)
                        arr_iso = arr_pt.isoformat()

                    dur_min = parse_duration_minutes(f.duration) if f.duration else None

                    sid = insert_flight_schedule(
                        conn, route_id, dep_iso, arr_iso, dur_min,
                        f.stops, "outbound", friday_date,
                        f.departure, f.arrival,
                    )
                    if sid:
                        stats["flights_inserted"] += 1

                time.sleep(RATE_LIMIT_SECONDS)

    print(f"\r  Done: {stats['flights_found']} flights found, {stats['flights_inserted']} new schedules inserted.          ")
    return stats


def seed_next_n_weekends(n: int = 4, max_stops: int = None) -> dict:
    """Seed schedules for the next N weekends."""
    today = datetime.now()
    days_to_friday = (4 - today.weekday()) % 7
    if days_to_friday == 0 and today.hour >= 18:
        days_to_friday = 7
    next_friday = today + timedelta(days=days_to_friday)

    total_stats = {"weekends_seeded": 0, "total_inserted": 0}
    for i in range(n):
        friday = next_friday + timedelta(weeks=i)
        friday_str = friday.strftime("%Y-%m-%d")
        print(f"\nSeeding weekend of {friday_str}...")
        stats = seed_weekend(friday_str, max_stops=max_stops)
        total_stats["weekends_seeded"] += 1
        total_stats["total_inserted"] += stats["flights_inserted"]

    return total_stats
