"""Seed flight schedules by searching Google Flights for upcoming weekends."""

import sys
import time
from datetime import datetime, timedelta

import pytz

from config import MONITORED_ROUTES, PACIFIC_TZ, RATE_LIMIT_SECONDS, DEFAULT_MAX_STOPS
from db import db_session, get_or_create_route, insert_flight_schedule
from gowild_search import search_flights, FRONTIER_NONSTOP, parse_flight_time, parse_duration_minutes

PT = pytz.timezone(PACIFIC_TZ)


def _is_nonstop_route(origin: str, dest: str) -> bool:
    return dest in FRONTIER_NONSTOP.get(origin, [])


def seed_weekend(friday_date: str, max_stops: int = DEFAULT_MAX_STOPS) -> dict:
    """Seed flight schedules for a weekend: all destinations, both directions, Fri-Mon.

    friday_date: 'YYYY-MM-DD' format, must be a Friday.
    Returns: {routes_checked, flights_found, flights_inserted, errors}
    """
    if max_stops is None:
        max_stops = DEFAULT_MAX_STOPS

    fri_dt = datetime.strptime(friday_date, "%Y-%m-%d")
    dates = [
        (fri_dt + timedelta(days=i)).strftime("%Y-%m-%d")
        for i in range(7)  # Friday through Thursday (full week)
    ]

    stats = {"routes_checked": 0, "flights_found": 0, "flights_inserted": 0, "errors": 0}

    # Build search list: (search_origin, search_dest, db_direction)
    searches = []
    for home_airport, destinations in MONITORED_ROUTES.items():
        for dest in destinations:
            searches.append((home_airport, dest, "outbound"))
            searches.append((dest, home_airport, "return"))

    total = len(searches) * len(dates)
    search_num = 0

    with db_session() as conn:
        for search_origin, search_dest, direction in searches:
            is_nonstop = 1 if _is_nonstop_route(search_origin, search_dest) else 0
            route_id = get_or_create_route(conn, search_origin, search_dest, is_nonstop)

            for date_str in dates:
                search_num += 1
                sys.stdout.write(
                    f"\r  [{search_num}/{total}] {search_origin}->{search_dest} {date_str} ({direction})...          "
                )
                sys.stdout.flush()
                stats["routes_checked"] += 1

                try:
                    flights = search_flights(search_origin, search_dest, date_str, max_stops=max_stops)
                except Exception as e:
                    stats["errors"] += 1
                    continue

                for f in flights:
                    if not f.dep_dt:
                        continue

                    stats["flights_found"] += 1

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
                        f.stops, direction, friday_date,
                        f.departure, f.arrival,
                    )
                    if sid:
                        stats["flights_inserted"] += 1

                time.sleep(RATE_LIMIT_SECONDS)

    print(f"\r  Done: {stats['flights_found']} flights found, {stats['flights_inserted']} new schedules inserted.          ")
    return stats


def seed_next_n_weekends(n: int = 4, max_stops: int = DEFAULT_MAX_STOPS) -> dict:
    """Seed schedules for the next N weekends."""
    if max_stops is None:
        max_stops = DEFAULT_MAX_STOPS

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
