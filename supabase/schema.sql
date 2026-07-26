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
    train_number       text primary key,
    name               text,
    destination_code   text,
    scheduled_arrival  time,          -- IST local time of arrival at destination
    updated_at         timestamptz not null default now()
);

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

create index if not exists idx_polls_train_date on polls (train_number, journey_date);
create index if not exists idx_polls_polled_at  on polls (polled_at desc);

-- One row per (train, journey_date): the LAST poll of that journey, i.e. our
-- best estimate of how late the train ultimately was. Postgres DISTINCT ON
-- replaces the self-join the SQLite version needs.
--
-- 'not-started' rows are excluded on purpose. RailRadar's live endpoint
-- returns the train's *currently active* run, so polling either too early or
-- after arrival yields the next run sitting at its origin with delay=0. That
-- is not a journey we observed -- counting it would silently inflate on-time
-- percentage with runs that never happened.
create or replace view daily_delays
with (security_invoker = on) as
select distinct on (p.train_number, p.journey_date)
    p.train_number,
    t.name,
    p.journey_date,
    p.delay_minutes,
    p.status,
    p.current_station_code,
    p.polled_at,
    (p.delay_minutes is not null and p.delay_minutes <= 10) as on_time
from polls p
join trains t using (train_number)
where p.status is distinct from 'not-started'
order by p.train_number, p.journey_date, p.polled_at desc;

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
create or replace view train_summary
with (security_invoker = on) as
select
    t.train_number,
    t.name,
    t.destination_code,
    t.scheduled_arrival,
    count(d.delay_minutes)                                  as journeys_observed,
    round(avg(d.delay_minutes)::numeric, 1)                 as avg_delay_minutes,
    case when count(d.delay_minutes) > 0
         then round(100.0 * count(*) filter (where d.on_time) / count(d.delay_minutes), 1)
    end                                                     as on_time_pct
from trains t
left join daily_delays d using (train_number)
group by t.train_number, t.name, t.destination_code, t.scheduled_arrival;

-- ---------------------------------------------------------------------------
-- Row Level Security
--
-- The dashboard is a static page on GitHub Pages using the anon key, which is
-- public by definition. So: anon may READ, and nothing else. All writes go
-- through the poller using the service_role key (kept in GitHub Actions
-- secrets), which bypasses RLS.
-- ---------------------------------------------------------------------------
alter table trains enable row level security;
alter table polls  enable row level security;

drop policy if exists "anon reads trains" on trains;
create policy "anon reads trains" on trains for select to anon using (true);

drop policy if exists "anon reads polls" on polls;
create policy "anon reads polls" on polls for select to anon using (true);
