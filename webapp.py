"""Flask web app for the GoWild flight availability monitor dashboard."""

from flask import Flask, jsonify, render_template

from db import get_connection, init_db
from config import BAY_AREA_AIRPORTS, INTERNATIONAL_DESTS

# airports.py will define: AIRPORTS = {code: (lat, lng, city), ...}
try:
    from airports import AIRPORTS as _AIRPORTS_RAW
except ImportError:
    _AIRPORTS_RAW = {}

app = Flask(__name__)

# Ensure DB schema is up-to-date (runs migration if needed)
init_db()


# ── Helpers ───────────────────────────────────────────────────────────────────

def _airports_as_dict():
    """Convert AIRPORTS to JSON-friendly dict."""
    result = {}
    for code, value in _AIRPORTS_RAW.items():
        if isinstance(value, (list, tuple)) and len(value) >= 3:
            lat, lng, city = value[0], value[1], value[2]
        elif isinstance(value, dict):
            lat = value.get("lat")
            lng = value.get("lng")
            city = value.get("city", "")
        else:
            continue
        result[code] = {"lat": lat, "lng": lng, "city": city}
    return result


def _null_safe(value):
    """Return None instead of falsy-but-not-None values from SQLite."""
    return value if value is not None else None


# ── API: /api/airports ────────────────────────────────────────────────────────

@app.route("/api/airports")
def api_airports():
    """Return all known airport coordinates and cities as JSON."""
    return jsonify(_airports_as_dict())


# ── API: /api/routes ──────────────────────────────────────────────────────────

@app.route("/api/routes")
def api_routes():
    """Return all routes with aggregated outbound/return availability stats."""
    conn = get_connection()
    try:
        rows = conn.execute(
            """
            SELECT r.origin, r.destination, r.is_nonstop, fs.direction,
                COUNT(DISTINCT fs.week_of) AS weeks,
                COUNT(*) AS total_checks,
                SUM(ac.flight_found) AS times_found,
                ROUND(100.0 * SUM(ac.flight_found) / COUNT(*), 1) AS availability_pct,
                ROUND(AVG(CASE WHEN ac.flight_found THEN ac.price_cents END) / 100.0, 0) AS avg_price
            FROM routes r
            LEFT JOIN flight_schedules fs ON r.route_id = fs.route_id
            LEFT JOIN availability_checks ac ON ac.schedule_id = fs.schedule_id
                AND ac.check_type = 'T-24h' AND ac.search_success = 1
            WHERE r.active = 1
            GROUP BY r.origin, r.destination, r.is_nonstop, fs.direction
            ORDER BY r.destination, r.origin
            """
        ).fetchall()
    finally:
        conn.close()

    # Merge outbound + return rows into single route objects keyed by (origin, destination)
    route_map = {}
    for row in rows:
        key = (row["origin"], row["destination"])
        if key not in route_map:
            route_map[key] = {
                "origin": row["origin"],
                "destination": row["destination"],
                "is_nonstop": bool(row["is_nonstop"]),
                "outbound": None,
                "return": None,
                "weeks": 0,
            }

        direction = row["direction"]
        wks = row["weeks"] or 0
        if wks > route_map[key]["weeks"]:
            route_map[key]["weeks"] = wks

        # direction may be NULL when there are no flight_schedules yet (LEFT JOIN)
        if direction in ("outbound", "return"):
            checks = row["total_checks"]
            found = _null_safe(row["times_found"])
            pct = _null_safe(row["availability_pct"])
            avg_price = _null_safe(row["avg_price"])

            route_map[key][direction] = {
                "checks": checks if checks else 0,
                "found": int(found) if found is not None else None,
                "pct": float(pct) if pct is not None else None,
                "avg_price": int(avg_price) if avg_price is not None else None,
            }

    return jsonify(list(route_map.values()))


# ── API: /api/routes/<origin>/<destination> ───────────────────────────────────

