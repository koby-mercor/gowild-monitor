"""Database layer for GoWild availability monitor."""

import os
import sqlite3
from contextlib import contextmanager
from typing import List, Optional

from config import DB_PATH


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS routes (
    route_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    origin      TEXT NOT NULL,
    destination TEXT NOT NULL,
    is_nonstop  INTEGER NOT NULL DEFAULT 1,
    active      INTEGER NOT NULL DEFAULT 1,
    created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S','now')),
    UNIQUE(origin, destination)
);

CREATE TABLE IF NOT EXISTS flight_schedules (
    schedule_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    route_id        INTEGER NOT NULL REFERENCES routes(route_id),
    departure_pt    TEXT NOT NULL,
    arrival_pt      TEXT NOT NULL,
    duration_min    INTEGER,
    stops           INTEGER NOT NULL DEFAULT 0,
    direction       TEXT NOT NULL CHECK(direction IN ('outbound', 'return')),
    week_of         TEXT NOT NULL,
    raw_departure   TEXT,
    raw_arrival     TEXT,
    discovered_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S','now')),
    UNIQUE(route_id, departure_pt, direction)
);

CREATE TABLE IF NOT EXISTS availability_checks (
    check_id         INTEGER PRIMARY KEY AUTOINCREMENT,
    schedule_id      INTEGER NOT NULL REFERENCES flight_schedules(schedule_id),
    check_type       TEXT NOT NULL CHECK(check_type IN ('T-24h', 'T-23h', 'manual')),
    checked_at       TEXT NOT NULL,
    hours_before_dep REAL NOT NULL,
    flight_found     INTEGER NOT NULL,
    price            TEXT,
    price_cents      INTEGER,
    num_results      INTEGER,
    search_success   INTEGER NOT NULL DEFAULT 1,
    error_message    TEXT,
    search_duration_ms INTEGER,
    raw_response     TEXT
);

CREATE TABLE IF NOT EXISTS check_log (
    log_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id      TEXT NOT NULL,
    logged_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S','now')),
    level       TEXT NOT NULL CHECK(level IN ('INFO', 'WARN', 'ERROR')),
    message     TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_checks_schedule_type
    ON availability_checks(schedule_id, check_type);
CREATE INDEX IF NOT EXISTS idx_schedules_week
    ON flight_schedules(week_of);
CREATE INDEX IF NOT EXISTS idx_schedules_departure
    ON flight_schedules(departure_pt);
CREATE INDEX IF NOT EXISTS idx_routes_pair
    ON routes(origin, destination);
"""


def get_connection(db_path=None):
    db = db_path or os.environ.get("GOWILD_DB_PATH") or str(DB_PATH)
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


@contextmanager
def db_session(db_path=None):
    conn = get_connection(db_path)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _migrate_weekend_to_week_of(conn):
    """Rename weekend_of -> week_of and shift Friday anchors to Monday if needed."""
    # Check if flight_schedules table exists at all (fresh DB won't have it yet)
    table_exists = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='flight_schedules'"
    ).fetchone()
    if not table_exists:
        return
    rows = conn.execute("PRAGMA table_info(flight_schedules)").fetchall()
    columns = [row["name"] for row in rows]
    if "weekend_of" not in columns:
        return
    conn.execute("ALTER TABLE flight_schedules RENAME COLUMN weekend_of TO week_of")
    conn.execute("UPDATE flight_schedules SET week_of = date(week_of, '-4 days')")
    conn.execute("DROP INDEX IF EXISTS idx_schedules_weekend")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_schedules_week ON flight_schedules(week_of)"
    )


def init_db(db_path=None):
    with db_session(db_path) as conn:
        _migrate_weekend_to_week_of(conn)
        conn.executescript(SCHEMA_SQL)


def get_or_create_route(conn, origin: str, destination: str, is_nonstop: int = 1) -> int:
    conn.execute(
        "INSERT OR IGNORE INTO routes (origin, destination, is_nonstop) VALUES (?, ?, ?)",
        (origin, destination, is_nonstop),
    )
    row = conn.execute(
        "SELECT route_id FROM routes WHERE origin = ? AND destination = ?",
        (origin, destination),
    ).fetchone()
    return row["route_id"]


def insert_flight_schedule(
    conn, route_id: int, departure_pt: str, arrival_pt: str,
    duration_min: int, stops: int, direction: str, week_of: str,
    raw_departure: str = "", raw_arrival: str = "",
) -> Optional[int]:
    cur = conn.execute(
        """INSERT OR IGNORE INTO flight_schedules
           (route_id, departure_pt, arrival_pt, duration_min, stops, direction,
            week_of, raw_departure, raw_arrival)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (route_id, departure_pt, arrival_pt, duration_min, stops, direction,
         week_of, raw_departure, raw_arrival),
    )
    if cur.lastrowid and cur.rowcount > 0:
        return cur.lastrowid
    row = conn.execute(
        "SELECT schedule_id FROM flight_schedules WHERE route_id = ? AND departure_pt = ? AND direction = ?",
        (route_id, departure_pt, direction),
    ).fetchone()
    return row["schedule_id"] if row else None


