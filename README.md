# HowsMyTrain

Tracks live delay data for a handful of Indian trains over time, using the
[RailRadar](https://railradar.in) API, and shows day/week efficiency trends
in a small local dashboard.

Because you're starting data collection today, trends will fill in as polls
accumulate — there's no historical backfill. Give it a few days to a week
before the weekly view is meaningful.

## How it works

```
scripts/poll_once.py  --(cron, periodic)-->  RailRadar API
                                                    |
                                                    v
                                          data/trains.db (SQLite)
                                                    |
                                                    v
                              app/main.py (FastAPI) --serves--> static/ dashboard
                                                    |
scripts/weekly_report.py <---------------------------
   (writes reports/weekly-YYYY-Www.md)
```

- **`scripts/poll_once.py`** — polls every train number in `TRAIN_NUMBERS`
  once and stores a snapshot row per train. Run this periodically via cron
  (see `crontab.example`) so data accumulates while you're not watching.
- **`app/main.py`** — a FastAPI server exposing `/api/trains`,
  `/api/stats/daily`, `/api/stats/weekly`, `/api/polls`, and serving the
  dashboard in `static/`.
- **`static/`** — the dashboard itself (plain HTML/CSS/JS + Chart.js from a
  CDN). This is the part you'll likely want to extend — it's intentionally
  a simple starting point, not a finished product.
- **`scripts/weekly_report.py`** — writes a markdown delay summary for the
  past week to `reports/`.

Data model: every poll is stored verbatim (see `polls` table in
`app/db.py`), and "how late was train X on day Y" is derived as the *last*
poll recorded for that train that day (`app/aggregate.py`). This is a
reasonable approximation for a first version — refine it if you find
RailRadar's `status` field lets you detect "arrived at destination" more
precisely.

## Setup

```bash
cd howsmytrain
python3 -m venv .venv
source .venv/bin/activate        # PowerShell: .venv\Scripts\Activate.ps1
                                 # cmd.exe:    .venv\Scripts\activate.bat
pip install -r requirements.txt

cp .env.example .env
# then edit .env:
#   RAILRADAR_API_KEY=rr_live_...   (from https://railradar.in/developers)
#   TRAIN_NUMBERS=12301,12951        (comma-separated, no spaces needed)
```

Track one direction per route: if 12583 (LKO→ANVT) is in the list, leave out
12584 (ANVT→LKO). The return leg is a second request per day for the same
corridor. Note also that a number not in RailRadar's registry has no schedule
to learn, so it never leaves the "unknown schedule" branch of
`scripts/poll_due.py` and is polled on *every* run — a typo costs ~96 requests
a day against a 50/day cap. Verify new numbers before adding them.

## Running it

**One-off poll** (useful to sanity-check your API key/train numbers before
setting up cron):

```bash
python scripts/poll_once.py
```

**Dashboard server:**

```bash
python scripts/run_server.py
# then open http://127.0.0.1:8000
```

**Weekly report:**

```bash
python scripts/weekly_report.py
```

## Scheduling background polling

**RailRadar's free tier caps at 50 requests/day**, and each train costs one
request per poll. `poll_once.py` polls *every* configured train, so it burns
N requests per run — with 9 trains that's only 5 runs a day.

`scripts/poll_due.py` exists to avoid that. Because the daily stat only needs
one reading per train per day (taken near arrival, when the delay is final),
it polls only the trains whose scheduled arrival is close to now, and makes
zero API calls when nothing is due. That's one request per train per day.

```bash
python scripts/poll_due.py            # poll what's due right now
python scripts/poll_due.py --dry-run  # show what would be polled, no API calls
python scripts/poll_due.py --all      # poll everything (bootstrap/manual)
```

Arrival times are learned automatically: every poll refreshes
`trains.scheduled_arrival` from the route in RailRadar's response, so the
schedule tracks timetable changes instead of going stale.

## Hosted setup (GitHub Actions + Supabase)

Runs the poller on schedule without your laptop being awake, and publishes
the dashboard to GitHub Pages. Everything below is free tier.

**The git repo must be initialised in *this* directory**, not its parent —
GitHub only reads `.github/workflows/` at the repo root.

1. **Supabase** — create a project, open the SQL editor, paste and run
   `supabase/schema.sql`. It creates the tables plus the `daily_delays`,
   `weekly_stats` and `train_summary` views the dashboard reads, and enables
   row-level security so the public anon key can only read.

2. **GitHub** — push this directory, then under Settings → Secrets and
   variables → Actions add:

   | Kind | Name | Value |
   |---|---|---|
   | Secret | `RAILRADAR_API_KEY` | your RailRadar key |
   | Secret | `SUPABASE_URL` | `https://<project>.supabase.co` |
   | Secret | `SUPABASE_SERVICE_KEY` | Supabase service_role key |
   | Variable | `TRAIN_NUMBERS` | `12301,12951,…` |

3. **Bootstrap** — run the "Poll train delays" workflow manually with
   *all* checked. This learns every train's arrival time; afterwards each
   train is polled once near its own arrival.

3b. **External trigger (needed — GitHub's cron is not reliable enough).**
   Measured over two days, GitHub delivered roughly one scheduled run every
   two hours regardless of the cron expression, losing ~40% of journeys
   because a train is only due for 100 minutes. Manually-dispatched runs
   start within seconds, so the fix is to trigger from outside.

   Create a **fine-grained personal access token** scoped to this repo with
   *Contents: Read and write*, then point any free cron service
   (cron-job.org, EasyCron, a cheap VPS) at it every 15 minutes:

   ```
   POST https://api.github.com/repos/<owner>/howsmytrain/dispatches
   Authorization: Bearer <token>
   Accept: application/vnd.github+json
   Content-Type: application/json

   {"event_type": "poll"}
   ```

   A 204 means accepted. The workflow's `schedule:` block stays as a
   fallback — it costs nothing, since a run with nothing due makes zero
   API calls.

4. **Pages** — Settings → Pages → deploy from branch, folder `/docs`. Then
   fill in `SUPABASE_URL` and `SUPABASE_ANON_KEY` at the top of the script in
   `docs/index.html` and commit. The anon key is public by design; RLS keeps
   it read-only.

### Dashboards

- `docs/index.html` — reads Supabase directly. Served by GitHub Pages, works
  as a plain file too. No backend needed.
- `docs/local.html` — the original dashboard, reads the `/api/*` routes off
  local SQLite. Needs `run_server.py`.
- `docs/db.html` — raw `polls` table view, also via `/api/*`.

`app/main.py` serves the same `docs/` directory, so local and hosted never
drift apart.

## Extending the dashboard

`static/index.html` / `app.js` / `styles.css` are a deliberately minimal
starting point: a KPI row, one weekly trend chart, and a recent-polls table.
Everything reads from the JSON API in `app/main.py`, so you can add new
views without touching the backend, or add new `/api/...` endpoints backed
by `app/aggregate.py` as you need new aggregations (e.g. per-station
punctuality, day-of-week patterns, etc.).

Color roles are defined as CSS custom properties at the top of
`static/styles.css` (categorical series slots, status colors, ink/gridline
tokens) — reuse those roles rather than introducing new hex values, so new
charts stay visually consistent.

## Tests

```bash
pytest
```

## Known trade-offs

Deferred work and deliberate simplifications are tracked in [TODO.md](TODO.md),
each with the trigger that makes it worth doing.

## Notes & limitations

- RailRadar is a third-party service (not official Indian Railways), free
  tier only, so treat delay figures as indicative rather than authoritative.
- If the API is briefly unavailable or rate-limited, `poll_once.py` logs
  the failure and moves on to the next train rather than crashing — you'll
  just have a gap in that day's data.
- `journey_date` groups polls by the local calendar date at poll time. A
  train running past midnight could have its journey split across two
  dates — noted here so it doesn't surprise you later.
