"""
SQLite schema and access layer.

Design notes
------------
Every poll of RailRadar's /trains/{number}/live endpoint is stored as one row
in `polls`, verbatim (raw_json) plus a few extracted columns used for fast
aggregation. We never overwrite old polls -- the history of snapshots is what
lets us compute day/week efficiency trends, including retroactively once
enough data has accumulated.

`journey_date` approximates "which day's run is this" using the local date
(Asia/Kolkata) at the moment we polled. RailRadar's live endpoint reports the
currently active run for a train number, so consecutive polls on the same
day naturally belong to the same journey. This is a simplification documented
in the README -- trains that run past midnight may span two journey_dates.
"""
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone, timedelta
from typing import Iterator, Optional

from . import config

IST = timezone(timedelta(hours=5, minutes=30))

SCHEMA = """
CREATE TABLE IF NOT EXISTS trains (
    train_number TEXT PRIMARY KEY,
    name TEXT,
    updated_at TEXT
);

CREATE TABLE IF NOT EXISTS polls (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    train_number TEXT NOT NULL,
    journey_date TEXT NOT NULL,
    polled_at TEXT NOT NULL,
    status TEXT,
    delay_minutes INTEGER,
    current_station_code TEXT,
    current_station_status TEXT,
    railradar_last_updated_at TEXT,
    raw_json TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_polls_train_date ON polls (train_number, journey_date);
CREATE INDEX IF NOT EXISTS idx_polls_polled_at ON polls (polled_at);
"""


def init_db() -> None:
    config.DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with get_connection() as conn:
        conn.executescript(SCHEMA)


@contextmanager
def get_connection() -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(config.DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def now_ist_iso() -> str:
    return datetime.now(IST).isoformat()


def today_ist() -> str:
    return datetime.now(IST).date().isoformat()


def upsert_train(train_number: str, name: Optional[str]) -> None:
    with get_connection() as conn:
        conn.execute(
            """
            INSERT INTO trains (train_number, name, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(train_number) DO UPDATE SET
                name = COALESCE(excluded.name, trains.name),
                updated_at = excluded.updated_at
            """,
            (train_number, name, now_ist_iso()),
        )


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
    with get_connection() as conn:
        conn.execute(
            """
            INSERT INTO polls (
                train_number, journey_date, polled_at, status, delay_minutes,
                current_station_code, current_station_status,
                railradar_last_updated_at, raw_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                train_number,
                journey_date or today_ist(),
                now_ist_iso(),
                status,
                delay_minutes,
                current_station_code,
                current_station_status,
                railradar_last_updated_at,
                json.dumps(raw),
            ),
        )


def list_trains() -> list[sqlite3.Row]:
    with get_connection() as conn:
        return conn.execute("SELECT * FROM trains ORDER BY train_number").fetchall()


def list_polls(
    train_number: Optional[str] = None,
    since_date: Optional[str] = None,
    limit: int = 500,
) -> list[sqlite3.Row]:
    query = "SELECT * FROM polls WHERE 1=1"
    params: list = []
    if train_number:
        query += " AND train_number = ?"
        params.append(train_number)
    if since_date:
        query += " AND journey_date >= ?"
        params.append(since_date)
    query += " ORDER BY polled_at DESC LIMIT ?"
    params.append(limit)
    with get_connection() as conn:
        return conn.execute(query, params).fetchall()


def list_journey_final_delays(
    train_number: Optional[str] = None,
    since_date: Optional[str] = None,
) -> list[sqlite3.Row]:
    """
    For each (train_number, journey_date), return the last poll of that journey
    that actually carried a delay -- our best estimate of how late the train
    ultimately was that day. This is the row used for day/week aggregation.

    Preferring a reading over a non-reading matters because a journey can be
    polled several times: a later poll with a null delay would otherwise hide
    an earlier one that had a number, and the day would report nothing. Falls
    back to the plain latest when every poll is null, so the journey still
    appears -- "we saw it and learned nothing" is a real outcome.

    Mirrors the `daily_delays` view in supabase/schema.sql; keep the two in step.
    """
    query = """
        SELECT p.*
        FROM polls p
        INNER JOIN (
            SELECT train_number, journey_date,
                   COALESCE(
                       MAX(CASE WHEN delay_minutes IS NOT NULL THEN polled_at END),
                       MAX(polled_at)
                   ) AS max_polled_at
            FROM polls
            WHERE 1=1
            {train_filter}
            {date_filter}
            GROUP BY train_number, journey_date
        ) latest
        ON p.train_number = latest.train_number
        AND p.journey_date = latest.journey_date
        AND p.polled_at = latest.max_polled_at
        ORDER BY p.journey_date DESC, p.train_number
    """
    train_filter = ""
    date_filter = ""
    params: list = []
    if train_number:
        train_filter = "AND train_number = ?"
        params.append(train_number)
    if since_date:
        date_filter = "AND journey_date >= ?"
        params.append(since_date)
    query = query.format(train_filter=train_filter, date_filter=date_filter)
    with get_connection() as conn:
        return conn.execute(query, params).fetchall()
