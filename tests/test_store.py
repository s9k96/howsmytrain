"""
The Supabase write path, specifically its behaviour while the schema is behind
the code.

polls.arrival_delay_minutes ships in supabase/schema.sql, which is applied by
hand -- PostgREST cannot run DDL. So there is always a window where the poller
is sending a column the database doesn't have yet, and PostgREST rejects the
whole row rather than the field. Losing a reading because a migration hasn't
been pasted yet is the one outcome worth writing a test for.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import config, store


class _Response:
    def __init__(self, status_code, text=""):
        self.status_code = status_code
        self.text = text


def _enable(monkeypatch):
    monkeypatch.setattr(config, "SUPABASE_URL", "https://example.supabase.co")
    monkeypatch.setattr(config, "SUPABASE_SERVICE_KEY", "service-key")
    monkeypatch.setattr(store, "_HAS_ARRIVAL_COLUMN", True)


def _capture(monkeypatch, responses):
    sent = []

    def fake_post(url, headers=None, json=None, timeout=None):
        sent.append(json[0])
        return responses[len(sent) - 1]

    monkeypatch.setattr(store.httpx, "post", fake_post)
    return sent


def _poll(**kw):
    store.insert_poll(
        train_number="12584", status="running", delay_minutes=58,
        current_station_code="DAN", current_station_status="departed",
        railradar_last_updated_at=None, raw={}, journey_date="2026-07-26",
        **kw,
    )


def test_the_arrival_delay_is_sent_when_the_column_is_there(monkeypatch):
    _enable(monkeypatch)
    sent = _capture(monkeypatch, [_Response(201)])
    _poll(arrival_delay_minutes=64)

    assert len(sent) == 1
    assert sent[0]["arrival_delay_minutes"] == 64
    assert sent[0]["delay_minutes"] == 58      # both, never one instead of the other


def test_a_missing_column_costs_the_field_not_the_reading(monkeypatch):
    _enable(monkeypatch)
    sent = _capture(monkeypatch, [
        _Response(400, "Could not find the 'arrival_delay_minutes' column of 'polls'"),
        _Response(201),
    ])
    _poll(arrival_delay_minutes=64)

    assert len(sent) == 2                       # retried rather than raised
    assert "arrival_delay_minutes" not in sent[1]
    assert sent[1]["delay_minutes"] == 58       # the reading still lands
    assert store._HAS_ARRIVAL_COLUMN is False   # and we stop trying this run


def test_an_unrelated_failure_still_raises(monkeypatch):
    # Only the known-missing-column case degrades. A dead database or a bad key
    # must fail loudly, or the run reports success having stored nothing.
    _enable(monkeypatch)
    _capture(monkeypatch, [_Response(500, "upstream connect error")])
    try:
        _poll(arrival_delay_minutes=64)
    except RuntimeError as exc:
        assert "500" in str(exc)
    else:
        raise AssertionError("a 500 must not be swallowed")
    assert store._HAS_ARRIVAL_COLUMN is True
