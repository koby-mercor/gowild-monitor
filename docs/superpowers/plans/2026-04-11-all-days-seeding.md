# All-Days Seeding Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Convert GoWild monitor from weekend-only (Fri-Mon) to full-week coverage (Mon-Sun) by renaming `weekend_of` to `week_of` and re-anchoring the seeder on Mondays.

**Architecture:** The seeder, CLI, and seed workflow all anchor on Fridays and use a `weekend_of` column. We rename that column to `week_of`, change the anchor from Friday to Monday, and update all references across 7 files + 1 HTML template. The dispatcher/checker are already day-agnostic and need no changes.

**Tech Stack:** Python 3, SQLite, Flask, Typer, Rich, GitHub Actions

---

### Task 1: Migrate DB schema — rename `weekend_of` to `week_of`

**Files:**
- Modify: `db.py:10-68` (SCHEMA_SQL, insert_flight_schedule)
- Modify: `tests/conftest.py:30-56` (seeded_db fixture)
- Test: `tests/test_db.py`

- [ ] **Step 1: Update SCHEMA_SQL in db.py**

In `db.py`, replace `weekend_of` with `week_of` in the schema and index:

```python
# In SCHEMA_SQL, the flight_schedules CREATE TABLE:
    week_of         TEXT NOT NULL,
# (was: weekend_of      TEXT NOT NULL,)

# The index:
CREATE INDEX IF NOT EXISTS idx_schedules_week
    ON flight_schedules(week_of);
# (was: idx_schedules_weekend on weekend_of)
```

- [ ] **Step 2: Update `insert_flight_schedule()` in db.py**

Rename the `weekend_of` parameter to `week_of` and update the SQL:

```python
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
```

- [ ] **Step 3: Add migration function in db.py**

Add a `migrate_weekend_to_week_of()` function and call it from `init_db()`:

```python
def _migrate_weekend_to_week_of(conn):
    """Rename weekend_of -> week_of and shift Friday anchors to Monday anchors."""
    # Check if old column exists
    cols = [row[1] for row in conn.execute("PRAGMA table_info(flight_schedules)").fetchall()]
    if "weekend_of" not in cols:
        return  # Already migrated or fresh DB
    conn.execute("ALTER TABLE flight_schedules RENAME COLUMN weekend_of TO week_of")
    conn.execute("UPDATE flight_schedules SET week_of = date(week_of, '-4 days')")
    # Rename old index and create new one
    conn.execute("DROP INDEX IF EXISTS idx_schedules_weekend")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_schedules_week ON flight_schedules(week_of)")


def init_db(db_path=None):
    with db_session(db_path) as conn:
        conn.executescript(SCHEMA_SQL)
        _migrate_weekend_to_week_of(conn)
```

- [ ] **Step 4: Update test fixtures in tests/conftest.py**

Change all `weekend_of` references in `insert_flight_schedule()` calls to use `week_of`. The value `"2026-04-10"` (a Friday) becomes `"2026-04-06"` (the Monday of that week):

```python
        insert_flight_schedule(conn, r1, "2026-04-11T22:00:00-07:00", "2026-04-12T00:30:00-07:00",
                               150, 0, "outbound", "2026-04-06")
        insert_flight_schedule(conn, r2, "2026-04-11T23:30:00-07:00", "2026-04-12T03:00:00-07:00",
                               210, 1, "outbound", "2026-04-06")
        insert_flight_schedule(conn, r3, "2026-04-12T06:00:00-07:00", "2026-04-12T14:00:00-07:00",
                               300, 1, "outbound", "2026-04-06")
        insert_flight_schedule(conn, r4, "2026-04-11T15:00:00-07:00", "2026-04-11T17:00:00-07:00",
                               120, 0, "outbound", "2026-04-06")
        insert_flight_schedule(conn, r5, "2026-04-11T23:00:00-07:00", "2026-04-12T00:30:00-07:00",
                               90, 0, "outbound", "2026-04-06")
```

Also update the `test_catchup_query_includes_return_flights` test:

```python
        insert_flight_schedule(conn, r, "2026-04-11T23:00:00-07:00", "2026-04-12T01:00:00-07:00",
                               120, 0, "return", "2026-04-06")
```

- [ ] **Step 5: Run tests to verify migration doesn't break anything**

Run: `cd /Users/koby/Downloads/GoWild && python -m pytest tests/test_db.py -v`
Expected: All 3 tests PASS

- [ ] **Step 6: Commit**

```bash
git add db.py tests/conftest.py
git commit -m "refactor: rename weekend_of to week_of in schema and add migration"
```

---

### Task 2: Update seeder — rename functions and re-anchor on Monday

**Files:**
- Modify: `seeder.py` (full file)

