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

### User-requested trains — designed, not built

Let a visitor enter any train number. If it's already tracked, serve what we
have. If not, it joins the fleet: the next poller tick reads it once, which
teaches us its schedule, and from then on it's an ordinary tracked train polled
near its arrival. Nothing is fetched at request time.

**Most of this already exists.** `_due_now` polls a train with no known
`scheduled_arrival` exactly once, then falls back to a 12 h guard so it can't
loop (`test_unknown_schedule_is_polled_to_learn_it`). `poller.py` extracts
source, destination, both times, `run_days` and `arrival_day_offset` from that
first response and upserts them. `docs/index.html` already renders the
in-between state as *Awaiting first data*.

**The cost is recurring, not per-lookup.** Each train added costs one request
per running day, forever — so the cap belongs on *fleet size*, not on requests
per day. Demand by weekday as of 2026-08-02, with every `run_days` learned:

| mon | tue | wed | thu | fri | sat | sun |
|---|---|---|---|---|---|---|
| 31 | 31 | 27 | 31 | **34** | 32 | 33 |

Against the 50/day cap, holding ~6 back for failure retries, the ceiling is
about 44 — **room for roughly 10 more daily trains, total**. A train running
one day a week costs a seventh of that. Past the ceiling, the rotating sample
above is what buys more room.

**One blocker.** The fleet is not read from the database:
`poll_due.main` takes it from `config.require_train_numbers()`, the
`TRAIN_NUMBERS` Actions variable, and `store.list_trains()` only supplies
schedule metadata for numbers already on that list. Inserting a row into
`trains` does nothing today. Needs the configured list unioned with requested
trains, capped by remaining budget.

**Shape:** a `train_requests` table with an anon INSERT policy and a
`CHECK (train_number ~ '^\d{4,5}$')`; ~15 lines in `poll_due.py` to promote
pending requests; a request box on the fleet page. No Edge Function and no API
key in the browser, because nothing is fetched at request time — which also
means spam costs rows rather than quota, since the poller decides how many to
promote.

**Undecided:** at the ceiling, reject ("fleet full") or queue? Queueing is
friendlier but requests can then sit indefinitely, so it needs an honest status
in the UI rather than a silent pending state.

### Confirm the sparse trains are worth their slot
05576 (thu only), 13066 (sat only), 05580 (tue/sun) each cost 1–2 requests a
week now, so they're nearly free — but confirm they produce usable journeys
rather than `not-started` rows before adding more of that shape.

## Correctness, when accuracy starts mattering

### ~~`journey_date` uses poll time~~ — verified working 2026-07-30
`_extract_start_date` does what it claims. First decisive evidence: 12621
(`arrival_day_offset = 2`) polled 2026-07-30 06:30 IST filed under
`journey_date = 2026-07-29`, the run's real departure date — corroborated by
position (Warangal, ~9 h out of Chennai, consistent with a 22:00 departure the
previous night). Polls before `03af7ec` still carry the arrival date and are
not backfilled, so mixed conventions live in the table until they age out.

Verifying it exposed the re-poll bug below, now fixed.

### ~~Dedup predicted the journey instead of observing it~~ — fixed
`poll_due._due_now` used to skip a train when
`(number, now - arrival_day_offset)` was already on file: a *prediction* of
which journey the poll would land under. It could not be made correct.

**Why it broke.** Twelve of 39 services are scheduled longer than 24 h, so two
of their runs are always in the air. RailRadar's live endpoint returns whichever
it considers active, which may be the later one — whose `startDate` is a date
the prediction never guessed. The key then never matched and the train was
re-polled on *every* remaining tick of its window. On 2026-07-30, 12621 spent
**7 requests on one journey** (14% of the daily budget), and the last of them
overwrote a usable `delay=20` with `delay=None`, because `daily_delays` takes
the latest poll per journey regardless of whether it carries a reading.

**The fix** keys dedup on *when we last read the train* (`store.last_polled`)
rather than on which journey the reading turned out to be: "have we read this
train since its window opened?" — both sides of which are poll times we
control. Replaying the seven real ticks against the new logic yields one call.
A failed poll still writes no row, so the window keeps retrying it.

**Verified in production 2026-08-01.** Over the first day under the new logic:
32 journeys, 32 polls, zero spent on re-polls. The 16 long-haul journeys
(`arrival_day_offset > 0`) — the shape that caused the bug — took exactly one
poll each, 12621 included. Daily spend now tracks weekday demand instead of
running ahead of it.

