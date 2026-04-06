"""Analysis and reporting for GoWild availability data."""

from rich.console import Console
from rich.table import Table

from db import get_connection

console = Console()


def availability_rate_by_route(check_type: str = "T-24h", min_samples: int = 1):
    """Show availability rate for each route."""
    conn = get_connection()
    rows = conn.execute(
        """
        SELECT r.origin, r.destination,
            COUNT(*) AS checks,
            SUM(ac.flight_found) AS found,
            ROUND(100.0 * SUM(ac.flight_found) / COUNT(*), 1) AS pct,
            ROUND(AVG(CASE WHEN ac.flight_found THEN ac.price_cents END) / 100.0, 0) AS avg_price
        FROM availability_checks ac
        JOIN flight_schedules fs ON fs.schedule_id = ac.schedule_id
        JOIN routes r ON r.route_id = fs.route_id
        WHERE ac.check_type = ? AND ac.search_success = 1
        GROUP BY r.origin, r.destination
        HAVING COUNT(*) >= ?
        ORDER BY pct DESC, checks DESC
        """,
        (check_type, min_samples),
    ).fetchall()
    conn.close()

    if not rows:
        console.print(f"[yellow]No {check_type} data yet.[/yellow]")
        return

    table = Table(title=f"Availability Rate ({check_type})")
    table.add_column("Route", style="cyan")
    table.add_column("Checks", justify="right")
    table.add_column("Available", justify="right")
    table.add_column("Rate", justify="right")
    table.add_column("Avg Price", justify="right")

    for r in rows:
        rate_style = "green" if r["pct"] >= 75 else ("yellow" if r["pct"] >= 50 else "red")
        avg = f"${int(r['avg_price'])}" if r["avg_price"] else "-"
        table.add_row(
            f"{r['origin']}->{r['destination']}",
            str(r["checks"]),
            str(r["found"]),
            f"[{rate_style}]{r['pct']}%[/{rate_style}]",
            avg,
        )

    console.print(table)


def availability_change_t24_to_t23():
    """Compare T-24h vs T-23h availability to see how seats change."""
    conn = get_connection()
    rows = conn.execute(
        """
        SELECT r.origin, r.destination,
            COUNT(*) AS pairs,
            SUM(CASE WHEN t24.flight_found AND t23.flight_found THEN 1 ELSE 0 END) AS still_avail,
            SUM(CASE WHEN t24.flight_found AND NOT t23.flight_found THEN 1 ELSE 0 END) AS sold_out,
            SUM(CASE WHEN NOT t24.flight_found AND t23.flight_found THEN 1 ELSE 0 END) AS appeared,
            SUM(CASE WHEN NOT t24.flight_found AND NOT t23.flight_found THEN 1 ELSE 0 END) AS never_avail
        FROM availability_checks t24
        JOIN availability_checks t23 ON t24.schedule_id = t23.schedule_id
        JOIN flight_schedules fs ON fs.schedule_id = t24.schedule_id
        JOIN routes r ON r.route_id = fs.route_id
        WHERE t24.check_type = 'T-24h' AND t23.check_type = 'T-23h'
          AND t24.search_success = 1 AND t23.search_success = 1
        GROUP BY r.origin, r.destination
        ORDER BY pairs DESC
        """,
    ).fetchall()
    conn.close()

    if not rows:
        console.print("[yellow]No paired T-24h/T-23h data yet.[/yellow]")
        return

    table = Table(title="T-24h vs T-23h Availability Change")
    table.add_column("Route", style="cyan")
    table.add_column("Pairs", justify="right")
    table.add_column("Still Avail", justify="right", style="green")
    table.add_column("Sold Out", justify="right", style="red")
    table.add_column("Appeared", justify="right", style="blue")
    table.add_column("Never Avail", justify="right", style="dim")

    for r in rows:
        table.add_row(
            f"{r['origin']}->{r['destination']}",
            str(r["pairs"]),
            str(r["still_avail"]),
            str(r["sold_out"]),
            str(r["appeared"]),
            str(r["never_avail"]),
        )

    console.print(table)