- [ ] **Step 1: Rename `seed_weekend()` to `seed_week()`**

Rename the function and change parameter from `friday_date` to `week_start`. Update the docstring. Change `friday_date` references inside the function to `week_start`:

```python
def seed_week(week_start: str, max_stops: int = DEFAULT_MAX_STOPS) -> dict:
    """Seed flight schedules for a full week: all destinations, both directions, Mon-Sun.

    week_start: 'YYYY-MM-DD' format, must be a Monday.
    Returns: {routes_checked, flights_found, flights_inserted, errors}
    """
```

The date generation (`range(7)`) stays the same — it already covers 7 days from the start.

Update the `insert_flight_schedule()` call to pass `week_start` as the `week_of` parameter:

```python
                    sid = insert_flight_schedule(
                        conn, route_id, dep_iso, arr_iso, dur_min,
                        f.stops, direction, week_start,
                        f.departure, f.arrival,
                    )
```

- [ ] **Step 2: Rename `seed_next_n_weekends()` to `seed_next_n_weeks()`**

Change Friday-finding logic to Monday-finding:

```python
def seed_next_n_weeks(n: int = 4, max_stops: int = DEFAULT_MAX_STOPS) -> dict:
    """Seed schedules for the next N weeks."""
    if max_stops is None:
        max_stops = DEFAULT_MAX_STOPS

    today = datetime.now()
    days_to_monday = (0 - today.weekday()) % 7  # 0 = Monday
    if days_to_monday == 0 and today.hour >= 18:
        days_to_monday = 7
    next_monday = today + timedelta(days=days_to_monday)

    total_stats = {"weeks_seeded": 0, "total_inserted": 0}
    for i in range(n):
        monday = next_monday + timedelta(weeks=i)
        monday_str = monday.strftime("%Y-%m-%d")
        print(f"\nSeeding week of {monday_str}...")
        stats = seed_week(monday_str, max_stops=max_stops)
        total_stats["weeks_seeded"] += 1
        total_stats["total_inserted"] += stats["flights_inserted"]

    return total_stats
```

- [ ] **Step 3: Update the module docstring**

```python
"""Seed flight schedules by searching Google Flights for upcoming weeks."""
```

- [ ] **Step 4: Commit**

```bash
git add seeder.py
git commit -m "refactor: rename seed_weekend to seed_week, anchor on Monday"
```

---

### Task 3: Update CLI in monitor.py

**Files:**
- Modify: `monitor.py`

- [ ] **Step 1: Update the `seed` command**

Change Friday logic to Monday and update import names:

```python
@app.command()
def seed(
    start_date: str = typer.Argument(None, help="Monday date YYYY-MM-DD (default: next Monday)"),
    weeks: int = typer.Option(1, "--weeks", "-w", help="Number of weeks to seed"),
    max_stops: Optional[int] = typer.Option(None, "--max-stops", help="Max connections"),
):
    """Seed flight schedules by searching Google Flights."""
    from seeder import seed_week, seed_next_n_weeks

    if start_date is None:
        today = datetime.now()
        days_to_monday = (0 - today.weekday()) % 7
        if days_to_monday == 0 and today.hour >= 18:
            days_to_monday = 7
        start_date = (today + timedelta(days=days_to_monday)).strftime("%Y-%m-%d")

    if weeks > 1:
        stats = seed_next_n_weeks(weeks, max_stops=max_stops)
        typer.echo(f"\nSeeded {stats['weeks_seeded']} weeks, {stats['total_inserted']} new schedules.")
    else:
        typer.echo(f"Seeding week of {start_date}...")
        stats = seed_week(start_date, max_stops=max_stops)
        typer.echo(f"Routes checked: {stats['routes_checked']}, Flights found: {stats['flights_found']}, "
                    f"Inserted: {stats['flights_inserted']}, Errors: {stats['errors']}")
```

- [ ] **Step 2: Update CRON_SEED template**

Change the date expression from `+fri` to `+mon`:

```python
CRON_SEED = f'7 9 * * 1 cd {PROJECT_DIR} && {PYTHON_PATH} {PROJECT_DIR}/monitor.py seed $(date -v+mon +\\%Y-\\%m-\\%d) >> {LOG_DIR}/seed.log 2>&1'
```

- [ ] **Step 3: Commit**

```bash
git add monitor.py
git commit -m "refactor: update CLI seed command to anchor on Monday"
```

---

### Task 4: Update analysis.py — rename weekend references

**Files:**
- Modify: `analysis.py`

- [ ] **Step 1: Update `route_detail()` function**

Line 111: Change SQL column reference:
```python
        SELECT fs.departure_pt, fs.week_of, fs.stops,
```

Line 130: Update table column header:
```python
    table.add_column("Week", style="cyan")
```

