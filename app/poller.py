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
import time
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


def _extract_run_days(data: dict) -> Optional[list[str]]:
    """
    Days the train DEPARTS its source, e.g. ['tue','thu','fri','sun'].

    Many trains -- specials especially -- run once or twice a week. Polling
    them daily spends a request per day for data that exists on one.
    """
    days = (data.get("train") or {}).get("runDays")
    if not days:
        return None
    return [str(d).strip().lower()[:3] for d in days]


def _extract_arrival_offset(data: dict) -> int:
    """
    Whole days between departure and arrival at the destination.

    RailRadar reports the last stop's `arrivalDay` as 1-based (day1 = same
    day as departure), so a train leaving Thursday and arriving Friday has
    arrivalDay=2 -> offset 1. runDays are departure days but we poll at
    arrival, so this offset is what maps one onto the other.
    """
    route = data.get("route") or []
    if not route:
        return 0
    try:
        return max(0, int(route[-1].get("arrivalDay", 1)) - 1)
    except (TypeError, ValueError):
        return 0


def _time_of(ts: Optional[str]) -> Optional[str]:
    """'2026-07-26T14:05:00+05:30' -> '14:05:00'."""
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts).strftime("%H:%M:%S")
    except ValueError:
        return None


def _extract_origin(data: dict) -> tuple[Optional[str], Optional[str]]:
    """(source_code, scheduled_departure) from the first stop on the route."""
    code = ((data.get("train") or {}).get("source") or {}).get("code")
    route = data.get("route") or []
    departure = _time_of(_first(route[0], "scheduledDeparture") if route else None)
    if not code and route:
        code = _first(route[0], "stationCode", "code")
    return code, departure


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
    return code, _time_of(_first(last, "scheduledArrival", "scheduledDeparture"))


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
    src_code, sched_departure = _extract_origin(data)

    store.upsert_train(
        train_number, name, dest_code, sched_arrival,
        run_days=_extract_run_days(data),
        arrival_day_offset=_extract_arrival_offset(data),
        source_code=src_code,
        scheduled_departure=sched_departure,
    )
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


# RailRadar's free tier allows 10 requests/minute. Arrival times cluster hard
# (most long-distance trains reach Delhi in the morning), so a single run can
# have a dozen trains due at once and would otherwise fire them back-to-back
# and start collecting 429s.
# ponytail: fixed delay, not a token bucket -- swap if the burst cap changes
# or a run ever needs to poll more than ~40 trains.
SECONDS_BETWEEN_CALLS = 7.0


def poll_all(train_numbers: list[str]) -> dict:
    """Poll every configured train once. Returns a summary dict."""
    client = RailRadarClient()
    results = {"ok": [], "failed": []}
    for i, train_number in enumerate(train_numbers):
        if i:  # no delay before the first call -- the common case is 1-2 trains
            time.sleep(SECONDS_BETWEEN_CALLS)
        ok = poll_train(client, train_number)
        (results["ok"] if ok else results["failed"]).append(train_number)
    return results
