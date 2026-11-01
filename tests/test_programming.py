"""Hand programming: lineup, shuffle, and the grid they generate.

The scheduler's promise is determinism, and hand programming must not weaken it.
Every test here is written so that a non-deterministic implementation -- a real
RNG, a cursor, a wall-clock read -- would fail rather than merely look different.
"""

from __future__ import annotations

from datetime import date

import pytest

from nostalgia import programming
from nostalgia.config import ProgrammingConfig
from nostalgia.grid import day_bounds_ms, quantum_ms
from nostalgia.plan import PlanContext, build_plan
from nostalgia.rotation import build_rings

DAY = date(2003, 6, 4)
NEXT_DAY = date(2003, 6, 5)


def _config(base, **overrides):
    return base.model_copy(
        update={"programming": ProgrammingConfig(**overrides)}
    )


def _rings(root, catalog):
    orders = {show_id: show.order for show_id, show in root.shows.items()}
    return build_rings(catalog.episodes(), orders, seed_salt=root.channel.name)


def _context(root, catalog, paths, rings):
    assets = catalog.assets()
    return PlanContext(
        root=root,
        catalog=catalog,
        rings=rings,
        bumps=[asset for asset in assets if asset.kind == "bump"],
        ads=[asset for asset in assets if asset.kind == "ad"],
        idents=[asset for asset in assets if asset.kind == "ident"],
        last_aired=catalog.last_aired(),
        era_file=paths.config_dir / "bumps-era.yaml",
        generated_dir=paths.media("bumps", "generated"),
    )


# -- pick_episode -----------------------------------------------------------


def test_sequential_walks_the_ring_in_order(config, catalog):
    rings = programming.rings_for(config, catalog.episodes())
    ring = rings["aqua-teen-hunger-force"]
    picked = [
        programming.pick_episode(ring, "sequential", position) for position in range(4)
    ]
    assert picked == list(ring.episodes[:4])


def test_sequential_wraps_around(config, catalog):
    rings = programming.rings_for(config, catalog.episodes())
    ring = rings["aqua-teen-hunger-force"]
    assert (
        programming.pick_episode(ring, "sequential", len(ring.episodes))
        == ring.episodes[0]
    )


def test_random_is_stable_for_a_position(config, catalog):
    rings = programming.rings_for(config, catalog.episodes())
    ring = rings["aqua-teen-hunger-force"]
    first = programming.pick_episode(ring, "random", 3)
    second = programming.pick_episode(ring, "random", 3)
    assert first == second


def test_random_does_not_always_pick_the_first_episode(config, catalog):
    """A stable hash must still look random; index 0 for everything is a bug."""
    rings = programming.rings_for(config, catalog.episodes())
    ring = rings["aqua-teen-hunger-force"]
    picks = {
        programming.pick_episode(ring, "random", position).key for position in range(12)
    }
    assert len(picks) > 1


def test_by_season_weighs_seasons_not_episodes(config, catalog):
    """A show with one huge season must not dominate its own airtime.

    The fixture's seasons are all the same size, so a uniform pick cannot reveal
    the difference here. The assertion is instead that every season is reachable
    and that the choice is stable per position, which is what equal-season
    weighting plus a hashed index guarantees.
    """
    rings = programming.rings_for(config, catalog.episodes())
    ring = rings["aqua-teen-hunger-force"]
    seasons = {
        programming.pick_episode(ring, "by_season", position).season
        for position in range(24)
    }
    assert seasons == {episode.season for episode in ring.episodes}


def test_by_season_is_stable(config, catalog):
    rings = programming.rings_for(config, catalog.episodes())
    ring = rings["aqua-teen-hunger-force"]
    first = programming.pick_episode(ring, "by_season", 5)
    second = programming.pick_episode(ring, "by_season", 5)
    assert first == second


# -- lineup -----------------------------------------------------------------


def _lineup(config, catalog, *, entries, pinned=(), slots=8):
    root = _config(
        config,
        mode="lineup",
        lineup=[{"show_id": show_id, "order": order} for show_id, order in entries],
        pinned=list(pinned),
    )
    return root, programming.build_lineup(
        root, catalog.episodes(), slots_per_day=slots, day=DAY
    )


