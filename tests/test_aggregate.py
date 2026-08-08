import time

from app import aggregate, db


def _insert(train_number, journey_date, delay_minutes, status="running", station="NDLS",
            arrival_delay_minutes=None):  # noqa: E302
    db.insert_poll(
        train_number=train_number,
        status=status,
        delay_minutes=delay_minutes,
        arrival_delay_minutes=arrival_delay_minutes,
        current_station_code=station,
        current_station_status="departed",
        railradar_last_updated_at=None,
        raw={"fixture": True},
        journey_date=journey_date,
    )
    # Ensure strictly increasing polled_at across inserts within the same test,
    # since sqlite datetime() resolution combined with fast test execution
    # could otherwise produce ties.
    time.sleep(0.001)


def test_daily_stats_uses_last_poll_of_the_day(temp_db):
    # Train A, single day: delay grows over the journey; the LAST poll (45m)
    # should be treated as that day's final observed delay, not the first.
    db.upsert_train("12301", "Test Express")
    _insert("12301", "2026-07-21", delay_minutes=5)
    _insert("12301", "2026-07-21", delay_minutes=45, status="terminated")

    rows = aggregate.daily_stats(train_number="12301", days=30)
    assert len(rows) == 1
    assert rows[0]["journey_date"] == "2026-07-21"
    assert rows[0]["delay_minutes"] == 45
    assert rows[0]["on_time"] is False


def test_daily_stats_prefers_a_poll_that_carries_a_delay(temp_db):
    # A journey polled repeatedly can end on a reading with no delay. Taking
    # the plain latest poll then reports the whole day as "no data" even though
    # we measured it -- how 12621's 2026-07-30 journey lost a 20 min delay.
    db.upsert_train("12621", "Tamil Nadu Express")
    _insert("12621", "2026-07-29", delay_minutes=9)
    _insert("12621", "2026-07-29", delay_minutes=20)
    _insert("12621", "2026-07-29", delay_minutes=None)
    _insert("12621", "2026-07-29", delay_minutes=None)

    rows = aggregate.daily_stats(train_number="12621", days=30)
    assert len(rows) == 1
    assert rows[0]["delay_minutes"] == 20      # latest that measured anything


def test_daily_stats_keeps_a_journey_whose_polls_all_lack_a_delay(temp_db):
    # "We saw it and learned nothing" is a real outcome, not a row to drop.
    db.upsert_train("12553", "Vaishali Express")
    _insert("12553", "2026-07-29", delay_minutes=None)
    _insert("12553", "2026-07-29", delay_minutes=None)

    rows = aggregate.daily_stats(train_number="12553", days=30)
    assert len(rows) == 1
    assert rows[0]["delay_minutes"] is None
    assert rows[0]["on_time"] is False


def test_cancelled_runs_are_not_punctuality_observations(temp_db):
    # RailRadar reports a cancelled service with delay=0, which reads as a
    # flawless journey. 12553 came back cancelled on all 13 polls it ever
    # received and was counted as on time every time -- for a train that never
    # ran. Excluding it can only ever lower the on-time figure, which is the
    # honest direction.
    db.upsert_train("12553", "Vaishali Express")
    _insert("12553", "2026-07-29", delay_minutes=0, status="cancelled")
    _insert("12553", "2026-07-30", delay_minutes=0, status="cancelled")

    assert aggregate.daily_stats(train_number="12553", days=30) == []


def test_not_started_runs_are_excluded_too(temp_db):
    # The same trap from the other end: a poll placed outside the window gets
    # the NEXT run parked at its origin, also with delay=0.
    db.upsert_train("12301", "Test Express")
    _insert("12301", "2026-07-29", delay_minutes=0, status="not-started")
    _insert("12301", "2026-07-29", delay_minutes=25, status="running")

    rows = aggregate.daily_stats(train_number="12301", days=30)
    assert len(rows) == 1
    assert rows[0]["delay_minutes"] == 25   # the real reading, not the parked 0


