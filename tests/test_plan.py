"""End-to-end plan properties: exactness, adjacency and determinism.

These are the tests that justify the architecture. If any of them fail, the
channel is not a channel.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from dataclasses import replace

from nostalgia.catalog import Catalog
from nostalgia.config import load_config
from nostalgia.emit import (
    EmitPaths,
    playout_filename,
    to_playout_json,
    to_xmltv,
    write_ersatztv_config,
    write_playout,
)
from nostalgia.grid import day_bounds_ms
from nostalgia.paths import Paths
from nostalgia.pipeline import build, scan
from nostalgia.plan import PlanContext, build_plan
from nostalgia.plan import Plan

DAY = date(2003, 6, 16)


def without_other_show_bumps(context: PlanContext) -> PlanContext:
    """*context* with only neutral and Aqua Teen cards left in the pool.

    The "a bump naming the wrong show is never chosen" guarantees are about a card
    that would *contradict* the running order, so they have to be checked with the
    neighbouring promos taken out -- otherwise they only prove that a promo is
    allowed, not that a lie never is.
    """
    keep = {"aqua-teen-hunger-force"}
    return replace(
        context,
        bumps=[asset for asset in context.bumps if asset.is_neutral_bump or asset.subject in keep],
    )


@pytest.fixture()
def context(config, catalog) -> PlanContext:
    return PlanContext.load(config, catalog)


@pytest.fixture()
def plan(config, context) -> Plan:
    from nostalgia.pipeline import day_config_for

    return build_plan(DAY, day_config_for(config, DAY), context)


def test_plan_timeline_is_contiguous(plan: Plan) -> None:
    assert plan.validate() == []


def test_plan_covers_the_whole_day(plan: Plan) -> None:
    start, end = plan.day_start_ms, plan.day_end_ms
    assert plan.items[0].start_ms == start
    assert plan.items[-1].end_ms == end
    assert plan.duration_ms() == end - start


def test_no_item_crosses_a_slot_boundary(plan: Plan) -> None:
    by_slot: dict[int, list] = {}
    for item in plan.items:
        by_slot.setdefault(item.slot_index, []).append(item)
    for items in by_slot.values():
        for previous, current in zip(items, items[1:]):
            assert current.start_ms == previous.end_ms


def test_every_slot_has_an_episode(plan: Plan) -> None:
    slots = {item.slot_index for item in plan.items}
    episode_slots = {item.slot_index for item in plan.episode_items()}
    assert slots == episode_slots


def test_a_promo_never_contradicts_the_running_order(plan: Plan, config) -> None:
    """The headline requirement, stated the way a viewer would notice a breach.

    A card naming a *different* show is fine -- that is a promo, and 2003 breaks
    were full of them. What must never happen is a card making a **positional
    claim** about a show it is not adjacent to: "next up: X" in front of Y, or
    "we now return to X" after Y. Those are checkable lies.
    """
    for index, item in enumerate(plan.items):
        if item.kind != "bump" or item.show_id is None:
            continue
        neighbours = _adjacent_shows(plan.items, index)
        claims_order = item.subtitle.rsplit(" ", 1)[-1] in {"into", "out", "next", "after"}
        if claims_order:
            assert item.show_id in neighbours, (
                f"{item.item_id} claims {item.subtitle!r} for {item.show_id} "
                f"but sits next to {neighbours}"
            )
        else:
            # No claim: the card is topical filler and may name any show at all,
            # as long as the show exists in the roster.
            assert item.show_id in config.shows


def test_a_promo_for_another_show_never_uses_a_positional_claim(plan: Plan) -> None:
    """The specific relaxation, asserted directly."""
    for index, item in enumerate(plan.items):
        if item.kind != "bump" or item.show_id is None:
            continue
        neighbours = _adjacent_shows(plan.items, index)
        if item.show_id in neighbours:
            continue
        assert not item.subtitle.rsplit(" ", 1)[-1] in {"into", "out", "next", "after"}, (
            f"{item.item_id} makes a positional claim about {item.show_id}"
        )


def test_into_bump_precedes_its_episode(plan: Plan) -> None:
    """An "into" card either names the episode's show or claims nothing at all."""
    for index, item in enumerate(plan.items):
        if item.kind != "bump" or not item.subtitle.endswith("into"):
            continue
        following = _next_episode(plan.items, index)
        if following is None:
            continue
        assert item.show_id in (following.show_id, None), (
            f"{item.item_id} says 'into' {item.show_id} but {following.show_id} plays next"
        )


