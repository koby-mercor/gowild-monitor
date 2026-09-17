#!/usr/bin/env python3
"""
GoWild Weekend Flight Finder

Searches for Frontier Airlines flights from Bay Area airports that fit
a weekend trip pattern:
  - Depart: Friday 6pm PT through Saturday 11am PT
  - Return: Arrive Bay Area before Monday 10am PT
  - All legs must be Frontier

Uses Google Flights data via the fast-flights scraper library.
"""

import time
import re
import sys
from datetime import datetime, timedelta
from typing import List, Optional, Tuple, Dict
from dataclasses import dataclass, field

from fast_flights import FlightData, Passengers, get_flights

# ── Configuration ──────────────────────────────────────────────────────────

BAY_AREA_AIRPORTS = ["SFO", "SJC", "OAK"]

# Frontier nonstop destinations from each Bay Area airport (as of Apr 2026)
FRONTIER_NONSTOP = {
    "SFO": ["ATL", "DEN", "DFW", "LAS", "LAX", "MCO", "MDW", "PHX", "SAN", "SLC"],
    "SJC": ["DEN", "LAS", "LAX", "PHX", "SAN"],
    "OAK": [],  # Frontier no longer serves OAK
}

# Complete Frontier Airlines destination network (as of Apr 2026)
# Source: Wikipedia, FlightConnections, Frontier press releases
# Google Flights handles intermediate routing automatically,
# so this is the list of FINAL destinations to check.
FRONTIER_CONNECTING_DESTS = [
    # Southwest / Mountain
    "AUS", "ABQ", "COS", "ELP", "TUS", "AZA", "BOI", "RNO",
    # Texas
    "SAT", "IAH", "HOU", "DFW",
    # Midwest
    "MDW", "ORD", "MCI", "STL", "MSP", "MKE", "IND", "CMH",
    "DTW", "GRR", "CLE", "CVG", "OMA", "DSM", "SDF", "OKC",
    # Southeast
    "ATL", "CLT", "RDU", "BNA", "MEM", "TYS", "CHS", "JAX",
    "MSY", "LIT", "FAY", "PNS",
    # Florida
    "MCO", "MIA", "FLL", "TPA", "RSW", "PBI",
    # Northeast
    "BOS", "EWR", "JFK", "LGA", "ISP", "PHL", "PIT", "BUF",
    "SYR", "TTN", "IAD", "DCA",
    # West Coast (non-Bay Area)
    "ONT", "SNA", "BUR", "PDX", "SMF",
    # International (GoWild may cover)
    "CUN", "SJD",
]

# Departure window: Friday 6pm PT to Saturday 11am PT
OUTBOUND_EARLIEST_HOUR = 18  # 6pm for Friday
OUTBOUND_LATEST_HOUR = 11   # 11am for Saturday

# Return: must arrive Bay Area before Monday 10am PT
RETURN_LATEST_HOUR = 10  # 10am Monday


@dataclass
class FlightOption:
    origin: str
    destination: str
    departure: str        # raw time string
    arrival: str          # raw time string
    duration: str
    stops: int
    price: str
    dep_dt: Optional[datetime] = None
    arr_dt: Optional[datetime] = None


@dataclass
class WeekendTrip:
    outbound: FlightOption
    ret: FlightOption
    net_hours: float = 0.0  # hours at destination


def parse_flight_time(time_str: str, reference_year: int = 2026) -> Optional[datetime]:
    """Parse times like '9:00 PM on Fri, Apr 10' or '12:47 AM on Sat, Apr 11'"""
    # Pattern: "H:MM AM/PM on Day, Mon DD"
    m = re.search(r'(\d{1,2}):(\d{2})\s*(AM|PM)\s+on\s+\w+,\s+(\w+)\s+(\d{1,2})', time_str)
    if not m:
        return None
    hour, minute, ampm, month_str, day = m.groups()
    hour = int(hour)
    minute = int(minute)
    day = int(day)

    if ampm == "PM" and hour != 12:
        hour += 12
    elif ampm == "AM" and hour == 12:
        hour = 0

    month_map = {
        "Jan": 1, "Feb": 2, "Mar": 3, "Apr": 4, "May": 5, "Jun": 6,
        "Jul": 7, "Aug": 8, "Sep": 9, "Oct": 10, "Nov": 11, "Dec": 12
    }
    month = month_map.get(month_str, 1)
    try:
        return datetime(reference_year, month, day, hour, minute)
    except ValueError:
        return None


