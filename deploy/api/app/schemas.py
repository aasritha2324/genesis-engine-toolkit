from datetime import datetime, timedelta, timezone
from typing import Literal

from pydantic import BaseModel, Field, field_validator

MAX_AGE = timedelta(hours=24)
MAX_FUTURE = timedelta(minutes=5)


class Credentials(BaseModel):
    email: str = Field(min_length=3, max_length=254, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    password: str = Field(min_length=8, max_length=128)


class Register(Credentials):
    organization: str = Field(min_length=2, max_length=80)


class CampaignIn(BaseModel):
    name: str = Field(min_length=2, max_length=80)


class AdIn(BaseModel):
    campaign_id: str = Field(min_length=1, max_length=120)
    title: str = Field(min_length=2, max_length=120)


class KeyIn(BaseModel):
    label: str = Field(default="default", min_length=1, max_length=60)


class ClickIn(BaseModel):
    event_id: str = Field(min_length=8, max_length=120, pattern=r"^[A-Za-z0-9_\-:.]+$")
    ad_id: str = Field(min_length=1, max_length=120)
    campaign_id: str | None = Field(default=None, max_length=120)
    viewer_id: str = Field(min_length=1, max_length=120)
    timestamp: datetime
    ip: str | None = Field(default=None, max_length=64)
    country: str = Field(pattern=r"^[A-Za-z]{2}$")
    device: Literal["mobile", "desktop", "tablet", "other"]

    @field_validator("country")
    @classmethod
    def upper_country(cls, v: str) -> str:
        return v.upper()

    @field_validator("timestamp")
    @classmethod
    def within_bounds(cls, v: datetime) -> datetime:
        if v.tzinfo is None:
            raise ValueError("timestamp must include a timezone, e.g. 2026-09-28T10:20:00Z")
        now = datetime.now(timezone.utc)
        if v < now - MAX_AGE:
            raise ValueError("timestamp older than 24 hours")
        if v > now + MAX_FUTURE:
            raise ValueError("timestamp more than 5 minutes in the future")
        return v.astimezone(timezone.utc)


class ClickBatch(BaseModel):
    events: list[ClickIn] = Field(min_length=1, max_length=500)
