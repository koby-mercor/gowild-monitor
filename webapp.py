"""Flask web app for the GoWild flight availability monitor dashboard."""

import os
import shutil

from flask import Flask, jsonify, render_template

from db import get_connection, init_db
from config import DB_PATH, BAY_AREA_AIRPORTS, INTERNATIONAL_DESTS

# airports.py will define: AIRPORTS = {code: (lat, lng, city), ...}
try:
    from airports import AIRPORTS as _AIRPORTS_RAW
except ImportError:
    _AIRPORTS_RAW = {}

app = Flask(__name__)

# On Vercel (read-only filesystem), copy the bundled DB to /tmp so we can
# run migrations and use WAL journal mode.
if os.environ.get("VERCEL"):
    _tmp_db = "/tmp/gowild_monitor.db"
    if not os.path.exists(_tmp_db):
        shutil.copy2(str(DB_PATH), _tmp_db)
    os.environ["GOWILD_DB_PATH"] = _tmp_db
    init_db(_tmp_db)
else:
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
                COUNT(ac.check_id) AS total_checks,
                SUM(ac.flight_found) AS times_found,
                CASE WHEN COUNT(ac.check_id) > 0
                    THEN ROUND(100.0 * SUM(ac.flight_found) / COUNT(ac.check_id), 1)
                END AS availability_pct,
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
        # Fetch both the outbound route (home->far) and the reverse route (far->home)
        # so returns (stored with origin=far_dest, destination=home) are included too.
        rows = conn.execute(
            """
            SELECT fs.schedule_id, fs.departure_pt, fs.arrival_pt, fs.duration_min,
                fs.stops, fs.direction, fs.week_of, fs.flight_number,
                ac.check_type, ac.checked_at, ac.hours_before_dep,
                ac.flight_found, ac.price, ac.num_results
            FROM flight_schedules fs
            JOIN routes r ON r.route_id = fs.route_id
            LEFT JOIN availability_checks ac ON ac.schedule_id = fs.schedule_id
            WHERE (r.origin = :home AND r.destination = :far AND fs.direction = 'outbound')
               OR (r.origin = :far AND r.destination = :home AND fs.direction = 'return')
            ORDER BY fs.week_of DESC, fs.direction, fs.departure_pt
            """,
            {"home": origin.upper(), "far": destination.upper()},
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
                "flight_number": row["flight_number"],
                "checks": [],
            }
        if row["check_type"] is not None:
            schedules[sid]["checks"].append({
                "check_type": row["check_type"],
                "checked_at": row["checked_at"],
                "hours_before_dep": row["hours_before_dep"],
                "flight_found": bool(row["flight_found"]) if row["flight_found"] is not None else None,
                "price": row["price"],
                "num_results": row["num_results"],
            })

    # Annotate upcoming schedules with per-flight prediction based on past T-24h
    # checks of similar schedules on this route. Widening fallback buckets:
    #   (dow, hour, stops) → (dow, stops) → (dow) → (route-wide).
    # The narrowest bucket with ≥3 samples wins. We use successful T-24h checks only.
    _annotate_predictions(list(schedules.values()))

    return jsonify(list(schedules.values()))


def _parse_pt(iso_str):
    """Return (dow 0=Sun..6=Sat, hour, minute) from a stored departure_pt string."""
    from datetime import datetime
    try:
        dt = datetime.fromisoformat(iso_str)
    except (ValueError, TypeError):
        return None
    # %w style: Sun=0..Sat=6 (we store with tz, strftime('%w') gives the local dow)
    # datetime.weekday() is Mon=0..Sun=6; convert to match SQLite %w
    py_dow = dt.weekday()  # Mon=0..Sun=6
    dow = (py_dow + 1) % 7  # → Sun=0..Sat=6
    return dow, dt.hour, dt.minute