def parse_duration_minutes(dur_str: str) -> int:
    """Parse '2 hr 47 min' or '5 hr' into total minutes"""
    hours = 0
    minutes = 0
    h = re.search(r'(\d+)\s*hr', dur_str)
    m = re.search(r'(\d+)\s*min', dur_str)
    if h:
        hours = int(h.group(1))
    if m:
        minutes = int(m.group(1))
    return hours * 60 + minutes


def search_flights(origin: str, dest: str, date: str, max_stops: Optional[int] = None) -> List[FlightOption]:
    """Search for Frontier flights between two airports on a date.

    Returns an empty list when the search succeeds but finds no matching
    Frontier flights. Propagates the underlying exception when the scraper
    itself fails (network error, rate limit, bad response) so callers can
    distinguish "no availability" from "search failed" and retry / record
    the error instead of silently treating a failure as zero availability.
    """
    kwargs = dict(
        flight_data=[FlightData(date=date, from_airport=origin, to_airport=dest)],
        trip="one-way",
        seat="economy",
        passengers=Passengers(adults=1),
        fetch_mode="fallback",
    )
    if max_stops is not None:
        kwargs["max_stops"] = max_stops
    result = get_flights(**kwargs)

    flights = []
    seen = set()
    for f in result.flights:
        name = str(getattr(f, 'name', '')).lower()
        if 'frontier' not in name:
            continue
        dep = getattr(f, 'departure', '')
        arr = getattr(f, 'arrival', '')
        dur = getattr(f, 'duration', '')
        key = f"{dep}|{arr}|{dur}"
        if key in seen:
            continue
        seen.add(key)

        opt = FlightOption(
            origin=origin,
            destination=dest,
            departure=dep,
            arrival=arr,
            duration=dur,
            stops=getattr(f, 'stops', 0),
            price=getattr(f, 'price', ''),
            dep_dt=parse_flight_time(dep),
            arr_dt=parse_flight_time(arr),
        )
        flights.append(opt)
    return flights


def is_valid_outbound(flight: FlightOption, fri_date: datetime) -> bool:
    """Check if flight departs Friday 6pm - Saturday 11am PT"""
    if not flight.dep_dt:
        return False
    dt = flight.dep_dt
    fri = fri_date.date()
    sat = (fri_date + timedelta(days=1)).date()

    # Friday 6pm or later
    if dt.date() == fri and dt.hour >= OUTBOUND_EARLIEST_HOUR:
        return True
    # Saturday before 11am
    if dt.date() == sat and dt.hour < OUTBOUND_LATEST_HOUR:
        return True
    return False


def is_valid_return(flight: FlightOption, mon_date: datetime) -> bool:
    """Check if flight arrives at Bay Area before Monday 10am PT"""
    if not flight.arr_dt:
        return False
    dt = flight.arr_dt
    mon = mon_date.date()

    # Must arrive before Monday 10am
    if dt.date() < mon:
        return True
    if dt.date() == mon and dt.hour < RETURN_LATEST_HOUR:
        return True
    return False


