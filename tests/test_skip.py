"""Skipping the programme that is on now.

A live channel cannot "skip" the way a video player can: the timeline is written in
advance and ErsatzTV plays it by wall clock, so the only honest implementation is to
change what the file says. These tests pin the two things that makes that safe --
the day still tiles exactly, and the replacement is genuinely the next episode.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from nostalgia.catalog import Catalog
from nostalgia.plan import Plan, PlanContext, build_plan
from nostalgia.pipeline import day_config_for, scan
from nostalgia.skip import (
    OverrideFile,
    Skip,
    SkipError,
    clear_skip,
    load_overrides,
    mark_used,
    pending_for,
    prune,
    request_skip,
    save_overrides,
)

DAY = date(2003, 6, 16)


@pytest.fixture()
def context(config, catalog) -> PlanContext:
    return PlanContext.load(config, catalog)


@pytest.fixture()
def plan(config, context) -> Plan:
    return build_plan(DAY, day_config_for(config, DAY), context)


def _first_episode_slot(plan: Plan) -> int:
    episodes = plan.episode_items()
    assert episodes
    return episodes[0].slot_index


def _skip_for(plan: Plan, slot_index: int) -> dict[int, Skip]:
    playing = next(item for item in plan.episode_items() if item.slot_index == slot_index)
    return {
        slot_index: Skip(
            day=plan.day.isoformat(),
            slot_index=slot_index,
            episode_key=playing.asset_key,
        )
    }


# -- the override file ----------------------------------------------------


def test_an_absent_file_is_not_an_error(tmp_path: Path) -> None:
    """A channel that has never been skipped must not need a file to exist."""
    assert load_overrides(tmp_path / "skips.json").skips == []


def test_a_truncated_file_does_not_stop_the_channel(tmp_path: Path) -> None:
    """Losing one requested skip is recoverable; refusing to plan is not."""
    path = tmp_path / "skips.json"
    path.write_text('{"skips": [{"day": "2003-06-16"', encoding="utf-8")
    assert load_overrides(path).skips == []


def test_a_skip_round_trips(tmp_path: Path) -> None:
    path = tmp_path / "skips.json"
    written = request_skip(path, day=DAY, slot_index=40, episode_key="athf:s01e01")
    read = load_overrides(path).skips
    assert len(read) == 1
    assert read[0] == written
    assert read[0].slot_index == 40
    assert read[0].day == "2003-06-16"
    assert read[0].used is False


def test_requesting_the_same_slot_twice_is_idempotent(tmp_path: Path) -> None:
    """Double-clicking the button must not queue two skips for one programme."""
    path = tmp_path / "skips.json"
    first = request_skip(path, day=DAY, slot_index=40, episode_key="athf:s01e01")
    second = request_skip(path, day=DAY, slot_index=40, episode_key="athf:s01e01")
    assert first == second
    assert len(load_overrides(path).skips) == 1


def test_clearing_a_skip_forgets_it(tmp_path: Path) -> None:
    path = tmp_path / "skips.json"
    request_skip(path, day=DAY, slot_index=40, episode_key=None)
    assert clear_skip(path, day=DAY, slot_index=40) is True
    assert load_overrides(path).skips == []
    assert clear_skip(path, day=DAY, slot_index=40) is False


def test_marking_used_leaves_earlier_days_alone(tmp_path: Path) -> None:
    path = tmp_path / "skips.json"
    request_skip(path, day=DAY, slot_index=40, episode_key="a")
    request_skip(path, day=DAY, slot_index=41, episode_key="b")
    pending = pending_for(load_overrides(path), DAY)
    mark_used(path, {40: pending[40]})

    after = load_overrides(path).skips
    assert {skip.slot_index: skip.used for skip in after} == {40: True, 41: False}


def test_pruning_drops_finished_days(tmp_path: Path) -> None:
    path = tmp_path / "skips.json"
    request_skip(path, day=date(2003, 6, 15), slot_index=1, episode_key=None)
    request_skip(path, day=DAY, slot_index=1, episode_key=None)
    assert prune(path, before=DAY) == 1
    assert [skip.day for skip in load_overrides(path).skips] == ["2003-06-16"]


# -- the plan override ----------------------------------------------------


def test_a_skip_replaces_the_episode_and_keeps_the_day_whole(
    config, context, plan: Plan
) -> None:
    """The headline requirement, stated as the invariant it protects.

    Swapping in the next episode must not shift anything: the timeline still
    starts at the day start, still ends at the day end, and still has no gap or
    overlap anywhere. A hole here is black video on a live stream.
    """
    slot_index = _first_episode_slot(plan)
    before = next(item for item in plan.episode_items() if item.slot_index == slot_index)

    skipped = build_plan(
        DAY,
        day_config_for(config, DAY),
        context,
        skips=_skip_for(plan, slot_index),
    )

    assert skipped.validate() == [], skipped.validate()
    assert skipped.duration_ms() == plan.duration_ms()
    after = next(item for item in skipped.episode_items() if item.slot_index == slot_index)
    assert after.asset_key != before.asset_key, "the episode did not change"
    assert after.show_id == before.show_id, "a skip must not change the show"
    assert after.start_ms == before.start_ms, "the slot moved"


def test_a_skip_keeps_the_slot_on_its_own_boundaries(config, context, plan: Plan) -> None:
    """The replacement must live inside the slot it inherited, not spill into the next."""
    slot_index = _first_episode_slot(plan)
    skipped = build_plan(
        DAY, day_config_for(config, DAY), context, skips=_skip_for(plan, slot_index)
    )
    occupied = [
        item for item in skipped.items if item.slot_index == slot_index
    ]
    assert occupied
    # Every item in the slot is still tagged with that slot, and the next slot's
    # first item starts when this one's last item ends.
    following = [item for item in skipped.items if item.slot_index > slot_index]
    if following:
        assert occupied[-1].end_ms == following[0].start_ms


def test_a_skip_without_a_plan_is_a_warning_not_a_crash(config, context) -> None:
    """A stale click is normal: the day may have been rebuilt since."""
    skips = {9999: Skip(day=DAY.isoformat(), slot_index=9999)}
    built = build_plan(DAY, day_config_for(config, DAY), context, skips=skips)
    assert built.validate() == []
    assert any("skip ignored" in warning for warning in built.warnings)


def test_a_stale_skip_does_not_replace_a_different_episode(config, context, plan: Plan) -> None:
    """If the episode changed underneath the request, do nothing.

    Otherwise a skip recorded against slot 40 would fire hours later and swap out
    an episode nobody was watching when they clicked.
    """
    slot_index = _first_episode_slot(plan)
    stale = {
        slot_index: Skip(
            day=DAY.isoformat(), slot_index=slot_index, episode_key="not-what-is-airing"
        )
    }
    built = build_plan(DAY, day_config_for(config, DAY), context, skips=stale)
    assert next(i for i in built.episode_items() if i.slot_index == slot_index).asset_key == (
        next(i for i in plan.episode_items() if i.slot_index == slot_index).asset_key
    )
    assert any("no longer the episode airing" in warning for warning in built.warnings)


def test_the_replacement_is_playable_and_has_an_episode_number(config, context, plan: Plan) -> None:
    slot_index = _first_episode_slot(plan)
    skipped = build_plan(
        DAY, day_config_for(config, DAY), context, skips=_skip_for(plan, slot_index)
    )
    item = next(i for i in skipped.episode_items() if i.slot_index == slot_index)
    assert item.subtitle.startswith("S")
    assert item.path
    assert item.duration_ms > 0


def test_a_skip_does_not_touch_any_other_slot(config, context, plan: Plan) -> None:
    """Only the skipped slot may differ; the rest of the day is untouched."""
    slot_index = _first_episode_slot(plan)
    skipped = build_plan(
        DAY, day_config_for(config, DAY), context, skips=_skip_for(plan, slot_index)
    )
    untouched = [
        item.asset_key
        for item in plan.episode_items()
        if item.slot_index != slot_index
    ]
    after = [
        item.asset_key
        for item in skipped.episode_items()
        if item.slot_index != slot_index
    ]
    assert after == untouched


def test_no_skips_leaves_the_day_byte_identical(config, context, plan: Plan) -> None:
    """The override path must be a no-op when nothing is pending."""
    again = build_plan(DAY, day_config_for(config, DAY), context, skips={})
    assert [(i.item_id, i.start_ms, i.end_ms) for i in again.items] == [
        (i.item_id, i.start_ms, i.end_ms) for i in plan.items
    ]
