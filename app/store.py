"""
Storage dispatch: Supabase (Postgres) when configured, SQLite otherwise.

The poller calls this module instead of `db` directly. If SUPABASE_URL and
SUPABASE_SERVICE_KEY are set (they are in GitHub Actions), writes go to
Supabase via PostgREST; otherwise they go to the local SQLite file exactly
as before, so local runs and the test suite are unaffected.

PostgREST is used over plain httpx rather than psycopg/supabase-py so CI
installs nothing beyond what requirements.txt already has.
"""
import logging
from datetime import datetime
from typing import Optional

import httpx

from . import config, db

logger = logging.getLogger("store")

TIMEOUT = 20.0


def enabled() -> bool:
    return bool(config.SUPABASE_URL and config.SUPABASE_SERVICE_KEY)


def _headers(extra: Optional[dict] = None) -> dict:
    h = {
        "apikey": config.SUPABASE_SERVICE_KEY,
        "Authorization": f"Bearer {config.SUPABASE_SERVICE_KEY}",
        "Content-Type": "application/json",
    }
    if extra:
        h.update(extra)
    return h


def _url(path: str) -> str:
    return f"{config.SUPABASE_URL.rstrip('/')}/rest/v1/{path}"


def _check(resp: httpx.Response, what: str) -> None:
    if resp.status_code >= 400:
        raise RuntimeError(f"Supabase {what} failed HTTP {resp.status_code}: {resp.text[:300]}")


def upsert_train(
    train_number: str,
    name: Optional[str],
    destination_code: Optional[str] = None,
    scheduled_arrival: Optional[str] = None,
    run_days: Optional[list[str]] = None,
    arrival_day_offset: Optional[int] = None,
    source_code: Optional[str] = None,
    scheduled_departure: Optional[str] = None,
) -> None:
    if not enabled():
        db.upsert_train(train_number, name)
        return

    row = {"train_number": train_number, "name": name, "updated_at": db.now_ist_iso()}
    # Don't overwrite a known destination/arrival with nulls if this poll
    # happened to come back without a usable route.
    if destination_code:
        row["destination_code"] = destination_code
    if scheduled_arrival:
        row["scheduled_arrival"] = scheduled_arrival
    if run_days:
        row["run_days"] = run_days
    if arrival_day_offset is not None:
        row["arrival_day_offset"] = arrival_day_offset
    if source_code:
        row["source_code"] = source_code
    if scheduled_departure:
        row["scheduled_departure"] = scheduled_departure

    resp = httpx.post(
        _url("trains"),
        headers=_headers({"Prefer": "resolution=merge-duplicates"}),
        json=[row],
        timeout=TIMEOUT,
    )
    _check(resp, "upsert_train")


# polls.arrival_delay_minutes ships in supabase/schema.sql, which is applied by
# hand -- PostgREST cannot run DDL. Between deploying this code and running that
# SQL, PostgREST rejects the whole insert over the unknown column, which would
# throw away the reading rather than degrade. So the first rejection drops the
# field for the rest of the process and the poll still lands. Flips back on the
# next run, which is the run after the SQL is applied.
_HAS_ARRIVAL_COLUMN = True


def insert_poll(
    *,
    train_number: str,
    status: Optional[str],
    delay_minutes: Optional[int],
    current_station_code: Optional[str],
    current_station_status: Optional[str],
    railradar_last_updated_at: Optional[str],
    raw: dict,
    journey_date: Optional[str] = None,
    arrival_delay_minutes: Optional[int] = None,
) -> None:
    if not enabled():
        db.insert_poll(
            train_number=train_number,
            status=status,
            delay_minutes=delay_minutes,
            current_station_code=current_station_code,
            current_station_status=current_station_status,
            railradar_last_updated_at=railradar_last_updated_at,
            raw=raw,
            journey_date=journey_date,
            arrival_delay_minutes=arrival_delay_minutes,
        )
        return

    global _HAS_ARRIVAL_COLUMN
    # raw is deliberately dropped here -- see supabase/schema.sql.
    row = {
        "train_number": train_number,
        "journey_date": journey_date or db.today_ist(),
        "polled_at": db.now_ist_iso(),
        "status": status,
        "delay_minutes": delay_minutes,
        "current_station_code": current_station_code,
        "current_station_status": current_station_status,
        "railradar_last_updated_at": railradar_last_updated_at,
    }
    if _HAS_ARRIVAL_COLUMN:
        row["arrival_delay_minutes"] = arrival_delay_minutes

    resp = httpx.post(_url("polls"), headers=_headers(), json=[row], timeout=TIMEOUT)
    if resp.status_code >= 400 and "arrival_delay_minutes" in resp.text:
        logger.warning(
            "polls.arrival_delay_minutes is missing -- run supabase/schema.sql. "
            "Storing this poll without the arrival delay."
        )
        _HAS_ARRIVAL_COLUMN = False
        row.pop("arrival_delay_minutes", None)
        resp = httpx.post(_url("polls"), headers=_headers(), json=[row], timeout=TIMEOUT)
    _check(resp, "insert_poll")


