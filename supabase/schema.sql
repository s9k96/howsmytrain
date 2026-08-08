-- HowsMyTrain -- Supabase/Postgres schema.
-- Paste into the Supabase SQL editor and run once.
--
-- Differences from the local SQLite schema (app/db.py), on purpose:
--   * no raw_json column. RailRadar's payload is ~100KB/poll, almost all of
--     it the `route` array. We only want (train, date, delay), so the one
--     piece of route we DO need -- the scheduled arrival at destination --
--     is denormalised onto `trains` and refreshed on every poll.
--   * aggregation lives in views here instead of Python, so the static
--     dashboard can read it straight from PostgREST with no backend.

create table if not exists trains (
    train_number        text primary key,
    name                text,
    source_code         text,
    destination_code    text,
    scheduled_departure time,          -- IST local time of departure from source
    scheduled_arrival   time,          -- IST local time of arrival at destination
    run_days            text[],        -- DEPARTURE days at source: {tue,thu,fri,sun}
    arrival_day_offset  integer not null default 0,  -- days from departure to arrival
    updated_at          timestamptz not null default now()
);

-- Already ran an earlier version of this file? These are all additive.
alter table trains add column if not exists run_days text[];
alter table trains add column if not exists arrival_day_offset integer not null default 0;
alter table trains add column if not exists source_code text;
alter table trains add column if not exists scheduled_departure time;
-- RailRadar's own class for the service -- 'Rajdhani Express', 'Shatabdi
-- Express', 'Superfast Express', 'Mail/Express', 'Train on Demand'. Taken from
-- the payload rather than guessed from the name, which misreads both ways:
-- 15274 Satyagrah looks like a plain express and is Mail/Express, the AC
-- Double Deckers are Superfast Express. Fills in as each train is next polled,
-- so it is null for a train not seen since this shipped.
alter table trains add column if not exists train_type text;

create table if not exists polls (
    id                        bigserial primary key,
    train_number              text not null references trains (train_number),
    journey_date              date not null,
    polled_at                 timestamptz not null default now(),
    status                    text,
    delay_minutes             integer,
    current_station_code      text,
    current_station_status    text,
    railradar_last_updated_at timestamptz
);

-- How late the train is projected to reach its DESTINATION, from the last
-- entry of the payload's route. `delay_minutes` beside it is a different
-- quantity: how late it was at the station it had reached when we looked.
-- Those agree only for a train read near the end of its run. 12584 was stored
-- at 58 and 59 minutes while the same payloads projected it into Lucknow 64
-- and 72 minutes late. Both are kept -- the old one is a fact we observed, and
-- overwriting history with a differently-defined number would be worse than
-- the gap it fills.
alter table polls add column if not exists arrival_delay_minutes integer;

create index if not exists idx_polls_train_date on polls (train_number, journey_date);
create index if not exists idx_polls_polled_at  on polls (polled_at desc);