def find_weekend_trips(friday_date: str, include_connections: bool = False, max_stops: Optional[int] = None) -> List[WeekendTrip]:
    """
    Find all viable weekend trips for a given Friday date.
    friday_date: 'YYYY-MM-DD' format
    """
    fri_dt = datetime.strptime(friday_date, "%Y-%m-%d")
    sat_dt = fri_dt + timedelta(days=1)
    sun_dt = fri_dt + timedelta(days=2)
    mon_dt = fri_dt + timedelta(days=3)
    fri_str = fri_dt.strftime("%Y-%m-%d")
    sat_str = sat_dt.strftime("%Y-%m-%d")
    sun_str = sun_dt.strftime("%Y-%m-%d")
    mon_str = mon_dt.strftime("%Y-%m-%d")

    # Build destination list
    all_dests = set()
    origin_dest_pairs = []
    for origin in BAY_AREA_AIRPORTS:
        nonstops = FRONTIER_NONSTOP.get(origin, [])
        for dest in nonstops:
            all_dests.add(dest)
            origin_dest_pairs.append((origin, dest))

    if include_connections:
        for dest in FRONTIER_CONNECTING_DESTS:
            if dest not in all_dests:
                all_dests.add(dest)
                # Search connections from SFO and SJC
                for origin in ["SFO", "SJC"]:
                    origin_dest_pairs.append((origin, dest))

    total_searches = len(origin_dest_pairs) * 2  # Friday + Saturday for outbound
    print(f"\nSearching {len(origin_dest_pairs)} route pairs × 2 dates = {total_searches} outbound searches...")
    print(f"Destinations: {sorted(all_dests)}\n")

    # ── Phase 1: Search outbound flights ──
    valid_outbounds: Dict[str, List[FlightOption]] = {}  # dest -> [flights]
    search_count = 0

    for origin, dest in origin_dest_pairs:
        for date_str in [fri_str, sat_str]:
            search_count += 1
            day_label = "Fri" if date_str == fri_str else "Sat"
            sys.stdout.write(f"\r  [{search_count}/{total_searches}] {origin}->{dest} {day_label}...          ")
            sys.stdout.flush()

            try:
                flights = search_flights(origin, dest, date_str, max_stops=max_stops)
            except Exception as e:
                print(f"\n  ! Search failed for {origin}->{dest} {date_str}: {e}")
                continue
            for f in flights:
                if is_valid_outbound(f, fri_dt):
                    if dest not in valid_outbounds:
                        valid_outbounds[dest] = []
                    valid_outbounds[dest].append(f)

            time.sleep(0.5)  # Rate limit

    print(f"\r  Found valid outbound flights to {len(valid_outbounds)} destinations.          ")

    if not valid_outbounds:
        return []

    # ── Phase 2: Search return flights for valid destinations ──
    matched_dests = sorted(valid_outbounds.keys())
    return_searches = len(matched_dests) * 2 * len(BAY_AREA_AIRPORTS)  # Sun + Mon × origins
    print(f"\nSearching returns for {len(matched_dests)} destinations × {len(BAY_AREA_AIRPORTS)} Bay Area airports × 2 dates = up to {return_searches} searches...")

    trips: List[WeekendTrip] = []
    search_count = 0

    for dest in matched_dests:
        for ret_origin_airport in BAY_AREA_AIRPORTS:
            if not FRONTIER_NONSTOP.get(ret_origin_airport, []) and not include_connections:
                continue
            for date_str in [sun_str, mon_str]:
                search_count += 1
                day_label = "Sun" if date_str == sun_str else "Mon"
                sys.stdout.write(f"\r  [{search_count}] {dest}->{ret_origin_airport} {day_label}...          ")
                sys.stdout.flush()

                try:
                    return_flights = search_flights(dest, ret_origin_airport, date_str, max_stops=max_stops)
                except Exception as e:
                    print(f"\n  ! Search failed for {dest}->{ret_origin_airport} {date_str}: {e}")
                    continue
                for ret in return_flights:
                    if is_valid_return(ret, mon_dt):
                        # Match with outbound flights
                        for out in valid_outbounds[dest]:
                            if out.arr_dt and ret.dep_dt and ret.dep_dt > out.arr_dt:
                                net = (ret.dep_dt - out.arr_dt).total_seconds() / 3600
                                trip = WeekendTrip(
                                    outbound=out,
                                    ret=ret,
                                    net_hours=net,
                                )
                                trips.append(trip)

                time.sleep(0.5)

    print(f"\r  Found {len(trips)} viable round-trip options.                    ")
    return trips


