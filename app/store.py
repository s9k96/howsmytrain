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
        )
        return

    # raw is deliberately dropped here -- see supabase/schema.sql.
    resp = httpx.post(
        _url("polls"),
        headers=_headers(),
        json=[{
            "train_number": train_number,
            "journey_date": journey_date or db.today_ist(),
            "polled_at": db.now_ist_iso(),
            "status": status,
            "delay_minutes": delay_minutes,
            "current_station_code": current_station_code,
            "current_station_status": current_station_status,
            "railradar_last_updated_at": railradar_last_updated_at,
        }],
        timeout=TIMEOUT,
    )
    _check(resp, "insert_poll")


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


def polled_journey_dates(journey_date: str) -> set[str]:
    """Train numbers already polled for the given journey_date."""
    if not enabled():
        rows = db.list_polls(since_date=journey_date, limit=2000)
        return {r["train_number"] for r in rows if r["journey_date"] == journey_date}
    resp = httpx.get(
        _url(f"polls?select=train_number&journey_date=eq.{journey_date}"),
        headers=_headers(),
        timeout=TIMEOUT,
    )
    _check(resp, "polled_journey_dates")
    return {r["train_number"] for r in resp.json()}
