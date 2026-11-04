"""Staged schedule deploys: build a schedule, then push it when you say so.

Why this exists
---------------
The streamer re-reads the playout document at every item boundary and positions
itself by wall clock. So rewriting a playout file *is* the change -- and it lands on
whatever is airing next, which can be the middle of an episode. A schedule you have
just edited is therefore something you prepare first and deploy on purpose, at a
moment you choose.

Two halves, both here:

``stage``
    ``pipeline.build(..., deploy=True)`` builds the horizon into
    ``playout/pending`` and ``xmltv/pending`` instead of the live folders, and
    records a :class:`Deploy` saying what is waiting and when it would switch over.

``launch``
    :func:`launch` moves the staged days into place. The one rule that makes the
    switch clean is in :func:`merge_playout`: items already on air when the boundary
    passes are carried over from the live document, so the old and new schedules meet
    exactly at the boundary instead of mid-item.

The live files are never touched by a stage, so a failed or abandoned deploy costs
nothing: :func:`cancel` just deletes the staging folder.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import dataclass
from datetime import datetime, tzinfo
from pathlib import Path

from .emit import EmitPaths

#: Written only once a staged build has fully succeeded, so a half-built staging
#: folder is never mistaken for a deploy that is ready.
REQUEST_NAME = "deploy.json"

#: Report of the last launch, so the pages can say "launched at 21:00".
LAST_NAME = "last-deploy.json"

LEAD_MS = 5_000
"""Do not switch for less than this: a jump a couple of seconds before the end of an
item is indistinguishable from one mid-item, so it is not worth the seam."""


# -- where things live ---------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Roots:
    """The live folders, and the staging folders beside them."""

    live: EmitPaths
    pending: EmitPaths

    @classmethod
    def of(cls, playout_dir: Path, xmltv_dir: Path) -> "Roots":
        playout_dir, xmltv_dir = Path(playout_dir), Path(xmltv_dir)
        return cls(
            live=EmitPaths(playout_dir, xmltv_dir),
            pending=EmitPaths(playout_dir / "pending", xmltv_dir / "pending"),
        )


@dataclass(frozen=True, slots=True)
class Deploy:
    """A staged schedule waiting to go on air."""

    staged_at_ms: int
    switch_at_ms: int
    days: tuple[str, ...]
    fingerprint: str = ""
    """The config the stage was built from, so a launch can tell whether the staged
    files still describe what the settings say."""

    def summary(self, zone: tzinfo) -> str:
        return f"{len(self.days)} day(s), switching at {clock(self.switch_at_ms, zone)}"

    def to_json(self) -> str:
        return json.dumps(
            {
                "staged_at_ms": self.staged_at_ms,
                "switch_at_ms": self.switch_at_ms,
                "days": list(self.days),
                "fingerprint": self.fingerprint,
            },
            indent=2,
        ) + "\n"

    @classmethod
    def from_json(cls, text: str) -> "Deploy":
        raw = json.loads(text)
        return cls(
            staged_at_ms=int(raw["staged_at_ms"]),
            switch_at_ms=int(raw["switch_at_ms"]),
            days=tuple(str(day) for day in raw.get("days", ())),
            fingerprint=str(raw.get("fingerprint", "")),
        )


@dataclass(frozen=True, slots=True)
class LaunchReport:
    """What a launch actually did, for the page to report honestly."""

    swap_ms: int
    kept: int
    days: int
    from_live: bool
    """True when the live document was used for the carry-over; False when there was
    no live document to read and the boundary therefore could not be honoured."""

    @property
    def note(self) -> str:
        if not self.from_live:
            return "no live playout to carry over; the new schedule starts at the top of its day"
        return f"{self.kept} item(s) on air finished, then the new schedule took over"

    def to_json(self) -> str:
        return json.dumps(
            {
                "swap_ms": self.swap_ms,
                "kept": self.kept,
                "days": self.days,
                "from_live": self.from_live,
            },
            indent=2,
        ) + "\n"

    @classmethod
    def from_json(cls, text: str) -> "LaunchReport":
        raw = json.loads(text)
        return cls(
            swap_ms=int(raw["swap_ms"]),
            kept=int(raw.get("kept", 0)),
            days=int(raw.get("days", 0)),
            from_live=bool(raw.get("from_live", True)),
        )


@dataclass(frozen=True, slots=True)
class SchedulePreview:
    compared_days: int
    added: int
    removed: int
    changed: int
    details: tuple[str, ...]


def fingerprint(config) -> str:
    """A short stable hash of the settings a stage was built from.

    Content rather than identity: saving from a page rebuilds the config object, so
    an ``id()`` check would call every stage stale.
    """
    return hashlib.sha256(config.model_dump_json().encode("utf-8")).hexdigest()[:16]


def missing_inputs(roots: Roots, *, fresh: bool = False) -> list[str]:
    """Local files a staged document names that are no longer on disk.

    The case this catches: a merged break built by the stage was pruned by a later
    live build, so launching would hand the streamer a path to nothing. Better to
    rebuild the stage than to air a black rectangle.

    Each distinct path is checked once and reported once. A staged day names ~1800
    items but only ~1100 distinct files, and every check is a ``stat`` -- slow
    enough over a bind-mounted media tree that the web page spent most of its load
    time here. The result is cached against the staged files' identity, because
    this drives a warning banner; ``launch`` passes ``fresh=True`` so the actual
    interlock always re-checks.
    """
    stamp = _staged_stamp(roots)
    if not fresh:
        cached = _MISSING_CACHE.get(stamp)
        if cached is not None:
            return list(cached)

    gone: list[str] = []
    checked: dict[str, bool] = {}
    for path in staged_playouts(roots):
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            gone.append(path.name)
            continue
        for entry in document.get("items", ()):
            source = entry.get("source") or {}
            location = source.get("path")
            if source.get("source_type") != "local" or not location:
                continue
            present = checked.get(location)
            if present is None:
                present = checked[location] = Path(location).is_file()
            if not present and location not in gone:
                gone.append(str(location))

    _MISSING_CACHE.clear()
    _MISSING_CACHE[stamp] = tuple(gone)
    return gone


_MISSING_CACHE: dict[tuple, tuple[str, ...]] = {}
"""Last ``missing_inputs`` result, keyed by the staged files' identity."""


