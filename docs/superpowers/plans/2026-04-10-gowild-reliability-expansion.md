# GoWild Monitor: Reliability & Full Coverage Expansion

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the GoWild monitor reliable, expand to all Frontier destinations (nonstop + 1-stop connections) from SFO and SJC in both directions, and add historical confidence analysis so the user can identify "safe bet" destinations for GoWild pass weekend trips.

**Architecture:** Replace the fragile ±10-minute tolerance scheduling with a "catch-up" model: each dispatch run checks ALL flights whose 24h booking window has opened (T-24h passed) but haven't been checked yet, ordered by freshness. Expand seeding to all Frontier destinations, both directions, Fri–Mon, with no departure-time filtering. Batch availability checks by (origin, dest, date) so one Google Flights search covers multiple flights. Add per-route confidence scoring over time.

**Tech Stack:** Python 3.11, SQLite (WAL), fast-flights 2.2, Typer, Rich, GitHub Actions, pytest

**Key design decisions:**
- Check AFTER T-24h (GoWild booking opens exactly 24h before departure — we want post-open availability)
- Catch-up architecture: tolerant of GitHub Actions skipping runs; freshest flights checked first
- No departure-time filtering at seed time — store everything, filter in analysis
- Batch checking: one Google Flights search per (origin, dest, date) covers all flight times on that route
- ~608 searches per weekly seed (~15 min), well within GitHub Actions limits

---

### Task 1: Expand destination config and add new constants

**Files:**
- Modify: `config.py`

- [ ] **Step 1: Replace MONITORED_ROUTES and add new constants**

Replace the entire contents of `config.py` with:

```python
"""Shared constants for the GoWild availability monitor."""

from pathlib import Path

# Paths
PROJECT_DIR = Path(__file__).parent
DB_PATH = PROJECT_DIR / "gowild_monitor.db"
LOCK_FILE = PROJECT_DIR / ".dispatch.lock"
LOG_DIR = PROJECT_DIR / "logs"

# Timezone
PACIFIC_TZ = "America/Los_Angeles"

# ── Check scheduling ─────────────────────────────────────────────────────
# After T-24h, how long is the check still worth doing?
# 6h means we check flights departing 18–24h from now.
MAX_STALENESS_HOURS = 6.0

# Rate limiting for Google Flights scraper
RATE_LIMIT_SECONDS = 1.0
MAX_RETRIES = 2
RETRY_DELAY_SECONDS = 3

# Max connections for searches (0 = nonstop only, 1 = up to 1 stop)
DEFAULT_MAX_STOPS = 1

# ── Airports ─────────────────────────────────────────────────────────────
BAY_AREA_AIRPORTS = ["SFO", "SJC"]

# ── All Frontier destinations reachable from Bay Area ────────────────────
# Nonstop + 1-stop connections. Google Flights handles intermediate routing
# automatically, so these are FINAL destinations to search.
#
# Sources: Frontier route map, FlightConnections, Wikipedia (Apr 2026)

ALL_FRONTIER_DESTS_SFO = sorted(set([
    # Nonstop from SFO
    "ATL", "DEN", "DFW", "LAS", "LAX", "MCO", "MDW", "PHX", "SAN", "SLC",
    # Connecting via DEN/LAS/PHX hubs
    "ABQ", "AUS", "BNA", "BOI", "BOS", "BUF", "BUR", "CHS", "CLE", "CLT",
    "CMH", "COS", "CUN", "CVG", "DCA", "DSM", "DTW", "ELP", "EWR", "FAY",
    "FLL", "GRR", "HOU", "IAD", "IAH", "IND", "ISP", "JAX", "JFK", "LGA",
    "LIT", "MCI", "MEM", "MIA", "MKE", "MSP", "MSY", "OKC", "OMA", "ONT",
    "ORD", "PBI", "PDX", "PHL", "PIT", "PNS", "RDU", "RNO", "RSW", "SAT",
    "SDF", "SMF", "SNA", "STL", "SJD", "SYR", "TPA", "TTN", "TUS", "TYS",
]))

ALL_FRONTIER_DESTS_SJC = sorted(set([
    # Nonstop from SJC
    "DEN", "LAS", "LAX", "PHX", "SAN",
    # Key connecting destinations (via DEN hub)
    "ATL", "AUS", "BNA", "CLT", "DFW", "FLL", "IAH", "MCO", "MIA",
    "MSP", "ORD", "SAT",
]))

MONITORED_ROUTES = {
    "SFO": ALL_FRONTIER_DESTS_SFO,
    "SJC": ALL_FRONTIER_DESTS_SJC,
}
```

- [ ] **Step 2: Verify config loads without errors**