-- When this train entered the fleet. Coverage is meaningless without it: the
-- health page would otherwise count a train as "expected" on days before it
-- was ever tracked, so adding trains silently tanks the reported percentage.
--
-- Added nullable and backfilled rather than defaulted, or every existing row
-- would claim it was created the day this migration ran. Rows predating the
-- column get one shared floor: the earliest poll on record. Their real
-- creation time is not recoverable, and the per-train alternative (that
-- train's first poll) is wrong in the direction that matters -- a train
-- tracked for days before its first successful poll would have those misses
-- erased, which is precisely what this metric exists to surface. A floor can
-- only over-report misses, never hide one.
--
-- Must stay below `polls`: the backfill reads it, so on a fresh project this
-- block cannot sit with the other `alter table trains` lines above.
alter table trains add column if not exists created_at timestamptz;
update trains set created_at = coalesce((select min(polled_at) from polls), now())
    where created_at is null;
alter table trains alter column created_at set default now();

-- Heartbeat: one row per workflow execution, including the ~90% of runs that
-- find nothing due and call the API zero times.
--
-- Without this, "is the pipeline alive?" is unanswerable: a gap in `polls`
-- means either the cron died or simply that no train was scheduled to arrive,
-- and those need very different reactions. Cheap at ~48 rows/day.
create table if not exists poller_runs (
    id          bigserial primary key,
    ran_at      timestamptz not null default now(),
    due_count   integer not null default 0,
    ok_count    integer not null default 0,
    failed_count integer not null default 0
);

create index if not exists idx_poller_runs_ran_at on poller_runs (ran_at desc);

-- One row per (train, journey_date): the last poll of that journey that
-- actually carried a delay -- our best estimate of how late the train
-- ultimately was. Postgres DISTINCT ON replaces the self-join the SQLite
-- version needs.
--
-- `(delay_minutes is null)` sorts first (false < true), so a reading always
-- beats a non-reading and only then does the latest win. Plain `polled_at
-- desc` let a later poll with no delay hide an earlier one that had it: on
-- 2026-07-30, 12621 was read seven times as 0/0/9/20/null/null and the
-- journey reported no delay at all. A journey where *every* poll is null
-- still appears, with a null delay -- that is a real "we saw it and learned
-- nothing", not something to hide.
--
-- 'not-started' rows are excluded on purpose. RailRadar's live endpoint
-- returns the train's *currently active* run, so polling either too early or
-- after arrival yields the next run sitting at its origin with delay=0. That
-- is not a journey we observed -- counting it would silently inflate on-time
-- percentage with runs that never happened.
--
-- 'cancelled' is excluded for the same reason and was found the same way.
-- RailRadar reports a cancelled service with delay=0, which read as a
-- perfectly punctual journey: 12553 has come back cancelled on all 13 polls
-- it has ever received, and each one was being counted as an on-time run for
-- a train that never moved. A cancellation is a real fact but it is not a
-- punctuality observation, and averaging it in only ever flatters the fleet.
--
-- `is distinct from` rather than `not in`: null statuses must survive, and
-- `status not in (...)` evaluates to null -- i.e. drops the row -- when
-- status is null.
--
-- `delay_minutes` here is the arrival delay when the poll carried one, falling
-- back to the position delay. Every page says "arrived late by", so this is
-- the number they meant all along; rows predating the column keep the only one
-- they ever had. The raw pair is exposed at the end for anyone checking.
-- Appended, not inserted: CREATE OR REPLACE VIEW can only add columns at the
-- end, and reordering fails with 42P16.
create or replace view daily_delays
with (security_invoker = on) as
select distinct on (p.train_number, p.journey_date)
    p.train_number,
    t.name,
    p.journey_date,
    coalesce(p.arrival_delay_minutes, p.delay_minutes) as delay_minutes,
    p.status,
    p.current_station_code,
    p.polled_at,
    (coalesce(p.arrival_delay_minutes, p.delay_minutes) is not null
     and coalesce(p.arrival_delay_minutes, p.delay_minutes) <= 10) as on_time,
    p.delay_minutes            as position_delay_minutes,
    p.arrival_delay_minutes
from polls p
join trains t using (train_number)
where p.status is distinct from 'not-started'
  and p.status is distinct from 'cancelled'
order by p.train_number, p.journey_date,
         (coalesce(p.arrival_delay_minutes, p.delay_minutes) is null),
         p.polled_at desc;

-- Weekly rollup. Mirrors app/aggregate.py:weekly_stats.
create or replace view weekly_stats
with (security_invoker = on) as
select
    train_number,
    name,
    to_char(journey_date, 'IYYY"-W"IW')            as iso_week,
    count(*)                                       as runs_observed,
    round(avg(delay_minutes)::numeric, 1)          as avg_delay_minutes,
    round((percentile_cont(0.5) within group (order by delay_minutes))::numeric, 1)
                                                   as median_delay_minutes,
    max(delay_minutes)                             as max_delay_minutes,
    round(100.0 * count(*) filter (where on_time) / count(*), 1) as on_time_pct
from daily_delays
where delay_minutes is not null
group by train_number, name, to_char(journey_date, 'IYYY"-W"IW');

-- All-time-so-far per train. Mirrors app/aggregate.py:train_summary.
--
-- Dropped rather than replaced: CREATE OR REPLACE VIEW can only append
-- columns, so inserting one mid-list reads as renaming a column and fails
-- with 42P16. Nothing depends on this view, so dropping is safe.
drop view if exists train_summary;
create view train_summary
with (security_invoker = on) as
select
    t.train_number,
    t.name,
    t.train_type,
    t.source_code,
    t.destination_code,
    t.scheduled_departure,
    t.scheduled_arrival,
    t.run_days,
    t.arrival_day_offset,
    t.updated_at,
    t.created_at,
    count(d.delay_minutes)                                  as journeys_observed,
    round(avg(d.delay_minutes)::numeric, 1)                 as avg_delay_minutes,
    case when count(d.delay_minutes) > 0
         then round(100.0 * count(*) filter (where d.on_time) / count(d.delay_minutes), 1)
    end                                                     as on_time_pct
from trains t
left join daily_delays d using (train_number)
group by t.train_number, t.name, t.train_type, t.source_code, t.destination_code,
         t.scheduled_departure, t.scheduled_arrival, t.run_days,
         t.arrival_day_offset, t.updated_at, t.created_at;

-- ---------------------------------------------------------------------------
-- Row Level Security
--
-- The dashboard is a static page on GitHub Pages using the anon key, which is
-- public by definition. So: anon may READ, and nothing else. All writes go
-- through the poller using the service_role key (kept in GitHub Actions
-- secrets), which bypasses RLS.
-- ---------------------------------------------------------------------------
alter table trains       enable row level security;
alter table polls        enable row level security;
alter table poller_runs  enable row level security;

drop policy if exists "anon reads trains" on trains;
create policy "anon reads trains" on trains for select to anon using (true);

drop policy if exists "anon reads polls" on polls;
create policy "anon reads polls" on polls for select to anon using (true);

drop policy if exists "anon reads poller_runs" on poller_runs;
create policy "anon reads poller_runs" on poller_runs for select to anon using (true);
