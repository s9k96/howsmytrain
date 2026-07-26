"""
Day/week efficiency aggregation over stored poll snapshots.

All aggregation works off `db.list_journey_final_delays`, i.e. the last
poll seen for each (train, journey_date) -- our best available estimate of
how late that day's run ultimately was. On-time is defined as
delay_minutes <= ON_TIME_THRESHOLD_MINUTES (default 10, matching Indian
Railways' common convention for "on time").
"""
from collections import defaultdict
from datetime import date, timedelta
from statistics import mean, median
from typing import Optional

from . import db

ON_TIME_THRESHOLD_MINUTES = 10


def _safe_delays(rows) -> list[int]:
    return [r["delay_minutes"] for r in rows if r["delay_minutes"] is not None]


def daily_stats(train_number: Optional[str] = None, days: int = 30) -> list[dict]:
    """
    One row per (train_number, journey_date) with that day's final observed
    delay, grouped/sorted by date descending.
    """
    since = (date.today() - timedelta(days=days)).isoformat()
    rows = db.list_journey_final_delays(train_number=train_number, since_date=since)
    return [
        {
            "train_number": r["train_number"],
            "journey_date": r["journey_date"],
            "delay_minutes": r["delay_minutes"],
            "status": r["status"],
            "current_station_code": r["current_station_code"],
            "on_time": (r["delay_minutes"] is not None and r["delay_minutes"] <= ON_TIME_THRESHOLD_MINUTES),
        }
        for r in rows
    ]


def weekly_stats(train_number: Optional[str] = None, weeks: int = 8) -> list[dict]:
    """
    One row per (train_number, iso_year_week) with average/median delay and
    on-time percentage across that week's journeys.
    """
    since = (date.today() - timedelta(weeks=weeks)).isoformat()
    rows = db.list_journey_final_delays(train_number=train_number, since_date=since)

    buckets: dict[tuple[str, str], list[int]] = defaultdict(list)
    counts: dict[tuple[str, str], int] = defaultdict(int)
    on_time_counts: dict[tuple[str, str], int] = defaultdict(int)

    for r in rows:
        if r["delay_minutes"] is None:
            continue
        d = date.fromisoformat(r["journey_date"])
        iso_year, iso_week, _ = d.isocalendar()
        key = (r["train_number"], f"{iso_year}-W{iso_week:02d}")
        buckets[key].append(r["delay_minutes"])
        counts[key] += 1
        if r["delay_minutes"] <= ON_TIME_THRESHOLD_MINUTES:
            on_time_counts[key] += 1

    result = []
    for (t_number, iso_week), delays in sorted(buckets.items(), key=lambda kv: (kv[0][1], kv[0][0]), reverse=True):
        n = counts[(t_number, iso_week)]
        result.append(
            {
                "train_number": t_number,
                "iso_week": iso_week,
                "runs_observed": n,
                "avg_delay_minutes": round(mean(delays), 1),
                "median_delay_minutes": round(median(delays), 1),
                "max_delay_minutes": max(delays),
                "on_time_pct": round(100 * on_time_counts[(t_number, iso_week)] / n, 1),
            }
        )
    return result


def train_summary() -> list[dict]:
    """
    All-time-so-far summary per train: number of journeys observed, average
    delay, on-time percentage. Used for the dashboard's top-level cards.
    """
    trains = db.list_trains()
    out = []
    for t in trains:
        rows = db.list_journey_final_delays(train_number=t["train_number"])
        delays = _safe_delays(rows)
        out.append(
            {
                "train_number": t["train_number"],
                "name": t["name"],
                "journeys_observed": len(rows),
                "avg_delay_minutes": round(mean(delays), 1) if delays else None,
                "on_time_pct": (
                    round(100 * sum(1 for d in delays if d <= ON_TIME_THRESHOLD_MINUTES) / len(delays), 1)
                    if delays
                    else None
                ),
            }
        )
    return out
