"""OpenSky-based flight number enrichment.

Walks Frontier origin airports, queries OpenSky's historical
`/flights/departure` endpoint for a rolling window, and writes the
IATA-formatted flight number (e.g. ``F9 1234``) onto matching nonstop
`flight_schedules` rows.

Credentials come from the env (``OPENSKY_CLIENT_ID`` +
``OPENSKY_CLIENT_SECRET``) or, as a convenience for local dev, from
``~/Downloads/credentials.json`` with keys ``clientId`` and
``clientSecret``.

Matching rules
--------------
* **Nonstops only.** OpenSky records operated flight segments, so a
  1-stop flight in our DB corresponds to two OpenSky rows with different
  callsigns. Connection flight numbers aren't useful to surface anyway,
  and matching them robustly would require pairing segments. We skip.
* Airport codes are converted IATA↔ICAO via a small inline mapping
  (K-prefix works for US; CUN/SJD use MMUN/MMSD).
* A candidate is accepted only when (origin, destination, date, stops=0)
  match AND departure time is within ±15 min.
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import pytz

from config import PACIFIC_TZ
from db import db_session


PT = pytz.timezone(PACIFIC_TZ)

TOKEN_URL = "https://auth.opensky-network.org/auth/realms/opensky-network/protocol/openid-connect/token"
API_BASE = "https://opensky-network.org/api"

# IATA codes where ICAO ≠ "K" + IATA. Everything else we treat as US.
_ICAO_OVERRIDES = {
    "CUN": "MMUN",
    "SJD": "MMSD",
}


def iata_to_icao(iata: str) -> str:
    return _ICAO_OVERRIDES.get(iata, "K" + iata)


def icao_to_iata(icao: str) -> Optional[str]:
    if not icao or len(icao) != 4:
        return None
    # Reverse overrides
    for iata, mapped in _ICAO_OVERRIDES.items():
        if mapped == icao:
            return iata
    if icao.startswith("K"):
        return icao[1:]
    return None


def _load_credentials() -> tuple[str, str]:
    cid = os.environ.get("OPENSKY_CLIENT_ID")
    secret = os.environ.get("OPENSKY_CLIENT_SECRET")
    if cid and secret:
        return cid, secret
    # local-dev fallback
    cred_path = Path.home() / "Downloads" / "credentials.json"
    if cred_path.exists():
        with open(cred_path) as f:
            data = json.load(f)
        cid = data.get("clientId")
        secret = data.get("clientSecret")
        if cid and secret:
            return cid, secret
    raise RuntimeError(
        "OpenSky credentials missing. Set OPENSKY_CLIENT_ID / "
        "OPENSKY_CLIENT_SECRET env vars, or place "
        "~/Downloads/credentials.json with clientId/clientSecret."
    )


class OpenSkyClient:
    """Tiny OpenSky REST client with OAuth2 token caching."""

    def __init__(self, client_id: str, client_secret: str):
        self._client_id = client_id
        self._client_secret = client_secret
        self._token: Optional[str] = None
        self._token_expires_at = 0.0

    def _get_token(self) -> str:
        now = time.time()
        if self._token and now < self._token_expires_at - 30:
            return self._token
        body = urllib.parse.urlencode({
            "grant_type": "client_credentials",
            "client_id": self._client_id,
            "client_secret": self._client_secret,
        }).encode()
        req = urllib.request.Request(
            TOKEN_URL,
            data=body,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        with urllib.request.urlopen(req, timeout=20) as r:
            data = json.load(r)
        self._token = data["access_token"]
        self._token_expires_at = now + int(data.get("expires_in", 60))
        return self._token

    def departures(self, icao_airport: str, begin_ts: int, end_ts: int) -> tuple[list[dict], int]:
        """Return ``(rows, http_status)``. On 400/404 returns ``([], code)``
        instead of raising — OpenSky uses those for "window outside coverage"
        and "no flights in window", both of which are benign to us."""
        params = urllib.parse.urlencode({
            "airport": icao_airport,
            "begin": begin_ts,
            "end": end_ts,
        })
        url = f"{API_BASE}/flights/departure?{params}"
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {self._get_token()}"})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.load(r), 200
        except urllib.error.HTTPError as e:
            if e.code in (400, 404):
                return [], e.code
            raise


def callsign_to_flight_number(callsign: str) -> Optional[str]:
    """`FFT1234` → `F9 1234`. Returns None for non-Frontier or odd callsigns."""
    if not callsign:
        return None
    cs = callsign.strip()
    if not cs.startswith("FFT"):
        return None
    suffix = cs[3:]
    # Strip leading zeros / letters (some ferry callsigns look like FFTA1); keep digits only
    digits = "".join(c for c in suffix if c.isdigit())
    if not digits:
        return None
    return f"F9 {int(digits)}"


def _unenriched_airports(conn, days_forward: int) -> list[str]:
    """IATA codes that appear as origins of outbound or return schedules
    in the window [now, now + days_forward]. These are the airports we need
    to query OpenSky for to learn flight numbers."""
    now_iso = datetime.now(PT).isoformat()
    end_iso = (datetime.now(PT) + timedelta(days=days_forward)).isoformat()
    rows = conn.execute(
        """
        SELECT DISTINCT r.origin AS origin
        FROM flight_schedules fs
        JOIN routes r ON r.route_id = fs.route_id
        WHERE fs.flight_number IS NULL
          AND fs.stops = 0
          AND fs.departure_pt BETWEEN ? AND ?
        """,
        (now_iso, end_iso),
    ).fetchall()
    return [row["origin"] for row in rows]


def _match_and_update(conn, opensky_row: dict) -> int:
    """Given one OpenSky departure record, write flight_number on any
    unenriched nonstop schedule that matches. Returns rows updated (0 or 1)."""
    callsign = (opensky_row.get("callsign") or "").strip()
    flight_number = callsign_to_flight_number(callsign)
    if not flight_number:
        return 0

    dep_icao = opensky_row.get("estDepartureAirport")
    arr_icao = opensky_row.get("estArrivalAirport")
    origin = icao_to_iata(dep_icao) if dep_icao else None
    dest = icao_to_iata(arr_icao) if arr_icao else None
    if not origin or not dest:
        return 0

    first_seen = opensky_row.get("firstSeen")
    if first_seen is None:
        return 0

    # OpenSky's firstSeen is UTC unix; convert to PT for date/time matching.
    dep_pt = datetime.fromtimestamp(first_seen, tz=timezone.utc).astimezone(PT)
    dep_date = dep_pt.strftime("%Y-%m-%d")
    dep_min = dep_pt.hour * 60 + dep_pt.minute

    # Narrow candidates: nonstops on the right route + date, unenriched.
    # Pull stored dep time as fractional minutes for easy comparison.
    rows = conn.execute(
        """
        SELECT fs.schedule_id, fs.departure_pt
        FROM flight_schedules fs
        JOIN routes r ON r.route_id = fs.route_id
        WHERE r.origin = ? AND r.destination = ?
          AND fs.stops = 0
          AND fs.flight_number IS NULL
          AND substr(fs.departure_pt, 1, 10) = ?
        """,
        (origin, dest, dep_date),
    ).fetchall()
    if not rows:
        return 0

    best_sid = None
    best_diff = None
    for row in rows:
        # departure_pt looks like "2026-04-24T18:29:00-07:00"
        hh = int(row["departure_pt"][11:13])
        mm = int(row["departure_pt"][14:16])
        sched_min = hh * 60 + mm
        raw = abs(sched_min - dep_min)
        diff = min(raw, 1440 - raw)
        if diff <= 15 and (best_diff is None or diff < best_diff):
            best_sid = row["schedule_id"]
            best_diff = diff

    if best_sid is None:
        return 0

    conn.execute(
        "UPDATE flight_schedules SET flight_number = ? WHERE schedule_id = ?",
        (flight_number, best_sid),
    )
    return 1


def _propagate_flight_numbers(conn, verbose: bool = True) -> int:
    """Spread flight_number from enriched past schedules to matching future
    schedules on the same route, day-of-week, and ±15 min dep time.

    Frontier reuses the same flight_number weekly for the same slot, so
    ``F9 1234`` observed on past Fridays at ~18:45 SFO→ATL implies the
    upcoming Friday 18:45 SFO→ATL schedule is that same flight.
    """
    # Past schedules that have been enriched, one row per (route, flight_number, dep_time, stops).
    past = conn.execute(
        """
        SELECT fs.route_id, fs.flight_number, fs.departure_pt, fs.stops
        FROM flight_schedules fs
        WHERE fs.flight_number IS NOT NULL
          AND fs.stops = 0
          AND fs.departure_pt < datetime('now')
        """
    ).fetchall()

    # Unenriched future schedules that are candidates for propagation.
    future = conn.execute(
        """
        SELECT fs.schedule_id, fs.route_id, fs.departure_pt, fs.stops
        FROM flight_schedules fs
        WHERE fs.flight_number IS NULL
          AND fs.stops = 0
          AND fs.departure_pt > datetime('now')
        """
    ).fetchall()

    def _dow_and_min(iso: str) -> tuple[int, int]:
        dt = datetime.fromisoformat(iso).astimezone(PT)
        # ISO weekday: Mon=0..Sun=6; align with our other code via py_dow.
        py_dow = dt.weekday()
        return py_dow, dt.hour * 60 + dt.minute

    # Index past observations by (route_id, py_dow) → list of (minutes, flight_number)
    past_index: dict[tuple[int, int], list[tuple[int, str]]] = {}
    for row in past:
        dow, minutes = _dow_and_min(row["departure_pt"])
        past_index.setdefault((row["route_id"], dow), []).append((minutes, row["flight_number"]))

    propagated = 0
    for row in future:
        dow, minutes = _dow_and_min(row["departure_pt"])
        candidates = past_index.get((row["route_id"], dow), [])
        if not candidates:
            continue
        best_fn = None
        best_diff = None
        for past_minutes, fn in candidates:
            raw = abs(past_minutes - minutes)
            diff = min(raw, 1440 - raw)
            if diff <= 15 and (best_diff is None or diff < best_diff):
                best_fn = fn
                best_diff = diff
        if best_fn is None:
            continue
        conn.execute(
            "UPDATE flight_schedules SET flight_number = ? WHERE schedule_id = ?",
            (best_fn, row["schedule_id"]),
        )
        propagated += 1

    if verbose:
        print(f"Propagated flight numbers to {propagated} future schedules.")
    return propagated


def enrich_schedules(
    *,
    days_back: int = 14,
    days_forward: int = 60,
    verbose: bool = True,
) -> dict:
    """Hit OpenSky for each relevant airport across the recent window,
    match returned Frontier callsigns to unenriched schedules, and write
    flight_number. Returns summary stats.

    The window [now - days_back, now] covers observed Frontier operations;
    we use those observations to assign flight numbers to *future*
    schedules whose departure time matches a recurring past slot.
    """
    try:
        cid, secret = _load_credentials()
    except RuntimeError as e:
        if verbose:
            print(f"Skipping enrichment: {e}")
        return {"airports": 0, "queries": 0, "http_errors": 0, "matched": 0, "skipped": True}
    client = OpenSkyClient(cid, secret)

    with db_session() as conn:
        airports = _unenriched_airports(conn, days_forward)

    if not airports:
        if verbose:
            print("Nothing to enrich — every nonstop future schedule already has a flight_number.")
        # Still run the propagation pass cheaply, in case an enrichment from a
        # past run can be carried to a newly-seeded future schedule.
        with db_session() as conn:
            propagated = _propagate_flight_numbers(conn, verbose=verbose)
        return {"airports": 0, "queries": 0, "http_errors": 0, "matched": 0, "propagated": propagated}

    if verbose:
        print(f"Airports to query: {len(airports)}")

    now = datetime.now(tz=timezone.utc)
    # OpenSky returns data batched nightly — query through yesterday's date.
    end = int(now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp())
    begin = int((now - timedelta(days=days_back)).timestamp())

    stats = {"airports": len(airports), "queries": 0, "http_errors": 0, "matched": 0}

    # OpenSky's /flights/departure effectively requires windows ≤ ~2 days;
    # wider ranges return 404 (handled below as empty). 1-day chunks are
    # what the local spike verified working.
    CHUNK_DAYS = 1

    for iata in sorted(airports):
        icao = iata_to_icao(iata)
        t = begin
        while t < end:
            tnext = min(t + CHUNK_DAYS * 86400, end)
            try:
                rows, status = client.departures(icao, t, tnext)
                stats["queries"] += 1
            except Exception as e:
                stats["http_errors"] += 1
                if verbose:
                    print(f"  {iata}: HTTP error {type(e).__name__}: {e}")
                t = tnext
                continue

            matched_this_chunk = 0
            frontier_rows = [r for r in rows if (r.get("callsign") or "").strip().startswith("FFT")]
            if frontier_rows:
                with db_session() as conn:
                    for r in frontier_rows:
                        matched_this_chunk += _match_and_update(conn, r)

            if verbose:
                window = datetime.fromtimestamp(t, tz=timezone.utc).strftime("%m-%d")
                note = "" if status == 200 else f" [HTTP {status}]"
                print(
                    f"  {iata} from {window} ({len(rows)} total / {len(frontier_rows)} FFT): "
                    f"+{matched_this_chunk} enriched{note}"
                )
            stats["matched"] += matched_this_chunk
            t = tnext

    # Propagate flight numbers from enriched past schedules to matching future ones.
    with db_session() as conn:
        stats["propagated"] = _propagate_flight_numbers(conn, verbose=verbose)

    return stats


if __name__ == "__main__":
    stats = enrich_schedules(verbose=True)
    print(f"\nDone: {stats}")