Run: `cd /Users/koby/Downloads/GoWild && python3 -c "from config import MONITORED_ROUTES, MAX_STALENESS_HOURS, DEFAULT_MAX_STOPS; print(f'SFO: {len(MONITORED_ROUTES[\"SFO\"])} dests, SJC: {len(MONITORED_ROUTES[\"SJC\"])} dests')"`

Expected: `SFO: 71 dests, SJC: 17 dests` (approximate — exact count depends on dedup)

- [ ] **Step 3: Commit**

```bash
git add config.py
git commit -m "feat: expand destination config to all Frontier routes (nonstop + connections)

Replaces the 19-route subset with full Frontier destination coverage:
- SFO: ~71 destinations (10 nonstop + ~61 connecting)
- SJC: ~17 destinations (5 nonstop + ~12 connecting)
Adds MAX_STALENESS_HOURS and DEFAULT_MAX_STOPS constants for
the upcoming catch-up scheduling redesign."
```

---

### Task 2: Add catch-up query to DB layer

**Files:**
- Modify: `db.py`
- Create: `tests/__init__.py`
- Create: `tests/conftest.py`
- Create: `tests/test_db.py`
- Modify: `requirements.txt`

- [ ] **Step 1: Add pytest to requirements.txt**

Append `pytest` to `requirements.txt`:

```
fast-flights==2.2
pytz
typer
rich
python-dateutil
pytest
```

- [ ] **Step 2: Create test infrastructure**

Create `tests/__init__.py` (empty file).

Create `tests/conftest.py`:

```python
"""Shared fixtures for GoWild tests."""

import sqlite3
import pytest
from db import init_db, db_session, get_or_create_route, insert_flight_schedule


@pytest.fixture
def test_db(tmp_path):
    """Create a temporary test database with schema initialized."""
    db_path = str(tmp_path / "test.db")
    init_db(db_path)
    return db_path


@pytest.fixture
def seeded_db(test_db):
    """Test DB populated with sample flights for catch-up query testing.

    Creates flights at various hours before 'now' to test the catch-up window.
    Reference time: 2026-04-11T00:00:00-07:00 (Fri midnight PT)

    Flights:
      schedule_id 1: SFO->LAS dep 2026-04-11T22:00 PT (22h away — T-24h passed 2h ago)  ← should match
      schedule_id 2: SFO->DEN dep 2026-04-11T23:30 PT (23.5h away — T-24h passed 0.5h ago) ← should match (freshest)
      schedule_id 3: SFO->ATL dep 2026-04-12T06:00 PT (30h away — T-24h not yet passed)  ← should NOT match
      schedule_id 4: SFO->PHX dep 2026-04-11T15:00 PT (15h away — T-24h passed 9h ago)   ← too stale (>6h)
      schedule_id 5: SFO->LAX dep 2026-04-11T23:00 PT (23h away — already checked)       ← should NOT match
    """
    with db_session(test_db) as conn:
        r1 = get_or_create_route(conn, "SFO", "LAS", 1)
        r2 = get_or_create_route(conn, "SFO", "DEN", 1)
        r3 = get_or_create_route(conn, "SFO", "ATL", 0)
        r4 = get_or_create_route(conn, "SFO", "PHX", 1)
        r5 = get_or_create_route(conn, "SFO", "LAX", 1)

        insert_flight_schedule(conn, r1, "2026-04-11T22:00:00-07:00", "2026-04-12T00:30:00-07:00",
                               150, 0, "outbound", "2026-04-10")
        insert_flight_schedule(conn, r2, "2026-04-11T23:30:00-07:00", "2026-04-12T03:00:00-07:00",
                               210, 1, "outbound", "2026-04-10")
        insert_flight_schedule(conn, r3, "2026-04-12T06:00:00-07:00", "2026-04-12T14:00:00-07:00",
                               300, 1, "outbound", "2026-04-10")
        insert_flight_schedule(conn, r4, "2026-04-11T15:00:00-07:00", "2026-04-11T17:00:00-07:00",
                               120, 0, "outbound", "2026-04-10")
        insert_flight_schedule(conn, r5, "2026-04-11T23:00:00-07:00", "2026-04-12T00:30:00-07:00",
                               90, 0, "outbound", "2026-04-10")

        # Mark flight 5 (SFO->LAX) as already checked
        conn.execute(
            """INSERT INTO availability_checks
               (schedule_id, check_type, checked_at, hours_before_dep, flight_found,
                search_success)
               VALUES (5, 'T-24h', '2026-04-11T00:00:00-07:00', 23.0, 1, 1)"""
        )

    return test_db
```

- [ ] **Step 3: Write failing test for catch-up query**

Create `tests/test_db.py`:

