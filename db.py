"""Database layer for GoWild availability monitor."""

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
    weekend_of      TEXT NOT NULL,
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
CREATE INDEX IF NOT EXISTS idx_schedules_weekend
    ON flight_schedules(weekend_of);
CREATE INDEX IF NOT EXISTS idx_schedules_departure
    ON flight_schedules(departure_pt);
CREATE INDEX IF NOT EXISTS idx_routes_pair
    ON routes(origin, destination);
"""


def get_connection(db_path=None):
    db = db_path or str(DB_PATH)
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


def init_db(db_path=None):
    with db_session(db_path) as conn:
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
    duration_min: int, stops: int, direction: str, weekend_of: str,
    raw_departure: str = "", raw_arrival: str = "",
) -> Optional[int]:
    cur = conn.execute(
        """INSERT OR IGNORE INTO flight_schedules
           (route_id, departure_pt, arrival_pt, duration_min, stops, direction,
            weekend_of, raw_departure, raw_arrival)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (route_id, departure_pt, arrival_pt, duration_min, stops, direction,
         weekend_of, raw_departure, raw_arrival),
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


def check_already_done(conn, schedule_id: int, check_type: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM availability_checks WHERE schedule_id = ? AND check_type = ? AND search_success = 1",
        (schedule_id, check_type),
    ).fetchone()
    return row is not None


def get_flights_needing_check(conn, now_iso: str, tolerance_minutes: int = 10) -> List[sqlite3.Row]:
    return conn.execute(
        """
        SELECT
            fs.schedule_id, fs.departure_pt, fs.arrival_pt, fs.direction,
            fs.duration_min, fs.stops, fs.weekend_of,
            r.route_id, r.origin, r.destination,
            (julianday(fs.departure_pt) - julianday(:now)) * 24.0 AS hours_until_dep
        FROM flight_schedules fs
        JOIN routes r ON r.route_id = fs.route_id
        WHERE r.active = 1
          AND fs.direction = 'outbound'
          AND (
            ABS((julianday(fs.departure_pt) - julianday(:now)) * 24.0 - 24.0) * 60 <= :tol
            OR
            ABS((julianday(fs.departure_pt) - julianday(:now)) * 24.0 - 23.0) * 60 <= :tol
          )
        """,
        {"now": now_iso, "tol": tolerance_minutes},
    ).fetchall()


def get_unchecked_flights_past_t24h(
    conn, now_iso: str, max_staleness_hours: float = 6.0
) -> List[sqlite3.Row]:
    """Get flights where T-24h has passed, not yet checked, not too stale.

    Returns flights ordered by freshness: those closest to the T-24h mark
    (i.e., highest hours_until_dep, closest to 24.0) come first.

    A flight departing in 23.5h means T-24h passed 0.5h ago — very fresh.
    A flight departing in 18h means T-24h passed 6h ago — stale.
    """
    min_hours = 24.0 - max_staleness_hours
    return conn.execute(
        """
        SELECT
            fs.schedule_id, fs.departure_pt, fs.arrival_pt, fs.direction,
            fs.duration_min, fs.stops, fs.weekend_of,
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
          AND (julianday(fs.departure_pt) - julianday(:now)) * 24.0 > :min_hours
          AND (julianday(fs.departure_pt) - julianday(:now)) * 24.0 < 24.0
        ORDER BY (julianday(fs.departure_pt) - julianday(:now)) * 24.0 DESC
        """,
        {"now": now_iso, "min_hours": min_hours},
    ).fetchall()


def log_entry(conn, run_id: str, level: str, message: str):
    conn.execute(
        "INSERT INTO check_log (run_id, level, message) VALUES (?, ?, ?)",
        (run_id, level, message),
    )
