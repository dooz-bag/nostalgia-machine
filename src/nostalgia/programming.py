"""Hand programming: what airs when the day files are not in charge.

The day-file scheduler answers "what runs at 21:00", which is the right model for
recreating a real broadcast block. It is the wrong model for "play these shows",
so this module provides three simpler ones:

``lineup``    an explicit list, cycled. Either whole shows or pinned episodes.
``shuffle``   a continuous stream drawn from the whole collection, no repeats
              within a pass.

Two properties are non-negotiable, because the rest of the project depends on
them and because a channel that drifts is unwatchable:

* **Deterministic.** Random picks are a function of
  ``(day, slot ordinal, config)``. Sequential picks use the recorded last episode
  before the broadcast day, plus the prior planned day inside a horizon. Previewing
  future days cannot consume their episodes.
* **Never empty.** A lineup naming a show with no catalogued episodes is a
  configuration mistake, so it is dropped with a warning rather than producing a
  slot that cannot be filled.

The ordering helpers (:func:`pick_episode`) are pure functions of an index into a
ring, so the same episode choice logic serves lineup, shuffle and the day-file
scheduler.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import date
from functools import lru_cache
from typing import Mapping, Sequence

from .config import ProgrammingConfig, ProgrammingEntry, RootConfig
from .grid import Slot
from .rng import rng_for
from .rotation import EpisodeRef, RotationRing, build_rings


class ProgrammingError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class ProgrammeSlot:
    """A slot after hand programming has decided what airs in it."""

    index: int
    show_id: str
    episode: EpisodeRef


@dataclass(frozen=True, slots=True)
class PlacedSlot:
    """A grid slot together with what hand programming put in it."""

    slot: Slot
    show_id: str
    episode: EpisodeRef


@dataclass
class LineupReport:
    slots: list[ProgrammeSlot] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    dropped: list[str] = field(default_factory=list)
    """Show ids that were in the config but have nothing catalogued."""


def _parse_pinned(key: str) -> tuple[str, int, int] | None:
    """``athf:s01e002`` -> ``("athf", 1, 2)``."""
    if ":" not in key:
        return None
    show_id, _, rest = key.partition(":")
    if not rest.startswith("s") or "e" not in rest:
        return None
    season, _, episode = rest[1:].partition("e")
    if not (season.isdigit() and episode.isdigit()):
        return None
    return show_id, int(season), int(episode)


def find_episode(ring: RotationRing, season: int, episode: int) -> EpisodeRef | None:
    for candidate in ring.episodes:
        if candidate.season == season and candidate.episode == episode:
            return candidate
    return None


@lru_cache(maxsize=256)
def walk_order(
    ring: RotationRing,
    order: str,
    seed_parts: tuple[object, ...] = (),
) -> tuple[EpisodeRef, ...]:
    """The order a show's episodes are walked in, so one pass never repeats.

    This is the fix for "the same episodes keep coming back". The random modes
    used to hash ``(show, position, day)`` into an index, which is a fresh uniform
    draw *with replacement* every day: the ring was never walked, so episodes
    collided within a day and some never aired at all. A stable permutation walked
    by position instead guarantees every episode airs once before any repeats.

    The permutation is seeded on the show alone -- never the date -- because a
    date-seeded order would reshuffle daily and reintroduce the collisions. What
    carries the walk across days is the position, which the callers continue from
    the last aired episode.

    ``by_season`` keeps its equal-season weighting by interleaving the seasons
    round-robin, so a show with one long season and nine short ones still spends
    the same airtime in each.
    """
    episodes = ring.episodes
    if not episodes:
        raise ProgrammingError(f"{ring.show_id}: no episodes")

    if order == "sequential":
        return tuple(episodes)

    if order == "by_season":
        seasons: dict[int, list[EpisodeRef]] = {}
        for episode in episodes:
            seasons.setdefault(episode.season, []).append(episode)
        keys = sorted(seasons)
        rng = rng_for("pick-season-order", ring.show_id, *seed_parts)
        rng.shuffle(keys)
        queues: list[list[EpisodeRef]] = []
        for season in keys:
            pool = list(seasons[season])
            rng_for("pick-episode-order", ring.show_id, season, *seed_parts).shuffle(pool)
            queues.append(pool)
        interleaved: list[EpisodeRef] = []
        while any(queues):
            for queue in queues:
                if queue:
                    interleaved.append(queue.pop(0))
        return tuple(interleaved)

    pool = list(episodes)
    rng_for("pick-order", ring.show_id, *seed_parts).shuffle(pool)
    return tuple(pool)


def pick_episode(
    ring: RotationRing,
    order: str,
    position: int,
    *,
    seed_parts: Sequence[object] = (),
) -> EpisodeRef:
    """The episode for a given play position in a ring.

    Every mode walks :func:`walk_order`, so a full pass plays each episode once
    before repeating. ``position`` is an absolute play index for the show, not a
    within-day one: callers continue it from the last aired episode so the walk
    carries across days instead of restarting at midnight.
    """
    walk = walk_order(ring, order, tuple(seed_parts))
    return walk[position % len(walk)]


def _plays_per_show(slots_per_day: int, entry_count: int, index: int) -> int:
    """How many times the show at *index* in the cycle airs in one day.

    Not simply ``slots_per_day // entry_count``: when the day does not divide
    evenly the earlier entries get one extra play, and using the floor for all of
    them makes the date-derived position drift by a play a day -- which is exactly
    the kind of slow desync that brings episodes back early.
    """
    if entry_count <= 0:
        return 0
    total = max(0, slots_per_day)
    return len(range(index, total, entry_count))


def _walk_position(walk: Sequence[EpisodeRef], key: str) -> int | None:
    """Where *key* sits in a show's walk order, or ``None`` if it is not there."""
    for index, episode in enumerate(walk):
        if episode.key == key:
            return index
    return None