def _annotate_predictions(schedules):
    """Mutate `schedules` in place: add prediction_pct/samples/bucket to future ones.

    Widening-fallback buckets in priority order:
      1. flight_number (true "same flight") — requires enrichment
      2. (dow, hour, stops)
      3. (dow, stops)
      4. (dow,)
      5. route-wide
    Narrowest bucket with ≥3 samples wins; if nothing clears 3 we still
    return the narrowest non-empty bucket so the UI can be honest about
    small-N situations.
    """
    from datetime import datetime, timezone
    now_ts = datetime.now(timezone.utc).timestamp()

    by_flight_num = {}  # flight_number → [found, total]
    by_exact = {}       # (dow, hour, stops)
    by_dow_stops = {}   # (dow, stops)
    by_dow = {}         # (dow,)
    overall = [0, 0]

    def _accum(bucket_dict, key, pair):
        slot = bucket_dict.setdefault(key, [0, 0])
        slot[0] += pair[0]
        slot[1] += pair[1]

    future = []
    for s in schedules:
        parsed = _parse_pt(s["departure_pt"])
        if parsed is None:
            continue
        dow, hour, _ = parsed
        stops = s.get("stops") or 0
        fn = s.get("flight_number")
        is_future = datetime.fromisoformat(s["departure_pt"]).timestamp() > now_ts

        if is_future:
            future.append((s, dow, hour, stops, fn))
            continue

        found = 0
        total = 0
        for c in s["checks"]:
            if c["check_type"] != "T-24h":
                continue
            if c["flight_found"] is None:
                continue
            total += 1
            if c["flight_found"]:
                found += 1
        if total == 0:
            continue
        pair = (found, total)
        if fn:
            _accum(by_flight_num, fn, pair)
        _accum(by_exact, (dow, hour, stops), pair)
        _accum(by_dow_stops, (dow, stops), pair)
        _accum(by_dow, (dow,), pair)
        overall[0] += found
        overall[1] += total

    # flight_number matches are exact-identity signals — even a single past
    # check of THE same flight is stronger than 10 checks of rough-pattern
    # neighbors. Pattern-based buckets need ≥3 to escape small-sample noise.
    MIN_SAMPLES_PATTERN = 3
    MIN_SAMPLES_FLIGHT_NUM = 1

    def _lookup(dow_, hour_, stops_, fn_):
        # Flight-number bucket first, with its own lower threshold.
        if fn_:
            pair = by_flight_num.get(fn_)
            if pair and pair[1] >= MIN_SAMPLES_FLIGHT_NUM:
                return fn_, pair[0], pair[1]

        pattern_ladder = [
            ("same slot", by_exact, (dow_, hour_, stops_)),
            ("same day · stops", by_dow_stops, (dow_, stops_)),
            ("same day", by_dow, (dow_,)),
        ]
        for label, bucket_dict, key in pattern_ladder:
            pair = bucket_dict.get(key)
            if pair and pair[1] >= MIN_SAMPLES_PATTERN:
                return label, pair[0], pair[1]
        if overall[1] >= MIN_SAMPLES_PATTERN:
            return "route-wide", overall[0], overall[1]
        # Narrowest bucket even if <3 samples (honesty over coverage)
        for label, bucket_dict, key in pattern_ladder:
            pair = bucket_dict.get(key)
            if pair:
                return label, pair[0], pair[1]
        if overall[1] > 0:
            return "route-wide", overall[0], overall[1]
        return None, 0, 0

    for s, dow, hour, stops, fn in future:
        label, found, total = _lookup(dow, hour, stops, fn)
        s["prediction_bucket"] = label
        s["prediction_samples"] = total
        s["prediction_pct"] = (round(100.0 * found / total, 1) if total > 0 else None)


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
                COUNT(ac.check_id) AS total_checks,
                SUM(ac.flight_found) AS times_found,
                CASE WHEN COUNT(ac.check_id) > 0
                    THEN ROUND(100.0 * SUM(ac.flight_found) / COUNT(ac.check_id), 1)
                END AS availability_pct,
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

        # Schedule day/hour metadata per far destination (for day-of-week + time filters)
        schedule_meta = {}
        meta_rows = conn.execute(
            """
            SELECT
                CASE WHEN fs.direction = 'return' THEN r.origin ELSE r.destination END AS far_dest,
                CAST(strftime('%w', substr(fs.departure_pt, 1, 19)) AS INTEGER) AS dow,
                CAST(substr(fs.departure_pt, 12, 2) AS INTEGER) AS dep_hour
            FROM flight_schedules fs
            JOIN routes r ON r.route_id = fs.route_id
            WHERE r.active = 1
            GROUP BY far_dest, dow, dep_hour
            """
        ).fetchall()
        for mr in meta_rows:
            far = mr["far_dest"]
            if far not in schedule_meta:
                schedule_meta[far] = {"days": set(), "hours": set()}
            schedule_meta[far]["days"].add(mr["dow"])
            schedule_meta[far]["hours"].add(mr["dep_hour"])

        # Upcoming flight info per far destination
        upcoming_info = {}
        upcoming_rows = conn.execute(
            """
            SELECT
                CASE WHEN fs.direction = 'return' THEN r.origin ELSE r.destination END AS far_dest,
                COUNT(*) AS upcoming_count,
                MIN(fs.departure_pt) AS next_departure
            FROM flight_schedules fs
            JOIN routes r ON r.route_id = fs.route_id
            WHERE r.active = 1
              AND fs.departure_pt > datetime('now')
            GROUP BY far_dest
            """
        ).fetchall()
        for ur in upcoming_rows:
            upcoming_info[ur["far_dest"]] = {
                "count": ur["upcoming_count"],
                "next_departure": ur["next_departure"],
            }
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
            "departure_days": sorted(schedule_meta.get(dest, {}).get("days", set())),
            "departure_hours": sorted(schedule_meta.get(dest, {}).get("hours", set())),
            "has_upcoming_flights": dest in upcoming_info,
            "upcoming_flight_count": upcoming_info.get(dest, {}).get("count", 0),
            "next_departure": upcoming_info.get(dest, {}).get("next_departure"),
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