def test_lineup_cycles_shows_in_order(config, catalog):
    _root, report = _lineup(
        config,
        catalog,
        entries=[("futurama", "sequential"), ("sealab-2021", "random")],
        slots=6,
    )
    assert [slot.show_id for slot in report.slots] == [
        "futurama",
        "sealab-2021",
        "futurama",
        "sealab-2021",
        "futurama",
        "sealab-2021",
    ]


def test_lineup_sequential_advances_one_episode_per_appearance(config, catalog):
    _root, report = _lineup(
        config, catalog, entries=[("futurama", "sequential")], slots=3
    )
    assert [slot.episode.episode for slot in report.slots] == [1, 2, 3]


def test_lineup_sequential_starts_at_configured_episode(config, catalog):
    root = _config(
        config,
        mode="lineup",
        lineup=[
            {
                "show_id": "futurama",
                "order": "sequential",
                "start_season": 1,
                "start_episode": 3,
            }
        ],
    )
    report = programming.build_lineup(
        root, catalog.episodes(), slots_per_day=3, day=DAY
    )
    assert [
        (slot.episode.season, slot.episode.episode) for slot in report.slots
    ] == [(1, 3), (2, 1), (2, 2)]


def test_lineup_sequential_can_start_at_first_episode_of_season(config, catalog):
    root = _config(
        config,
        mode="lineup",
        lineup=[
            {
                "show_id": "futurama",
                "order": "sequential",
                "start_season": 2,
            }
        ],
    )
    report = programming.build_lineup(
        root, catalog.episodes(), slots_per_day=2, day=DAY
    )
    assert [(slot.episode.season, slot.episode.episode) for slot in report.slots] == [
        (2, 1),
        (2, 2),
    ]


def test_lineup_random_ignores_configured_start(config, catalog):
    root = _config(
        config,
        mode="lineup",
        lineup=[
            {
                "show_id": "futurama",
                "order": "random",
                "start_season": 1,
                "start_episode": 3,
            }
        ],
    )
    report = programming.build_lineup(
        root, catalog.episodes(), slots_per_day=6, day=DAY
    )
    rings = programming.rings_for(root, catalog.episodes())
    expected = [
        programming.pick_episode(rings["futurama"], "random", i, seed_parts=(DAY.isoformat(),))
        for i in range(6)
    ]
    assert [slot.episode for slot in report.slots] == expected


def test_sequential_continues_after_last_aired_episode(config, catalog):
    root = _config(
        config,
        mode="lineup",
        lineup=[{"show_id": "futurama", "order": "sequential"}],
    )
    report = programming.build_lineup(
        root,
        catalog.episodes(),
        slots_per_day=2,
        day=NEXT_DAY,
        last_episodes={"futurama": "futurama:s01e003"},
    )
    assert [
        (slot.episode.season, slot.episode.episode) for slot in report.slots
    ] == [(2, 1), (2, 2)]


def test_sequential_shuffle_continues_after_last_aired_episode(config, catalog):
    root = _config(
        config,
        mode="shuffle",
        shuffle_shows=["futurama"],
        shuffle_order="sequential",
    )
    report = programming.build_shuffle(
        root,
        catalog.episodes(),
        slots_per_day=2,
        day=NEXT_DAY,
        last_episodes={"futurama": "futurama:s01e003"},
    )
    assert [
        (slot.episode.season, slot.episode.episode) for slot in report.slots
    ] == [(2, 1), (2, 2)]


def test_lineup_does_not_reshuffle_at_midnight(config, catalog):
    """Lineup order is a property of the config, not of the day."""
    root, _ = _lineup(
        config, catalog, entries=[("futurama", "sequential")], slots=4
    )
    today = programming.build_lineup(
        root, catalog.episodes(), slots_per_day=4, day=DAY
    )
    tomorrow = programming.build_lineup(
        root, catalog.episodes(), slots_per_day=4, day=NEXT_DAY
    )
    assert [slot.show_id for slot in today.slots] == [
        slot.show_id for slot in tomorrow.slots
    ]


def test_pinned_episode_plays_verbatim(config, catalog):
    _root, report = _lineup(
        config,
        catalog,
        entries=[("futurama", "sequential")],
        pinned=["aqua-teen-hunger-force:s02e003"],
        slots=4,
    )
    pinned = [slot for slot in report.slots if slot.show_id == "aqua-teen-hunger-force"]
    assert pinned, "the pinned episode never appears"
    assert {(slot.episode.season, slot.episode.episode) for slot in pinned} == {(2, 3)}