def _staged_stamp(roots: Roots) -> tuple:
    """Cheap identity for the staged playouts: names, sizes and mtimes."""
    stamp: list[tuple] = []
    for path in staged_playouts(roots):
        try:
            info = path.stat()
        except OSError:
            stamp.append((path.name, None, None))
            continue
        stamp.append((path.name, info.st_size, info.st_mtime_ns))
    return tuple(stamp)


def request_path(roots: Roots) -> Path:
    return roots.pending.playout_dir / REQUEST_NAME


def last_path(roots: Roots) -> Path:
    return roots.live.playout_dir / LAST_NAME


# -- reading and clearing ------------------------------------------------------------


def load(roots: Roots) -> Deploy | None:
    """The pending deploy, if a staged build has completed. A missing file is None."""
    try:
        return Deploy.from_json(request_path(roots).read_text(encoding="utf-8"))
    except (OSError, ValueError, KeyError):
        return None


def last(roots: Roots) -> LaunchReport | None:
    try:
        return LaunchReport.from_json(last_path(roots).read_text(encoding="utf-8"))
    except (OSError, ValueError, KeyError):
        return None


def live_day_for(roots: Roots, moment_ms: int) -> Path | None:
    """The live document that covers *moment_ms*.

    The live folder holds the whole horizon -- three days by default -- so taking
    the last file reads tomorrow's schedule and computes a boundary from a day that
    is not on air yet. The day containing the moment is the only one whose items the
    streamer is playing.
    """
    for path in _live_days(roots):
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(document, dict):
            continue
        spans = intervals(document)
        if spans and spans[0].start_ms <= moment_ms < spans[-1].finish_ms:
            return path
    return None


def _live_days(roots: Roots) -> list[Path]:
    folder = roots.live.playout_dir
    if not folder.is_dir():
        return []
    return sorted(path for path in folder.glob("*.json") if path.name != LAST_NAME)


def staged_playouts(roots: Roots) -> list[Path]:
    """The staged day documents, in day order.

    ``deploy.json`` lives in the same folder and is not a day, so it is excluded by
    suffix rather than by name: the naming rule belongs to ``emit``.
    """
    folder = roots.pending.playout_dir
    if not folder.is_dir():
        return []
    return sorted(path for path in folder.glob("*.json") if path.name != REQUEST_NAME)