def _start_index(
    ring: RotationRing, entry: ProgrammingEntry, report: LineupReport
) -> int:
    """Resolve a configured season/episode to its position in a show's ring."""
    assert entry.start_season is not None
    requested = (entry.start_season, entry.start_episode or 0)
    for index, episode in enumerate(ring.episodes):
        if episode.season == requested[0] and (
            entry.start_episode is None or episode.episode == requested[1]
        ):
            return index

    next_episode = next(
        (
            index
            for index, episode in enumerate(ring.episodes)
            if (episode.season, episode.episode) >= requested
        ),
        None,
    )
    if next_episode is not None:
        selected = ring.episodes[next_episode]
        report.warnings.append(
            f"lineup: {entry.show_id} has no S{requested[0]:02d}"
            f"E{requested[1]:03d}; starting at S{selected.season:02d}"
            f"E{selected.episode:03d} instead"
        )
        return next_episode

    report.warnings.append(
        f"lineup: {entry.show_id} has no episode at or after "
        f"S{requested[0]:02d}E{requested[1]:03d}; starting at the beginning"
    )
    return 0


def rings_for(
    root: RootConfig, episodes: Sequence[EpisodeRef]
) -> dict[str, RotationRing]:
    orders = {show_id: show.order for show_id, show in root.shows.items()}
    return build_rings(episodes, orders, seed_salt=root.channel.name)


