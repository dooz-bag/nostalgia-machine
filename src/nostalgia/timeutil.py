"""Time helpers.

All durations are integer milliseconds and all instants are timezone-aware
``datetime`` objects. Naive datetimes are never accepted at a boundary.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

UTC = timezone.utc

SECOND_MS = 1000
MINUTE_MS = 60 * SECOND_MS
HOUR_MS = 60 * MINUTE_MS
DAY_MS = 24 * HOUR_MS


def tz(name: str) -> ZoneInfo:
    return ZoneInfo(name)


def epoch_ms(dt: datetime) -> int:
    if dt.tzinfo is None:
        raise ValueError(f"naive datetime not allowed: {dt!r}")
    delta = dt.astimezone(UTC) - datetime(1970, 1, 1, tzinfo=UTC)
    return round(delta / timedelta(milliseconds=1))


def from_epoch_ms(ms: int, zone: ZoneInfo) -> datetime:
    return (datetime(1970, 1, 1, tzinfo=UTC) + timedelta(milliseconds=ms)).astimezone(zone)


def parse_hhmm(value: str) -> time:
    """Parse ``"21:00"`` / ``"9:00pm"`` / ``"2100"`` into a :class:`datetime.time`."""
    raw = str(value).strip().lower()
    if not raw:
        raise ValueError("empty time")

    suffix = ""
    for candidate in ("am", "pm"):
        if raw.endswith(candidate):
            suffix = candidate
            raw = raw[: -len(candidate)].strip()
            break

    if ":" in raw:
        hours, minutes = raw.split(":", 1)
    elif raw.isdigit() and len(raw) in (3, 4):
        hours, minutes = raw[:-2], raw[-2:]
    elif raw.isdigit():
        hours, minutes = raw, "0"
    else:
        raise ValueError(f"cannot parse time: {value!r}")

    hour = int(hours)
    minute = int(minutes)
    if suffix == "pm" and hour < 12:
        hour += 12
    elif suffix == "am" and hour == 12:
        hour = 0
    return time(hour, minute)


def local_instant(day: date, at: time, zone: ZoneInfo) -> datetime:
    """Attach *zone* to a wall-clock date/time.

    Ambiguous times (DST fall-back) resolve to the first occurrence; this keeps
    day boundaries stable regardless of platform-specific ``fold`` defaults.
    """
    return datetime.combine(day, at).replace(tzinfo=zone, fold=0)


def rfc3339(dt: datetime, *, millis: bool = True) -> str:
    """RFC 3339 with an explicit offset, e.g. ``2001-10-02T23:30:00.000-04:00``."""
    if dt.tzinfo is None:
        raise ValueError("naive datetime not allowed")
    base = dt.strftime("%Y-%m-%dT%H:%M:%S")
    if millis:
        base += f".{dt.microsecond // 1000:03d}"
    offset = dt.strftime("%z")
    return f"{base}{offset[:3]}:{offset[3:]}"


def compact_stamp(dt: datetime) -> str:
    """Compact ISO 8601 used in ErsatzTV Next playout filenames."""
    return f"{dt.strftime('%Y%m%dT%H%M%S')}.{dt.microsecond * 1000:09d}{dt.strftime('%z')}"


def human(ms: int) -> str:
    sign = "-" if ms < 0 else ""
    ms = abs(ms)
    hours, rem = divmod(ms, HOUR_MS)
    minutes, rem = divmod(rem, MINUTE_MS)
    seconds = rem / SECOND_MS
    if hours:
        return f"{sign}{hours}:{minutes:02d}:{seconds:04.1f}"
    return f"{sign}{minutes}:{seconds:04.1f}"
