"""
The Supabase write path, specifically its behaviour while the schema is behind
the code.

New columns ship in supabase/schema.sql, which is applied by hand -- PostgREST
cannot run DDL. So there is always a window where the poller sends a column the
database doesn't have yet, and PostgREST rejects the whole row rather than the
field. On `polls` that loses a reading; on `trains` it raises past poll_train's
handler and fails the entire run. Both are worse than storing one column short.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import config, store


class _Response:
    def __init__(self, status_code, text=""):
        self.status_code = status_code
        self.text = text


@pytest.fixture
def supabase(monkeypatch):
    """Store configured for Supabase, with a clean memory of missing columns."""
    monkeypatch.setattr(config, "SUPABASE_URL", "https://example.supabase.co")
    monkeypatch.setattr(config, "SUPABASE_SERVICE_KEY", "service-key")
    monkeypatch.setattr(store, "_MISSING_COLUMNS", set())

    def arrange(*responses):
        sent = []

        def fake_post(url, headers=None, json=None, timeout=None):
            sent.append(json[0])
            return responses[len(sent) - 1]

        monkeypatch.setattr(store.httpx, "post", fake_post)
        return sent

    return arrange


def _poll(**kw):
    store.insert_poll(
        train_number="12584", status="running", delay_minutes=58,
        current_station_code="DAN", current_station_status="departed",
        railradar_last_updated_at=None, raw={}, journey_date="2026-07-26", **kw,
    )


def test_both_delays_are_sent_when_the_column_is_there(supabase):
    sent = supabase(_Response(201))
    _poll(arrival_delay_minutes=64)

    assert len(sent) == 1
    assert sent[0]["arrival_delay_minutes"] == 64
    assert sent[0]["delay_minutes"] == 58      # both, never one instead of the other


def test_a_missing_column_costs_the_field_not_the_reading(supabase):
    sent = supabase(
        _Response(400, "Could not find the 'arrival_delay_minutes' column of 'polls'"),
        _Response(201),
    )
    _poll(arrival_delay_minutes=64)

    assert len(sent) == 2                       # retried rather than raised
    assert "arrival_delay_minutes" not in sent[1]
    assert sent[1]["delay_minutes"] == 58       # the reading still lands


def test_the_next_poll_does_not_pay_for_the_same_missing_column(supabase):
    sent = supabase(
        _Response(400, "Could not find the 'arrival_delay_minutes' column of 'polls'"),
        _Response(201),
        _Response(201),
    )
    _poll(arrival_delay_minutes=64)
    _poll(arrival_delay_minutes=70)

    assert len(sent) == 3                       # 2 for the first poll, 1 for the second
    assert "arrival_delay_minutes" not in sent[2]


def test_the_same_tolerance_covers_the_trains_upsert(supabase):
    # This one matters more than it looks: upsert_train raises past
    # poll_train's RailRadarError handler, so an unknown column here fails the
    # whole run rather than one train.
    sent = supabase(
        _Response(400, "Could not find the 'train_type' column of 'trains'"),
        _Response(201),
    )
    store.upsert_train("12584", "Lucknow AC Double Decker", train_type="Superfast Express")

    assert len(sent) == 2
    assert "train_type" not in sent[1]
    assert sent[1]["name"] == "Lucknow AC Double Decker"


def test_an_unrelated_failure_still_raises(supabase):
    # Only the known-missing-column case degrades. A dead database or a bad key
    # must fail loudly, or the run reports success having stored nothing.
    supabase(_Response(500, "upstream connect error"))
    with pytest.raises(RuntimeError, match="500"):
        _poll(arrival_delay_minutes=64)
    assert store._MISSING_COLUMNS == set()


def test_a_rejection_naming_a_required_column_is_not_shrugged_off(supabase):
    # `optional` is the whitelist. A complaint about delay_minutes is a real
    # failure and must not quietly drop the number we exist to record.
    supabase(_Response(400, "invalid input syntax for type integer: delay_minutes"))
    with pytest.raises(RuntimeError, match="400"):
        _poll(arrival_delay_minutes=64)
