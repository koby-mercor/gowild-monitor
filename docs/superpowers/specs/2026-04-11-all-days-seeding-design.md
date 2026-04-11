# All-Days Seeding: Convert GoWild Monitor from Weekend-Only to Full-Week Coverage

**Date:** 2026-04-11
**Status:** Approved

## Problem

The seeder only discovers flights for Fri-Mon (weekend travel). Flights on Tue-Thu are never seeded, so the dispatcher never checks them. The system should monitor all days of the week.

## Approach

Seed once per week (Monday), covering all 7 days (Mon-Sun). Rename the `weekend_of` grouping column to `week_of` and anchor on Monday instead of Friday.

## Changes by File

### `db.py` — Schema migration

- Rename column `weekend_of` to `week_of` in `flight_schedules` table using `ALTER TABLE RENAME COLUMN`.
- Add a migration function that runs on `init_db()`:
  1. Check if `weekend_of` column exists (pre-migration state).
  2. If so, run `ALTER TABLE flight_schedules RENAME COLUMN weekend_of TO week_of`.
  3. Update existing data: convert Friday-anchored values to Monday-anchored by subtracting 4 days (`date(week_of, '-4 days')`).
- Update `SCHEMA_SQL` to use `week_of` in the CREATE TABLE and index.
- Update `insert_flight_schedule()` parameter name from `weekend_of` to `week_of`.

### `seeder.py` — Rename and re-anchor

- Rename `seed_weekend()` to `seed_week()`.
- Parameter changes: `friday_date` becomes `week_start` (a Monday in YYYY-MM-DD).
- Date generation unchanged: still 7 days from the start date (now Mon-Sun instead of Fri-Thu).
- `seed_next_n_weekends()` becomes `seed_next_n_weeks()`. Logic: find next Monday (instead of next Friday), then hop week-by-week.
- All references to `friday_date` / `weekend_of` in the insert call become `week_start` / `week_of`.

### `monitor.py` — CLI updates

- `seed` command: default date changes from "next Friday" to "next Monday". `--weeks` flag still works.
- `CRON_SEED` template: update the date expression from `+fri` to `+mon` (not currently used since GitHub Actions handles it, but keep consistent).
- Help text and echo messages: "weekend" becomes "week".

### `analysis.py` — Column reference updates

- All SQL queries and display logic: replace `weekend_of` with `week_of`.
- Update any user-facing labels from "weekend" to "week" where they appear in reports.

### `webapp.py` — Column reference updates

- All SQL queries: replace `weekend_of` with `week_of`.
- All JSON response keys: replace `weekend_of` with `week_of`.
- The `weekends` key in API responses becomes `weeks` (e.g., in `/api/routes`, `/api/destinations`, `/api/stats`).

### `seed.yml` — Workflow update

- Keep Monday 5:07pm UTC schedule (already correct).
- Update the step name and log messages from "weekend" to "week".
- The `friday_date` workflow input becomes `start_date`.

### `dispatch.yml` — No changes

The dispatcher is already day-agnostic. It checks whatever flights exist in `flight_schedules` when their T-24h window opens.

### `scheduler.py`, `checker.py`, `config.py` — No changes

These don't reference `weekend_of` or weekend-specific logic.

## Files Not Changed

- `gowild_search.py` — Google Flights scraper, no weekend concept.
- `airports.py` — Static airport data.
- `templates/dashboard.html` — Check if it references `weekend_of` or `weekends` in JS; update if so.

## Data Migration

Existing `weekend_of` values (Friday dates) are shifted to Monday dates by subtracting 4 days. Example: `2026-04-10` (Friday) becomes `2026-04-06` (Monday). This preserves the grouping semantics — all flights in a given week still share the same `week_of` value.

## What Doesn't Change

- Dispatch frequency and logic (every 2h via GitHub Actions schedule + every 30min via cron-job.org workflow_dispatch).
- T-24h / T-23h check windows and staleness cutoff.
- The checker, rate limiting, and error handling.
- International vs domestic booking window logic.
- Database structure beyond the column rename.
- The webapp dashboard functionality (just column/key renames in queries and JSON).