def test_a_cancelled_poll_does_not_hide_a_real_one(temp_db):
    # Cancellation is filtered before "latest wins" is applied, so a later
    # cancelled row must not displace an earlier genuine reading.
    db.upsert_train("12951", "Rajdhani")
    _insert("12951", "2026-07-29", delay_minutes=18, status="running")
    _insert("12951", "2026-07-29", delay_minutes=0, status="cancelled")

    rows = aggregate.daily_stats(train_number="12951", days=30)
    assert len(rows) == 1
    assert rows[0]["delay_minutes"] == 18
    assert rows[0]["on_time"] is False


def test_the_arrival_delay_is_the_one_reported(temp_db):
    # Read 20 min down where it had reached, projected 45 min late into its
    # destination. Every page says "arrived late by", so 45 is the answer --
    # and it crosses the on-time line that 20 also crossed, in the right
    # direction for once.
    db.upsert_train("12584", "Lucknow AC Double Decker")
    _insert("12584", "2026-07-26", delay_minutes=20, arrival_delay_minutes=45)

    rows = aggregate.daily_stats(train_number="12584", days=30)
    assert rows[0]["delay_minutes"] == 45
    assert rows[0]["on_time"] is False


def test_history_without_an_arrival_delay_keeps_its_own_number(temp_db):
    # Every poll before the column existed has only the position delay. The
    # fallback is what stops a schema change quietly blanking months of data.
    db.upsert_train("12005", "Kalka Shatabdi")
    _insert("12005", "2026-07-26", delay_minutes=8, arrival_delay_minutes=None)

    rows = aggregate.daily_stats(train_number="12005", days=30)
    assert rows[0]["delay_minutes"] == 8
    assert rows[0]["on_time"] is True


def test_a_poll_carrying_only_an_arrival_delay_counts_as_a_reading(temp_db):
    # "Latest poll that measured anything" has to consider both columns, or a
    # later arrival-only reading loses to an earlier position-only one.
    db.upsert_train("12423", "New Delhi Rajdhani")
    _insert("12423", "2026-07-26", delay_minutes=30)
    _insert("12423", "2026-07-26", delay_minutes=None, arrival_delay_minutes=150)

    rows = aggregate.daily_stats(train_number="12423", days=30)
    assert rows[0]["delay_minutes"] == 150


def test_daily_stats_on_time_threshold(temp_db):
    db.upsert_train("12951", "Rajdhani")
    _insert("12951", "2026-07-20", delay_minutes=8)  # within 10 min threshold

    rows = aggregate.daily_stats(train_number="12951", days=30)
    assert rows[0]["delay_minutes"] == 8
    assert rows[0]["on_time"] is True


def test_weekly_stats_aggregates_across_days_same_week(temp_db):
    # 2026-07-20 (Mon) and 2026-07-21 (Tue) fall in the same ISO week.
    db.upsert_train("12301", "Test Express")
    _insert("12301", "2026-07-20", delay_minutes=5)
    _insert("12301", "2026-07-21", delay_minutes=45)

    weekly = aggregate.weekly_stats(train_number="12301", weeks=8)
    assert len(weekly) == 1
    row = weekly[0]
    assert row["runs_observed"] == 2
    assert row["avg_delay_minutes"] == 25.0
    assert row["median_delay_minutes"] == 25.0
    assert row["max_delay_minutes"] == 45
    assert row["on_time_pct"] == 50.0  # only the 5-min run counts as on-time


def test_weekly_stats_separates_different_trains(temp_db):
    db.upsert_train("12301", "Train A")
    db.upsert_train("22691", "Train B")
    _insert("12301", "2026-07-20", delay_minutes=5)
    _insert("22691", "2026-07-20", delay_minutes=60)

    weekly = aggregate.weekly_stats(weeks=8)
    by_train = {r["train_number"]: r for r in weekly}
    assert by_train["12301"]["avg_delay_minutes"] == 5.0
    assert by_train["22691"]["avg_delay_minutes"] == 60.0


def test_train_summary_handles_no_data(temp_db):
    # No polls inserted for this train -- should not error, just report nulls.
    db.upsert_train("99999", "Untracked So Far")
    summary = {t["train_number"]: t for t in aggregate.train_summary()}
    assert summary["99999"]["journeys_observed"] == 0
    assert summary["99999"]["avg_delay_minutes"] is None
    assert summary["99999"]["on_time_pct"] is None
