"""Grid invariants: alignment, contiguity and DST behaviour."""

from __future__ import annotations

from datetime import date

import pytest

from nostalgia.config import DayConfig, Daypart, GridConfig
from nostalgia.grid import (
    GridError,
    build_day,
    day_bounds_ms,
    infer_family,
    is_dst_transition_day,
    quantum_ms,
)
from nostalgia.timeutil import MINUTE_MS, from_epoch_ms, local_instant, parse_hhmm, tz


@pytest.fixture()
def grid() -> GridConfig:
    return GridConfig(anchor=date(2001, 9, 2), quantum_minutes=15, timezone="America/New_York")


def _day(*parts: Daypart, name: str = "test") -> DayConfig:
    return DayConfig(name=name, days=["mon"], dayparts=list(parts))


def test_slots_are_contiguous_and_cover_the_day(grid: GridConfig) -> None:
    day = date(2003, 6, 16)
    slots = build_day(
        day,
        _day(
            Daypart(start=parse_hhmm("00:00"), end=parse_hhmm("12:00"), family="short", shows=["a"]),
            Daypart(start=parse_hhmm("12:00"), end=parse_hhmm("00:00"), family="half", shows=["b"]),
        ),
        grid,
    )
    start, end = day_bounds_ms(day, grid)
    assert slots[0].start_ms == start
    assert slots[-1].end_ms == end
    for previous, current in zip(slots, slots[1:]):
        assert previous.end_ms == current.start_ms
    for slot in slots:
        assert slot.start_ms % quantum_ms(grid) == 0


def test_every_slot_starts_on_a_quarter_hour(grid: GridConfig) -> None:
    zone = tz(grid.timezone)
    slots = build_day(
        date(2003, 6, 16),
        _day(Daypart(start=parse_hhmm("00:00"), end=parse_hhmm("00:00"), family="short", shows=["a"])),
        grid,
    )
    for slot in slots:
        moment = from_epoch_ms(slot.start_ms, zone)
        assert moment.minute % 15 == 0
        assert moment.second == 0


def test_spring_forward_day_is_23_hours(grid: GridConfig) -> None:
    day = date(2003, 4, 6)
    assert is_dst_transition_day(day, grid)
    start, end = day_bounds_ms(day, grid)
    assert (end - start) // (60 * MINUTE_MS) == 23

    slots = build_day(
        day,
        _day(Daypart(start=parse_hhmm("00:00"), end=parse_hhmm("00:00"), family="short", shows=["a"])),
        grid,
    )
    assert slots[0].start_ms == start
    assert slots[-1].end_ms == end
    for previous, current in zip(slots, slots[1:]):
        assert previous.end_ms == current.start_ms


def test_fall_back_day_is_25_hours(grid: GridConfig) -> None:
    day = date(2003, 10, 26)
    start, end = day_bounds_ms(day, grid)
    assert (end - start) // (60 * MINUTE_MS) == 25
    slots = build_day(
        day,
        _day(Daypart(start=parse_hhmm("00:00"), end=parse_hhmm("00:00"), family="short", shows=["a"])),
        grid,
    )
    assert slots[-1].end_ms == end


def test_implicit_end_runs_until_the_next_daypart(grid: GridConfig) -> None:
    slots = build_day(
        date(2003, 6, 16),
        _day(
            Daypart(start=parse_hhmm("00:00"), family="short", shows=["a"]),
            Daypart(start=parse_hhmm("02:00"), end=parse_hhmm("00:00"), family="half", shows=["b"]),
        ),
        grid,
    )
    assert slots[0].start_ms == day_bounds_ms(date(2003, 6, 16), grid)[0]
    # 00:00-02:00 = 8 short slots, then 02:00-24:00 = 44 half slots
    assert len(slots) == 8 + 44


def test_overlapping_dayparts_are_rejected(grid: GridConfig) -> None:
    with pytest.raises(GridError, match="overlap"):
        build_day(
            date(2003, 6, 16),
            _day(
                Daypart(start=parse_hhmm("00:00"), end=parse_hhmm("13:00"), family="short", shows=["a"]),
                Daypart(start=parse_hhmm("12:00"), end=parse_hhmm("00:00"), family="short", shows=["b"]),
            ),
            grid,
        )


def test_explicit_end_must_match_the_next_start(grid: GridConfig) -> None:
    with pytest.raises(GridError, match="overlap or leave a gap"):
        build_day(
            date(2003, 6, 16),
            _day(
                Daypart(start=parse_hhmm("00:00"), end=parse_hhmm("13:00"), family="short", shows=["a"]),
                Daypart(start=parse_hhmm("14:00"), end=parse_hhmm("00:00"), family="short", shows=["b"]),
            ),
            grid,
        )


def test_non_grid_aligned_span_is_rejected(grid: GridConfig) -> None:
    with pytest.raises(GridError):
        build_day(
            date(2003, 6, 16),
            _day(Daypart(start=parse_hhmm("00:00"), end=parse_hhmm("12:07"), family="short", shows=["a"])),
            grid,
        )


def test_slot_count_mismatch_is_rejected(grid: GridConfig) -> None:
    with pytest.raises(GridError, match="declares"):
        build_day(
            date(2003, 6, 16),
            _day(
                Daypart(
                    start=parse_hhmm("00:00"),
                    end=parse_hhmm("01:00"),
                    family="short",
                    shows=["a"],
                    slots=3,
                )
            ),
            grid,
        )


def test_infer_family_picks_the_coarsest_divisor(grid: GridConfig) -> None:
    assert infer_family(60 * MINUTE_MS, grid) == "hour"
    assert infer_family(30 * MINUTE_MS, grid) == "half"
    assert infer_family(15 * MINUTE_MS, grid) == "short"
    assert infer_family(45 * MINUTE_MS, grid) == "short"


def test_dayparts_must_ascend(grid: GridConfig) -> None:
    with pytest.raises(GridError, match="ascend"):
        build_day(
            date(2003, 6, 16),
            _day(
                Daypart(start=parse_hhmm("12:00"), end=parse_hhmm("18:00"), family="half", shows=["a"]),
                Daypart(start=parse_hhmm("06:00"), end=parse_hhmm("12:00"), family="half", shows=["b"]),
            ),
            grid,
        )


def test_local_instant_is_timezone_aware() -> None:
    zone = tz("America/New_York")
    moment = local_instant(date(2003, 6, 16), parse_hhmm("23:30"), zone)
    assert moment.utcoffset() is not None
    assert moment.hour == 23
