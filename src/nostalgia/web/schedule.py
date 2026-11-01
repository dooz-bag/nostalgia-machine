"""Reading and writing ``config/programming.yaml`` from the web page.

The schedule is edited as a *form round-trip*: load the YAML, replace only the
keys the form owns, write it back. Anything the page does not understand -- a
hand-written comment, a key added by a future version -- survives untouched,
which matters because this file is also meant to be edited by hand.

Nothing here validates the schedule's *content*: that is
:mod:`nostalgia.programming`'s job, and a second implementation of the rules
would be one too many. This module only guarantees that what the form submits
becomes a well-formed document on disk.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Iterable, Sequence

from ..config import ProgrammingConfig, ProgrammingEntry, RootConfig


class ScheduleFormError(ValueError):
    """A submitted form that cannot be turned into a programming config."""


@dataclass
class ScheduleForm:
    """What the editor page submits, normalised but not yet validated."""

    mode: str = "dayparts"
    lineup: list[ProgrammingEntry] = field(default_factory=list)
    pinned: list[str] = field(default_factory=list)
    shuffle_shows: list[str] = field(default_factory=list)
    shuffle_order: str = "random"

    def __post_init__(self) -> None:
        # Accept plain mappings as well as models, so a caller does not have to
        # import the config types just to build a form.
        self.lineup = [
            entry
            if isinstance(entry, ProgrammingEntry)
            else ProgrammingEntry(**entry)
            for entry in self.lineup
        ]

    def to_config(self) -> ProgrammingConfig:
        return ProgrammingConfig(
            mode=self.mode,
            lineup=self.lineup,
            pinned=self.pinned,
            shuffle_shows=self.shuffle_shows,
            shuffle_order=self.shuffle_order,
        )


def load_schedule(config: RootConfig) -> ScheduleForm:
    programming = config.programming
    return ScheduleForm(
        mode=programming.mode,
        lineup=list(programming.lineup),
        pinned=list(programming.pinned),
        shuffle_shows=list(programming.shuffle_shows),
        shuffle_order=programming.shuffle_order,
    )


def parse_form(
    *,
    mode: str,
    lineup_show: Sequence[str] = (),
    lineup_order: Sequence[str] = (),
    lineup_start_season: Sequence[str] = (),
    lineup_start_episode: Sequence[str] = (),
    lineup_start_date: Sequence[str] = (),
    pinned_show: Sequence[str] = (),
    pinned_season: Sequence[str] = (),
    pinned_episode: Sequence[str] = (),
    shuffle_shows: Iterable[str] = (),
    shuffle_order: str = "random",
) -> ScheduleForm:
    """Turn the raw form lists into a :class:`ScheduleForm`.

    HTML forms submit parallel lists for repeated rows, so the show/order and
    show/season/episode lists are zipped with length checks rather than assumed
    to line up. A mismatch means the page and the handler have drifted apart, and
    silently truncating would drop a row the user filled in.
    """
    if len(lineup_show) != len(lineup_order):
        raise ScheduleFormError(
            "lineup rows arrived with mismatched lengths; reload the page"
        )
    lineup_start_season = _lineup_column(
        lineup_start_season, len(lineup_show), "start-season"
    )
    lineup_start_episode = _lineup_column(
        lineup_start_episode, len(lineup_show), "start-episode"
    )
    lineup_start_date = _lineup_column(
        lineup_start_date, len(lineup_show), "start-date"
    )
    if not (len(pinned_show) == len(pinned_season) == len(pinned_episode)):
        raise ScheduleFormError(
            "pinned-episode rows arrived with mismatched lengths; reload the page"
        )

    entries = []
    for show_id, order, season, episode, start_date in zip(
        lineup_show,
        lineup_order,
        lineup_start_season,
        lineup_start_episode,
        lineup_start_date,
    ):
        show_id = show_id.strip()
        if not show_id:
            continue
        season = (season or "").strip()
        episode = (episode or "").strip()
        start_date = (start_date or "").strip()
        if episode and not season:
            raise ScheduleFormError(
                f"starting episode for {show_id} needs a starting season"
            )
        if season and not season.isdigit():
            raise ScheduleFormError(f"starting season for {show_id} must be numeric")
        if episode and not episode.isdigit():
            raise ScheduleFormError(f"starting episode for {show_id} must be numeric")
        try:
            parsed_date = date.fromisoformat(start_date) if season and start_date else None
        except ValueError as exc:
            raise ScheduleFormError(
                f"starting date for {show_id} must be YYYY-MM-DD"
            ) from exc
        entries.append(
            ProgrammingEntry(
                show_id=show_id,
                order=order or "random",
                start_season=int(season) if season else None,
                start_episode=int(episode) if episode else None,
                start_date=parsed_date,
            )
        )

    pinned: list[str] = []
    for show_id, season, episode in zip(pinned_show, pinned_season, pinned_episode):
        show_id = (show_id or "").strip()
        season = (season or "").strip()
        episode = (episode or "").strip()
        if not show_id or not season or not episode:
            # A half-filled row is a typo, not an instruction to pin episode 0.
            continue
        if not (season.isdigit() and episode.isdigit()):
            raise ScheduleFormError(
                f"pinned episode for {show_id} needs numeric season and episode"
            )
        pinned.append(f"{show_id}:s{int(season):02d}e{int(episode):03d}")

    return ScheduleForm(
        mode=mode,
        lineup=entries,
        pinned=pinned,
        shuffle_shows=[show for show in shuffle_shows if show],
        shuffle_order=shuffle_order or "random",
    )


def _lineup_column(values: Sequence[str], row_count: int, name: str) -> list[str]:
    """Pad omitted optional columns for older form callers, reject partial rows."""
    if not values:
        return [""] * row_count
    if len(values) != row_count:
        raise ScheduleFormError(
            f"lineup {name} rows arrived with mismatched lengths; reload the page"
        )
    return list(values)


def write_schedule(path: Path, form: ScheduleForm) -> None:
    """Persist *form* to ``programming.yaml``, preserving everything else."""
    document = _read_document(path)
    section = document.get("programming")
    if not isinstance(section, dict):
        section = {}
    section = dict(section)

    section["mode"] = form.mode
    section["lineup"] = [
        {
            "show_id": entry.show_id,
            "order": entry.order,
            **(
                {"start_season": entry.start_season}
                if entry.start_season is not None
                else {}
            ),
            **(
                {"start_episode": entry.start_episode}
                if entry.start_episode is not None
                else {}
            ),
            **(
                {"start_date": entry.start_date.isoformat()}
                if entry.start_date is not None
                else {}
            ),
        }
        for entry in form.lineup
    ]
    section["pinned"] = list(form.pinned)
    section["shuffle_shows"] = list(form.shuffle_shows)
    section["shuffle_order"] = form.shuffle_order
    document["programming"] = section

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_dump(document), encoding="utf-8")


def _read_document(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    import yaml

    try:
        loaded = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError:
        # Refusing to save would leave the user stuck with a file they cannot
        # edit through the page they came to for help.
        return {}
    return loaded if isinstance(loaded, dict) else {}


_HEADER = '''\
# What decides the schedule.
#
# This file is edited by the web page (nostalgia serve) and by hand. The page
# rewrites only the keys below, so comments elsewhere survive.
#
#   dayparts  the hand-written day files in config/days/ -- the original mode
#   lineup    cycle the shows listed under `lineup`, plus any `pinned` episodes
#   shuffle   a continuous stream drawn from the whole collection
#
# `order` per show is how that show's episodes are chosen:
#   random      any episode
#   by_season   pick a season first, then an episode inside it
#   sequential  in order, wrapping -- watch the show progress
'''


def _dump(document: dict[str, Any]) -> str:
    """Serialise with the header comment and unsorted keys."""
    import yaml

    try:
        body = yaml.safe_dump(document, sort_keys=False, default_flow_style=False)
    except Exception:  # noqa: BLE001 - PyYAML is always present; JSON is a fallback
        import json

        body = json.dumps(document, indent=2) + "\n"
    return _HEADER + body


__all__ = [
    "ScheduleForm",
    "ScheduleFormError",
    "load_schedule",
    "parse_form",
    "write_schedule",
]