Line 144: Update row value:
```python
            r["week_of"],
```

- [ ] **Step 2: Update `confidence_report()` function**

Line 216: Rename parameter:
```python
def confidence_report(min_weeks: int = 2):
```

Line 223: Update SQL:
```python
            COUNT(DISTINCT fs.week_of) AS weeks,
```

Line 234: Update HAVING clause:
```python
        HAVING COUNT(DISTINCT fs.week_of) >= ?
```

Line 235: Update ORDER BY:
```python
        ORDER BY availability_pct DESC, weeks DESC
```

Line 237: Update parameter:
```python
        (min_weeks,),
```

Line 242: Update message:
```python
        console.print(f"[yellow]Not enough data yet (need {min_weeks}+ weeks).[/yellow]")
```

Line 245: Update table title:
```python
    table = Table(title=f"Route Confidence (min {min_weeks} weeks)")
```

Line 248: Update column header:
```python
    table.add_column("Wks", justify="right")
```

Line 263: Update row value:
```python
            str(r["weeks"]),
```

- [ ] **Step 3: Update `safe_destinations()` function**

Line 274: Rename parameter:
```python
def safe_destinations(min_pct: float = 75.0, min_weeks: int = 2):
```

Line 284: Update SQL:
```python
                COUNT(DISTINCT fs.week_of) AS weeks,
```

Line 294: Update HAVING:
```python
            HAVING COUNT(DISTINCT fs.week_of) >= :min_wk
```

Lines 298-299: Update aliases:
```python
            o.pct AS out_pct, o.weeks AS out_wk, o.avg_price AS out_price,
            r.pct AS ret_pct, r.weeks AS ret_wk, r.avg_price AS ret_price,
```

Line 307: Update parameter:
```python
            {"min_wk": min_weeks, "min_pct": min_pct},
```

Line 313: Update message:
```python
        console.print("[dim]Need more weeks of data. Run 'confidence' to see current rates.[/dim]")
```

Line 316: Update title:
```python
    table = Table(title=f"Safe GoWild Destinations (>{min_pct}% availability, {min_weeks}+ weeks)")
```

Line 343: Update tip:
```python
    console.print("\n[dim]Tip: Rates improve in accuracy over more weeks of data.[/dim]")
```

- [ ] **Step 4: Update CLI commands in monitor.py that call these functions**

In `monitor.py`, the `confidence` and `safe` commands pass `min_weekends` — rename to `min_weeks`:

```python
@app.command()
def confidence(
    min_weeks: int = typer.Option(2, "--min-weeks", "-w", help="Minimum weeks of data"),
):
    """Show per-route availability confidence over time."""
    from analysis import confidence_report
    confidence_report(min_weeks)


@app.command()
def safe(
    min_pct: float = typer.Option(75.0, "--min-pct", "-p", help="Minimum availability %"),
    min_weeks: int = typer.Option(2, "--min-weeks", "-w", help="Minimum weeks of data"),
):
    """Show destinations safe for GoWild booking (reliable outbound + return)."""
    from analysis import safe_destinations
    safe_destinations(min_pct, min_weeks)
```

- [ ] **Step 5: Commit**

```bash
git add analysis.py monitor.py
git commit -m "refactor: rename weekend references to week in analysis and CLI"
```

---

### Task 5: Update webapp.py — rename in SQL queries and JSON keys

**Files:**
- Modify: `webapp.py`

- [ ] **Step 1: Update `/api/routes` endpoint**

Line 58: Update SQL:
```python
                COUNT(DISTINCT fs.week_of) AS weeks,
```

Line 86: Update dict key:
```python
                "weeks": 0,
```

Lines 90-92: Update variable:
```python
        weekends = row["weeks"] or 0
        if weekends > route_map[key]["weeks"]:
            route_map[key]["weeks"] = weekends
```

Note: Keep the local variable name `weekends` or rename it too — for clarity rename to `weeks`:
```python
        wks = row["weeks"] or 0
        if wks > route_map[key]["weeks"]:
            route_map[key]["weeks"] = wks
```

- [ ] **Step 2: Update `/api/routes/<origin>/<destination>` endpoint**

Line 120: Update SQL column:
```python
                fs.stops, fs.direction, fs.week_of,
```

Line 148: Update dict key:
```python
                "week_of": row["week_of"],
```

- [ ] **Step 3: Update `/api/destinations` endpoint**

Line 177: Update SQL:
```python
                COUNT(DISTINCT fs.week_of) AS weeks,
```

Lines 233, 246: Update dict structure:
```python
    # Structure: {dest: {origins: set, directions: {outbound: ...}, weeks: int}}
    ...
                "weeks": 0,
```

