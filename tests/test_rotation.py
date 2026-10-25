"""Rotation must be pure: same inputs, same episode, in any order."""

from __future__ import annotations

from datetime import date

import pytest

from nostalgia.rotation import (
    EpisodeRef,
    NoEpisodesError,
    build_rings,
    cycle_length,
    order_episodes,
    ordinal_for,
    select,
)


def _episode(show: str, season: int, episode: int, duration: int = 660_000) -> EpisodeRef:
    return EpisodeRef(
        key=f"{show}:s{season:02d}e{episode:03d}",
        show_id=show,
        season=season,
        episode=episode,
        path=f"/media/{show}/s{season:02d}e{episode:03d}.mkv",
        duration_ms=duration,
    )


@pytest.fixture()
def episodes() -> list[EpisodeRef]:
    return [
        _episode("athf", 1, index)
        for index in range(1, 5)
    ] + [
        _episode("sealab", 1, index)
        for index in range(1, 3)
    ]


def test_select_is_pure(episodes: list[EpisodeRef]) -> None:
    rings = build_rings(episodes, {})
    ring = rings["athf"]
    anchor = date(2001, 9, 2)
    day = date(2003, 6, 16)

    first = select(ring, day=day, anchor=anchor, slots_per_day=4, slot_in_day=2)
    for _ in range(5):
        assert select(ring, day=day, anchor=anchor, slots_per_day=4, slot_in_day=2) == first


def test_out_of_order_requests_agree(episodes: list[EpisodeRef]) -> None:
    rings = build_rings(episodes, {})
    ring = rings["athf"]
    anchor = date(2001, 9, 2)

    later = select(ring, day=date(2003, 7, 1), anchor=anchor, slots_per_day=4)
    earlier = select(ring, day=date(2003, 6, 16), anchor=anchor, slots_per_day=4)
    # asking again in the other order must not shift anything
    assert select(ring, day=date(2003, 7, 1), anchor=anchor, slots_per_day=4) == later
    assert select(ring, day=date(2003, 6, 16), anchor=anchor, slots_per_day=4) == earlier


def test_ordinal_advances_one_per_play(episodes: list[EpisodeRef]) -> None:
    anchor = date(2001, 9, 2)
    base = ordinal_for("athf", day=anchor, anchor=anchor, slots_per_day=3, slot_in_day=0)
    next_slot = ordinal_for("athf", day=anchor, anchor=anchor, slots_per_day=3, slot_in_day=1)
    next_day = ordinal_for(
        "athf", day=date(2001, 9, 3), anchor=anchor, slots_per_day=3, slot_in_day=0
    )
    assert next_slot == base + 1
    assert next_day == base + 3


def test_rotation_wraps_without_repeating_early(episodes: list[EpisodeRef]) -> None:
    rings = build_rings(episodes, {})
    ring = rings["athf"]
    anchor = date(2001, 9, 2)
    seen = [
        select(ring, day=anchor, anchor=anchor, slots_per_day=1, slot_in_day=0).key
    ]
    # advance by asking for successive days, one play each
    from datetime import timedelta

    for offset in range(1, len(ring)):
        chosen = select(
            ring,
            day=anchor + timedelta(days=offset),
            anchor=anchor,
            slots_per_day=1,
            slot_in_day=0,
        )
        assert chosen.key not in seen
        seen.append(chosen.key)
    assert len(seen) == len(ring)

    # and then it wraps to the start
    wrapped = select(
        ring,
        day=anchor + timedelta(days=len(ring)),
        anchor=anchor,
        slots_per_day=1,
        slot_in_day=0,
    )
    assert wrapped.key == seen[0]


def test_shuffle_is_stable_and_a_permutation(episodes: list[EpisodeRef]) -> None:
    first = order_episodes(episodes[:4], order="shuffle", show_id="athf", seed_salt="test")
    second = order_episodes(episodes[:4], order="shuffle", show_id="athf", seed_salt="test")
    assert [ep.key for ep in first] == [ep.key for ep in second]
    assert {ep.key for ep in first} == {ep.key for ep in episodes[:4]}


def test_episode_order_is_season_then_episode(episodes: list[EpisodeRef]) -> None:
    mixed = [
        _episode("x", 2, 1),
        _episode("x", 1, 10),
        _episode("x", 1, 2),
    ]
    ordered = order_episodes(mixed, order="episode", show_id="x")
    assert [(ep.season, ep.episode) for ep in ordered] == [(1, 2), (1, 10), (2, 1)]


def test_empty_show_raises(episodes: list[EpisodeRef]) -> None:
    with pytest.raises(NoEpisodesError):
        order_episodes([], order="episode", show_id="empty")


def test_cycle_length_is_the_lcm(episodes: list[EpisodeRef]) -> None:
    rings = build_rings(episodes, {})
    # 4 ATHF episodes, 2 Sealab episodes -> lcm 4
    assert cycle_length(rings) == 4
