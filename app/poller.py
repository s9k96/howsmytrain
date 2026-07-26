"""
Polling logic: fetch live status for configured trains and persist a
snapshot row per train per run.

Parsing is defensive -- RailRadar's exact field names weren't fully
documented when this was written, so we try a few likely keys and always
keep the full raw JSON in the database so nothing is lost even if a field
we expect turns out to be named differently. If you notice a field isn't
being picked up, check `raw_json` in the `polls` table and adjust the
`_extract_*` helpers below.
"""
import logging
from datetime import datetime
from typing import Optional

from . import store
from .railradar import RailRadarClient, RailRadarError, RailRadarRateLimitError

logger = logging.getLogger("poller")


def _first(d: dict, *keys, default=None):
    for k in keys:
        if isinstance(d, dict) and k in d and d[k] is not None:
            return d[k]
    return default


def _extract_name(data: dict) -> Optional[str]:
    return _first(data, "trainName", "name") or _first(data.get("train", {}) or {}, "name")


def _extract_status(data: dict) -> Optional[str]:
    return _first(data, "status")


def _extract_delay_minutes(data: dict) -> Optional[int]:
    val = _first(data, "delayMinutes", "delay")
    try:
        return int(val) if val is not None else None
    except (TypeError, ValueError):
        return None


def _extract_current_location(data: dict) -> tuple[Optional[str], Optional[str]]:
    loc = _first(data, "currentLocation", default={}) or {}
    station_code = _first(loc, "stationCode", "code")
    loc_status = _first(loc, "status")
    return station_code, loc_status


def _extract_last_updated_at(data: dict) -> Optional[str]:
    return _first(data, "lastUpdatedAt", "updatedAt")


def _extract_destination(data: dict) -> tuple[Optional[str], Optional[str]]:
    """
    (destination_code, scheduled_arrival_time) from the last stop on the route.

    Refreshed on every poll so the polling schedule tracks timetable changes
    on its own instead of going stale -- scripts/poll_due.py reads this to
    decide which trains are due.
    """
    route = data.get("route") or []
    if not route:
        return None, None
    last = route[-1]
    code = _first(last, "stationCode", "code")
    ts = _first(last, "scheduledArrival", "scheduledDeparture")
    if not ts:
        return code, None
    try:
        # "2026-07-26T22:25:00+05:30" -> "22:25:00"
        return code, datetime.fromisoformat(ts).strftime("%H:%M:%S")
    except ValueError:
        return code, None


def poll_train(client: RailRadarClient, train_number: str) -> bool:
    """
    Poll a single train and store the snapshot. Returns True on success,
    False if the poll failed (logged, not raised) so a caller polling
    multiple trains can continue with the rest.
    """
    try:
        data = client.get_live_status(train_number)
    except RailRadarRateLimitError as exc:
        logger.warning("Rate limited polling train %s: %s", train_number, exc)
        return False
    except RailRadarError as exc:
        logger.error("Failed to poll train %s: %s", train_number, exc)
        return False

    name = _extract_name(data)
    status = _extract_status(data)
    delay_minutes = _extract_delay_minutes(data)
    station_code, loc_status = _extract_current_location(data)
    last_updated_at = _extract_last_updated_at(data)
    dest_code, sched_arrival = _extract_destination(data)

    store.upsert_train(train_number, name, dest_code, sched_arrival)
    store.insert_poll(
        train_number=train_number,
        status=status,
        delay_minutes=delay_minutes,
        current_station_code=station_code,
        current_station_status=loc_status,
        railradar_last_updated_at=last_updated_at,
        raw=data,
    )
    logger.info(
        "Polled %s (%s): status=%s delay=%smin at %s",
        train_number, name or "?", status, delay_minutes, station_code,
    )
    return True


def poll_all(train_numbers: list[str]) -> dict:
    """Poll every configured train once. Returns a summary dict."""
    client = RailRadarClient()
    results = {"ok": [], "failed": []}
    for train_number in train_numbers:
        ok = poll_train(client, train_number)
        (results["ok"] if ok else results["failed"]).append(train_number)
    return results