### ~~`daily_delays` prefers the latest poll over a usable one~~ — fixed, live 2026-07-31
A later poll with a null `delay_minutes` used to hide an earlier one that had a
number, so the whole journey reported no delay. Both twins now prefer the last
poll that actually measured something and fall back to the plain latest only
when every poll is null:

- `supabase/schema.sql` — `order by ..., (p.delay_minutes is null), p.polled_at desc`
- `app/db.py:list_journey_final_delays` — the `COALESCE(MAX(CASE WHEN ...))` twin
- covered by two cases in `tests/test_aggregate.py`

The view was pasted into the Supabase SQL editor by hand on 2026-07-31 —
PostgREST cannot run DDL and there is no DB password in `.env`, so any future
change to `supabase/schema.sql` needs the same manual step. Re-running it is
safe: the column list is unchanged, so `train_summary` and `weekly_stats`
keep working.

Predicted against all 97 stored journeys it changed exactly one, and that is
what it did: 12621's 2026-07-29 run went `null -> 20 min`, fleet-wide nulls
10 -> 9. The remaining 9 are journeys where no poll ever carried a delay,
which is the honest answer.

Note it does **not** address a journey drifting across several non-null
readings (12723 on 2026-07-31 was polled 6 times, 27→9, and the latest wins
either way). That was the re-polling, fixed by the dedup change above.

### RailRadar returns the wrong run for services longer than 24 h
Separate from the dedup, and not fixed. Polling near arrival is supposed to
catch a near-final delay, but for the 12 long-haul services the endpoint may
return the run that departed *later* and is still mid-journey — so we record a
mid-run reading of a different journey and never see the one that just arrived.
12621 on 2026-07-30 is the worked example: seven readings of the 29 Jul
departure, nothing for the run that arrived that morning.

`app/poller.py` now logs `journey=<startDate>` on every poll, so the
substitution is visible in the Actions log instead of silent. A real fix needs
the endpoint to accept a departure date (or picking the run out of a schedule
listing), so it is worth doing only if long-haul accuracy starts mattering.

### Dashboard is coupled to the `journey_date` migration
`docs/index.html` answers "did today's run report?" from `polled_at`, not
`journey_date`, because the table holds both conventions until the pre-`03af7ec`
rows age out. Keying on `journey_date == today` would drop every long-haul train
from the TODAY view. The row sparkbars and the weekly panel still bucket by
`journey_date` and read a day early for those trains meanwhile.

### The due window still clips at midnight
`_window` in `scripts/poll_due.py` ends a window at 23:59 rather than letting
it wrap. Wrapping is now *cheap* as well as safe: dedup keys on poll time, so
it no longer has to agree with anything about which date a journey belongs to —
`_window` just has to return a span that may cross midnight.

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

## Watching

### Transient RailRadar failures come in bursts
A poll can fail against a healthy pipeline: the workflow's earlier steps pass,
`record_run` writes, and `poll_train` caught a `RailRadarError`. The window
retries on the next tick and it self-heals, so no journey has been lost yet —
but each attempt spends a request.

| date | calls | failed | |
|---|---|---|---|
| 07-28 | 28 | 0 | |
| 07-29 | 27 | 1 | 12049, recovered 14 min later |
| 07-30 | 31 | 0 | |
| 07-31 | 41 | 0 | |
| 08-01 | 33 | 6 | **one burst, 17:30–18:15**, hit 15274 then 22448 |

The 08-01 cluster is the shape to watch: five consecutive ticks failing, six
attempts to land two readings that both arrived anyway at 18:30. A one-off is
noise; a second burst is a trend.

**We don't record what a failure was.** `poll_train` returns False for all of:
network error (DNS, refused, 15 s timeout), HTTP 429, any other HTTP >= 400,
a non-JSON body, and `success: false` on a 2xx. `record_run` stores one
integer, so the cause is not recoverable from the database — and the cost
isn't either, since a connection that never reached RailRadar spends no quota
while a 500 does. The 08-01 burst cost somewhere between 0 and 6 requests.

Timing does rule out both rate limits for that burst: 22 of 50 requests were
spent by 17:30, and the failing runs made one call each 15 minutes apart, so
neither the daily cap nor the 10/min burst applies. That leaves network, 5xx,
or `success: false`.

**Cheapest fix, if this recurs:** record the status code (or `network`) on
`poller_runs`, so the next burst is diagnosable without the Actions log —
which needs auth to read, the API returns 403 anonymously. Backing off rather
than retrying every tick is the follow-on: a 100-minute window currently
offers ~7 free attempts, which is exactly wrong when upstream is down.

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