Lines 258-260: Update variables:
```python
        wks = row["weeks"] or 0
        if wks > dest_map[dest]["weeks"]:
            dest_map[dest]["weeks"] = wks
```

Lines 282-285: Update confidence function:
```python
    def _confidence(weeks, combined_pct):
        if combined_pct is not None and weeks >= 3 and combined_pct >= 75:
            return "high"
        if weeks >= 2:
            return "medium"
        return "low"
```

Line 321: Update JSON key:
```python
            "weeks": data["weeks"],
```

Line 332: Update call:
```python
            "confidence": _confidence(data["weeks"], combined_pct),
```

- [ ] **Step 4: Update `/api/stats` endpoint**

Line 355: Update SQL:
```python
                (SELECT COUNT(DISTINCT week_of) FROM flight_schedules) AS weeks_tracked,
```

Line 368: Update JSON key:
```python
        "weeks_tracked": row["weeks_tracked"],
```

- [ ] **Step 5: Commit**

```bash
git add webapp.py
git commit -m "refactor: rename weekend references to week in webapp API"
```

---

### Task 6: Update dashboard template

**Files:**
- Modify: `templates/dashboard.html`

- [ ] **Step 1: Update the detail stats card label**

Line 171: Change label text:
```html
          <div class="label">Weeks</div>
```

Line 172: Rename the element ID:
```html
          <div class="value font-mono" id="ds-weeks">--</div>
```

- [ ] **Step 2: Update JavaScript references**

Line 877: Update the data binding:
```javascript
    document.getElementById('ds-weeks').textContent = destData ? (destData.weeks || '--') : '--';
```

Line 1000: Update comment:
```javascript
    // Sort by week desc, then departure time
```

Line 1002: Update sort key:
```javascript
      var wkCmp = (b.week_of || '').localeCompare(a.week_of || '');
```

Line 1015: Update variable:
```javascript
      var wk = s.week_of || '';
```

- [ ] **Step 3: Commit**

```bash
git add templates/dashboard.html
git commit -m "refactor: rename weekend references to week in dashboard template"
```

---

### Task 7: Update seed.yml workflow

**Files:**
- Modify: `.github/workflows/seed.yml`

- [ ] **Step 1: Update workflow input and step names**

Line 10-12: Rename the input:
```yaml
    inputs:
      start_date:
        description: 'Monday date (YYYY-MM-DD)'
        required: false
```

Lines 36-41: Update the seed step:
```yaml
      - name: Seed flight schedules
        run: |
          if [ -n "${{ github.event.inputs.start_date }}" ]; then
            python monitor.py seed "${{ github.event.inputs.start_date }}" --max-stops 1
          else
            python monitor.py seed --max-stops 1
          fi
```

Line 52: Update commit message:
```yaml
          git commit -m "chore: seed weekly flight schedules [skip ci]"
```

Line 60: Update conflict message:
```yaml
            echo "Push conflict (likely concurrent dispatch), resetting to remote..."
```

- [ ] **Step 2: Update the workflow name and comments**

Line 1: Update name:
```yaml
name: GoWild Seed Weekly
```

Lines 3-4: Update comment:
```yaml
# Seed all Frontier destinations (SFO + SJC), both directions, Mon-Sun.
# Runs Monday morning PT to discover flights for the upcoming week.
```

- [ ] **Step 3: Commit**

```bash
git add .github/workflows/seed.yml
git commit -m "refactor: update seed workflow for weekly seeding"
```

---

### Task 8: Run full test suite and verify

**Files:**
- None (verification only)

- [ ] **Step 1: Run all tests**

Run: `cd /Users/koby/Downloads/GoWild && python -m pytest tests/ -v`
Expected: All tests PASS

- [ ] **Step 2: Verify CLI help text is consistent**

Run: `python monitor.py seed --help`
Expected: Shows `start_date` (not `friday_date`), mentions "Monday", `--weeks`

Run: `python monitor.py confidence --help`
Expected: Shows `--min-weeks` (not `--min-weekends`)

Run: `python monitor.py safe --help`
Expected: Shows `--min-weeks` (not `--min-weekends`)

- [ ] **Step 3: Verify dispatch still works (dry run)**

Run: `python monitor.py dispatch`
Expected: "No flights due for check." (no errors — confirms the dispatcher reads `week_of` schema correctly)

- [ ] **Step 4: Verify webapp starts and serves data**

Run: `python -c "from webapp import app; client = app.test_client(); r = client.get('/api/stats'); print(r.get_json())"`
Expected: JSON with `weeks_tracked` key (not `weekends_tracked`)

- [ ] **Step 5: Final commit if any fixups needed, then done**

```bash
# Only if fixups were needed:
git add -A
git commit -m "fix: address issues found during verification"
```