def test_pinned_episode_missing_from_library_is_reported(config, catalog):
    _root, report = _lineup(
        config,
        catalog,
        entries=[("futurama", "sequential")],
        pinned=["futurama:s09e099"],
        slots=2,
    )
    assert any("not in the library" in warning for warning in report.warnings)
    assert all(slot.show_id == "futurama" for slot in report.slots)


def test_lineup_drops_a_show_with_no_episodes(config, catalog):
    root = _config(
        config,
        mode="lineup",
        lineup=[
            {"show_id": "futurama", "order": "sequential"},
            {"show_id": "not-a-show", "order": "random"},
        ],
    )
    report = programming.build_lineup(
        root, catalog.episodes(), slots_per_day=2, day=DAY
    )
    assert report.dropped == ["not-a-show"]
    assert {slot.show_id for slot in report.slots} == {"futurama"}


def test_lineup_with_nothing_catalogued_is_an_error(config, catalog):
    """Silently airing nothing would be worse than refusing to plan."""
    root = _config(config, mode="lineup", lineup=[{"show_id": "ghost"}])
    with pytest.raises(programming.ProgrammingError, match="lineup is empty"):
        programming.build_lineup(
            root, catalog.episodes(), slots_per_day=2, day=DAY
        )


# -- shuffle ----------------------------------------------------------------


def test_shuffle_uses_every_show_once_per_pass(config, catalog):
    root = _config(config, mode="shuffle", shuffle_order="by_season")
    report = programming.build_shuffle(
        root, catalog.episodes(), slots_per_day=8, day=DAY
    )
    shown = [slot.show_id for slot in report.slots]
    assert len(set(shown[:4])) == 4, shown
    assert shown[:4] == shown[4:8]


def test_shuffle_respects_the_show_selection(config, catalog):
    root = _config(
        config, mode="shuffle", shuffle_shows=["futurama"], shuffle_order="random"
    )
    report = programming.build_shuffle(
        root, catalog.episodes(), slots_per_day=4, day=DAY
    )
    assert {slot.show_id for slot in report.slots} == {"futurama"}


def test_shuffle_turn_order_is_not_weighted_by_episode_count(config, catalog):
    """Every show gets one turn per pass regardless of library size.

    ``aqua-teen-hunger-force`` has more episodes than ``king-of-the-hill``; a
    proportional sample would give it more turns and the stream would feel less
    varied, which is the opposite of what "shuffle everything" should do.
    """
    root = _config(config, mode="shuffle", shuffle_order="random")
    report = programming.build_shuffle(
        root, catalog.episodes(), slots_per_day=12, day=DAY
    )
    counts: dict[str, int] = {}
    for slot in report.slots:
        counts[slot.show_id] = counts.get(slot.show_id, 0) + 1
    assert set(counts.values()) == {3}


# -- build_programmed_day ---------------------------------------------------


def test_programmed_day_tiles_the_day_exactly(config, catalog):
    root = _config(
        config,
        mode="lineup",
        lineup=[{"show_id": "futurama", "order": "sequential"}],
    )
    slots, placed = programming.build_programmed_day(
        root, catalog.episodes(), day=DAY
    )
    day_start, day_end = day_bounds_ms(DAY, root.channel.grid)
    assert slots[0].start_ms == day_start
    assert slots[-1].end_ms == day_end
    assert all(a.end_ms == b.start_ms for a, b in zip(slots, slots[1:]))
    assert len(placed) == len(slots)


def test_programmed_day_slots_are_whole_quanta(config, catalog):
    root = _config(
        config,
        mode="lineup",
        lineup=[
            {"show_id": "futurama", "order": "sequential"},
            {"show_id": "aqua-teen-hunger-force", "order": "random"},
        ],
    )
    slots, _placed = programming.build_programmed_day(
        root, catalog.episodes(), day=DAY
    )
    quantum = quantum_ms(root.channel.grid)
    for slot in slots:
        assert slot.length_ms % quantum == 0, slot


