"""Dispatcher: find flights due for availability checks and execute them."""

import json
import os
import time
import uuid
from datetime import datetime

import pytz

from config import (
    PACIFIC_TZ, CHECK_WINDOWS, CHECK_TOLERANCE_MINUTES,
    RATE_LIMIT_SECONDS, LOCK_FILE,
)
from db import (
    db_session, get_flights_needing_check, check_already_done,
    insert_availability_check, log_entry,
)
from checker import check_flight_availability

PT = pytz.timezone(PACIFIC_TZ)


def classify_check_type(hours_until_dep: float) -> str:
    """Determine if this is a T-24h or T-23h check based on hours until departure."""
    best_type = None
    best_diff = float("inf")
    for check_type, target_hours in CHECK_WINDOWS.items():
        diff = abs(hours_until_dep - target_hours)
        if diff < best_diff:
            best_diff = diff
            best_type = check_type
    return best_type


def _acquire_lock():
    """Try to acquire file lock. Returns lock fd or None. Skipped in CI."""
    if os.environ.get("CI") or os.environ.get("GITHUB_ACTIONS"):
        return None  # No lock needed in CI — runs are isolated
    import fcntl
    try:
        fd = open(LOCK_FILE, "w")
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return fd
    except (IOError, OSError):
        fd.close()
        return False  # False = another run is active


def _release_lock(fd):
    if fd is None:
        return
    import fcntl
    fcntl.flock(fd, fcntl.LOCK_UN)
    fd.close()


def dispatch():
    """Main dispatcher: find flights needing checks and run them."""
    lock_fd = _acquire_lock()
    if lock_fd is False:
        print("Another dispatch is running. Exiting.")
        return

    run_id = str(uuid.uuid4())[:8]
    now_pt = datetime.now(PT)
    now_iso = now_pt.isoformat()
    checked = 0
    skipped = 0
    errors = 0

    try:
        with db_session() as conn:
            log_entry(conn, run_id, "INFO", f"Dispatch started at {now_iso}")

            flights = get_flights_needing_check(conn, now_iso, CHECK_TOLERANCE_MINUTES)

            if not flights:
                log_entry(conn, run_id, "INFO", "No flights due for check")
                print(f"[{run_id}] {now_iso} - No flights due for check.")
                return

            log_entry(conn, run_id, "INFO", f"Found {len(flights)} flights to check")
            print(f"[{run_id}] {now_iso} - {len(flights)} flights to check.")

            for row in flights:
                hours_until = row["hours_until_dep"]
                check_type = classify_check_type(hours_until)

                # Skip if already checked
                if check_already_done(conn, row["schedule_id"], check_type):
                    skipped += 1
                    continue

                origin = row["origin"]
                dest = row["destination"]
                dep_pt = row["departure_pt"]

                # Parse departure to get date and time for the search
                dep_dt = datetime.fromisoformat(dep_pt)
                date_str = dep_dt.strftime("%Y-%m-%d")
                target_hour = dep_dt.hour
                target_minute = dep_dt.minute

                print(f"  Checking {origin}->{dest} {dep_pt} ({check_type}, {hours_until:.1f}h before)...")
                log_entry(conn, run_id, "INFO",
                          f"Checking {origin}->{dest} dep={dep_pt} type={check_type} hours_before={hours_until:.1f}")

                result = check_flight_availability(
                    origin, dest, date_str,
                    target_dep_hour=target_hour,
                    target_dep_minute=target_minute,
                )

                check_id = insert_availability_check(
                    conn,
                    schedule_id=row["schedule_id"],
                    check_type=check_type,
                    checked_at=now_iso,
                    hours_before_dep=hours_until,
                    flight_found=result["flight_found"],
                    price=result["price"],
                    price_cents=result["price_cents"],
                    num_results=result["num_frontier_results"],
                    search_success=result["error"] is None,
                    error_message=result["error"],
                    search_duration_ms=result["duration_ms"],
                    raw_response=json.dumps(result["matched_flight"]) if result["matched_flight"] else None,
                )

                status = "FOUND" if result["flight_found"] else "NOT FOUND"
                price_info = f" @ {result['price']}" if result["price"] else ""
                print(f"    -> {status}{price_info} ({result['duration_ms']}ms)")

                if result["error"]:
                    errors += 1
                    log_entry(conn, run_id, "ERROR",
                              f"Check failed for {origin}->{dest}: {result['error']}")
                else:
                    checked += 1

                time.sleep(RATE_LIMIT_SECONDS)

            log_entry(conn, run_id, "INFO",
                      f"Dispatch complete: {checked} checked, {skipped} skipped, {errors} errors")
            print(f"[{run_id}] Done: {checked} checked, {skipped} skipped, {errors} errors.")

    finally:
        _release_lock(lock_fd)