@app.route("/api/routes/<origin>/<destination>")
def api_route_detail(origin, destination):
    """Return detailed check history for a specific route, grouped by schedule."""
    conn = get_connection()
    try:
        rows = conn.execute(
            """
            SELECT fs.schedule_id, fs.departure_pt, fs.arrival_pt, fs.duration_min,
                fs.stops, fs.direction, fs.week_of,
                ac.check_type, ac.checked_at, ac.hours_before_dep,
                ac.flight_found, ac.price, ac.num_results
            FROM flight_schedules fs
            JOIN routes r ON r.route_id = fs.route_id
            LEFT JOIN availability_checks ac ON ac.schedule_id = fs.schedule_id
            WHERE r.origin = ? AND r.destination = ?
            ORDER BY fs.week_of DESC, fs.direction, fs.departure_pt
            """,
            (origin.upper(), destination.upper()),
        ).fetchall()
    finally:
        conn.close()

    # Group checks by schedule_id
    schedules = {}
    for row in rows:
        sid = row["schedule_id"]
        if sid not in schedules:
            schedules[sid] = {
                "schedule_id": sid,
                "departure_pt": row["departure_pt"],
                "arrival_pt": row["arrival_pt"],
                "duration_min": row["duration_min"],
                "stops": row["stops"],
                "direction": row["direction"],
                "week_of": row["week_of"],
                "checks": [],
            }
        # Only append if there is an actual check (LEFT JOIN may yield NULLs)
        if row["check_type"] is not None:
            schedules[sid]["checks"].append({
                "check_type": row["check_type"],
                "checked_at": row["checked_at"],
                "hours_before_dep": row["hours_before_dep"],
                "flight_found": bool(row["flight_found"]) if row["flight_found"] is not None else None,
                "price": row["price"],
                "num_results": row["num_results"],
            })

    return jsonify(list(schedules.values()))


# ── API: /api/destinations ────────────────────────────────────────────────────

@app.route("/api/destinations")
def api_destinations():
    """
    Group routes by destination (merging SFO + SJC home airports).
    Compute combined availability stats and a confidence rating.
    """
    conn = get_connection()
    try:
        rows = conn.execute(
            """
            SELECT r.origin, r.destination, r.is_nonstop, fs.direction,
                COUNT(DISTINCT fs.week_of) AS weeks,
                COUNT(*) AS total_checks,
                SUM(ac.flight_found) AS times_found,
                ROUND(100.0 * SUM(ac.flight_found) / COUNT(*), 1) AS availability_pct,
                ROUND(AVG(CASE WHEN ac.flight_found THEN ac.price_cents END) / 100.0, 0) AS avg_price
            FROM routes r
            LEFT JOIN flight_schedules fs ON r.route_id = fs.route_id
            LEFT JOIN availability_checks ac ON ac.schedule_id = fs.schedule_id
                AND ac.check_type = 'T-24h' AND ac.search_success = 1
            WHERE r.active = 1
            GROUP BY r.origin, r.destination, r.is_nonstop, fs.direction
            ORDER BY r.destination, r.origin
            """
        ).fetchall()

        # Count seeded flights per far-destination + direction
        # For outbound routes (SFO→CUN), far dest = r.destination
        # For return routes (CUN→SFO), far dest = r.origin
        flight_counts = {}
        return_flight_dests = set()
        fc_rows = conn.execute(
            """
            SELECT
                CASE WHEN fs.direction = 'return' THEN r.origin ELSE r.destination END AS far_dest,
                fs.direction, COUNT(*) AS cnt
            FROM flight_schedules fs
            JOIN routes r ON r.route_id = fs.route_id
            WHERE r.active = 1
            GROUP BY far_dest, fs.direction
            """
        ).fetchall()
        for fc in fc_rows:
            far = fc["far_dest"]
            flight_counts[far] = flight_counts.get(far, 0) + fc["cnt"]
            if fc["direction"] == "return":
                return_flight_dests.add(far)

        # Get stops info per far destination
        stops_info = {}
        stops_rows = conn.execute(
            """
            SELECT
                CASE WHEN fs.direction = 'return' THEN r.origin ELSE r.destination END AS far_dest,
                MIN(fs.stops) AS min_stops, MAX(fs.stops) AS max_stops
            FROM flight_schedules fs
            JOIN routes r ON r.route_id = fs.route_id
            WHERE r.active = 1
            GROUP BY far_dest
            """
        ).fetchall()
        for sr in stops_rows:
            stops_info[sr["far_dest"]] = {"min": sr["min_stops"], "max": sr["max_stops"]}
    finally:
        conn.close()

    # Aggregate by destination across all home airports
    # Structure: {dest: {origins: set, directions: {outbound: {checks,found,pct,avg_price}, return: ...}, weeks: int}}
    dest_map = {}

    for row in rows:
        origin = row["origin"]
        direction = row["direction"]
        # For return routes (CUN→SFO), the "far destination" is the origin
        dest = row["origin"] if direction == "return" else row["destination"]

        if dest not in dest_map:
            dest_map[dest] = {
                "destination": dest,
                "origins": set(),
                "weeks": 0,
                "_dir_accum": {
                    "outbound": {"checks": 0, "found": 0, "price_sum": 0, "price_count": 0},
                    "return":   {"checks": 0, "found": 0, "price_sum": 0, "price_count": 0},
                },
            }

        # Track home airports — for outbound, origin is home; for return, destination is home
        home = origin if direction != "return" else row["destination"]
        if home in ('SFO', 'SJC'):
            dest_map[dest]["origins"].add(home)

        wks = row["weeks"] or 0
        if wks > dest_map[dest]["weeks"]:
            dest_map[dest]["weeks"] = wks

        if direction in ("outbound", "return"):
            acc = dest_map[dest]["_dir_accum"][direction]
            acc["checks"] += row["total_checks"] or 0
            acc["found"]  += int(row["times_found"]) if row["times_found"] is not None else 0
            if row["avg_price"] is not None:
                acc["price_sum"]   += float(row["avg_price"]) * (row["total_checks"] or 1)
                acc["price_count"] += row["total_checks"] or 1

    def _build_dir_stats(acc):
        if acc["checks"] == 0:
            return {"checks": 0, "found": None, "pct": None, "avg_price": None}
        pct = round(100.0 * acc["found"] / acc["checks"], 1)
        avg_price = round(acc["price_sum"] / acc["price_count"]) if acc["price_count"] else None
        return {
            "checks": acc["checks"],
            "found": acc["found"],
            "pct": pct,
            "avg_price": int(avg_price) if avg_price is not None else None,
        }

    def _confidence(weeks, combined_pct):
        if combined_pct is not None and weeks >= 3 and combined_pct >= 75:
            return "high"
        if weeks >= 2:
            return "medium"
        return "low"

    result = []
    for dest, data in sorted(dest_map.items()):
        outbound = _build_dir_stats(data["_dir_accum"]["outbound"])
        ret      = _build_dir_stats(data["_dir_accum"]["return"])

        pcts = [v for v in [outbound["pct"], ret["pct"]] if v is not None]
        combined_pct = round(sum(pcts) / len(pcts), 1) if pcts else None

        has_return = dest in return_flight_dests
        is_intl = dest in INTERNATIONAL_DESTS
        si = stops_info.get(dest, {"min": 0, "max": 0})
        min_stops = si["min"]
        max_stops = si["max"]

        # Build stops description
        if max_stops == 0:
            stops_label = "Nonstop"
        elif min_stops == 0 and max_stops > 0:
            stops_label = "Nonstop + %d-stop" % max_stops
        elif min_stops == max_stops:
            stops_label = "%d stop" % min_stops
        else:
            stops_label = "%d-%d stops" % (min_stops, max_stops)

        # Connection hub — Frontier connections from Bay Area almost always go through DEN
        connection_hub = None
        if max_stops > 0:
            connection_hub = "via DEN"

        result.append({
            "destination": dest,
            "origins": sorted(data["origins"]),
            "weeks": data["weeks"],
            "outbound": outbound,
            "return": ret if has_return else None,
            "has_return_flights": has_return,
            "is_international": is_intl,
            "booking_window": "10 days" if is_intl else "24 hours",
            "stops_label": stops_label,
            "connection_hub": connection_hub,
            "min_stops": min_stops,
            "max_stops": max_stops,
            "combined_pct": combined_pct,
            "confidence": _confidence(data["weeks"], combined_pct),
            "flight_count": flight_counts.get(dest, 0),
        })

    return jsonify(result)