def build_lineup(
    root: RootConfig,
    episodes: Sequence[EpisodeRef],
    *,
    slots_per_day: int,
    day: date | None = None,
    config: ProgrammingConfig | None = None,
    last_episodes: Mapping[str, str] | None = None,
) -> LineupReport:
    """Fill a day from the hand-built lineup, cycling deterministically.

    The cycle is a flat list of "what airs next" entries: one per lineup show,
    followed by any pinned episodes. A show contributes one slot per pass, so a
    lineup of four shows airs them in turn and a pinned episode appears exactly
    where it was pinned.

    ``day`` only seeds the random modes; the lineup *order* does not depend on it,
    so the channel does not reshuffle itself at midnight.
    """
    config = config or root.programming
    report = LineupReport()
    rings = rings_for(root, episodes)

    # Each show's walk order, so one pass plays every episode once before any
    # repeat. Seeded on the show alone, never the date: a date-seeded order would
    # reshuffle daily and reintroduce the collisions this exists to prevent.
    walks: dict[str, tuple[EpisodeRef, ...]] = {}
    for entry in config.lineup:
        ring = rings.get(entry.show_id)
        if ring is not None and ring.episodes:
            walks.setdefault(entry.show_id, walk_order(ring, entry.order))

    entries: list[tuple[str, str | None]] = []
    for entry in config.lineup:
        ring = rings.get(entry.show_id)
        if ring is None or not ring.episodes:
            report.dropped.append(entry.show_id)
            report.warnings.append(
                f"lineup: {entry.show_id} has no catalogued episodes; skipped"
            )
            continue
        entries.append((entry.show_id, None))

    for key in config.pinned:
        parsed = _parse_pinned(key)
        if parsed is None:
            report.warnings.append(f"pinned: {key!r} is not a 'show:sXXeYYY' key")
            continue
        show_id, season, episode = parsed
        ring = rings.get(show_id)
        if ring is None:
            report.dropped.append(show_id)
            report.warnings.append(f"pinned: {show_id} is not catalogued")
            continue
        if find_episode(ring, season, episode) is None:
            report.warnings.append(
                f"pinned: {show_id} S{season:02d}E{episode:02d} is not in the library"
            )
            continue
        entries.append((show_id, key))

    # Where each show's walk resumes.
    #
    # Two sources, in order of authority. A recorded last-aired episode is the
    # truth about what actually played, so it wins. Without one -- a fresh
    # install, or a preview of a day nothing has aired yet -- the position is
    # derived from the date instead, so consecutive days continue the walk rather
    # than replaying it from the top. Both are pure functions of their inputs, so
    # previewing tomorrow still cannot consume tomorrow's episodes.
    starts: dict[str, int] = {}
    for position_in_cycle, entry in enumerate(config.lineup):
        walk = walks.get(entry.show_id)
        if walk is None:
            continue
        key = (last_episodes or {}).get(entry.show_id)
        if key is not None:
            position = _walk_position(walk, key)
            if position is None:
                report.warnings.append(
                    f"lineup: prior episode {key} for {entry.show_id} is not in the "
                    "library; starting at the beginning"
                )
            else:
                starts[entry.show_id] = position + 1
            continue
        if day is not None:
            stride = _plays_per_show(slots_per_day, len(entries), position_in_cycle)
            # A configured start is an *anchor*, not a fixed offset: it says where
            # the walk was on `start_date`, so the walk keeps advancing from there
            # instead of replaying the same episodes every day. Without a
            # start_date the anchor is the epoch, which is the same idea.
            anchor = entry.start_date.toordinal() if entry.start_date else 0
            starts[entry.show_id] = (day.toordinal() - anchor) * stride
        # A configured start only applies to `sequential`. The random modes are
        # deliberately unordered, so honouring it would just be a hidden offset.
        if entry.order == "sequential" and entry.start_season is not None:
            starts[entry.show_id] += _start_index(rings[entry.show_id], entry, report)

    if not entries:
        raise ProgrammingError(
            "the lineup is empty: add a show or a pinned episode on the web page"
        )

    # How many times each show has already been placed today. Added to the
    # resumed position so a show appearing twice in the lineup keeps walking
    # forward instead of replaying the same episode at each appearance.
    placed: dict[str, int] = {}
    for index in range(max(0, slots_per_day)):
        show_id, pinned_key = entries[index % len(entries)]
        ring = rings[show_id]
        if pinned_key is not None:
            parsed = _parse_pinned(pinned_key)
            assert parsed is not None
            episode = find_episode(ring, parsed[1], parsed[2])
            assert episode is not None
        else:
            order = next(
                (entry.order for entry in config.lineup if entry.show_id == show_id),
                "random",
            )
            position = placed.get(show_id, 0)
            episode = pick_episode(
                ring,
                order,
                starts.get(show_id, 0) + position,
            )
            placed[show_id] = position + 1
        report.slots.append(
            ProgrammeSlot(index=index, show_id=show_id, episode=episode)
        )
    return report


