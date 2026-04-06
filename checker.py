"""Single-flight availability checker using Google Flights scraper."""

import re
import time
from datetime import datetime
from typing import Optional

from config import MAX_RETRIES, RETRY_DELAY_SECONDS


def parse_price_cents(price_str: str) -> Optional[int]:
    if not price_str:
        return None
    digits = re.sub(r"[^0-9]", "", price_str)
    if not digits:
        return None
    return int(digits) * 100


def check_flight_availability(
    origin: str, destination: str, date_str: str,
    target_dep_hour: int = None, target_dep_minute: int = None,
    max_stops: int = None, time_tolerance_min: int = 5,
) -> dict:
    """Check if a specific Frontier flight has availability on Google Flights.

    Returns dict with: flight_found, price, price_cents, num_frontier_results,
    matched_flight (dict or None), error, duration_ms.
    """
    from gowild_search import search_flights, parse_flight_time

    result = {
        "flight_found": False,
        "price": None,
        "price_cents": None,
        "num_frontier_results": 0,
        "matched_flight": None,
        "error": None,
        "duration_ms": 0,
    }

    start = time.time()
    flights = None
    last_error = None

    for attempt in range(MAX_RETRIES + 1):
        try:
            flights = search_flights(origin, destination, date_str, max_stops=max_stops)
            last_error = None
            break
        except Exception as e:
            last_error = str(e)
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_DELAY_SECONDS)

    elapsed_ms = int((time.time() - start) * 1000)
    result["duration_ms"] = elapsed_ms

    if flights is None:
        result["error"] = last_error or "search returned None"
        return result

    result["num_frontier_results"] = len(flights)

    if not flights:
        return result

    # If no target time specified, just report whether any Frontier flight exists
    if target_dep_hour is None:
        best = flights[0]
        result["flight_found"] = True
        result["price"] = best.price
        result["price_cents"] = parse_price_cents(best.price)
        result["matched_flight"] = _flight_to_dict(best)
        return result

    # Match by departure time (with tolerance)
    for f in flights:
        if f.dep_dt is None:
            continue
        hour_diff = abs(f.dep_dt.hour - target_dep_hour)
        min_diff = abs(f.dep_dt.minute - (target_dep_minute or 0))
        total_diff = hour_diff * 60 + min_diff
        if total_diff <= time_tolerance_min:
            result["flight_found"] = True
            result["price"] = f.price
            result["price_cents"] = parse_price_cents(f.price)
            result["matched_flight"] = _flight_to_dict(f)
            return result

    # No exact time match — report general availability
    result["flight_found"] = True
    cheapest = min(flights, key=lambda f: parse_price_cents(f.price) or 999999)
    result["price"] = cheapest.price
    result["price_cents"] = parse_price_cents(cheapest.price)
    result["matched_flight"] = _flight_to_dict(cheapest)
    return result


def _flight_to_dict(f) -> dict:
    return {
        "origin": f.origin,
        "destination": f.destination,
        "departure": f.departure,
        "arrival": f.arrival,
        "duration": f.duration,
        "stops": f.stops,
        "price": f.price,
    }
