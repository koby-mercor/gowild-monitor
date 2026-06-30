<!-- BEGIN:ported-claude-memory -->
# Ported Claude Memory

Source: `/Users/koby/Downloads/GoWild/CLAUDE.md`

# GoWild Availability Monitor

Frontier Airlines GoWild pass availability monitor. Tracks flight availability at T-24h booking windows to predict which destinations are most likely to have seats.

## System Architecture

There are three components that work together:

### 1. GitHub repo (`koby-mercor/gowild-monitor`)

- Python codebase: scraper, scheduler, checker, analysis, Flask webapp
- `gowild_monitor.db` (SQLite) is committed to the repo and updated by CI
- Two GitHub Actions workflows:
  - `dispatch.yml` — runs `init` then `dispatch`, commits DB back. Triggered every 2h by GH cron AND every 30min by cron-job.org
  - `seed.yml` — runs `init` then `seed`, commits DB back. Runs Monday mornings (seeds Mon-Sun flights for the upcoming week)

### 2. cron-job.org (external trigger)

- Hits the GitHub API `workflow_dispatch` endpoint for `dispatch.yml` every 30 minutes
- Supplements GitHub's own cron schedule as a more frequent trigger

### 3. Webapp on Vercel (https://gowild-monitor.vercel.app)

- Flask dashboard with map visualization (`webapp.py`)
- Serves API endpoints (`/api/routes`, `/api/destinations`, `/api/stats`, etc.) and an HTML dashboard
- On Vercel: copies bundled DB to `/tmp` on cold start (read-only filesystem), auto-redeploys on each git push to main
- Locally: reads from `gowild_monitor.db` directly, run with `python3 webapp.py` on port 5001

## Key Design Decisions

- DB is committed to git so CI can read/write it without external storage
- Dispatcher uses a "catch-up" model: each run checks ALL flights past T-24h, so missed cron runs don't lose data
- `week_of` column groups flights by the Monday of their week
- Domestic flights have 24h booking window, international (CUN, SJD) have 10-day window

## Development Notes

- Code changes must work in three environments: locally, GitHub Actions CI, and Vercel serverless
- DB migrations (in `db.py`) must be safe to run on both fresh and existing DBs — check column existence before altering
- On Vercel the filesystem is read-only — the webapp copies the DB to `/tmp`
- Always run `monitor.py init` before other commands to ensure migrations have run
<!-- END:ported-claude-memory -->
