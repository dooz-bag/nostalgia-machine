"""The broadcast grid.

The grid is the contract that makes the channel feel like television: every
program starts on an ``:00``/``:15``/``:30``/``:45`` boundary, and the sum of a
slot's segments equals the slot length exactly. Everything downstream (schedule,
continuity, ads, emit) assumes these two invariants hold.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta

from .config import FAMILY_QUANTA, DayConfig, Daypart, GridConfig
from .timeutil import DAY_MS, MINUTE_MS, epoch_ms, local_instant, parse_hhmm, tz

MIDNIGHT = "00:00"


class GridError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class Slot:
    """One uninterrupted programme position on the grid."""

    index: int
    start_ms: int
    end_ms: int
    family: str
    daypart: str
    """Title of the owning daypart, used for logs and the EPG."""

    template: str | None = None
    """Continuity template the daypart asked for, if any.

    Carried on the slot because the planner resolves a slot's break structure
    from here; without it a daypart's `template:` was validated and then silently
    ignored, so a custom break length could never take effect.
    """

    @property
    def length_ms(self) -> int:
        return self.end_ms - self.start_ms

    def contains(self, ms: int) -> bool:
        return self.start_ms <= ms < self.end_ms


def quantum_ms(config: GridConfig) -> int:
    return config.quantum_minutes * MINUTE_MS


def day_bounds_ms(day: date, config: GridConfig) -> tuple[int, int]:
    """Local-midnight to local-midnight, in epoch ms (DST-safe)."""
    zone = tz(config.timezone)
    start = local_instant(day, parse_hhmm(MIDNIGHT), zone)
    end = local_instant(day + timedelta(days=1), parse_hhmm(MIDNIGHT), zone)
    return epoch_ms(start), epoch_ms(end)


def is_dst_transition_day(day: date, config: GridConfig) -> bool:
    zone = tz(config.timezone)
    start = local_instant(day, parse_hhmm(MIDNIGHT), zone)
    return start.utcoffset() != (start + timedelta(days=1)).utcoffset()


def _minutes_of(value) -> int:
    return value.hour * 60 + value.minute


def _quanta_for(family: str) -> int:
    if family not in FAMILY_QUANTA:
        aligned = ", ".join(sorted(FAMILY_QUANTA))
        raise GridError(f"slot family {family!r} is not grid-aligned; use one of {aligned}")
    return FAMILY_QUANTA[family]


def infer_family(span_ms: int, config: GridConfig) -> str:
    """Pick the coarsest family that divides *span_ms* exactly."""
    quantum = quantum_ms(config)
    quanta_total, remainder = divmod(span_ms, quantum)
    if remainder:
        raise GridError(
            f"span of {span_ms / MINUTE_MS:g} min is not a whole number of "
            f"{config.quantum_minutes}-minute quanta"
        )
    for family in ("hour", "half", "short"):
        if quanta_total % FAMILY_QUANTA[family] == 0:
            return family
    raise GridError(f"cannot infer a slot family for a {span_ms}ms span")


def build_day(day: date, day_config: DayConfig, config: GridConfig) -> list[Slot]:
    """Expand a day file into grid-aligned slots.

    Dayparts are resolved in declared order. A daypart with no ``end`` runs until
    the next daypart's start, or to midnight when it is last. Slot counts are
    derived from the span, so a day that straddles a DST change still fills the
    real day rather than a nominal 24 hours.
    """
    zone = tz(config.timezone)
    day_start, day_end = day_bounds_ms(day, config)
    quantum = quantum_ms(config)

    declared = list(day_config.dayparts)
    if not declared:
        raise GridError(f"{day_config.name}: no dayparts")

    for index in range(1, len(declared)):
        if _minutes_of(declared[index].start) <= _minutes_of(declared[index - 1].start):
            previous = declared[index - 1]
            raise GridError(
                f"{day_config.name}: dayparts must ascend by start time "
                f"({previous.start} then {declared[index].start})"
            )

    # An explicit `end` on a non-final daypart must agree with where the next one
    # starts. Silently ignoring it would hide a real authoring mistake.
    for index, part in enumerate(declared[:-1]):
        if part.end is None:
            continue
        declared_end = _minutes_of(part.end)
        next_start = _minutes_of(declared[index + 1].start)
        if declared_end != next_start:
            raise GridError(
                f"{day_config.name}: daypart {part.start:%H:%M} declares end "
                f"{part.end:%H:%M} but the next daypart starts at "
                f"{declared[index + 1].start:%H:%M} -- these overlap or leave a gap"
            )

    slots: list[Slot] = []
    cursor_ms = day_start

    for index, part in enumerate(declared):
        begin_ms = epoch_ms(local_instant(day, part.start, zone))
        finish_ms = _resolve_finish(day, declared, index, part, zone, day_end)

        if begin_ms < cursor_ms:
            raise GridError(
                f"{day_config.name}: daypart {part.start} overlaps an earlier daypart"
            )
        if begin_ms > cursor_ms:
            gap = begin_ms - cursor_ms
            if gap % quantum:
                raise GridError(
                    f"{day_config.name}: the gap before {part.start} "
                    f"({gap / MINUTE_MS:g} min) is not a whole number of quanta"
                )
            cursor_ms = begin_ms

        span = finish_ms - begin_ms
        if span <= 0:
            continue

        family = part.family or infer_family(span, config)
        unit = _quanta_for(family) * quantum

        if part.slots is not None:
            if part.slots * unit != span:
                raise GridError(
                    f"{day_config.name}: daypart {part.start} declares {part.slots} "
                    f"{family} slots ({part.slots * unit // MINUTE_MS} min) but spans "
                    f"{span / MINUTE_MS:g} min"
                )
            count = part.slots
        else:
            if span % unit:
                raise GridError(
                    f"{day_config.name}: daypart {part.start} spans "
                    f"{span / MINUTE_MS:g} min, which is not a whole number of "
                    f"{family} slots ({unit // MINUTE_MS} min each)"
                )
            count = span // unit

        label = part.title or f"{part.start:%H:%M}-{_label_finish(part)}"
        for offset in range(count):
            slot_start = begin_ms + offset * unit
            slots.append(
                Slot(
                    index=len(slots),
                    start_ms=slot_start,
                    end_ms=slot_start + unit,
                    family=family,
                    daypart=label,
                    template=part.template,
                )
            )
        cursor_ms = finish_ms

    if cursor_ms < day_end and (day_end - cursor_ms) % quantum:
        raise GridError(
            f"{day_config.name}: trailing {(day_end - cursor_ms) / MINUTE_MS:g} min "
            f"is not grid-aligned"
        )
    return slots


def _resolve_finish(
    day: date,
    declared: list[Daypart],
    index: int,
    part: Daypart,
    zone,
    day_end: int,
) -> int:
    begin = epoch_ms(local_instant(day, part.start, zone))
    if index + 1 < len(declared):
        return epoch_ms(local_instant(day, declared[index + 1].start, zone))
    if part.end is not None:
        finish = local_instant(day, part.end, zone)
        if epoch_ms(finish) <= begin:
            finish += timedelta(days=1)
        return min(epoch_ms(finish), day_end)
    return day_end


def _label_finish(part: Daypart) -> str:
    return f"{part.end:%H:%M}" if part.end else "end"


def validate_day(day_config: DayConfig, config: GridConfig) -> list[str]:
    """Return warnings about a day file; ``doctor`` prints them."""
    warnings: list[str] = []
    for part in day_config.dayparts:
        if not part.family:
            warnings.append(
                f"{day_config.name}: daypart {part.start:%H:%M} has no 'family'; "
                f"it will be inferred from the span"
            )
    return warnings


def format_clock(ms: int, config: GridConfig) -> str:
    """``HH:MM:SS`` in the channel timezone -- for logs and the web UI."""
    return datetime.fromtimestamp(ms / 1000, tz(config.timezone)).strftime("%H:%M:%S")


def format_wall(ms: int, config: GridConfig) -> str:
    return datetime.fromtimestamp(ms / 1000, tz(config.timezone)).strftime("%Y-%m-%d %H:%M:%S")


__all__ = [
    "FAMILY_QUANTA",
    "GridError",
    "Slot",
    "build_day",
    "day_bounds_ms",
    "format_clock",
    "format_wall",
    "infer_family",
    "is_dst_transition_day",
    "quantum_ms",
    "validate_day",
]