```python
"""Tests for the catch-up query logic."""

from db import db_session, get_unchecked_flights_past_t24h


def test_catchup_query_returns_correct_flights(seeded_db):
    """Catch-up query should return only flights where T-24h has passed,
    not already checked, and not too stale."""
    now_iso = "2026-04-11T00:00:00-07:00"

    with db_session(seeded_db) as conn:
        flights = get_unchecked_flights_past_t24h(conn, now_iso, max_staleness_hours=6.0)

    schedule_ids = [f["schedule_id"] for f in flights]

    # Should include: LAS (22h away, 2h stale) and DEN (23.5h away, 0.5h stale)
    assert 1 in schedule_ids, "SFO->LAS should be included (T-24h passed 2h ago)"
    assert 2 in schedule_ids, "SFO->DEN should be included (T-24h passed 0.5h ago)"

    # Should NOT include:
    assert 3 not in schedule_ids, "SFO->ATL should be excluded (T-24h not yet passed)"
    assert 4 not in schedule_ids, "SFO->PHX should be excluded (too stale, 9h after T-24h)"
    assert 5 not in schedule_ids, "SFO->LAX should be excluded (already checked)"


def test_catchup_query_orders_by_freshness(seeded_db):
    """Freshest flights (closest to T-24h) should come first."""
    now_iso = "2026-04-11T00:00:00-07:00"

    with db_session(seeded_db) as conn:
        flights = get_unchecked_flights_past_t24h(conn, now_iso, max_staleness_hours=6.0)

    schedule_ids = [f["schedule_id"] for f in flights]
    # DEN (23.5h = freshest, T-24h was 0.5h ago) should come before LAS (22h = 2h stale)
    den_idx = schedule_ids.index(2)
    las_idx = schedule_ids.index(1)
    assert den_idx < las_idx, "DEN (fresher) should come before LAS (staler)"


def test_catchup_query_includes_return_flights(test_db):
    """Catch-up query should check both outbound AND return flights."""
    from db import get_or_create_route, insert_flight_schedule

    with db_session(test_db) as conn:
        r = get_or_create_route(conn, "LAS", "SFO", 1)
        insert_flight_schedule(conn, r, "2026-04-11T23:00:00-07:00", "2026-04-12T01:00:00-07:00",
                               120, 0, "return", "2026-04-10")

    now_iso = "2026-04-11T00:00:00-07:00"
    with db_session(test_db) as conn:
        flights = get_unchecked_flights_past_t24h(conn, now_iso, max_staleness_hours=6.0)

    assert len(flights) == 1
    assert flights[0]["origin"] == "LAS"
    assert flights[0]["destination"] == "SFO"
```

- [ ] **Step 4: Run tests — verify they fail**

Run: `cd /Users/koby/Downloads/GoWild && python3 -m pytest tests/test_db.py -v`

Expected: FAIL — `get_unchecked_flights_past_t24h` does not exist yet.

- [ ] **Step 5: Implement the catch-up query in db.py**

