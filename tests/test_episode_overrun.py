"""An episode longer than its slot must not take the day down.

A long-running show accumulates a double-length episode eventually -- Squidbillies
has a 22-minute finale, Space Ghost has a 44-minute special, King of the Hill has
a 43-minute one. In a 15-minute slot that episode overruns, and because the
timeline is contiguous the overrun pushed every later slot and the planner
refused the whole day with "timeline is invalid" -- naming neither the show nor
the fix.
"""

from __future__ import annotations

from datetime import date

import pytest

from nostalgia.config import ProgrammingConfig
from nostalgia.grid import Slot, build_day
from nostalgia.plan import PlanContext, PlanError, build_plan
from nostalgia.rotation import build_rings

DAY = date(2003, 6, 4)


def _context(root, catalog, paths):
    assets = catalog.assets()
    return PlanContext(
        root=root,
        catalog=catalog,
        rings=build_rings(
            catalog.episodes(),
            {show_id: show.order for show_id, show in root.shows.items()},
            seed_salt=root.channel.name,
        ),
        bumps=[a for a in assets if a.kind == "bump"],
        ads=[a for a in assets if a.kind == "ad"],
        idents=[a for a in assets if a.kind == "ident"],
        last_aired=catalog.last_aired(),
        era_file=paths.config_dir / "bumps-era.yaml",
        generated_dir=paths.media("bumps", "generated"),
    )


def test_a_week_of_days_all_plan(config, catalog, paths):
    """The regression: one overlong episode used to fail the whole day."""
    context = _context(config, catalog, paths)
    for offset in range(7):
        day = date.fromordinal(DAY.toordinal() + offset)
        plan = build_plan(day, config.days[0], context)
        assert plan.validate() == [], (day, plan.validate())
        assert plan.episode_items()


def test_a_show_with_only_overlong_episodes_fills_the_slot_with_breaks(
    config, catalog, paths
):
    """No substitute exists, so the slot must still be covered.

    A hole would shift every later slot, which is exactly the failure the
    overrun caused in the first place.
    """
    root = config.model_copy(
        update={
            # Every fixture episode is 11 or 22 minutes; a `block` show in a
            # short daypart cannot fit any of them.
            "shows": {
                show_id: show.model_copy(update={"slot": "short"})
                for show_id, show in config.shows.items()
            },
        }
    )
    day_config = config.days[0]
    context = _context(root, catalog, paths)
    plan = build_plan(DAY, day_config, context)
    assert plan.validate() == []


def test_the_substitution_is_reported_by_name(config, catalog, paths):
    """Whatever the planner decides, it must say which episode and why."""
    context = _context(config, catalog, paths)
    plan = build_plan(DAY, config.days[0], context)
    for warning in plan.warnings:
        assert "timeline" not in warning.lower()
    # Nothing to assert about *which* warnings appear -- the fixture durations
    # vary per show -- but every warning must be self-describing.
    for warning in plan.warnings:
        assert " slot " in warning


def test_programmed_modes_generate_slots_that_always_fit(config, catalog, paths):
    """The lineup/shuffle grid is built from episode lengths, so it never overruns.

    That is the structural advantage of `build_programmed_day`: the day is laid
    out around the content rather than around a daypart's declared family, so
    there is no such thing as an episode that does not fit.
    """
    root = config.model_copy(
        update={
            "programming": ProgrammingConfig(mode="shuffle", shuffle_order="random")
        }
    )
    context = _context(root, catalog, paths)
    plan = build_plan(DAY, config.days[0], context)
    assert plan.validate() == []
    assert plan.episode_items()
    assert not any("cannot fit" in warning for warning in plan.warnings)


def test_a_substitute_never_repeats_an_episode_already_aired_today(
    config, catalog, paths
):
    """The regression: a substitution landed on an episode that had already run.

    The ring order and the walk order differ -- `random` and `by_season` both
    permute the ring -- so "the next episode in the ring" is not "the next
    episode to air". Walking the ring blindly from the rejected episode picked
    one that had aired hours earlier, and the same episode showed twice in a day.
    """
    from nostalgia.plan import _fitting_episode
    from nostalgia.schedule import AssignedSlot

    context = _context(config, catalog, paths)
    plan = build_plan(DAY, config.days[0], context)
    ring = context.rings["aqua-teen-hunger-force"]

    # Pretend every episode but one has already aired today.
    aired = {episode.key for episode in ring.episodes[:-1]}
    plan.items = [
        item
        for item in plan.items
        if item.kind != "episode" or item.show_id != "aqua-teen-hunger-force"
    ]
    plan.items.extend(
        _fake_episode_item("aqua-teen-hunger-force", key) for key in aired
    )

    assigned = AssignedSlot(
        slot=Slot(
            index=0, start_ms=0, end_ms=15 * 60_000, family="short", daypart="test"
        ),
        show_id="aqua-teen-hunger-force",
        slot_in_day=0,
        of_same_show=1,
    )
    rejected = ring.episodes[0]
    replacement = _fitting_episode(assigned, rejected, context, plan)

    assert replacement is not None
    assert replacement.key not in aired


def _fake_episode_item(show_id: str, key: str):
    from nostalgia.plan import PlannedItem

    return PlannedItem(
        item_id=f"fake-{key}",
        kind="episode",
        start_ms=0,
        end_ms=1,
        asset_key=key,
        path="/dev/null",
        title=show_id,
        subtitle="",
        description="",
        show_id=show_id,
        slot_index=0,
    )