def format_results(trips: List[WeekendTrip], friday_date: str):
    """Pretty-print the results grouped by destination."""
    if not trips:
        print("\n❌ No viable weekend trips found for this weekend.")
        return

    # Group by destination
    by_dest: Dict[str, List[WeekendTrip]] = {}
    for t in trips:
        dest = t.outbound.destination
        if dest not in by_dest:
            by_dest[dest] = []
        by_dest[dest].append(t)

    # Sort by net hours (most time at destination first)
    for dest in by_dest:
        by_dest[dest].sort(key=lambda t: -t.net_hours)

    print(f"\n{'='*80}")
    print(f"  GOWILD WEEKEND FLIGHTS — Weekend of {friday_date}")
    print(f"  Departure: Fri 6pm – Sat 11am PT from SFO/SJC/OAK")
    print(f"  Return: Before Mon 10am PT to SFO/SJC/OAK")
    print(f"{'='*80}")

    for dest in sorted(by_dest.keys()):
        dest_trips = by_dest[dest]
        best = dest_trips[0]
        print(f"\n{'─'*80}")
        print(f"  📍 {dest} — {len(dest_trips)} option(s), best: {best.net_hours:.1f}h at destination")
        print(f"{'─'*80}")

        # Show top 3 options per destination
        for i, t in enumerate(dest_trips[:3]):
            out = t.outbound
            ret = t.ret
            out_dur = parse_duration_minutes(out.duration)
            ret_dur = parse_duration_minutes(ret.duration)
            layover_out = f" ({out.stops} stop)" if out.stops > 0 else " (nonstop)"
            layover_ret = f" ({ret.stops} stop)" if ret.stops > 0 else " (nonstop)"

            print(f"\n  Option {i+1}:")
            print(f"    ✈️  OUT: {out.origin} → {out.destination}")
            print(f"        Depart:  {out.departure}")
            print(f"        Arrive:  {out.arrival}")
            print(f"        Duration: {out.duration}{layover_out}")
            print(f"    ✈️  RET: {ret.origin} → {ret.destination}")
            print(f"        Depart:  {ret.departure}")
            print(f"        Arrive:  {ret.arrival}")
            print(f"        Duration: {ret.duration}{layover_ret}")
            print(f"    ⏱️  Net time at {dest}: {t.net_hours:.1f} hours ({t.net_hours/24:.1f} days)")
            print(f"    💰  Outbound: {out.price} | Return: {ret.price}")

    # Summary
    all_dests = sorted(by_dest.keys())
    print(f"\n{'='*80}")
    print(f"  SUMMARY: {len(all_dests)} viable destinations")
    print(f"  {', '.join(all_dests)}")
    print(f"{'='*80}")
    print(f"\n  Note: Prices shown are regular fares. GoWild pass price is ~$15/segment (taxes only).")
    print(f"  GoWild availability is first-come-first-served and may differ from what's shown here.")
    print(f"  Check flyfrontier.com logged into your GoWild account for real-time availability.\n")


def main():
    # Calculate next Friday
    today = datetime.now()
    days_to_friday = (4 - today.weekday()) % 7
    if days_to_friday == 0 and today.hour >= 18:
        days_to_friday = 7  # Already past Friday 6pm, use next week
    next_friday = today + timedelta(days=days_to_friday)
    friday_date = next_friday.strftime("%Y-%m-%d")

    # Allow override via command line
    if len(sys.argv) > 1:
        friday_date = sys.argv[1]

    include_connections = "--connections" in sys.argv

    # Parse --max-stops N
    max_stops = None
    for i, arg in enumerate(sys.argv):
        if arg == "--max-stops" and i + 1 < len(sys.argv):
            max_stops = int(sys.argv[i + 1])

    stops_label = f", max {max_stops} stops" if max_stops is not None else ""
    print(f"🔍 GoWild Weekend Flight Finder")
    print(f"   Weekend of: {friday_date} (Friday)")
    print(f"   Origins: {', '.join(BAY_AREA_AIRPORTS)}")
    print(f"   Mode: {'Nonstop + Connections' if include_connections else 'Nonstop routes only'}{stops_label}")
    print(f"   (Pass --connections to include connecting flights via DEN hub)")
    print(f"   (Pass --max-stops N to allow up to N connections)")

    trips = find_weekend_trips(friday_date, include_connections=include_connections, max_stops=max_stops)
    format_results(trips, friday_date)


if __name__ == "__main__":
    main()
