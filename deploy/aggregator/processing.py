"""Pure event-processing rules for the aggregator (no I/O, unit tested)."""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

WINDOW = timedelta(minutes=1)
ALLOWED_LATENESS = timedelta(minutes=2)
MAX_AGE = timedelta(hours=24)
MAX_FUTURE = timedelta(minutes=5)

IP_BURST_THRESHOLD = 100          # clicks from one IP ...
IP_BURST_WINDOW_SECONDS = 10      # ... within 10 seconds
VIEWER_REPEAT_THRESHOLD = 50      # clicks by one viewer on one ad within 1 minute
SPIKE_MULTIPLIER = 5              # current minute > 5x trailing average ...
SPIKE_TRAILING_MINUTES = 10       # ... over the previous 10 minutes
SPIKE_MIN_VOLUME = 200            # ... and at least 200 clicks

DEVICES = {"mobile", "desktop", "tablet", "other"}
_EVENT_ID = re.compile(r"^[A-Za-z0-9_\-:.]{8,120}$")
_COUNTRY = re.compile(r"^[A-Z]{2}$")
_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


class InvalidEvent(ValueError):
    """Poison message: goes to the DLQ, never retried."""


@dataclass(frozen=True)
class Event:
    event_id: str
    ad_id: str
    campaign_id: str
    advertiser_id: str
    viewer_id: str
    event_time: datetime
    ip_hash: str | None
    country: str
    device: str
    served_by: str | None


def parse_time(value: str) -> datetime:
    if not isinstance(value, str):
        raise InvalidEvent("timestamp must be a string")
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise InvalidEvent(f"bad timestamp: {exc}") from exc
    if dt.tzinfo is None:
        raise InvalidEvent("timestamp has no timezone")
    return dt.astimezone(timezone.utc)


def _str(data: dict, key: str, lo: int = 1, hi: int = 120, optional: bool = False) -> str | None:
    v = data.get(key)
    if v is None and optional:
        return None
    if not isinstance(v, str) or not lo <= len(v) <= hi:
        raise InvalidEvent(f"{key} invalid")
    return v


def validate(data: object) -> Event:
    if not isinstance(data, dict):
        raise InvalidEvent("event must be a JSON object")
    event_id = _str(data, "event_id", 8, 120)
    if not _EVENT_ID.match(event_id):
        raise InvalidEvent("event_id has invalid characters")
    advertiser_id = _str(data, "advertiser_id", 36, 36)
    if not _UUID.match(advertiser_id):
        raise InvalidEvent("advertiser_id must be a uuid")
    country = _str(data, "country", 2, 2)
    if not _COUNTRY.match(country):
        raise InvalidEvent("country must be ISO-2 uppercase")
    device = data.get("device")
    if device not in DEVICES:
        raise InvalidEvent("device invalid")
    return Event(
        event_id=event_id,
        ad_id=_str(data, "ad_id"),
        campaign_id=_str(data, "campaign_id"),
        advertiser_id=advertiser_id,
        viewer_id=_str(data, "viewer_id", 1, 128),
        event_time=parse_time(data.get("timestamp")),
        ip_hash=_str(data, "ip_hash", 1, 128, optional=True),
        country=country,
        device=device,
        served_by=_str(data, "served_by", 1, 128, optional=True),
    )


def window_start(t: datetime) -> datetime:
    """1-minute tumbling window: 10:00:00-10:00:59 -> 10:00:00."""
    return t.replace(second=0, microsecond=0)


def ip_bucket(t: datetime) -> datetime:
    return t.replace(second=(t.second // IP_BURST_WINDOW_SECONDS) * IP_BURST_WINDOW_SECONDS, microsecond=0)


def classify(event_time: datetime, watermark: datetime | None, now: datetime) -> str:
    """Event-time lateness classification.

    on_time  - within allowed lateness of the partition watermark
    late     - behind watermark by more than 2 minutes but younger than 24h:
               still aggregated into its own (historical) window, counted as late
    too_old  - older than 24h: discarded safely, counted in a metric
    future   - more than 5 minutes ahead of the worker clock: poison (DLQ)
    """
    if event_time > now + MAX_FUTURE:
        return "future"
    if event_time < now - MAX_AGE:
        return "too_old"
    if watermark is not None and event_time < watermark - ALLOWED_LATENESS:
        return "late"
    return "on_time"


def is_campaign_spike(current: int, trailing: list[int]) -> bool:
    """current minute > 5x trailing 10-minute average, with a 200-click floor.

    `trailing` holds per-minute counts for the previous minutes (missing minutes = 0).
    """
    if current < SPIKE_MIN_VOLUME:
        return False
    padded = (list(trailing) + [0] * SPIKE_TRAILING_MINUTES)[:SPIKE_TRAILING_MINUTES]
    avg = sum(padded) / SPIKE_TRAILING_MINUTES
    return current > SPIKE_MULTIPLIER * avg
