"""Dispatcher: find flights due for availability checks and execute them.

Uses a 'catch-up' model: each run checks ALL flights where the T-24h
booking window has opened but haven't been checked yet. Flights are
ordered by freshness (closest to T-24h first) and batched by
(origin, destination, date) so one Google Flights search covers
multiple flights on the same route.
"""

import json
import os
import time
import uuid
from datetime import datetime
from itertools import groupby
from operator import itemgetter

import pytz

from config import (
    PACIFIC_TZ, MAX_STALENESS_HOURS,
    RATE_LIMIT_SECONDS, LOCK_FILE, DEFAULT_MAX_STOPS,
    INTERNATIONAL_DESTS, DOMESTIC_BOOKING_HOURS, INTERNATIONAL_BOOKING_HOURS,
)
from db import (
    db_session, get_unchecked_flights_past_t24h,
    insert_availability_check, log_entry,
)
from checker import batch_check_flights

PT = pytz.timezone(PACIFIC_TZ)

# Cap flights per dispatch run to avoid GitHub Actions timeouts
MAX_FLIGHTS_PER_RUN = 200


def _acquire_lock():
    """Try to acquire file lock. Returns lock fd or None. Skipped in CI."""
    if os.environ.get("CI") or os.environ.get("GITHUB_ACTIONS"):
        return None
    import fcntl
    try:
        fd = open(LOCK_FILE, "w")
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return fd
    except (IOError, OSError):
        fd.close()
        return False


def _release_lock(fd):
    if fd is None:
        return
    import fcntl
    fcntl.flock(fd, fcntl.LOCK_UN)
    fd.close()


def dispatch():
    """Main dispatcher: find all flights past T-24h and check them."""
    lock_fd = _acquire_lock()
    if lock_fd is False:
        print("Another dispatch is running. Exiting.")
        return

    run_id = str(uuid.uuid4())[:8]
    now_pt = datetime.now(PT)
    now_iso = now_pt.isoformat()
    checked = 0
    errors = 0

    try:
        with db_session() as conn:
            log_entry(conn, run_id, "INFO", f"Dispatch started at {now_iso}")

            flights = get_unchecked_flights_past_t24h(
                conn, now_iso, MAX_STALENESS_HOURS,
                international_dests=INTERNATIONAL_DESTS,
                domestic_booking_hours=DOMESTIC_BOOKING_HOURS,
                international_booking_hours=INTERNATIONAL_BOOKING_HOURS,
            )

            if not flights:
                log_entry(conn, run_id, "INFO", "No flights due for check")
                print(f"[{run_id}] {now_iso} - No flights due for check.")
                return

            # Cap the number of flights per run
            flights = flights[:MAX_FLIGHTS_PER_RUN]

            log_entry(conn, run_id, "INFO", f"Found {len(flights)} flights to check")
            print(f"[{run_id}] {now_iso} - {len(flights)} flights to check.")

            # Group by (origin, destination, date) for batch checking
            def group_key(row):
                dep_dt = datetime.fromisoformat(row["departure_pt"])
                return (row["origin"], row["destination"], dep_dt.strftime("%Y-%m-%d"))

            # Sort for groupby (flights are already sorted by freshness,
            # but groupby needs adjacent keys)
            flight_list = list(flights)
            flight_list.sort(key=group_key)

            for (origin, dest, date_str), group in groupby(flight_list, key=group_key):
                group_flights = list(group)
                freshness = 24.0 - group_flights[0]["hours_until_dep"]
                print(f"  Batch: {origin}->{dest} {date_str} ({len(group_flights)} flights, "
                      f"{freshness:.1f}h after T-24h)...")

                log_entry(conn, run_id, "INFO",
                          f"Batch {origin}->{dest} {date_str}: {len(group_flights)} flights")

                flight_dicts = [
                    {"schedule_id": f["schedule_id"], "departure_pt": f["departure_pt"]}
                    for f in group_flights
                ]

                results = batch_check_flights(
                    origin, dest, date_str, flight_dicts,
                    max_stops=DEFAULT_MAX_STOPS,
                )

                for flight, result in zip(group_flights, results):
                    insert_availability_check(
                        conn,
                        schedule_id=flight["schedule_id"],
                        check_type="T-24h",
                        checked_at=now_iso,
                        hours_before_dep=flight["hours_until_dep"],
                        flight_found=result["flight_found"],
                        price=result["price"],
                        price_cents=result["price_cents"],
                        num_results=result["num_frontier_results"],
                        search_success=result["error"] is None,
                        error_message=result["error"],
                        search_duration_ms=result["duration_ms"],
                        raw_response=(json.dumps(result["matched_flight"])
                                      if result["matched_flight"] else None),
                    )

                    status = "AVAILABLE" if result["flight_found"] else "FULL"
                    dep_time = flight["departure_pt"][11:16]
                    price_info = f" ${result['price']}" if result.get("price") else ""
                    print(f"    {dep_time} -> {status}{price_info}")

                    if result["error"]:
                        errors += 1
                        log_entry(conn, run_id, "ERROR",
                                  f"Check failed for {origin}->{dest} {dep_time}: {result['error']}")
                    else:
                        checked += 1

                time.sleep(RATE_LIMIT_SECONDS)

            log_entry(conn, run_id, "INFO",
                      f"Dispatch complete: {checked} checked, {errors} errors")
            print(f"[{run_id}] Done: {checked} checked, {errors} errors.")

    finally:
        _release_lock(lock_fd)
