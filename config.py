"""Shared constants for the GoWild availability monitor."""

from pathlib import Path

# Paths
PROJECT_DIR = Path(__file__).parent
DB_PATH = PROJECT_DIR / "gowild_monitor.db"
LOCK_FILE = PROJECT_DIR / ".dispatch.lock"
LOG_DIR = PROJECT_DIR / "logs"

# Timezone
PACIFIC_TZ = "America/Los_Angeles"

# ── Check scheduling ─────────────────────────────────────────────────────
# After T-24h, how long is the check still worth doing?
# 6h means we check flights departing 18–24h from now.
MAX_STALENESS_HOURS = 6.0

# ── Booking windows ──────────────────────────────────────────────────────
# GoWild domestic: booking opens 24h before departure
# GoWild international: booking opens 10 days before departure
DOMESTIC_BOOKING_HOURS = 24.0
INTERNATIONAL_BOOKING_HOURS = 240.0  # 10 days

# International destinations (GoWild has different booking window)
INTERNATIONAL_DESTS = frozenset(["CUN", "SJD"])

# Rate limiting for Google Flights scraper
RATE_LIMIT_SECONDS = 1.0
MAX_RETRIES = 2
RETRY_DELAY_SECONDS = 3

# Max connections for searches (0 = nonstop only, 1 = up to 1 stop)
DEFAULT_MAX_STOPS = 1

# ── Airports ─────────────────────────────────────────────────────────────
BAY_AREA_AIRPORTS = ["SFO", "SJC"]

# ── All Frontier destinations reachable from Bay Area ────────────────────
# Nonstop + 1-stop connections. Google Flights handles intermediate routing
# automatically, so these are FINAL destinations to search.
#
# Sources: Frontier route map, FlightConnections, Wikipedia (Apr 2026)

ALL_FRONTIER_DESTS_SFO = sorted(set([
    # Nonstop from SFO
    "ATL", "DEN", "DFW", "LAS", "LAX", "MCO", "MDW", "PHX", "SAN", "SLC",
    # Connecting via DEN/LAS/PHX hubs
    "ABQ", "AUS", "BNA", "BOI", "BOS", "BUF", "BUR", "CHS", "CLE", "CLT",
    "CMH", "COS", "CUN", "CVG", "DCA", "DSM", "DTW", "ELP", "EWR", "FAY",
    "FLL", "GRR", "HOU", "IAD", "IAH", "IND", "ISP", "JAX", "JFK", "LGA",
    "LIT", "MCI", "MEM", "MIA", "MKE", "MSP", "MSY", "OKC", "OMA", "ONT",
    "ORD", "PBI", "PDX", "PHL", "PIT", "PNS", "RDU", "RNO", "RSW", "SAT",
    "SDF", "SMF", "SNA", "STL", "SJD", "SYR", "TPA", "TTN", "TUS", "TYS",
]))

ALL_FRONTIER_DESTS_SJC = sorted(set([
    # Nonstop from SJC
    "DEN", "LAS", "LAX", "PHX", "SAN",
    # Key connecting destinations (via DEN hub)
    "ATL", "AUS", "BNA", "CLT", "DFW", "FLL", "IAH", "MCO", "MIA",
    "MSP", "ORD", "SAT",
]))

MONITORED_ROUTES = {
    "SFO": ALL_FRONTIER_DESTS_SFO,
    "SJC": ALL_FRONTIER_DESTS_SJC,
}
