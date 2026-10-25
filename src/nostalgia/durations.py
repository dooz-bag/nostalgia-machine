"""Duration parsing.

Internally every duration is an integer number of milliseconds. Config files may
use a bare number (seconds) or a suffixed string: ``"500ms"``, ``"4s"``,
``"1.5s"``, ``"2m"``, ``"1h"``.
"""

from __future__ import annotations

import re

from .timeutil import HOUR_MS, MINUTE_MS, SECOND_MS

_RE = re.compile(r"^\s*(?P<value>\d+(?:\.\d+)?)\s*(?P<unit>ms|s|m|h)?\s*$", re.IGNORECASE)
_SCALE = {"ms": 1, "s": SECOND_MS, "m": MINUTE_MS, "h": HOUR_MS}


def parse_duration_ms(value: object, *, default_unit: str = "s") -> int:
    """Parse a config duration into milliseconds.

    ``None`` and empty strings are rejected so a typo cannot silently become 0.
    Bare numbers are interpreted using *default_unit* (seconds unless stated).
    """
    if isinstance(value, bool):
        raise ValueError(f"invalid duration: {value!r}")
    if isinstance(value, (int, float)):
        return round(float(value) * _SCALE[default_unit])
    if not isinstance(value, str):
        raise ValueError(f"invalid duration: {value!r}")

    match = _RE.match(value)
    if not match:
        raise ValueError(f"cannot parse duration {value!r} (try '30s', '1.5s' or '500ms')")
    unit = (match.group("unit") or default_unit).lower()
    return round(float(match.group("value")) * _SCALE[unit])


def format_duration(ms: int) -> str:
    """Human-readable duration, used in logs and the web UI."""
    if ms < 1000:
        return f"{ms}ms"
    seconds = ms / 1000
    if seconds < 60:
        return f"{seconds:g}s"
    minutes, rem = divmod(seconds, 60)
    if not rem:
        return f"{minutes:g}m"
    return f"{minutes:g}m{rem:g}s"