def test_after_bump_follows_its_episode(plan: Plan) -> None:
    """An "after" card either names the episode's show or claims nothing at all."""
    for index, item in enumerate(plan.items):
        if item.kind != "bump" or not item.subtitle.endswith("after"):
            continue
        preceding = _previous_episode(plan.items, index)
        if preceding is None:
            continue
        assert item.show_id in (preceding.show_id, None), (
            f"{item.item_id} says 'after' {item.show_id} but {preceding.show_id} just played"
        )


def test_bump_before_an_episode_never_names_a_different_show(plan: Plan) -> None:
    """The card immediately before an episode must not be a promo for another one.

    A promo may air anywhere else in the break, but the last thing a viewer sees
    before the show starts is the one place a topical card would read as a lie.
    """
    for index, item in enumerate(plan.items):
        if item.kind != "episode" or index == 0:
            continue
        previous = plan.items[index - 1]
        if previous.kind == "bump" and previous.show_id:
            assert previous.show_id == item.show_id, (
                f"{previous.item_id} names {previous.show_id} immediately before "
                f"{item.show_id}"
            )


def test_next_bump_points_at_the_following_slot(plan: Plan) -> None:
    """A `next` bump at the tail names the show that actually follows.

    This is where the relaxed rule earns its keep: a promo for some *third* show
    is allowed in the break, but the closing card is the one that promises what is
    about to start, so it has to be either the right show or a general card.
    """
    slots = sorted({item.slot_index for item in plan.items})
    for slot_index in slots:
        tail_bumps = [
            item
            for item in plan.items
            if item.slot_index == slot_index and item.kind == "bump" and "next" in item.subtitle
        ]
        if not tail_bumps:
            continue
        later = [index for index in slots if index > slot_index]
        if not later:
            continue
        following = {
            item.show_id
            for item in plan.items
            if item.slot_index == later[0] and item.kind == "episode"
        }
        chosen = tail_bumps[0]
        if chosen.show_id is None:
            # A general card claims nothing, so it is always truthful here.
            continue
        assert chosen.show_id in following


def test_all_episodes_of_a_show_are_used_before_repeating(context, config) -> None:
    from nostalgia.pipeline import day_config_for

    seen: dict[str, set[str]] = {}
    months = [date(2003, 6, 16), date(2003, 6, 17), date(2003, 6, 18), date(2003, 6, 19)]
    for day in months:
        built = build_plan(day, day_config_for(config, day), context)
        for item in built.episode_items():
            seen.setdefault(item.show_id, set()).add(item.asset_key)
    for show_id, keys in seen.items():
        ring = context.rings[show_id]
        if len(ring) <= len(keys):
            assert keys == {episode.key for episode in ring.episodes}


def test_build_is_deterministic(config, catalog, paths: Paths) -> None:
    first = build(config, catalog, paths, start=DAY, days=1, emit=False, ignore_cooldowns=True)
    second = build(config, catalog, paths, start=DAY, days=1, emit=False, ignore_cooldowns=True)
    assert to_playout_json(first.plans[0]) == to_playout_json(second.plans[0])


def test_emitted_json_matches_on_a_rebuild(config, catalog, paths: Paths) -> None:
    build(config, catalog, paths, start=DAY, days=1, emit=True, ignore_cooldowns=True)
    target = next(paths.playout_dir.glob("*.json"))
    first = target.read_text(encoding="utf-8")
    build(config, catalog, paths, start=DAY, days=1, emit=True, ignore_cooldowns=True)
    assert target.read_text(encoding="utf-8") == first


def test_plan_is_independent_of_build_order(config, catalog) -> None:
    """Asking for a later day first must not shift an earlier day."""
    from nostalgia.pipeline import day_config_for

    context = PlanContext.load(config, catalog)
    early_day = date(2003, 6, 16)
    late_day = date(2003, 6, 20)

    late_first = build_plan(late_day, day_config_for(config, late_day), context)
    early_then = build_plan(early_day, day_config_for(config, early_day), context)
    late_again = build_plan(late_day, day_config_for(config, late_day), context)

    assert to_playout_json(late_first) == to_playout_json(late_again)
    assert to_playout_json(early_then) == to_playout_json(
        build_plan(early_day, day_config_for(config, early_day), context)
    )


