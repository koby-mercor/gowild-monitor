#!/usr/bin/env python3
"""GoWild Availability Monitor - CLI entry point."""

import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import typer

from config import PROJECT_DIR, DB_PATH, LOG_DIR

app = typer.Typer(help="GoWild flight availability monitor")

PYTHON_PATH = sys.executable
CRON_DISPATCH = f"*/15 * * * * cd {PROJECT_DIR} && {PYTHON_PATH} {PROJECT_DIR}/monitor.py dispatch >> {LOG_DIR}/cron.log 2>&1"
CRON_SEED = f'7 9 * * 1 cd {PROJECT_DIR} && {PYTHON_PATH} {PROJECT_DIR}/monitor.py seed $(date -v+fri +\\%Y-\\%m-\\%d) >> {LOG_DIR}/seed.log 2>&1'
CRON_MARKER = "# gowild-monitor"


@app.command()
def init():
    """Initialize database and directories."""
    from db import init_db
    LOG_DIR.mkdir(exist_ok=True)
    init_db()
    typer.echo(f"Database initialized at {DB_PATH}")
    typer.echo(f"Log directory: {LOG_DIR}")

    # Seed routes from config
    from db import db_session, get_or_create_route
    from config import MONITORED_ROUTES
    from gowild_search import FRONTIER_NONSTOP

    with db_session() as conn:
        count = 0
        for origin, dests in MONITORED_ROUTES.items():
            for dest in dests:
                is_nonstop = 1 if dest in FRONTIER_NONSTOP.get(origin, []) else 0
                get_or_create_route(conn, origin, dest, is_nonstop)
                count += 1
    typer.echo(f"Seeded {count} route pairs.")


@app.command()
def seed(
    friday_date: str = typer.Argument(None, help="Friday date YYYY-MM-DD (default: next Friday)"),
    weeks: int = typer.Option(1, "--weeks", "-w", help="Number of weekends to seed"),
    max_stops: Optional[int] = typer.Option(None, "--max-stops", help="Max connections"),
):
    """Seed flight schedules by searching Google Flights."""
    from seeder import seed_weekend, seed_next_n_weekends

    if friday_date is None:
        today = datetime.now()
        days_to_friday = (4 - today.weekday()) % 7
        if days_to_friday == 0 and today.hour >= 18:
            days_to_friday = 7
        friday_date = (today + timedelta(days=days_to_friday)).strftime("%Y-%m-%d")

    if weeks > 1:
        stats = seed_next_n_weekends(weeks, max_stops=max_stops)
        typer.echo(f"\nSeeded {stats['weekends_seeded']} weekends, {stats['total_inserted']} new schedules.")
    else:
        typer.echo(f"Seeding weekend of {friday_date}...")
        stats = seed_weekend(friday_date, max_stops=max_stops)
        typer.echo(f"Routes checked: {stats['routes_checked']}, Flights found: {stats['flights_found']}, "
                    f"Inserted: {stats['flights_inserted']}, Errors: {stats['errors']}")


@app.command()
def dispatch():
    """Check flights due now (T-24h or T-23h). Called by cron."""
    from scheduler import dispatch as run_dispatch
    run_dispatch()


@app.command()
def check(
    origin: str = typer.Argument(..., help="Origin airport (e.g., SFO)"),
    destination: str = typer.Argument(..., help="Destination airport (e.g., DEN)"),
    date: str = typer.Argument(..., help="Flight date YYYY-MM-DD"),
    hour: Optional[int] = typer.Option(None, "--hour", help="Target departure hour (0-23)"),
    minute: Optional[int] = typer.Option(0, "--minute", help="Target departure minute"),
):
    """Run a manual one-off availability check."""
    import json
    from checker import check_flight_availability

    typer.echo(f"Checking {origin}->{destination} on {date}...")
    result = check_flight_availability(
        origin, destination, date,
        target_dep_hour=hour, target_dep_minute=minute,
    )

    if result["error"]:
        typer.echo(f"  Error: {result['error']}")
    elif result["flight_found"]:
        typer.echo(f"  FOUND - {result['num_frontier_results']} Frontier flights")
        if result["matched_flight"]:
            f = result["matched_flight"]
            typer.echo(f"  Best match: {f['departure']} -> {f['arrival']}")
            typer.echo(f"  Duration: {f['duration']}, Stops: {f['stops']}, Price: {f['price']}")
    else:
        typer.echo(f"  NOT FOUND - No Frontier availability")

    typer.echo(f"  Search took {result['duration_ms']}ms")


@app.command()
def status():
    """Show monitor status: pending checks, recent activity."""
    from analysis import status_summary
    status_summary()


@app.command()
def report(
    check_type: str = typer.Option("T-24h", "--type", "-t", help="Check type: T-24h or T-23h"),
    min_samples: int = typer.Option(1, "--min", "-m", help="Minimum samples to include"),
):
    """Show availability rate by route."""
    from analysis import availability_rate_by_route
    availability_rate_by_route(check_type, min_samples)


@app.command()
def compare():
    """Compare T-24h vs T-23h availability changes."""
    from analysis import availability_change_t24_to_t23
    availability_change_t24_to_t23()


@app.command()
def detail(
    origin: str = typer.Argument(..., help="Origin airport"),
    destination: str = typer.Argument(..., help="Destination airport"),
):
    """Show full check history for a specific route."""
    from analysis import route_detail
    route_detail(origin, destination)


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


@app.command(name="cron-install")
def cron_install():
    """Install crontab entries for automatic monitoring."""
    LOG_DIR.mkdir(exist_ok=True)

    existing = subprocess.run(["crontab", "-l"], capture_output=True, text=True)
    current = existing.stdout if existing.returncode == 0 else ""

    if CRON_MARKER in current:
        typer.echo("Cron entries already installed. Use 'cron-remove' first to reinstall.")
        return

    new_crontab = current.rstrip() + f"""

{CRON_MARKER}
{CRON_DISPATCH}
{CRON_SEED}
"""
    proc = subprocess.run(["crontab", "-"], input=new_crontab, capture_output=True, text=True)
    if proc.returncode == 0:
        typer.echo("Cron entries installed:")
        typer.echo(f"  Dispatch: every 15 min")
        typer.echo(f"  Auto-seed: Mondays at 9:07am")
    else:
        typer.echo(f"Error installing crontab: {proc.stderr}")


@app.command(name="cron-remove")
def cron_remove():
    """Remove crontab entries."""
    existing = subprocess.run(["crontab", "-l"], capture_output=True, text=True)
    if existing.returncode != 0 or CRON_MARKER not in existing.stdout:
        typer.echo("No gowild-monitor cron entries found.")
        return

    lines = existing.stdout.split("\n")
    new_lines = []
    skip = False
    for line in lines:
        if CRON_MARKER in line:
            skip = True
            continue
        if skip and line.strip() and not line.startswith("#"):
            continue
        skip = False
        new_lines.append(line)

    new_crontab = "\n".join(new_lines).strip() + "\n"
    proc = subprocess.run(["crontab", "-"], input=new_crontab, capture_output=True, text=True)
    if proc.returncode == 0:
        typer.echo("Cron entries removed.")
    else:
        typer.echo(f"Error: {proc.stderr}")


if __name__ == "__main__":
    app()