def preview_schedule(roots: Roots, *, detail_limit: int = 6) -> SchedulePreview:
    """Compare staged day documents with their live counterparts by item start."""
    live = {path.name: path for path in _live_days(roots)}
    compared_days = added = removed = changed = 0
    details: list[str] = []

    def note(day: str, action: str, item: dict) -> None:
        if len(details) >= detail_limit:
            return
        stamp = str(item.get("start") or "unknown time")
        label = str(item.get("id") or item.get("title") or "item")
        details.append(f"{day} {stamp}: {action} {label}")

    for staged_path in staged_playouts(roots):
        staged = _read(staged_path)
        staged_items = staged.get("items", [])
        if not isinstance(staged_items, list):
            staged_items = []
        live_path = live.get(staged_path.name)
        if live_path is None:
            for item in staged_items:
                added += 1
                if isinstance(item, dict):
                    note(staged_path.name, "new", item)
            continue

        compared_days += 1
        live_doc = _read(live_path)
        live_items = live_doc.get("items", [])
        if not isinstance(live_items, list):
            live_items = []
        old_by_start = {
            str(item.get("start") or ""): item
            for item in live_items
            if isinstance(item, dict)
        }
        new_by_start = {
            str(item.get("start") or ""): item
            for item in staged_items
            if isinstance(item, dict)
        }
        for start in sorted(old_by_start.keys() | new_by_start.keys()):
            old = old_by_start.get(start)
            new = new_by_start.get(start)
            if old is None:
                added += 1
                note(staged_path.name, "new", new)
            elif new is None:
                removed += 1
                note(staged_path.name, "removed", old)
            elif _item_signature(old) != _item_signature(new):
                changed += 1
                note(staged_path.name, "replaced", new)

    return SchedulePreview(
        compared_days=compared_days,
        added=added,
        removed=removed,
        changed=changed,
        details=tuple(details),
    )


def _item_signature(item: dict) -> str:
    return json.dumps(item, sort_keys=True, separators=(",", ":"))


def save_request(roots: Roots, deploy: Deploy) -> None:
    from .emit import _write_atomic  # local: emit imports nothing from here

    _write_atomic(request_path(roots), deploy.to_json())


def clear(roots: Roots) -> int:
    """Delete the staging folder. Returns how many files went. Never raises."""
    removed = 0
    for folder in (roots.pending.playout_dir, roots.pending.xmltv_dir):
        if not folder.is_dir():
            continue
        for path in folder.iterdir():
            try:
                if path.is_dir():
                    for child in path.iterdir():
                        child.unlink()
                    path.rmdir()
                else:
                    path.unlink()
                removed += 1
            except OSError:
                continue
    try:
        roots.pending.playout_dir.rmdir()
    except OSError:
        pass
    return removed


# -- the boundary -----------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Interval:
    start_ms: int
    finish_ms: int
    kind: str = ""


def intervals(document: dict) -> list[Interval]:
    """Item spans from a playout document, tolerating a malformed entry.

    An unparseable item is skipped rather than fatal: the boundary is a convenience,
    and refusing to launch because one entry has a bad timestamp would be a worse
    failure than a switch ten seconds early.
    """
    spans: list[Interval] = []
    items = document.get("items", ())
    if not isinstance(items, list):
        return spans
    for entry in items:
        if not isinstance(entry, dict):
            continue
        try:
            start = _parse_ms(entry["start"])
            finish = _parse_ms(entry["finish"])
        except (KeyError, TypeError, ValueError):
            continue
        span = Interval(start, finish, str(entry.get("id", "")))
        spans.append(Interval(span.start_ms, span.finish_ms, item_kind(span.kind)))
    return spans


def item_kind(item_id: str) -> str:
    """``programme`` or ``break``, from an item id the emitter wrote.

    An id is ``<day>-<slot>-<kind...>``: ``20261030-0001-ep1`` for a programme and
    ``20261030-0001-a0824929-1``, ``-b03`` or ``-t`` for anything in a break. Episodes
    are the only thing worth protecting at a switch, so everything else is a break --
    including an id this does not recognise, which errs towards finishing the
    programme behind it.
    """
    last = str(item_id).rsplit("-", 1)[-1]
    # A merged break names how many items it holds: `b03+6`.
    last = last.split("+", 1)[0]
    return "programme" if last.startswith("ep") else "break"