def route_detail(origin: str, destination: str):
    """Show full check history for a specific route."""
    conn = get_connection()
    rows = conn.execute(
        """
        SELECT fs.departure_pt, fs.weekend_of, fs.stops,
            ac.check_type, ac.checked_at, ac.hours_before_dep,
            ac.flight_found, ac.price, ac.num_results,
            ac.search_success, ac.error_message
        FROM availability_checks ac
        JOIN flight_schedules fs ON fs.schedule_id = ac.schedule_id
        JOIN routes r ON r.route_id = fs.route_id
        WHERE r.origin = ? AND r.destination = ?
        ORDER BY fs.departure_pt DESC, ac.check_type
        """,
        (origin, destination),
    ).fetchall()
    conn.close()

    if not rows:
        console.print(f"[yellow]No data for {origin}->{destination}.[/yellow]")
        return

    table = Table(title=f"Check History: {origin} -> {destination}")
    table.add_column("Weekend", style="cyan")
    table.add_column("Departure", style="dim")
    table.add_column("Check", justify="center")
    table.add_column("Found", justify="center")
    table.add_column("Price", justify="right")
    table.add_column("Results", justify="right")

    for r in rows:
        found_style = "green" if r["flight_found"] else "red"
        found_text = "YES" if r["flight_found"] else "NO"
        if not r["search_success"]:
            found_text = "ERR"
            found_style = "yellow"
        table.add_row(
            r["weekend_of"],
            r["departure_pt"][:16],
            r["check_type"],
            f"[{found_style}]{found_text}[/{found_style}]",
            r["price"] or "-",
            str(r["num_results"] or 0),
        )

    console.print(table)


def status_summary():
    """Show pending checks, recent activity, and error counts."""
    conn = get_connection()

    # Total schedules
    total = conn.execute("SELECT COUNT(*) AS c FROM flight_schedules").fetchone()["c"]
    checked = conn.execute(
        "SELECT COUNT(DISTINCT schedule_id) AS c FROM availability_checks WHERE search_success = 1"
    ).fetchone()["c"]

    # Recent checks
    recent = conn.execute(
        """SELECT ac.checked_at, r.origin, r.destination, ac.check_type,
                  ac.flight_found, ac.price
           FROM availability_checks ac
           JOIN flight_schedules fs ON fs.schedule_id = ac.schedule_id
           JOIN routes r ON r.route_id = fs.route_id
           ORDER BY ac.check_id DESC LIMIT 10"""
    ).fetchall()

    # Errors
    error_count = conn.execute(
        "SELECT COUNT(*) AS c FROM availability_checks WHERE search_success = 0"
    ).fetchone()["c"]

    # Upcoming checks
    upcoming = conn.execute(
        """SELECT COUNT(*) AS c FROM flight_schedules fs
           JOIN routes r ON r.route_id = fs.route_id
           WHERE r.active = 1 AND fs.direction = 'outbound'
             AND julianday(fs.departure_pt) > julianday('now')"""
    ).fetchone()["c"]

    conn.close()

    console.print(f"\n[bold]GoWild Monitor Status[/bold]")
    console.print(f"  Schedules: {total} total, {upcoming} upcoming")
    console.print(f"  Checks: {checked} flights checked, {error_count} errors")

    if recent:
        table = Table(title="Recent Checks")
        table.add_column("Time", style="dim")
        table.add_column("Route", style="cyan")
        table.add_column("Type")
        table.add_column("Found", justify="center")
        table.add_column("Price", justify="right")

        for r in recent:
            found_style = "green" if r["flight_found"] else "red"
            table.add_row(
                r["checked_at"][:16],
                f"{r['origin']}->{r['destination']}",
                r["check_type"],
                f"[{found_style}]{'YES' if r['flight_found'] else 'NO'}[/{found_style}]",
                r["price"] or "-",
            )
        console.print(table)
    else:
        console.print("  [dim]No checks recorded yet.[/dim]")