def test_show_rotation_advances_across_days(config, catalog) -> None:
    from nostalgia.pipeline import day_config_for

    context = PlanContext.load(config, catalog)
    picks: list[str] = []
    for offset in range(3):
        day = date(2003, 6, 16 + offset)
        built = build_plan(day, day_config_for(config, day), context)
        first_athf = next(
            item.asset_key for item in built.items if item.show_id == "aqua-teen-hunger-force"
        )
        picks.append(first_athf)
    assert len(set(picks)) > 1, "the same episode aired three days running"


def test_ineligible_show_never_airs(config, catalog) -> None:
    """A show outside its window must not appear."""
    from nostalgia.pipeline import day_config_for

    mutated = config.model_copy(
        update={
            "shows": {
                **config.shows,
                "futurama": config.shows["futurama"].model_copy(
                    update={"eligibility": "window", "airing_from": date(2010, 1, 1)}
                ),
            }
        }
    )
    context = PlanContext.load(mutated, catalog)
    built = build_plan(DAY, day_config_for(mutated, DAY), context)
    assert all(item.show_id != "futurama" for item in built.items)


def test_eligibility_always_overrides_the_window(config, catalog) -> None:
    from nostalgia.pipeline import day_config_for

    mutated = config.model_copy(
        update={
            "shows": {
                **config.shows,
                "king-of-the-hill": config.shows["king-of-the-hill"].model_copy(
                    update={"eligibility": "always", "airing_from": date(2020, 1, 1)}
                ),
            }
        }
    )
    context = PlanContext.load(mutated, catalog)
    built = build_plan(DAY, day_config_for(mutated, DAY), context)
    assert any(item.show_id == "king-of-the-hill" for item in built.items)


def test_playout_filename_matches_ersatztv_convention(plan: Plan) -> None:
    from nostalgia.emit import set_channel_timezone

    set_channel_timezone("America/New_York")
    name = playout_filename(plan)
    assert name.endswith(".json")
    assert name.startswith("20030616T000000.")
    assert "_20030617T000000." in name


def test_playout_json_matches_the_published_shape(plan: Plan) -> None:
    from nostalgia.emit import set_channel_timezone

    set_channel_timezone("America/New_York")
    payload = to_playout_json(plan)
    assert payload["version"].startswith("https://ersatztv.org/playout/version/")
    assert payload["items"]
    for item in payload["items"]:
        assert set(item) >= {"id", "start", "finish", "source"}
        assert item["source"]["source_type"] in ("local", "lavfi")
        assert item["start"] < item["finish"]
        assert "+" in item["start"][-6:] or "-" in item["start"][-6:]
    ids = [item["id"] for item in payload["items"]]
    assert len(ids) == len(set(ids))


def test_synthetic_items_use_lavfi(plan: Plan) -> None:
    payload = to_playout_json(plan)
    synthetic = [item for item in payload["items"] if item["source"]["source_type"] == "lavfi"]
    for item in synthetic:
        assert "params" in item["source"]


def test_xmltv_only_contains_programmes(plan: Plan) -> None:
    from nostalgia.emit import set_channel_timezone

    set_channel_timezone("America/New_York")
    guide = to_xmltv(plan)
    assert guide.count("<programme ") == len(_merged_episodes(plan))
    assert "<commercial" not in guide


def test_xmltv_merges_mid_roll_parts(plan: Plan) -> None:
    from nostalgia.emit import set_channel_timezone

    set_channel_timezone("America/New_York")
    # 22-minute episodes are split in two by a mid-roll break.
    split = [item for item in plan.episode_items() if item.in_point_ms is not None]
    if not split:
        pytest.skip("this day has no mid-roll breaks")
    guide = to_xmltv(plan)
    assert guide.count("<programme ") < len(plan.episode_items())


