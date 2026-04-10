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
