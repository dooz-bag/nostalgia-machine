"""Deterministic episode rotation.

The cursor model matters more than it looks. A naive "advance a counter each time
we air" cursor is stateful: previewing tomorrow's plan would consume tomorrow's
episodes, and rebuilding a day would drift the whole week.

So rotation here is **pure**: the episode for a slot is a function of

    (show, slot ordinal across the run, ring length)

with no mutable state at all. Ask for the same slot twice and you get the same
episode; ask for day 400 and day 1 in either order and both are correct. The
"episode N of the run" position comes from a *slot ordinal* rather than a cursor,
which is what lets the plan be regenerated from scratch every time.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Iterable, Mapping, Sequence

from .rng import rng_for


class NoEpisodesError(LookupError):
    pass


@dataclass(frozen=True, slots=True)
class EpisodeRef:
    """A single playable episode."""

    key: str
    """Stable identity, e.g. ``athf:s01e01``."""

    show_id: str
    season: int
    episode: int
    path: str
    duration_ms: int
    title: str = ""
    order_key: str = ""
    """Sort key for ``order: chronological`` (release date, else season/episode)."""

    carries_bug: bool | None = None
    """Whether the picture already has a network corner bug of its own.

    ``None`` means the file has not been examined, which is not the same as
    "no bug present": only a definite ``False`` may have one stamped over it,
    or an episode could end up wearing two.
    """

    @property
    def label(self) -> str:
        code = f"S{self.season:02d}E{self.episode:03d}"
        return f"{code} {self.title}".strip()


@dataclass(frozen=True, slots=True)
class RotationRing:
    """A show's ordered episode list plus how it is walked."""

    show_id: str
    episodes: tuple[EpisodeRef, ...]
    order: str = "episode"

    def __len__(self) -> int:
        return len(self.episodes)

    def at(self, ordinal: int) -> EpisodeRef:
        if not self.episodes:
            raise NoEpisodesError(f"{self.show_id}: no episodes")
        return self.episodes[ordinal % len(self.episodes)]

    def runs_until_repeat(self, ordinal: int) -> int:
        """How many more plays before *ordinal* would repeat an episode."""
        return len(self.episodes) - (ordinal % len(self.episodes))


def episodes_per_day(slot_count: int) -> int:
    """Plays of one show per day, used to turn a date into a rotation ordinal."""
    return max(1, slot_count)


def ordinal_for(
    show_id: str,
    *,
    day: date,
    anchor: date,
    slots_per_day: int,
    slot_in_day: int = 0,
) -> int:
    """Absolute play index for a show at a given day and intra-day position."""
    day_index = (day - anchor).days
    return day_index * episodes_per_day(slots_per_day) + slot_in_day


def select(
    ring: RotationRing,
    *,
    day: date,
    anchor: date,
    slots_per_day: int,
    slot_in_day: int = 0,
) -> EpisodeRef:
    """The episode a show airs at a given day/position. Pure and repeatable."""
    if not ring.episodes:
        raise NoEpisodesError(f"{ring.show_id}: no episodes")
    ordinal = ordinal_for(
        ring.show_id, day=day, anchor=anchor, slots_per_day=slots_per_day, slot_in_day=slot_in_day
    )
    return ring.at(ordinal)


def order_episodes(
    episodes: Sequence[EpisodeRef],
    *,
    order: str,
    show_id: str,
    seed_salt: object = "",
) -> list[EpisodeRef]:
    """Return the rotation ring for a show.

    ``episode`` sorts by ``(season, episode)``; ``chronological`` by release date;
    ``shuffle`` produces a *stable* permutation, so the same show always shuffles
    the same way and a rebuilt plan matches the previous one exactly.
    """
    if not episodes:
        raise NoEpisodesError(f"{show_id}: no episodes found")

    if order == "chronological":
        return sorted(
            episodes,
            key=lambda ep: (ep.order_key or f"{ep.season:04d}{ep.episode:04d}", ep.key),
        )
    if order == "shuffle":
        rng = rng_for("rotation", show_id, seed_salt)
        pool = sorted(episodes, key=lambda ep: (ep.season, ep.episode, ep.key))
        rng.shuffle(pool)
        return pool
    return sorted(episodes, key=lambda ep: (ep.season, ep.episode, ep.key))


def build_rings(
    episodes: Iterable[EpisodeRef],
    orders: Mapping[str, str],
    *,
    seed_salt: object = "",
) -> dict[str, RotationRing]:
    """Group episodes per show and build each ring."""
    grouped: dict[str, list[EpisodeRef]] = {}
    for episode in episodes:
        grouped.setdefault(episode.show_id, []).append(episode)
    return {
        show_id: RotationRing(
            show_id=show_id,
            episodes=tuple(
                order_episodes(
                    items,
                    order=orders.get(show_id, "episode"),
                    show_id=show_id,
                    seed_salt=seed_salt,
                )
            ),
            order=orders.get(show_id, "episode"),
        )
        for show_id, items in sorted(grouped.items())
    }


def cycle_length(rings: Mapping[str, RotationRing]) -> int:
    """Plays after which every show's rotation has returned to its start.

    Used by ``doctor`` to tell you how long the channel runs before repeating.
    """
    from math import lcm

    lengths = [len(ring) for ring in rings.values() if ring.episodes]
    if not lengths:
        return 0
    result = 1
    for length in lengths:
        result = lcm(result, length)
    return result


__all__ = [
    "EpisodeRef",
    "NoEpisodesError",
    "RotationRing",
    "build_rings",
    "cycle_length",
    "episodes_per_day",
    "order_episodes",
    "ordinal_for",
    "select",
]