Add this function to `db.py`, after the existing `get_flights_needing_check` function (keep the old function for now — we'll remove it in a later task):

```python
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
```

- [ ] **Step 6: Run tests — verify they pass**

Run: `cd /Users/koby/Downloads/GoWild && python3 -m pytest tests/test_db.py -v`

Expected: All 3 tests PASS.

- [ ] **Step 7: Commit**

```bash
git add requirements.txt tests/ db.py
git commit -m "feat: add catch-up query for post-T-24h availability checking

New get_unchecked_flights_past_t24h() query finds flights where the
24h booking window has opened but we haven't checked yet. Orders by
freshness (closest to T-24h first). Caps at MAX_STALENESS_HOURS to
avoid checking flights where data would be too stale.

Checks both outbound and return flights.

Includes pytest setup and tests for the query logic."
```

---

### Task 3: Add batch checking to checker

**Files:**
- Modify: `checker.py`
- Create: `tests/test_checker.py`

- [ ] **Step 1: Write failing test for batch checking**

Create `tests/test_checker.py`:

```python
"""Tests for batch flight checking logic."""

from unittest.mock import patch, MagicMock
from checker import batch_check_flights


def _make_flight_option(dep_hour, dep_minute, price, stops=0):
    """Create a mock FlightOption for testing."""
    mock = MagicMock()
    mock.dep_dt = MagicMock()
    mock.dep_dt.hour = dep_hour
    mock.dep_dt.minute = dep_minute
    mock.price = price
    mock.stops = stops
    mock.origin = "SFO"
    mock.destination = "LAS"
    mock.departure = f"{dep_hour}:{dep_minute:02d} PM"
    mock.arrival = "11:00 PM"
    mock.duration = "2 hr 30 min"
    return mock


@patch("checker.search_flights")
def test_batch_matches_multiple_flights(mock_search):
    """A single search should match results to multiple seeded flights."""
    mock_search.return_value = [
        _make_flight_option(18, 44, "$160"),
        _make_flight_option(20, 14, "$185"),
        _make_flight_option(22, 50, "$142"),
    ]

    flights_to_check = [
        {"schedule_id": 1, "departure_pt": "2026-04-10T18:44:00-07:00"},
        {"schedule_id": 2, "departure_pt": "2026-04-10T20:14:00-07:00"},
        {"schedule_id": 3, "departure_pt": "2026-04-10T21:00:00-07:00"},  # no match
    ]

    results = batch_check_flights("SFO", "LAS", "2026-04-10", flights_to_check)

    assert len(results) == 3
    assert results[0]["flight_found"] is True
    assert results[0]["price"] == "$160"
    assert results[1]["flight_found"] is True
    assert results[1]["price"] == "$185"
    assert results[2]["flight_found"] is False  # 21:00 not in search results


@patch("checker.search_flights")
def test_batch_handles_search_failure(mock_search):
    """If the search raises an exception, all flights get error results."""
    mock_search.side_effect = Exception("rate limited")

    flights_to_check = [
        {"schedule_id": 1, "departure_pt": "2026-04-10T18:44:00-07:00"},
    ]

    results = batch_check_flights("SFO", "LAS", "2026-04-10", flights_to_check)

    assert len(results) == 1
    assert results[0]["flight_found"] is False
    assert results[0]["error"] is not None
    assert "rate limited" in results[0]["error"]


@patch("checker.search_flights")
def test_batch_returns_all_frontier_count(mock_search):
    """Each result should include the total Frontier flight count from the search."""
    mock_search.return_value = [
        _make_flight_option(18, 44, "$160"),
        _make_flight_option(20, 14, "$185"),
        _make_flight_option(22, 0, "$142"),
    ]

    flights_to_check = [
        {"schedule_id": 1, "departure_pt": "2026-04-10T18:44:00-07:00"},
    ]

    results = batch_check_flights("SFO", "LAS", "2026-04-10", flights_to_check)

    assert results[0]["num_frontier_results"] == 3
```

- [ ] **Step 2: Run tests — verify they fail**

Run: `cd /Users/koby/Downloads/GoWild && python3 -m pytest tests/test_checker.py -v`

Expected: FAIL — `batch_check_flights` does not exist.

- [ ] **Step 3: Implement batch_check_flights in checker.py**

Add to the end of `checker.py` (before the existing `_flight_to_dict` helper — batch_check_flights uses it):

```python
def batch_check_flights(
    origin: str, destination: str, date_str: str,
    flights_to_check: list,
    max_stops: int = None, time_tolerance_min: int = 5,
) -> list:
    """Check multiple flights on the same route+date with a single Google Flights search.

    flights_to_check: list of dicts with at least 'schedule_id' and 'departure_pt'.
    Returns: list of result dicts in the same order, one per flight.
    """
    from gowild_search import search_flights

    start = time.time()
    search_results = None
    last_error = None

    for attempt in range(MAX_RETRIES + 1):
        try:
            search_results = search_flights(origin, destination, date_str, max_stops=max_stops)
            last_error = None
            break
        except Exception as e:
            last_error = str(e)
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_DELAY_SECONDS)

    elapsed_ms = int((time.time() - start) * 1000)

    results = []
    for flight in flights_to_check:
        result = {
            "schedule_id": flight["schedule_id"],
            "flight_found": False,
            "price": None,
            "price_cents": None,
            "num_frontier_results": 0,
            "matched_flight": None,
            "error": last_error,
            "duration_ms": elapsed_ms,
        }

        if search_results is None:
            results.append(result)
            continue

        result["num_frontier_results"] = len(search_results)
        result["error"] = None

        dep_dt = datetime.fromisoformat(flight["departure_pt"])
        for sr in search_results:
            if sr.dep_dt is None:
                continue
            total_diff = abs(sr.dep_dt.hour - dep_dt.hour) * 60 + abs(sr.dep_dt.minute - dep_dt.minute)
            if total_diff <= time_tolerance_min:
                result["flight_found"] = True
                result["price"] = sr.price
                result["price_cents"] = parse_price_cents(sr.price)
                result["matched_flight"] = _flight_to_dict(sr)
                break

        results.append(result)

    return results
```

- [ ] **Step 4: Run tests — verify they pass**

Run: `cd /Users/koby/Downloads/GoWild && python3 -m pytest tests/test_checker.py -v`

Expected: All 3 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add checker.py tests/test_checker.py
git commit -m "feat: add batch checking — one search per route+date covers all flights

batch_check_flights() makes a single Google Flights search and matches
results against multiple seeded flights by departure time. Much more
efficient than individual searches when checking many flights on the
same route and date."
```

---

### Task 4: Rewrite seeder for full coverage (both directions, all dates)

**Files:**
- Modify: `seeder.py`

- [ ] **Step 1: Rewrite seed_weekend for broad coverage**

Replace the entire contents of `seeder.py`:

```python
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
    """Seed flight schedules for a weekend: all destinations, both directions, Fri–Mon.

    friday_date: 'YYYY-MM-DD' format, must be a Friday.
    Returns: {routes_checked, flights_found, flights_inserted, errors}
    """
    if max_stops is None:
        max_stops = DEFAULT_MAX_STOPS

    fri_dt = datetime.strptime(friday_date, "%Y-%m-%d")
    dates = [
        fri_dt.strftime("%Y-%m-%d"),                        # Friday
        (fri_dt + timedelta(days=1)).strftime("%Y-%m-%d"),  # Saturday
        (fri_dt + timedelta(days=2)).strftime("%Y-%m-%d"),  # Sunday
        (fri_dt + timedelta(days=3)).strftime("%Y-%m-%d"),  # Monday
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

        # Flush the progress line
        conn.commit()

    print(f"\r  Done: {stats['flights_found']} flights found, {stats['flights_inserted']} new schedules inserted.          ")
    return stats


def seed_next_n_weekends(n: int = 4, max_stops: int = DEFAULT_MAX_STOPS) -> dict:
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
```

- [ ] **Step 2: Verify the seeder loads without import errors**

Run: `cd /Users/koby/Downloads/GoWild && python3 -c "from seeder import seed_weekend; print('OK')"`

Expected: `OK`

- [ ] **Step 3: Commit**

```bash
git add seeder.py
git commit -m "feat: expand seeder to all destinations, both directions, Fri-Mon

Seeder now searches all Frontier destinations from SFO and SJC,
in both outbound and return directions, across Friday through Monday.
No departure-time filtering — stores all flights for analysis layer
to filter. Supports max_stops for connection discovery."
```

---

### Task 5: Rewrite scheduler with catch-up logic and batch checking

**Files:**
- Modify: `scheduler.py`

- [ ] **Step 1: Replace scheduler.py with catch-up + batch dispatch**

Replace the entire contents of `scheduler.py`:

```python
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
                conn, now_iso, MAX_STALENESS_HOURS
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

                    status = "FOUND" if result["flight_found"] else "NOT FOUND"
                    price_info = f" @ {result['price']}" if result["price"] else ""
                    dep_time = flight["departure_pt"][11:16]
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
```

- [ ] **Step 2: Verify scheduler loads without import errors**

Run: `cd /Users/koby/Downloads/GoWild && python3 -c "from scheduler import dispatch; print('OK')"`

Expected: `OK`

- [ ] **Step 3: Run all tests to check nothing is broken**

Run: `cd /Users/koby/Downloads/GoWild && python3 -m pytest tests/ -v`

Expected: All tests pass.

- [ ] **Step 4: Commit**

```bash
git add scheduler.py
git commit -m "feat: rewrite scheduler with catch-up logic and batch checking

Replaces the fragile ±10min tolerance scheduling with a catch-up model:
- Checks ALL flights where T-24h has passed (up to MAX_STALENESS_HOURS)
- Orders by freshness (closest to T-24h first)
- Batches by (origin, dest, date) — one search covers multiple flights
- Caps at MAX_FLIGHTS_PER_RUN to avoid timeouts
- Tolerant of GitHub Actions skipping/delaying runs"
```

---

### Task 6: Add confidence analysis and update CLI

**Files:**
- Modify: `analysis.py`
- Modify: `monitor.py`

- [ ] **Step 1: Add confidence report and safe destinations to analysis.py**

Add these two functions to the end of `analysis.py`:

```python
def confidence_report(min_weekends: int = 2):
    """Show per-route availability confidence based on historical data."""
    conn = get_connection()
    rows = conn.execute(
        """
        SELECT r.origin, r.destination,
            fs.direction,
            COUNT(DISTINCT fs.weekend_of) AS weekends,
            COUNT(*) AS total_checks,
            SUM(ac.flight_found) AS times_found,
            ROUND(100.0 * SUM(ac.flight_found) / COUNT(*), 1) AS availability_pct,
            ROUND(AVG(CASE WHEN ac.flight_found THEN ac.price_cents END) / 100.0, 0) AS avg_price,
            ROUND(AVG(24.0 - ac.hours_before_dep) * 60, 0) AS avg_min_after_t24h
        FROM availability_checks ac
        JOIN flight_schedules fs ON fs.schedule_id = ac.schedule_id
        JOIN routes r ON r.route_id = fs.route_id
        WHERE ac.check_type = 'T-24h' AND ac.search_success = 1
        GROUP BY r.origin, r.destination, fs.direction
        HAVING COUNT(DISTINCT fs.weekend_of) >= ?
        ORDER BY availability_pct DESC, weekends DESC
        """,
        (min_weekends,),
    ).fetchall()
    conn.close()

    if not rows:
        console.print(f"[yellow]Not enough data yet (need {min_weekends}+ weekends).[/yellow]")
        return

    table = Table(title=f"Route Confidence (min {min_weekends} weekends)")
    table.add_column("Route", style="cyan")
    table.add_column("Dir", style="dim")
    table.add_column("Wkds", justify="right")
    table.add_column("Checks", justify="right")
    table.add_column("Avail", justify="right")
    table.add_column("Rate", justify="right")
    table.add_column("Avg $", justify="right")
    table.add_column("Freshness", justify="right", style="dim")

    for r in rows:
        rate = r["availability_pct"]
        rate_style = "green" if rate >= 75 else ("yellow" if rate >= 50 else "red")
        avg_price = f"${int(r['avg_price'])}" if r["avg_price"] else "-"
        freshness = f"{int(r['avg_min_after_t24h'])}m" if r["avg_min_after_t24h"] else "-"
        table.add_row(
            f"{r['origin']}->{r['destination']}",
            r["direction"][:3],
            str(r["weekends"]),
            str(r["total_checks"]),
            str(r["times_found"]),
            f"[{rate_style}]{rate}%[/{rate_style}]",
            avg_price,
            freshness,
        )

    console.print(table)


def safe_destinations(min_pct: float = 75.0, min_weekends: int = 2):
    """Show destinations where both outbound and return have reliable availability."""
    conn = get_connection()

    rows = conn.execute(
        """
        WITH route_stats AS (
            SELECT
                CASE WHEN fs.direction = 'outbound' THEN r.destination ELSE r.origin END AS dest,
                fs.direction,
                COUNT(DISTINCT fs.weekend_of) AS weekends,
                COUNT(*) AS total_checks,
                SUM(ac.flight_found) AS times_found,
                ROUND(100.0 * SUM(ac.flight_found) / COUNT(*), 1) AS pct,
                ROUND(AVG(CASE WHEN ac.flight_found THEN ac.price_cents END) / 100.0, 0) AS avg_price
            FROM availability_checks ac
            JOIN flight_schedules fs ON fs.schedule_id = ac.schedule_id
            JOIN routes r ON r.route_id = fs.route_id
            WHERE ac.check_type = 'T-24h' AND ac.search_success = 1
            GROUP BY dest, fs.direction
            HAVING COUNT(DISTINCT fs.weekend_of) >= :min_wk
        )
        SELECT
            o.dest,
            o.pct AS out_pct, o.weekends AS out_wk, o.avg_price AS out_price,
            r.pct AS ret_pct, r.weekends AS ret_wk, r.avg_price AS ret_price,
            ROUND((o.pct + r.pct) / 2.0, 1) AS combined_pct
        FROM route_stats o
        JOIN route_stats r ON o.dest = r.dest
        WHERE o.direction = 'outbound' AND r.direction = 'return'
          AND o.pct >= :min_pct AND r.pct >= :min_pct
        ORDER BY combined_pct DESC
        """,
        {"min_wk": min_weekends, "min_pct": min_pct},
    ).fetchall()
    conn.close()

    if not rows:
        console.print(f"[yellow]No destinations meet the {min_pct}% threshold yet.[/yellow]")
        console.print("[dim]Need more weekends of data. Run 'confidence' to see current rates.[/dim]")
        return

    table = Table(title=f"Safe GoWild Destinations (>{min_pct}% availability, {min_weekends}+ weekends)")
    table.add_column("Destination", style="bold cyan")
    table.add_column("Out Rate", justify="right")
    table.add_column("Out Avg $", justify="right")
    table.add_column("Ret Rate", justify="right")
    table.add_column("Ret Avg $", justify="right")
    table.add_column("Combined", justify="right")
    table.add_column("Weekends", justify="right", style="dim")

    for r in rows:
        out_style = "green" if r["out_pct"] >= 75 else "yellow"
        ret_style = "green" if r["ret_pct"] >= 75 else "yellow"
        combined_style = "green bold" if r["combined_pct"] >= 80 else "green"
        out_price = f"${int(r['out_price'])}" if r["out_price"] else "-"
        ret_price = f"${int(r['ret_price'])}" if r["ret_price"] else "-"
        wk = max(r["out_wk"], r["ret_wk"])
        table.add_row(
            r["dest"],
            f"[{out_style}]{r['out_pct']}%[/{out_style}]",
            out_price,
            f"[{ret_style}]{r['ret_pct']}%[/{ret_style}]",
            ret_price,
            f"[{combined_style}]{r['combined_pct']}%[/{combined_style}]",
            str(wk),
        )

    console.print(table)
    console.print("\n[dim]Tip: Rates improve in accuracy over more weekends of data.[/dim]")
```

- [ ] **Step 2: Add confidence and safe CLI commands to monitor.py**

Add these commands to `monitor.py`, after the existing `detail` command and before `cron_install`:

```python
@app.command()
def confidence(
    min_weekends: int = typer.Option(2, "--min-weekends", "-w", help="Minimum weekends of data"),
):
    """Show per-route availability confidence over time."""
    from analysis import confidence_report
    confidence_report(min_weekends)


@app.command()
def safe(
    min_pct: float = typer.Option(75.0, "--min-pct", "-p", help="Minimum availability %"),
    min_weekends: int = typer.Option(2, "--min-weekends", "-w", help="Minimum weekends of data"),
):
    """Show destinations safe for GoWild booking (reliable outbound + return)."""
    from analysis import safe_destinations
    safe_destinations(min_pct, min_weekends)
```

- [ ] **Step 3: Verify commands register**

Run: `cd /Users/koby/Downloads/GoWild && python3 monitor.py --help`

Expected: `confidence` and `safe` commands should appear in the help output.

- [ ] **Step 4: Commit**

```bash
git add analysis.py monitor.py
git commit -m "feat: add confidence analysis and safe destinations report

New CLI commands:
- 'confidence': per-route availability % over historical weekends
- 'safe': destinations where both outbound and return exceed a
  reliability threshold — the 'safe bets' for GoWild trips"
```

---

### Task 7: Update GitHub Actions workflows

**Files:**
- Modify: `.github/workflows/dispatch.yml`
- Modify: `.github/workflows/seed.yml`

- [ ] **Step 1: Rewrite dispatch.yml for catch-up scheduling**

Replace the entire contents of `.github/workflows/dispatch.yml`:

```yaml
name: GoWild Dispatch

# Catch-up model: each run checks ALL flights past T-24h.
# Run every 2 hours Thu-Mon to cover outbound (Fri/Sat) and return (Sun/Mon).
# GitHub Actions may skip some runs — the catch-up logic handles gaps.
on:
  schedule:
    - cron: '0 */2 * * 4,5,6,0,1'  # Every 2h on Thu-Mon (UTC)
  workflow_dispatch: {}

jobs:
  check:
    runs-on: ubuntu-latest
    timeout-minutes: 30
    permissions:
      contents: write
    steps:
      - uses: actions/checkout@v4

      - name: Set up Python
        uses: actions/setup-python@v5
        with:
          python-version: '3.11'
          cache: 'pip'

      - name: Install dependencies
        run: pip install -r requirements.txt

      - name: Initialize DB if needed
        run: python monitor.py init

      - name: Run dispatcher
        run: python monitor.py dispatch

      - name: Commit database changes
        run: |
          git config user.name "github-actions[bot]"
          git config user.email "github-actions[bot]@users.noreply.github.com"
          git add gowild_monitor.db
          if git diff --cached --quiet; then
            echo "No DB changes to commit."
            exit 0
          fi
          git commit -m "chore: update availability data [skip ci]"
          for i in 1 2 3; do
            git push && break
            echo "Push failed, retrying ($i/3)..."
            sleep 5
            git pull --rebase
          done
```

- [ ] **Step 2: Rewrite seed.yml for expanded seeding**

Replace the entire contents of `.github/workflows/seed.yml`:

```yaml
name: GoWild Seed

# Seed all Frontier destinations (SFO + SJC), both directions, Fri-Mon.
# Runs Monday morning PT to discover flights for the upcoming weekend.
on:
  schedule:
    - cron: '7 17 * * 1'  # Monday 5:07pm UTC = 10:07am PT
  workflow_dispatch:
    inputs:
      friday_date:
        description: 'Friday date (YYYY-MM-DD)'
        required: false

jobs:
  seed:
    runs-on: ubuntu-latest
    timeout-minutes: 45  # Expanded from 10 — ~15 min for 600+ searches
    permissions:
      contents: write
    steps:
      - uses: actions/checkout@v4

      - name: Set up Python
        uses: actions/setup-python@v5
        with:
          python-version: '3.11'
          cache: 'pip'

      - name: Install dependencies
        run: pip install -r requirements.txt

      - name: Initialize DB
        run: python monitor.py init

      - name: Seed flight schedules
        run: |
          if [ -n "${{ github.event.inputs.friday_date }}" ]; then
            python monitor.py seed "${{ github.event.inputs.friday_date }}" --max-stops 1
          else
            python monitor.py seed --max-stops 1
          fi

      - name: Commit database
        run: |
          git config user.name "github-actions[bot]"
          git config user.email "github-actions[bot]@users.noreply.github.com"
          git add gowild_monitor.db
          if git diff --cached --quiet; then
            echo "No DB changes to commit."
            exit 0
          fi
          git commit -m "chore: seed weekend flight schedules [skip ci]"
          for i in 1 2 3; do
            git push && break
            echo "Push failed, retrying ($i/3)..."
            sleep 5
            git pull --rebase
          done
```

- [ ] **Step 3: Commit**

```bash
git add .github/workflows/dispatch.yml .github/workflows/seed.yml
git commit -m "feat: update workflows for catch-up scheduling and expanded seeding

Dispatch: every 2h Thu-Mon (catch-up handles GitHub Actions gaps)
Seed: 45-min timeout for ~600+ searches across all destinations"
```

---

### Task 8: Update monitor.py seed command to pass max_stops

**Files:**
- Modify: `monitor.py`

- [ ] **Step 1: Ensure the seed CLI command passes max_stops through**

In `monitor.py`, the existing `seed` command already has a `--max-stops` option. Verify it passes through to `seed_weekend`. Read the current `seed` command — it already calls `seed_weekend(friday_date, max_stops=max_stops)`. No code change needed if already correct.

If `seed_weekend` in the old code used a different signature, the new `seeder.py` from Task 4 handles `max_stops` as a keyword arg with default `DEFAULT_MAX_STOPS`. The existing CLI should work.

- [ ] **Step 2: Run the full test suite**

Run: `cd /Users/koby/Downloads/GoWild && python3 -m pytest tests/ -v`

Expected: All tests pass.

- [ ] **Step 3: Verify the CLI works end-to-end**

Run: `cd /Users/koby/Downloads/GoWild && python3 monitor.py --help`

Expected: All commands listed: `init`, `seed`, `dispatch`, `check`, `status`, `report`, `compare`, `detail`, `confidence`, `safe`, `cron-install`, `cron-remove`.

- [ ] **Step 4: Clean up old code — remove unused imports and functions**

In `scheduler.py`, the old `classify_check_type` function and `CHECK_WINDOWS`/`CHECK_TOLERANCE_MINUTES` imports from config are no longer used. Verify they are gone (Task 5's rewrite removed them).

In `config.py`, the old `CHECK_WINDOWS`, `CHECK_TOLERANCE_MINUTES`, `OUTBOUND_EARLIEST_HOUR`, `OUTBOUND_LATEST_HOUR` constants are no longer used. Verify they are gone (Task 1's rewrite removed them).

In `db.py`, the old `get_flights_needing_check` and `check_already_done` functions are no longer called by the scheduler. Remove them:

```python
# DELETE these two functions from db.py:
# - check_already_done (no longer called)
# - get_flights_needing_check (replaced by get_unchecked_flights_past_t24h)
```

- [ ] **Step 5: Run tests after cleanup**

Run: `cd /Users/koby/Downloads/GoWild && python3 -m pytest tests/ -v`

Expected: All tests pass.

- [ ] **Step 6: Commit**

```bash
git add db.py monitor.py scheduler.py config.py
git commit -m "chore: remove unused legacy scheduling code

Removes CHECK_WINDOWS, CHECK_TOLERANCE_MINUTES, OUTBOUND_EARLIEST_HOUR,
OUTBOUND_LATEST_HOUR from config. Removes check_already_done and
get_flights_needing_check from db.py (replaced by catch-up query)."
```

---

### Task 9: Manual verification and first expanded seed

- [ ] **Step 1: Test a manual seed run locally (dry run with a few routes)**

Run a quick manual check to verify the search works:

```bash
cd /Users/koby/Downloads/GoWild
python3 monitor.py check SFO LAS 2026-04-11 --hour 18
```

Expected: Should return a result (FOUND or NOT FOUND) without errors.

- [ ] **Step 2: Trigger expanded seed via GitHub Actions**

```bash
gh workflow run seed.yml -f friday_date=2026-04-17
```

Monitor: `gh run list --workflow=seed.yml --limit 3`

Wait for completion (expected ~15-20 min), then:

```bash
git pull
sqlite3 gowild_monitor.db "SELECT COUNT(*) FROM flight_schedules WHERE weekend_of='2026-04-17';"
sqlite3 gowild_monitor.db "SELECT direction, COUNT(*) FROM flight_schedules WHERE weekend_of='2026-04-17' GROUP BY direction;"
```

Expected: Several hundred flights, split between outbound and return.

- [ ] **Step 3: Verify dispatch picks up checks correctly**

Trigger a manual dispatch:

```bash
gh workflow run dispatch.yml
```

Monitor: `gh run list --workflow=dispatch.yml --limit 3`

After completion:

```bash
git pull
sqlite3 -header -column gowild_monitor.db "SELECT COUNT(*) AS checks FROM availability_checks WHERE checked_at > '2026-04-10';"
sqlite3 -header -column gowild_monitor.db "SELECT * FROM check_log ORDER BY log_id DESC LIMIT 10;"
```

Expected: Some availability checks recorded, log showing batch processing.

- [ ] **Step 4: Run status and report commands**

```bash
python3 monitor.py status
python3 monitor.py report
python3 monitor.py confidence --min-weekends 1
```

Expected: Tables rendered with available data. Confidence command may say "not enough data" if only 1 weekend — pass `--min-weekends 1` to see what's there.

- [ ] **Step 5: Final commit with any minor fixes**

If any minor issues were found during verification, fix and commit.