def record_run(due_count: int, ok_count: int, failed_count: int) -> None:
    """
    Heartbeat for one workflow execution -- written even when nothing was due.

    Best-effort: a failed heartbeat must never fail the run that collected
    real data, so this logs and swallows.
    """
    if not enabled():
        return
    try:
        resp = httpx.post(
            _url("poller_runs"),
            headers=_headers(),
            json=[{
                "ran_at": db.now_ist_iso(),
                "due_count": due_count,
                "ok_count": ok_count,
                "failed_count": failed_count,
            }],
            timeout=TIMEOUT,
        )
        _check(resp, "record_run")
    except Exception as exc:  # noqa: BLE001 -- heartbeat is never worth failing on
        logger.warning("Could not record poller run: %s", exc)


def list_trains() -> list[dict]:
    """Tracked trains with their learned destination/arrival time."""
    if not enabled():
        return [dict(r) for r in db.list_trains()]
    resp = httpx.get(
        _url("trains?select=train_number,name,destination_code,scheduled_arrival,run_days,arrival_day_offset"),
        headers=_headers(),
        timeout=TIMEOUT,
    )
    _check(resp, "list_trains")
    return resp.json()


def last_readings(since_date: str) -> dict[str, dict]:
    """
    train_number -> what we last saw, and when we have looked.

        {"at": datetime,            # most recent polled_at
         "delay_minutes": int|None, # from that same poll
         "status": str|None,        # from that same poll
         "times": [datetime, ...],  # every poll since since_date
         "journeys": {journey_date: {at, status, delay_minutes,
                                     arrival_delay_minutes}}}

    `at` is what dedup keys on. `delay_minutes` and `status` are what let the
    window follow a late train to where it will actually arrive rather than
    closing 90 minutes after a scheduled time it was never going to make.
    `times` is only there so a train that keeps slipping can be capped.

    `journeys` exists because a follow-up poll can come back describing a
    different run than the one it was sent after -- 15274's follow-up on
    2026-08-07 returned the next day's departure, 21 h from its own arrival.
    Keyed by run, the poller can tell "we looked again and still haven't seen
    that journey end" from "we saw it end".

    Ordinary dedup is still keyed on *when a train was last read*, never on
    which journey the reading turned out to belong to.

    Predicting the journey is what this replaces, and the prediction could not
    be made correct. `poll_due` derived the expected journey_date as
    `now - arrival_day_offset` -- i.e. "the run arriving now". But for a service
    whose scheduled run exceeds 24 h, two runs are in the air simultaneously and
    RailRadar's live endpoint returns whichever it considers active, which may
    be the later one. Its `startDate` then files the poll under a date the
    prediction never guessed, the key never matched, and the train was re-polled
    on every remaining tick of its window: 12621 spent 7 requests on one journey
    on 2026-07-30, and the last of them overwrote a usable delay with a null.

    "Have we read this train since its window opened?" cannot drift, because
    both sides of it are poll times we control. Twelve of the fleet's services
    run longer than 24 h, so this is the common case, not an edge one.
    """
    def _add(out: dict, row: dict) -> None:
        number = row["train_number"]
        at = datetime.fromisoformat(row["polled_at"])
        reading = {
            "at": at,
            "status": row.get("status"),
            "delay_minutes": row.get("delay_minutes"),
            # Absent until supabase/schema.sql has been run, hence .get().
            "arrival_delay_minutes": row.get("arrival_delay_minutes"),
        }
        e = out.setdefault(number, {**reading, "times": [], "journeys": {}})
        e["times"].append(at)
        if at >= e["at"]:
            e.update(reading)
        jd = str(row.get("journey_date") or "")[:10]
        if jd and (jd not in e["journeys"] or at >= e["journeys"][jd]["at"]):
            e["journeys"][jd] = reading

    out: dict[str, dict] = {}
    if not enabled():
        for r in db.list_polls(since_date=since_date, limit=5000):
            _add(out, dict(r))
        return out

    # select=* rather than a column list: this has to keep working on a project
    # where arrival_delay_minutes hasn't been added yet, and naming a column
    # PostgREST doesn't know fails the whole request.
    resp = httpx.get(
        _url(f"polls?select=*&polled_at=gte.{since_date}&order=polled_at.desc&limit=5000"),
        headers=_headers(),
        timeout=TIMEOUT,
    )
    _check(resp, "last_readings")
    for r in resp.json():   # newest first, so the first hit per train wins
        _add(out, r)
    return out