def test_xmltv_is_continuous_across_breaks(plan: Plan) -> None:
    """Programmes must tile the day, as a real broadcast guide does.

    Regression: only episodes were listed, so the guide leaked a gap at every
    commercial break and most players rendered a broken schedule.
    """
    import xml.etree.ElementTree as ET

    from nostalgia.emit import set_channel_timezone

    set_channel_timezone("America/New_York")
    programmes = ET.fromstring(to_xmltv(plan)).findall("programme")
    assert programmes
    for previous, current in zip(programmes, programmes[1:]):
        assert previous.get("stop") == current.get("start")

    from nostalgia.emit import xmltv_stamp
    from nostalgia.timeutil import from_epoch_ms, tz

    zone = tz("America/New_York")
    assert programmes[0].get("start") == xmltv_stamp(from_epoch_ms(plan.day_start_ms, zone))
    assert programmes[-1].get("stop") == xmltv_stamp(from_epoch_ms(plan.day_end_ms, zone))


def test_xmltv_is_well_formed(plan: Plan) -> None:
    import xml.etree.ElementTree as ET

    from nostalgia.emit import set_channel_timezone

    set_channel_timezone("America/New_York")
    root = ET.fromstring(to_xmltv(plan))
    assert root.tag == "tv"
    channels = root.findall("channel")
    assert len(channels) == 1


def test_ersatztv_config_is_written(paths: Paths, plan: Plan) -> None:
    emit_paths = EmitPaths(paths.playout_dir, paths.xmltv_dir)
    lineup, channel = write_ersatztv_config(
        emit_paths,
        channel_number="1",
        channel_name="Test",
        tvg_id="test.1",
        width=1280,
        height=720,
        bitrate_kbps=2500,
        audio_bitrate_kbps=160,
    )
    payload = json.loads(lineup.read_text(encoding="utf-8"))
    assert payload["channels"][0]["tvg_id"] == "test.1"
    assert payload["channels"][0]["config"].endswith("channel.json")
    assert json.loads(channel.read_text(encoding="utf-8"))["playout"]["folder"]


def test_duration_ms_sums_to_the_day(plan: Plan) -> None:
    assert plan.duration_ms() == plan.day_end_ms - plan.day_start_ms


def test_no_warnings_on_a_healthy_library(plan: Plan) -> None:
    assert plan.warnings == [], plan.warnings


def test_every_slot_ends_on_its_up_next_bump(plan: Plan) -> None:
    """The closing card is the last thing in a slot, not something a pod elbowed out.

    Regression: the final ad pod used to expand to fill the slot, which swallowed
    the "up next" bump and produced a `no room left` warning on every slot.
    """
    by_slot: dict[int, list] = {}
    for item in plan.items:
        by_slot.setdefault(item.slot_index, []).append(item)
    for slot_index, items in by_slot.items():
        assert items[-1].kind == "bump", (slot_index, items[-1].kind, items[-1].title)
        assert "next" in items[-1].subtitle, (slot_index, items[-1].subtitle)


def test_break_tails_are_topped_up_with_promos_not_black(plan: Plan) -> None:
    """A break that comes up short gets a card, not seconds of black.

    Regression: the tail pod capped its spot count, leaving ~9 s of black card at
    the end of every slot.
    """
    black = [item for item in plan.items if item.subtitle == "black"]
    covered = sum(item.duration_ms for item in black)
    assert covered < plan.duration_ms() * 0.01, (
        f"{covered}ms of black cards across the day"
    )
    for item in black:
        assert item.duration_ms < 5_000, f"{item.duration_ms}ms black card in {item.slot_index}"


def test_promos_inside_a_break_are_short_and_never_claim_a_commercial(plan: Plan) -> None:
    for item in plan.items:
        if item.subtitle != "promo":
            continue
        assert item.kind == "bump"
        assert item.duration_ms <= 15_000
        # A promo is filler; it must not displace the slot's own continuity cards.
        assert "next" not in (item.note or "")


# -- helpers ---------------------------------------------------------------


def _next_episode(items: list, index: int):
    for item in items[index + 1 :]:
        if item.kind == "episode":
            return item
    return None


def _previous_episode(items: list, index: int):
    for item in reversed(items[:index]):
        if item.kind == "episode":
            return item
    return None


def _adjacent_shows(items: list, index: int) -> set[str | None]:
    """Shows immediately before and after the bump at *index*."""
    shows = set()
    for item in (_previous_episode(items, index), _next_episode(items, index)):
        if item is not None:
            shows.add(item.show_id)
    return shows


def _merged_episodes(plan: Plan) -> list[str]:
    keys: list[str] = []
    for item in plan.guide_items():
        if not keys or keys[-1] != item.asset_key:
            keys.append(item.asset_key)
    return keys
