# TODO

Deliberate deferrals, with the trigger that should make each one worth doing.
Nothing here is a bug — these are known, chosen trade-offs. Items are ordered
by when they're likely to bite.

## Decide with real data (revisit ~2 weeks after collection starts)

### Rotating sample budget for daily trains
Skip a fraction of daily trains on the busiest weekday, once the per-day count
approaches the 50-request cap.

**Why not yet:** peak is currently 31/50, and 24 trains are still counted as
daily only because their `run_days` haven't been learned. The real peak will
drop on its own.

**The constraint that matters:** skips must *rotate*. Always dropping the same
train on the same weekday permanently excludes that weekday from its average,
and day-of-week effects in delays are real (weekend loads, freight pathing,
Monday knock-on). That produces a confidently wrong number nothing downstream
can detect. Random or drifting skips lose ~32% precision at 4 days/week
(error scales 1/√n) with no bias.

**Shape:** `sample_days_per_week` column, stable per-train offset, pattern
shifts one day per week so every train is eventually sampled on every weekday.
Sparse trains exempt. ~20 lines in `scripts/poll_due.py`.

**Cheaper lever to try first:** drop trains whose distribution has converged
(a Vande Bharat that's on time every day teaches nothing) rather than sampling
all of them. Needs no code.

### Confirm the sparse trains are worth their slot
05576 (thu only), 13066 (sat only), 05580 (tue/sun) each cost 1–2 requests a
week now, so they're nearly free — but confirm they produce usable journeys
rather than `not-started` rows before adding more of that shape.

## Correctness, when accuracy starts mattering

### `journey_date` uses poll time, not the train's start date
A train running past midnight has its journey split across two dates. Polls
land near *arrival*, so for the 7 of 9 known trains with
`arrival_day_offset=1`, the recorded date is the arrival day while the run
belongs to the departure day.

RailRadar already returns `startDate` in every payload — `app/db.py:114` and
`app/store.py` would use it instead of `today_ist()`. Do this before comparing
delays across dates seriously, or any day-of-week analysis will be off by one
for overnight trains.

### Monthly full refresh
`run_days` is refreshed on every poll, so timetable changes propagate — but
only for trains still being polled. If a train's run days change such that we
stop polling it, we'd never learn the update and it would go dark permanently.

Not a live risk at this scale. Fix is a monthly `--all` run (33 requests, one
day's budget).

## Divergences to clean up

### Aggregation exists twice
`app/aggregate.py` (Python/SQLite) and `supabase/schema.sql` (views) implement
the same logic — daily final delay, weekly rollup, train summary. They agree
today. They will drift.

The SQL views are what the live dashboard reads; the Python is what the tests
cover. Either port the tests to run against Postgres, or delete the Python path
once local SQLite stops being useful.

### Supabase credentials duplicated
`docs/index.html` and `docs/train.html` each carry the URL and publishable key.
Two places to edit on key rotation. Pull into a shared `docs/config.js` when a
third page appears.

### SQLite path doesn't track schedules
`trains` in SQLite has no `scheduled_arrival`, `run_days`, or
`arrival_day_offset`, so `poll_due.py` run locally treats every train as
unknown-schedule and polls everything. Fine for a bootstrap/manual run;
misleading if you expect local to mirror production. The due-window and
run-day logic are unit-tested as pure functions, so this isn't a test gap.

### Local dashboards read a store that's no longer canonical
`docs/local.html` and `docs/db.html` read `/api/*` off local SQLite, which now
only holds the pre-Supabase polls. They still work; they just don't show the
live data. Delete them, or point them at Supabase, once you stop using the
FastAPI server.

### `crontab.example` predates the current design
Refers to `poll_once.py` on a flat cron schedule. Superseded by
`.github/workflows/poll.yml` + `poll_due.py`. Delete it or rewrite it for
anyone running this without GitHub Actions.

### `scripts/weekly_report.py` is untouched and SQLite-only
Never run against the current setup, unaware of Supabase. Either port it to
read the `weekly_stats` view or drop it.

## Storage

### `raw_json` bloat in SQLite
~100KB per row, almost entirely the `route` array. The Supabase schema drops it
deliberately; the local SQLite path still stores it. Only matters if you resume
heavy local polling — strip `route` before insert if so.

Note the tension: `route` is where `scheduled_arrival` and `arrival_day_offset`
come from, and `train.runDays` too. Strip the array, keep the derived fields.

## Marked in code

- `app/poller.py:158` — fixed 7s delay between API calls rather than a token
  bucket. Swap if the burst cap changes or a run needs >40 trains.

## Operational

- GitHub disables scheduled workflows after **60 days without repo activity**.
  Any commit resets the clock.
- Scheduled runs don't fire for ~15 minutes after the first push.
- The dashboard's distribution chart needs 5+ journeys and the trend line 2+
  before either renders. Expected; they fill in.