def insert_availability_check(
    conn, schedule_id: int, check_type: str, checked_at: str,
    hours_before_dep: float, flight_found: bool, price: str = None,
    price_cents: int = None, num_results: int = None,
    search_success: bool = True, error_message: str = None,
    search_duration_ms: int = None, raw_response: str = None,
) -> int:
    cur = conn.execute(
        """INSERT INTO availability_checks
           (schedule_id, check_type, checked_at, hours_before_dep, flight_found,
            price, price_cents, num_results, search_success, error_message,
            search_duration_ms, raw_response)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (schedule_id, check_type, checked_at, hours_before_dep,
         1 if flight_found else 0, price, price_cents, num_results,
         1 if search_success else 0, error_message, search_duration_ms,
         raw_response),
    )
    return cur.lastrowid



def get_unchecked_flights_past_t24h(
    conn, now_iso: str, max_staleness_hours: float = 6.0,
    international_dests: frozenset = frozenset(),
    domestic_booking_hours: float = 24.0,
    international_booking_hours: float = 240.0,
) -> list:
    """Get flights where the booking window has opened, not yet checked, not too stale.

    Domestic flights: booking opens 24h before departure.
    International flights (CUN, SJD, etc.): booking opens 10 days before.

    Returns flights ordered by freshness (closest to their booking window first).
    """
    # Use the widest window for the SQL query, then filter in Python
    max_window = max(domestic_booking_hours, international_booking_hours) if international_dests else domestic_booking_hours

    rows = conn.execute(
        """
        SELECT
            fs.schedule_id, fs.departure_pt, fs.arrival_pt, fs.direction,
            fs.duration_min, fs.stops, fs.week_of,
            r.route_id, r.origin, r.destination,
            (julianday(fs.departure_pt) - julianday(:now)) * 24.0 AS hours_until_dep
        FROM flight_schedules fs
        JOIN routes r ON r.route_id = fs.route_id
        LEFT JOIN availability_checks ac
            ON ac.schedule_id = fs.schedule_id
            AND ac.check_type = 'T-24h'
            AND ac.search_success = 1
        WHERE r.active = 1
          AND ac.check_id IS NULL
          AND (julianday(fs.departure_pt) - julianday(:now)) * 24.0 > 0
          AND (julianday(fs.departure_pt) - julianday(:now)) * 24.0 < :max_window
        ORDER BY (julianday(fs.departure_pt) - julianday(:now)) * 24.0 DESC
        """,
        {"now": now_iso, "max_window": max_window},
    ).fetchall()

    # Filter each row by its appropriate booking window
    results = []
    for row in rows:
        is_intl = row["destination"] in international_dests or row["origin"] in international_dests
        booking_hours = international_booking_hours if is_intl else domestic_booking_hours
        min_hours = booking_hours - max_staleness_hours
        hours = row["hours_until_dep"]

        if min_hours < hours < booking_hours:
            results.append(row)

    return results


def log_entry(conn, run_id: str, level: str, message: str):
    conn.execute(
        "INSERT INTO check_log (run_id, level, message) VALUES (?, ?, ?)",
        (run_id, level, message),
    )