# ── API: /api/stats ───────────────────────────────────────────────────────────

@app.route("/api/stats")
def api_stats():
    """Return high-level summary statistics."""
    home_airports_sql = ", ".join(f"'{a}'" for a in BAY_AREA_AIRPORTS)
    conn = get_connection()
    try:
        row = conn.execute(
            f"""
            SELECT
                (SELECT COUNT(DISTINCT destination) FROM routes
                 WHERE origin IN ({home_airports_sql})) AS destinations,
                (SELECT COUNT(*) FROM flight_schedules) AS total_flights,
                (SELECT COUNT(*) FROM availability_checks WHERE search_success = 1) AS total_checks,
                (SELECT MAX(checked_at) FROM availability_checks) AS last_check,
                (SELECT COUNT(DISTINCT week_of) FROM flight_schedules) AS weeks_tracked,
                (SELECT CAST(julianday(MAX(checked_at)) - julianday(MIN(checked_at)) AS INTEGER)
                 FROM availability_checks WHERE search_success = 1) AS days_of_data
            """
        ).fetchone()
    finally:
        conn.close()

    return jsonify({
        "destinations": row["destinations"],
        "total_flights": row["total_flights"],
        "total_checks": row["total_checks"],
        "last_check": row["last_check"],
        "weeks_tracked": row["weeks_tracked"],
        "days_of_data": row["days_of_data"] or 0,
    })


# ── Dashboard page ────────────────────────────────────────────────────────────

@app.route("/")
def dashboard():
    """Render the main dashboard."""
    return render_template("dashboard.html")


# ── Launcher ──────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import webbrowser
    import threading

    PORT = 5001
    threading.Timer(1.0, lambda: webbrowser.open(f"http://localhost:{PORT}")).start()
    app.run(debug=True, port=PORT, use_reloader=False)