def build_shuffle(
    root: RootConfig,
    episodes: Sequence[EpisodeRef],
    *,
    slots_per_day: int,
    day: date | None = None,
    config: ProgrammingConfig | None = None,
    last_episodes: Mapping[str, str] | None = None,
) -> LineupReport:
    """A continuous stream from the whole collection.

    Each show takes a turn in a fixed order, and within a show the episodes are
    drawn by ``pick_episode``. Turn order is by show id, *not* weighted by how many
    episodes a show has: weighting by size would make a 10-season show dominate,
    and the point of "random continuous stream" is variety across the collection
    rather than a proportional sample of it.
    """
    config = config or root.programming
    report = LineupReport()
    rings = rings_for(root, episodes)

    wanted = config.shuffle_shows or [
        show_id for show_id, show in sorted(root.shows.items()) if show.enable
    ]
    active: list[str] = []
    for show_id in wanted:
        ring = rings.get(show_id)
        if ring is None or not ring.episodes:
            report.dropped.append(show_id)
            continue
        active.append(show_id)
    if not active:
        raise ProgrammingError(
            "nothing to shuffle: no catalogued episodes in the selected shows"
        )

    for show_id in report.dropped:
        report.warnings.append(f"shuffle: {show_id} has no catalogued episodes; skipped")

    placed: dict[str, int] = {}
    start_positions: dict[str, int] = {}
    for position_in_cycle, show_id in enumerate(active):
        walk = walk_order(rings[show_id], config.shuffle_order)
        key = (last_episodes or {}).get(show_id)
        if key is None:
            # Nothing has aired yet, so derive the position from the date. Without
            # this every day would replay the walk from the top.
            if day is not None:
                stride = _plays_per_show(slots_per_day, len(active), position_in_cycle)
                start_positions[show_id] = day.toordinal() * stride
            continue
        position = _walk_position(walk, key)
        if position is None:
            report.warnings.append(
                f"shuffle: prior episode {key} for {show_id} is not in the library; "
                "starting at the beginning"
            )
        else:
            start_positions[show_id] = position + 1
    for index in range(max(0, slots_per_day)):
        show_id = active[index % len(active)]
        position = placed.get(show_id, 0)
        episode = pick_episode(
            rings[show_id],
            config.shuffle_order,
            start_positions.get(show_id, 0) + position,
        )
        placed[show_id] = position + 1
        report.slots.append(
            ProgrammeSlot(index=index, show_id=show_id, episode=episode)
        )
    return report


def build_programming(
    root: RootConfig,
    episodes: Sequence[EpisodeRef],
    *,
    slots_per_day: int,
    day: date | None = None,
    last_episodes: Mapping[str, str] | None = None,
) -> LineupReport | None:
    """Dispatch on ``programming.mode``. ``None`` means "use the day files"."""
    mode = root.programming.mode
    if mode == "lineup":
        return build_lineup(
            root,
            episodes,
            slots_per_day=slots_per_day,
            day=day,
            last_episodes=last_episodes,
        )
    if mode == "shuffle":
        return build_shuffle(
            root,
            episodes,
            slots_per_day=slots_per_day,
            day=day,
            last_episodes=last_episodes,
        )
    return None


