"""Programming -- deciding *what* airs in each slot.

Given a day's slots (from :mod:`nostalgia.grid`) and a day file, this module
assigns a show to every slot. Three rules govern it, in order:

1. **Daypart membership.** Only shows listed in the covering daypart may air.
2. **Eligibility window.** A show is skipped outside its ``airing_from`` /
   ``airing_to`` window when ``eligibility: window`` (the default). This is what
   makes ``king-of-the-hill`` a config change instead of a code change.
3. **Weighted rotation.** Within a daypart, shows take turns. The turn order is
   deterministic (seeded on the date), so a rebuilt day is identical, and the
   order varies day to day so the channel does not feel like a metronome.

A daypart listing one show simply repeats that show (a "marathon block"); one
listing several shows alternates them.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Callable

from .config import DayConfig, Daypart, RootConfig, ShowConfig
from .grid import Slot
from .rng import rng_for, shuffled, weighted_choice
from .rotation import EpisodeRef


class ScheduleError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class AssignedSlot:
    slot: Slot
    show_id: str
    slot_in_day: int
    """Ordinal of this show within the day, used as the rotation position."""

    of_same_show: int
    """How many times this show airs today (for rotation spans)."""

    episode: EpisodeRef | None = None
    """The exact episode, when hand programming already chose it.

    ``None`` means "derive it from the show's rotation ring", which is what the
    day-file scheduler does. Set by :mod:`nostalgia.programming`.
    """


@dataclass
class ScheduleReport:
    slots: list[AssignedSlot] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    skipped: dict[str, str] = field(default_factory=dict)

    def show_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for assigned in self.slots:
            counts[assigned.show_id] = counts.get(assigned.show_id, 0) + 1
        return counts


def is_eligible(show: ShowConfig, day: date) -> bool:
    """Whether *show* may air on *day*."""
    if not show.enable:
        return False
    if show.eligibility == "always":
        return True
    if show.airing_from and day < show.airing_from:
        return False
    if show.airing_to and day > show.airing_to:
        return False
    return True


def assign_day(
    slots: list[Slot],
    day_config: DayConfig,
    root: RootConfig,
    day: date,
) -> ScheduleReport:
    """Assign a show to every slot in a day."""
    report = ScheduleReport()
    if not slots:
        return report

    # Group slots by daypart so rotation happens within a block, not across the day.
    groups: list[tuple[str, list[Slot]]] = []
    for slot in slots:
        if groups and groups[-1][0] == slot.daypart:
            groups[-1][1].append(slot)
        else:
            groups.append((slot.daypart, [slot]))

    # Pass 1: which show airs in each slot. Rotation happens within a daypart, so
    # a block listing one show is a marathon and a block listing several alternates
    # them.
    placed: list[tuple[Slot, str]] = []
    for daypart_label, group in groups:
        part = _resolve_part(daypart_label, day_config, group[0], root)
        if part is None:
            report.warnings.append(f"no daypart matched {daypart_label}; slots left unfilled")
            continue

        eligible = [show_id for show_id in part.shows if is_eligible(root.shows[show_id], day)]
        for show_id in part.shows:
            if show_id not in eligible:
                show = root.shows[show_id]
                reason = "disabled" if not show.enable else "outside airing window"
                report.skipped[show_id] = reason

        if not eligible:
            report.warnings.append(
                f"daypart {daypart_label}: every show is ineligible on {day}; "
                f"slots will fall back"
            )
            eligible = [show_id for show_id in part.shows if root.shows[show_id].enable]
            if not eligible:
                continue

        rotation = _rotation_order(eligible, root, day, daypart_label)
        for position, slot in enumerate(group):
            placed.append((slot, rotation[position % len(rotation)]))

    # Pass 2: the rotation position.
    #
    # `of_same_show` is the stride between one day and the next, so it has to be
    # the show's count for the *whole day*, not for whichever daypart it happens
    # to sit in. Counting per daypart gave a show listed in two blocks two
    # different strides, which advanced two colliding rotation tracks and brought
    # episodes back long before the ring was walked -- 2119 premature repeats over
    # 60 days on the shipped day files.
    totals: dict[str, int] = {}
    for _, show_id in placed:
        totals[show_id] = totals.get(show_id, 0) + 1

    seen: dict[str, int] = {}
    for slot, show_id in placed:
        position = seen.get(show_id, 0)
        seen[show_id] = position + 1
        report.slots.append(
            AssignedSlot(
                slot=slot,
                show_id=show_id,
                slot_in_day=position,
                of_same_show=totals[show_id],
            )
        )

    report.slots.sort(key=lambda assigned: assigned.slot.start_ms)
    return report


def _resolve_part(
    label: str, day_config: DayConfig, slot: Slot, root: RootConfig
) -> Daypart | None:
    for part in day_config.dayparts:
        if (part.title or f"{part.start:%H:%M}-{_label_finish(part)}") == label:
            return part
    for part in day_config.dayparts:
        if part.title == label:
            return part
    return None


def _label_finish(part: Daypart) -> str:
    return f"{part.end:%H:%M}" if part.end else "end"


def _rotation_order(
    show_ids: list[str], root: RootConfig, day: date, daypart_label: str
) -> list[str]:
    """Order shows within a daypart for the day.

    Deterministic per ``(date, daypart)``. A single-show block is returned as-is.
    A multi-show block gets a weighted permutation with no self-repeats, and the
    block cycles through it, so heavier shows appear more often while adjacent
    slots never repeat the same show.
    """
    if len(show_ids) == 1:
        return list(show_ids)

    rng = rng_for("daypart-order", day.isoformat(), daypart_label)
    pool = sorted(show_ids)
    weights = [root.shows[show_id].weight for show_id in pool]

    order: list[str] = []
    remaining = list(zip(pool, weights))
    last: str | None = None
    while remaining:
        options = [item for item in remaining if item[0] != last] or remaining
        picked = weighted_choice(rng, [item[0] for item in options], [item[1] for item in options])
        order.append(picked)
        last = picked
        remaining = [item for item in remaining if item[0] != picked]

    # Repeat the permutation until it covers the block, re-shuffling each pass.
    return order


def rotation_span(assigned: AssignedSlot) -> int:
    return max(1, assigned.of_same_show)


class PlayCounts:
    """How many times each show airs on a given day, from the day files.

    The rotation ordinal is a running total of plays, so it needs the show's real
    count for every day between the anchor and the day being planned -- not just
    for the day in hand. Weekday and weekend files give a show different counts, so
    assuming a constant stride makes the rotation jump at every weekend boundary.

    Counts are cached per *date*, not per weekday: a show's airing window can open
    or close mid-week, so two Mondays a year apart need not play it the same number
    of times. Each date is resolved with a single ``assign_day`` call covering every
    show at once.

    Derived from the day files alone, so it stays pure: previewing a day cannot
    change what any other day plays.
    """

    def __init__(self, root: RootConfig) -> None:
        self._root = root
        self._by_day: dict[date, dict[str, int]] = {}
        self._cursor: date | None = None
        self._running: dict[str, int] = {}

    def for_show(self, show_id: str) -> Callable[[date], int]:
        """A function giving *show_id*'s play count for any day."""
        return lambda day: self._counts(day).get(show_id, 0)

    def plays_before(self, show_id: str, anchor: date) -> Callable[[date], int]:
        """A function giving *show_id*'s total plays on every day before *day*.

        This is the rotation ordinal's base. It is memoised as a running total so
        planning a day costs one step, not one step per day since the anchor.
        """
        return lambda day: self._total_before(show_id, day, anchor)

    def _total_before(self, show_id: str, day: date, anchor: date) -> int:
        if day <= anchor:
            return 0
        if self._cursor is None or self._cursor < anchor:
            self._cursor = anchor
            self._running = {}
        if day < self._cursor:
            # Planning normally moves forward, so this only happens when a caller
            # asks for an earlier day after a later one. Re-summing from the anchor
            # is cheap because every day's counts are already cached.
            total = 0
            cursor = anchor
            while cursor < day:
                total += self._counts(cursor).get(show_id, 0)
                cursor += timedelta(days=1)
            return total
        while self._cursor < day:
            for key, count in self._counts(self._cursor).items():
                self._running[key] = self._running.get(key, 0) + count
            self._cursor += timedelta(days=1)
        return self._running.get(show_id, 0)

    def _counts(self, day: date) -> dict[str, int]:
        counts = self._by_day.get(day)
        if counts is None:
            counts = self._by_day[day] = _plays_on_day(self._root, day)
        return counts


def _plays_on_day(root: RootConfig, day: date) -> dict[str, int]:
    """How many slots each show fills on *day*, from the day files."""
    from .grid import build_day
    from .pipeline import day_config_for

    try:
        day_config = day_config_for(root, day)
        slots = build_day(day, day_config, root.channel.grid)
    except (ValueError, KeyError):
        return {}
    counts: dict[str, int] = {}
    for assigned in assign_day(slots, day_config, root, day).slots:
        counts[assigned.show_id] = counts.get(assigned.show_id, 0) + 1
    return counts


def interleave(show_ids: list[str], count: int, *, seed_parts: tuple = ()) -> list[str]:
    """Round-robin a show list to *count* slots, preserving order determinism."""
    if not show_ids:
        raise ScheduleError("cannot interleave an empty show list")
    rng = rng_for("interleave", *seed_parts)
    order = shuffled(rng, show_ids)
    return [order[index % len(order)] for index in range(count)]


__all__ = [
    "AssignedSlot",
    "ScheduleError",
    "ScheduleReport",
    "assign_day",
    "interleave",
    "is_eligible",
    "PlayCounts",
    "rotation_span",
]
