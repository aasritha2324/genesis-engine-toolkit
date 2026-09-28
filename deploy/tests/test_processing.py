"""Unit tests for the aggregator's pure processing rules (no I/O needed)."""
import uuid
from datetime import datetime, timedelta, timezone

import pytest

import processing as P

NOW = datetime(2026, 1, 15, 12, 0, 0, tzinfo=timezone.utc)


def _event(**over):
    data = {
        "event_id": "evt_" + uuid.uuid4().hex,
        "ad_id": "ad_12345678",
        "campaign_id": "camp_12345678",
        "advertiser_id": str(uuid.uuid4()),
        "viewer_id": "viewer-abc",
        "timestamp": NOW.isoformat(),
        "ip_hash": "h" * 64,
        "country": "IN",
        "device": "mobile",
        "served_by": "api-1",
    }
    data.update(over)
    return P.validate(data)


# ------------------------------------------------------------ validation

def test_valid_event_roundtrip():
    e = _event()
    assert e.event_id.startswith("evt_")
    assert e.device == "mobile"
    assert e.event_time.tzinfo is not None


@pytest.mark.parametrize("bad", [
    {"event_id": "short"},                       # < 8 chars
    {"event_id": "bad id with spaces"},          # invalid characters
    {"advertiser_id": "not-a-uuid"},
    {"country": "usa"},
    {"country": "in"},                           # must be uppercase
    {"device": "watch"},
    {"timestamp": "not-a-date"},
    {"timestamp": "2026-01-15T12:00:00"},        # no timezone
    {"ad_id": None},
])
def test_invalid_events_raise(bad):
    payload = {
        "event_id": "evt_okay1234", "ad_id": "ad_1", "campaign_id": "camp_1",
        "advertiser_id": str(uuid.uuid4()), "viewer_id": "v",
        "timestamp": "2026-01-15T12:00:00+00:00", "country": "IN", "device": "mobile",
    }
    payload.update({k: v for k, v in bad.items() if v is not None})
    if any(k in bad and bad[k] is None for k in bad):
        payload.update(bad)
    with pytest.raises(P.InvalidEvent):
        P.validate(payload)


def test_missing_optional_fields_allowed():
    e = _event(ip_hash=None, served_by=None)
    assert e.ip_hash is None and e.served_by is None


# --------------------------------------------------------------- lateness

def test_classify_on_time():
    assert P.classify(NOW - timedelta(seconds=30), NOW, NOW) == "on_time"


def test_classify_within_allowed_lateness():
    watermark = NOW - timedelta(minutes=2)
    assert P.classify(NOW - timedelta(minutes=3), watermark, NOW) == "on_time"


def test_classify_late():
    watermark = NOW - timedelta(minutes=2)
    assert P.classify(NOW - timedelta(minutes=5), watermark, NOW) == "late"


def test_classify_too_old():
    assert P.classify(NOW - timedelta(hours=25), None, NOW) == "too_old"


def test_classify_future_is_poison():
    assert P.classify(NOW + timedelta(minutes=10), None, NOW) == "future"
    assert P.classify(NOW + timedelta(minutes=4), None, NOW) == "on_time"  # <=5min is fine


# ---------------------------------------------------------------- windows

def test_window_start_floor():
    t = datetime(2026, 1, 15, 10, 0, 59, 999999, tzinfo=timezone.utc)
    assert P.window_start(t) == datetime(2026, 1, 15, 10, 0, tzinfo=timezone.utc)


def test_ip_bucket_10s_floor():
    t = datetime(2026, 1, 15, 10, 0, 27, tzinfo=timezone.utc)
    assert P.ip_bucket(t).second == 20


# ------------------------------------------------------------ spike rule

def test_spike_not_detected_below_floor():
    assert not P.is_campaign_spike(199, [10] * 10)


def test_spike_detected_when_5x_average():
    # avg 20 -> threshold 100; current 200 clears both floor and multiplier.
    assert P.is_campaign_spike(200, [20] * 10)


def test_spike_not_detected_when_below_multiplier():
    assert not P.is_campaign_spike(300, [100] * 10)


def test_spike_handles_missing_minutes():
    # Trailing minutes mostly idle: avg 0 -> multiplier 0, floor decides.
    assert P.is_campaign_spike(200, [])
    assert not P.is_campaign_spike(199, [])
