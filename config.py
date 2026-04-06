"""Shared constants for the GoWild availability monitor."""

from pathlib import Path

# Paths
PROJECT_DIR = Path(__file__).parent
DB_PATH = PROJECT_DIR / "gowild_monitor.db"
LOCK_FILE = PROJECT_DIR / ".dispatch.lock"
LOG_DIR = PROJECT_DIR / "logs"

# Timezone
PACIFIC_TZ = "America/Los_Angeles"

# Check windows: hours before departure
CHECK_WINDOWS = {"T-24h": 24.0, "T-23h": 23.0}
CHECK_TOLERANCE_MINUTES = 10  # +/- minutes around target

# Rate limiting for Google Flights scraper
RATE_LIMIT_SECONDS = 1.0
MAX_RETRIES = 2
RETRY_DELAY_SECONDS = 3

# Airports
BAY_AREA_AIRPORTS = ["SFO", "SJC"]

# Routes to monitor: nonstop + top connecting
MONITORED_ROUTES = {
    "SFO": [
        # Nonstop
        "ATL", "DEN", "DFW", "LAS", "LAX", "MCO", "MDW", "PHX", "SAN", "SLC",
        # Top connecting by net destination time
        "ORD", "MSY", "MSP", "IAH",
    ],
    "SJC": [
        # Nonstop
        "DEN", "LAS", "LAX", "PHX", "SAN",
    ],
}

# Departure window for outbound flights
OUTBOUND_EARLIEST_HOUR = 18  # Friday 6pm PT
OUTBOUND_LATEST_HOUR = 11    # Saturday 11am PT
