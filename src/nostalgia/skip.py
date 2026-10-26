"""Skip the programme that is on now, without touching the schedule.

A live channel is *continuous*: the timeline is fixed in advance, and the whole
design leans on that. ErsatzTV plays a day's playout JSON by wall clock, so the
only honest way to "skip this episode" is to change what the file says -- never to
tear down the session, which would drop every connected viewer and leave the rest
of the hour unplanned.

So a skip is an **override**, recorded in ``data/skips.json`` and applied when the
duration of the programme currently airing is known:

* The playing episode is replaced by the show's next episode.
* The episode gives back its slot *and* its mid-roll ad pods, because those pods
  exist only to pad a 22-minute episode out to a 30-minute slot. Holding onto them
  would push every following programme late and break the "one programme starts
  when the last one ends" invariant the guide depends on.
* The replacement slot is rebuilt from the new episode's own length, so the break
  around it is still whatever the break template says a slot of that size gets.
* The **next** programme is pulled back in time to meet it, and the day stays
  exactly as long as it was.

The override is keyed on ``(day, slot index)`` rather than on an item id: item ids
change every time the day is rebuilt, so an id would silently stop matching, which
is the worst possible failure for something a person just clicked.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field, replace
from datetime import date, datetime
from pathlib import Path

OVERRIDE_VERSION = 1


class SkipError(ValueError):
    """A skip that cannot be honoured."""


@dataclass(frozen=True, slots=True)
class Skip:
    """One "play the next episode instead" request."""

    day: str
    """ISO date, so the file stays readable and stable across restarts."""
    slot_index: int
    """Which slot of that day was skipped."""

    requested_at: str = ""
    """When the click happened, for the history trail."""

    episode_key: str | None = None
    """The episode being *replaced*, so a stale override can be recognised.

    Without it, a skip recorded against slot 40 would still fire hours later if the
    plan had changed underneath -- and replace an episode nobody was watching.
    """

    used: bool = False
    """Whether a rebuilt plan already consumed this override."""


@dataclass
class OverrideFile:
    skips: list[Skip] = field(default_factory=list)


def load_overrides(path: Path) -> OverrideFile:
    """Read the override file, tolerating absence and a truncated write."""
    if not path.is_file():
        return OverrideFile()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        # A corrupt override file must not stop the channel. Losing one requested
        # skip is recoverable; refusing to plan is not.
        return OverrideFile()
    skips: list[Skip] = []
    for entry in payload.get("skips") or []:
        if not isinstance(entry, dict):
            continue
        try:
            skips.append(
                Skip(
                    day=str(entry["day"]),
                    slot_index=int(entry["slot_index"]),
                    requested_at=str(entry.get("requested_at") or ""),
                    episode_key=entry.get("episode_key"),
                    used=bool(entry.get("used")),
                )
            )
        except (KeyError, TypeError, ValueError):
            continue
    return OverrideFile(skips=skips)


def save_overrides(path: Path, overrides: OverrideFile) -> None:
    """Write the override file atomically, so a reader never sees half of it."""
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": OVERRIDE_VERSION,
        "skips": [asdict(skip) for skip in overrides.skips],
    }
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    temp.replace(path)


def request_skip(
    path: Path,
    *,
    day: date,
    slot_index: int,
    episode_key: str | None,
    when: datetime | None = None,
) -> Skip:
    """Record a skip. Requesting one twice for the same slot is idempotent."""
    overrides = load_overrides(path)
    stamp = (when or datetime.now().astimezone()).isoformat(timespec="seconds")
    for existing in overrides.skips:
        if existing.day == day.isoformat() and existing.slot_index == slot_index:
            return existing
    skip = Skip(
        day=day.isoformat(),
        slot_index=slot_index,
        requested_at=stamp,
        episode_key=episode_key,
    )
    overrides.skips.append(skip)
    save_overrides(path, overrides)
    return skip


def clear_skip(path: Path, *, day: date, slot_index: int) -> bool:
    """Forget a skip, whether it has been applied yet or not."""
    overrides = load_overrides(path)
    kept = [
        skip
        for skip in overrides.skips
        if not (skip.day == day.isoformat() and skip.slot_index == slot_index)
    ]
    if len(kept) == len(overrides.skips):
        return False
    overrides.skips = kept
    save_overrides(path, overrides)
    return True


def prune(path: Path, *, before: date) -> int:
    """Drop overrides for days that have already finished.

    An override only means anything for a day that is still being served, and a
    file that grows forever is a file that eventually gets committed by accident.
    """
    overrides = load_overrides(path)
    kept = [skip for skip in overrides.skips if skip.day >= before.isoformat()]
    removed = len(overrides.skips) - len(kept)
    if removed:
        overrides.skips = kept
        save_overrides(path, overrides)
    return removed


def pending_for(overrides: OverrideFile, day: date) -> dict[int, Skip]:
    """The unused overrides for *day*, keyed by slot index."""
    wanted = day.isoformat()
    return {
        skip.slot_index: skip
        for skip in overrides.skips
        if skip.day == wanted and not skip.used
    }


def mark_used(path: Path, applied: dict[int, Skip]) -> None:
    """Flag the overrides a rebuilt plan actually consumed."""
    if not applied:
        return
    overrides = load_overrides(path)
    touched = {(skip.day, skip.slot_index) for skip in applied.values()}
    updated: list[Skip] = []
    changed = False
    for skip in overrides.skips:
        if (skip.day, skip.slot_index) in touched and not skip.used:
            updated.append(replace(skip, used=True))
            changed = True
        else:
            updated.append(skip)
    if changed:
        overrides.skips = updated
        save_overrides(path, overrides)
