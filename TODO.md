# TODO

Deliberate deferrals, with the trigger that should make each one worth doing.
Nothing here is a bug — these are known, chosen trade-offs. Items are ordered
by when they're likely to bite.

## Settled decisions (why things look odd)

### Why an external cron triggers the workflow

`.github/workflows/poll.yml` declares a `schedule:` block *and* is triggered
externally by cron-job.org via `repository_dispatch`. That looks redundant.
It isn't.

**GitHub's `schedule` trigger is throttled to roughly one run every two hours
on this repo**, regardless of the cron expression. Measured over 2026-07-26 to
07-28:

| Cron asked for | Runs delivered | Median gap | Worst gap |
|---|---|---|---|
| `*/30` (48/day) | ~9/day | 124 min | 231 min |
| `7,37` (48/day) | ~9/day | 117 min | 229 min |
| `3,13,…` (144/day) | 1 run in 5 h | — | 175 min |

Raising the requested rate six-fold produced **no additional runs**, and moving
off the contended `:00`/`:30` minutes changed nothing — so it is neither tick
frequency nor minute contention. `workflow_dispatch` and `repository_dispatch`
runs, by contrast, start within seconds.

**Why it mattered:** a train is only due for 100 minutes, so any gap longer
than that loses the journey outright. On 2026-07-28, 6 of 14 closed windows
were missed (43%), including the same 06:30–07:00 cluster two days running
(12621, 12553, 12273, 12417).

**Current setup:** cron-job.org POSTs `{"event_type":"poll"}` to
`/repos/<owner>/howsmytrain/dispatches` every ~15 min with a fine-grained PAT
(Contents: read/write). The `schedule:` block stays as a free fallback — a run
with nothing due makes zero API calls, so it costs nothing.

**Failure mode to watch:** that PAT expires. When it does, polling silently
degrades to GitHub's ~2-hourly schedule rather than stopping outright, so it
will not look broken. The health page's *Scheduler* tile and *runs today* are
what catch it. Note the expiry date somewhere.

**If this ever needs revisiting:** the honest alternative is moving the
scheduler off GitHub entirely, not tuning the cron further.

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

### `journey_date` — fix shipped, **not yet verified**. Watch this.
`app/poller.py:_extract_start_date` files each poll under RailRadar's
`startDate` (the run's departure date), and `poll_due.py` keys its dedup on
`(train_number, journey_date)` to match. Shipped in `03af7ec`, 2026-07-29
13:45 IST. Polls before that carry the arrival date and are not backfilled.

**It has never actually been exercised.** As of 2026-07-29 21:00 IST, all 5
polls since the fix are `arrival_day_offset = 0` trains, where the departure
date and the poll date are the same day — so they cannot tell the two
conventions apart. 68 of 68 stored polls still carry the poll date. The first
real test is the next **overnight** train (21 of 39 have `offset > 0`; the
early-morning cluster — 12425 arr 05:00 +1, 12565 arr 05:05 +1, 12615 arr
05:10 +2 — gets there first).

**What to check on the first offset>0 poll after the fix:**

1. Its `journey_date` should be the **day before** the poll date. If it still
   equals the poll date, `startDate` isn't landing where `_first(data,
   "startDate")` looks and the fix is silently a no-op.
2. No duplicate polls in that train's window. `poll_due._journey_date` derives
   the dedup key as `now - offset`; if the stored value doesn't match, the
   train is re-polled on every remaining tick of its 100-minute window —
   roughly 6 wasted requests each, against a 50/day budget.

```sql
-- both conventions, side by side
select p.train_number, t.arrival_day_offset, p.journey_date,
       (p.polled_at at time zone 'Asia/Kolkata')::date as polled_ist, count(*) over
       (partition by p.train_number, p.journey_date) as polls_in_bucket
from polls p join trains t using (train_number)
where t.arrival_day_offset > 0 and p.polled_at > '2026-07-29 08:15Z'
order by p.polled_at desc;
```

**Coupled to the dashboard.** `docs/index.html` answers "did today's run
report?" from `polled_at`, *not* `journey_date`, precisely because the latter
is mid-migration — keying on `journey_date == today` would drop every
overnight train from the TODAY view the moment the fix takes effect. Anything
else that buckets by day (the row sparkbars, the weekly panel) still uses
`journey_date` and will read a day early for overnight trains until the
legacy rows age out of the 30-day window.

### The due window still clips at midnight
`_window` in `scripts/poll_due.py` ends a window at 23:59 rather than letting
it wrap. With `journey_date` fixed, wrapping is now *safe* — but it needs
`_window` to return which arrival date it matched, and `_journey_date` to use
that instead of `now`, so the dedup stays aligned.

Worth doing only if the accuracy matters: the clip costs one train (20978,
arriving 23:58) a reading up to ~45 min early, and nothing else in the fleet.

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

- **The cron-job.org PAT expires.** Polling then degrades silently to GitHub's
  ~2-hourly fallback rather than failing loudly. Watch the *Scheduler* tile.
- GitHub disables scheduled workflows after **60 days without repo activity**.
  Only affects the fallback now, but worth knowing.
- The dashboard's distribution chart needs 5+ journeys and the trend line 2+
  before either renders. Expected; they fill in.
- Data lost to a collection gap is **unrecoverable** — RailRadar only reports
  currently-live runs, so there is no backfill. A day with missed windows stays
  missing.
