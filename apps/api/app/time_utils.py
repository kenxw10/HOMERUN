from __future__ import annotations

from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

from app.config import get_settings

TIME_BUCKETS: tuple[tuple[str, int], ...] = (
    ("24H", 24 * 60),
    ("18H", 18 * 60),
    ("12H", 12 * 60),
    ("10H", 10 * 60),
    ("8H", 8 * 60),
    ("6H", 6 * 60),
    ("4H", 4 * 60),
    ("3H", 3 * 60),
    ("2H", 2 * 60),
    ("90M", 90),
    ("60M", 60),
    ("30M", 30),
    ("15M", 15),
    ("5M", 5),
)


def utc_now() -> datetime:
    return datetime.now(UTC)


def today_eastern() -> date:
    return utc_now().astimezone(get_dashboard_zone()).date()


def get_dashboard_zone() -> ZoneInfo:
    return ZoneInfo(get_settings().dashboard_timezone)


def ensure_aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def parse_datetime(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None

    normalized = value.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None

    return ensure_aware_utc(parsed)


def to_eastern_iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    return ensure_aware_utc(value).astimezone(get_dashboard_zone()).isoformat()


def eastern_display(value: datetime | None) -> str | None:
    if value is None:
        return None
    local = ensure_aware_utc(value).astimezone(get_dashboard_zone())
    return local.strftime("%b %d, %Y %I:%M %p %Z").upper()


def classify_time_bucket(minutes_to_start: int | float | None) -> str:
    if minutes_to_start is None:
        return "MARKET_OPEN"

    if minutes_to_start <= 5:
        return "5M"

    for label, minutes in TIME_BUCKETS:
        if minutes_to_start >= minutes:
            return label

    return "5M"