def build_programmed_day(
    root: RootConfig,
    episodes: Sequence[EpisodeRef],
    *,
    day: date,
    last_episodes: Mapping[str, str] | None = None,
) -> tuple[list[Slot], list[PlacedSlot]]:
    """Generate the day's slots *and* what airs in them, for a hand-built lineup.

    The day files size slots per daypart, which assumes a show of the right length
    sits in each. A hand-built lineup has no such guarantee -- a 22-minute episode
    in a 15-minute slot overruns, and the planner then refuses to emit -- so here
    the grid follows the content: each entry gets a slot sized from its own show's
    ``slot`` family, and the day is filled until it is exhausted.

    Every slot length is a whole number of grid quanta, so the final slot is
    exactly the remainder and the day still tiles to the millisecond.
    """
    from .grid import day_bounds_ms, quantum_ms

    report = build_programming(
        root,
        episodes,
        slots_per_day=_GENERATE_ENTRIES,
        day=day,
        last_episodes=last_episodes,
    )
    if report is None:
        raise ProgrammingError("build_programmed_day called without a programming mode")

    day_start, day_end = day_bounds_ms(day, root.channel.grid)
    quantum = quantum_ms(root.channel.grid)
    family_ms = {"short": 15 * 60_000, "half": 30 * 60_000, "hour": 60 * 60_000}

    slots: list[Slot] = []
    placed: list[PlacedSlot] = []
    cursor = day_start
    index = 0
    while cursor < day_end:
        remaining = day_end - cursor
        entry = report.slots[index % len(report.slots)]
        ideal = family_ms.get(root.shows[entry.show_id].slot, quantum)

        if ideal > remaining:
            # The day ends mid-slot. Truncating this entry would put a 22-minute
            # episode into an 8-minute slot and the planner would refuse to emit,
            # so look ahead for one that actually fits the tail.
            fitting = _fitting_tail(report.slots, index, remaining, root, family_ms)
            if fitting is not None:
                skip, entry = fitting
                index += skip
                ideal = family_ms.get(root.shows[entry.show_id].slot, quantum)
            elif slots:
                # Nothing at all fits a tail this short. Giving the minutes to the
                # previous slot's break beats an unplayable programme.
                slots[-1] = replace(
                    slots[-1], end_ms=slots[-1].end_ms + remaining
                )
                break
            else:
                raise ProgrammingError(
                    f"the shortest slot in the lineup ({ideal // 60_000} min) is "
                    f"longer than the first slot of the day ({remaining // 60_000} min)"
                )

        length = min(ideal, remaining)
        if length < quantum:
            if slots:
                slots[-1] = replace(slots[-1], end_ms=slots[-1].end_ms + length)
            break

        slot = Slot(
            index=len(slots),
            start_ms=cursor,
            end_ms=cursor + length,
            family=root.shows[entry.show_id].slot,
            daypart="lineup",
        )
        slots.append(slot)
        placed.append(
            PlacedSlot(slot=slot, show_id=entry.show_id, episode=entry.episode)
        )
        cursor += length
        index += 1

    return slots, placed


def _fitting_tail(
    entries: Sequence[ProgrammeSlot],
    start: int,
    remaining: int,
    root: RootConfig,
    family_ms: dict[str, int],
) -> tuple[int, ProgrammeSlot] | None:
    """The nearest entry ahead whose slot and episode both fit *remaining*.

    Returns ``(entries_to_skip, entry)``, or ``None`` when nothing in the lineup
    fits. Only the last slot of a day can reach this, so one pass over the horizon
    is the entire cost.
    """
    for offset in range(1, len(entries)):
        candidate = entries[(start + offset) % len(entries)]
        if family_ms.get(root.shows[candidate.show_id].slot, 0) > remaining:
            continue
        if candidate.episode.duration_ms > remaining:
            continue
        return offset, candidate
    return None


#: How many entries to generate before laying them onto the grid. Generous on
#: purpose: an hour of 15-minute slots needs 96, and a few spare is cheaper than
#: generating too few and having to top up mid-day.
_GENERATE_ENTRIES = 512


__all__ = [
    "LineupReport",
    "PlacedSlot",
    "ProgrammeSlot",
    "ProgrammingError",
    "build_lineup",
    "build_programmed_day",
    "build_programming",
    "build_shuffle",
    "find_episode",
    "pick_episode",
    "rings_for",
]