def _parse_ms(stamp: str) -> int:
    return int(datetime.fromisoformat(str(stamp)).timestamp() * 1000)


def switch_boundary(spans: list[Interval], now_ms: int) -> int:
    """When to take over *spans*, having decided at *now_ms*.

    The end of the programme that is on air, never a break: cutting a break in half
    shows the tail of the old commercials followed by the new schedule, while
    finishing the programme and *then* switching is exactly the seam a station has.
    """
    live = [span for span in spans if span.start_ms <= now_ms < span.finish_ms]
    for span in live:
        if span.kind == "programme":
            if span.finish_ms - now_ms <= LEAD_MS:
                return now_ms
            return span.finish_ms
    if live:
        # A break: nothing to protect, so wait for the programme behind it. A gap or
        # the end of the document falls through to the same rule.
        upcoming = sorted(
            (span for span in spans if span.kind == "programme" and span.start_ms >= now_ms),
            key=lambda span: span.start_ms,
        )
        if upcoming:
            return max(int(upcoming[0].finish_ms), now_ms + LEAD_MS)
    # Nothing on air, or no programme left today: the next whole minute is an item
    # boundary whatever the new plan says, because items are whole milliseconds.
    return (_ceil_minute(now_ms) // 60_000) * 60_000


def _ceil_minute(now_ms: int) -> int:
    minute = 60_000
    return ((now_ms + minute - 1) // minute) * minute


def plan_switch(spans: list[Interval], now_ms: int, horizon_ms: int | None = None) -> int:
    """``switch_boundary``, narrowed to the day the streamer is watching.

    *spans* may cover several days; only the first counts. A boundary beyond the end
    of the live document cannot be honoured by carrying items over, so it is capped
    at *horizon_ms* when one is given.
    """
    boundary = switch_boundary(spans, now_ms)
    if horizon_ms is not None and boundary > horizon_ms:
        return int(max(horizon_ms, now_ms))
    return boundary


# -- carrying the current item over ----------------------------------------------------


def merge_playout(
    live: dict, staged: dict, swap_ms: int, now_ms: int | None = None
) -> tuple[dict, int]:
    """The document to serve at *swap_ms*.

    The window from *now_ms* up to *swap_ms* is kept from the live document, whole:
    that is what the stream is playing until the switch, and dropping it would leave
    the streamer with nothing to play until the boundary. From *swap_ms* on, the
    document comes from *staged*: a staged item that would start inside a kept one is
    moved to its end, and one that would end before it is dropped. That leaves no gap
    and no overlap, and nothing on air is cut short.
    """
    moment = swap_ms if now_ms is None else now_ms
    kept: list[dict] = []
    kept_until = swap_ms
    for entry in live.get("items", ()):
        try:
            start = _parse_ms(entry["start"])
            finish = _parse_ms(entry["finish"])
        except (KeyError, TypeError, ValueError):
            continue
        if finish <= moment or start >= swap_ms:
            # Before the switch and already over, or past the boundary: the staged
            # document owns that time.
            continue
        # Kept whole, never trimmed. The boundary is normally the end of this very
        # item, so trimming only bites when it was switched a few seconds early --
        # and cutting the last seconds off a programme is exactly what this is
        # meant to avoid.
        kept.append(entry)
        kept_until = max(kept_until, finish)

    fresh: list[dict] = []
    for entry in staged.get("items", ()):
        try:
            start = _parse_ms(entry["start"])
            finish = _parse_ms(entry["finish"])
        except (KeyError, TypeError, ValueError):
            fresh.append(entry)
            continue
        if finish <= kept_until:
            continue
        if start < kept_until:
            moved = dict(entry)
            moved["start"] = _stamp_at(entry["start"], kept_until)
            fresh.append(moved)
        else:
            fresh.append(entry)

    merged = dict(staged)
    merged["items"] = kept + fresh
    return merged, len(kept)


def _stamp_at(template: str, epoch_ms: int) -> str:
    """The same instant format as *template*, with a new value.

    Reusing the template keeps the document's own offset and precision instead of
    re-deriving them, which is the one thing that would make a mixed document look
    inconsistent to a reader.
    """
    reference = datetime.fromisoformat(str(template))
    shifted = datetime.fromtimestamp(epoch_ms / 1000, tz=reference.tzinfo)
    text = reference.isoformat()
    # ``fromisoformat`` accepts both ``+09:00`` and ``Z``; emit writes the first, so
    # format from the parsed value and keep whatever offset style came in.
    new = shifted.isoformat()
    if "Z" in text:
        return new.replace("+00:00", "Z")
    return new


# -- launching -----------------------------------------------------------------------


def _staged_guide(roots: Roots) -> Path | None:
    folder = roots.pending.xmltv_dir
    if not folder.is_dir():
        return None
    guides = sorted(folder.glob("*.xml"))
    return guides[0] if guides else None


def launch(roots: Roots, *, now_ms: int | None = None) -> LaunchReport:
    """Swap the staged schedule in, honouring the boundary. Raises on unusable input.

    Every file is written before anything is replaced, so the window in which the
    live folders are inconsistent is a couple of ``os.replace`` calls rather than the
    length of a build. Merged break files are made by the caller before this runs.
    """
    moment = int(time.time() * 1000) if now_ms is None else now_ms
    days = staged_playouts(roots)
    if not days:
        raise LookupError("nothing is staged; press Replan first")

    # The day on air now, not the last day of the horizon: a boundary has to come
    # from the items the streamer is actually playing.
    today = live_day_for(roots, moment)
    from_live = today is not None
    spans = intervals(_read(today)) if from_live else []
    horizon = spans[-1].finish_ms if spans else None
    swap_ms = plan_switch(spans, moment, horizon) if from_live else moment

    kept_total = 0
    written: list[tuple[Path, Path]] = []
    for staged_path in days:
        staged = _read(staged_path)
        if from_live:
            staged, kept = merge_playout(_read(today), staged, swap_ms, moment)
            kept_total = max(kept_total, kept)
        target = roots.live.playout_dir / staged_path.name
        temporary = target.with_name(f".{target.name}.launch")
        temporary.write_text(json.dumps(staged, indent=2) + "\n", encoding="utf-8")
        written.append((temporary, target))

    guide = _staged_guide(roots)
    guide_temporary = None
    if guide is not None:
        guide_target = roots.live.xmltv_dir / guide.name
        guide_target.parent.mkdir(parents=True, exist_ok=True)
        guide_temporary = guide_target.with_name(f".{guide_target.name}.launch")
        guide_temporary.write_text(guide.read_text(encoding="utf-8"), encoding="utf-8")

    # From here on nothing can fail: files are already on the live filesystem.
    for temporary, target in written:
        os.replace(temporary, target)
    if guide is not None and guide_temporary is not None:
        os.replace(guide_temporary, roots.live.xmltv_dir / guide.name)

    report = LaunchReport(
        swap_ms=swap_ms, kept=kept_total, days=len(days), from_live=from_live
    )
    from .emit import _write_atomic

    _write_atomic(last_path(roots), report.to_json())
    clear(roots)
    return report


def _read(path: Path) -> dict:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise LookupError(f"{path.name} is not readable: {exc}") from exc
    if not isinstance(document, dict):
        raise LookupError(f"{path.name} is not a JSON object")
    return document


def clock(epoch_ms: int, zone: tzinfo) -> str:
    return datetime.fromtimestamp(epoch_ms / 1000, tz=zone).strftime("%H:%M:%S")


__all__ = [
    "Deploy",
    "Interval",
    "LEAD_MS",
    "LaunchReport",
    "LAST_NAME",
    "REQUEST_NAME",
    "Roots",
    "SchedulePreview",
    "cancel",
    "clear",
    "fingerprint",
    "clock",
    "intervals",
    "item_kind",
    "last",
    "last_path",
    "live_day_for",
    "launch",
    "load",
    "merge_playout",
    "missing_inputs",
    "plan_switch",
    "preview_schedule",
    "request_path",
    "save_request",
    "staged_playouts",
    "switch_boundary",
]


def cancel(roots: Roots) -> int:
    """Throw the staged schedule away. Same as :func:`clear`, named for the button."""
    return clear(roots)