def test_programmed_day_never_puts_an_episode_in_a_short_slot(config, catalog):
    """The bug this whole function exists to prevent.

    A 22-minute episode in a 15-minute slot makes the planner refuse to emit.
    Sizing each slot from its own show's family is what avoids it.
    """
    root = _config(
        config,
        mode="lineup",
        lineup=[
            {"show_id": "futurama", "order": "sequential"},
            {"show_id": "aqua-teen-hunger-force", "order": "random"},
        ],
    )
    slots, placed = programming.build_programmed_day(
        root, catalog.episodes(), day=DAY
    )
    for slot, entry in zip(slots, placed):
        assert entry.episode.duration_ms <= slot.length_ms, (slot.index, entry.show_id)


def test_programmed_day_is_deterministic(config, catalog):
    root = _config(
        config, mode="shuffle", shuffle_order="by_season"
    )
    first = programming.build_programmed_day(root, catalog.episodes(), day=DAY)
    second = programming.build_programmed_day(root, catalog.episodes(), day=DAY)
    assert [(s.start_ms, s.end_ms) for s in first[0]] == [
        (s.start_ms, s.end_ms) for s in second[0]
    ]
    assert [e.episode.key for e in first[1]] == [e.episode.key for e in second[1]]


def test_programmed_day_moves_on_the_next_day(config, catalog):
    root = _config(config, mode="shuffle", shuffle_order="random")
    today = programming.build_programmed_day(root, catalog.episodes(), day=DAY)
    tomorrow = programming.build_programmed_day(root, catalog.episodes(), day=NEXT_DAY)
    assert [e.episode.key for e in today[1]] != [e.episode.key for e in tomorrow[1]]


def test_programmed_day_refuses_without_a_programming_mode(config, catalog):
    with pytest.raises(programming.ProgrammingError, match="without a programming mode"):
        programming.build_programmed_day(config, catalog.episodes(), day=DAY)


# -- dispatch ---------------------------------------------------------------


def test_build_programming_returns_none_for_dayparts(config, catalog):
    assert (
        programming.build_programming(
            config, catalog.episodes(), slots_per_day=4, day=DAY
        )
        is None
    )


@pytest.mark.parametrize(
    ("mode", "extra"),
    [
        ("lineup", {"lineup": [{"show_id": "futurama"}]}),
        ("shuffle", {"shuffle_order": "random"}),
    ],
)
def test_build_programming_dispatches_on_mode(config, catalog, mode, extra):
    root = _config(config, mode=mode, **extra)
    report = programming.build_programming(
        root, catalog.episodes(), slots_per_day=2, day=DAY
    )
    assert report is not None
    assert report.slots


# -- planner integration ----------------------------------------------------


@pytest.mark.parametrize(
    ("mode", "extra"),
    [
        ("lineup", {"lineup": [{"show_id": "futurama", "order": "sequential"}]}),
        ("lineup", {"pinned": ["aqua-teen-hunger-force:s02e001"]}),
        ("shuffle", {"shuffle_order": "by_season"}),
        ("shuffle", {"shuffle_shows": ["sealab-2021"], "shuffle_order": "random"}),
    ],
)
def test_build_plan_accepts_every_programming_mode(
    config, catalog, paths, mode, extra
):
    root = _config(config, mode=mode, **extra)
    context = _context(root, catalog, paths, _rings(root, catalog))
    plan = build_plan(DAY, root.days[0], context)
    assert plan.validate() == []
    assert plan.episode_items()


def test_build_plan_uses_the_pinned_episode(config, catalog, paths):
    root = _config(
        config,
        mode="lineup",
        lineup=[{"show_id": "futurama", "order": "sequential"}],
        pinned=["aqua-teen-hunger-force:s02e002"],
    )
    context = _context(root, catalog, paths, _rings(root, catalog))
    plan = build_plan(DAY, root.days[0], context)
    subtitles = {
        item.subtitle
        for item in plan.episode_items()
        if item.show_id == "aqua-teen-hunger-force"
    }
    assert "S02E02" in subtitles


def test_build_plan_keeps_the_daypart_schedule_when_programming_is_off(
    config, catalog, paths
):
    context = _context(config, catalog, paths, _rings(config, catalog))
    plan = build_plan(DAY, config.days[0], context)
    dayparts = {item.note for item in plan.items if item.note}
    assert any("lineup" not in note for note in dayparts)
