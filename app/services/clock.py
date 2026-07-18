from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Optional
from zoneinfo import ZoneInfo

from app.config import get_settings


def local_now(
    value: Optional[datetime] = None,
    timezone_name: Optional[str] = None,
) -> datetime:
    current = value or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    zone = ZoneInfo(timezone_name or get_settings().timezone)
    return current.astimezone(zone)


def local_today(
    value: Optional[datetime] = None,
    timezone_name: Optional[str] = None,
) -> date:
    return local_now(value, timezone_name).date()


def format_local_datetime(value: Optional[datetime]) -> str:
    if value is None:
        return "-"
    localized = local_now(value)
    return localized.strftime("%Y-%m-%d %H:%M %Z")
