"""Read models for the web UI.

Kept separate from the FastAPI wiring so they can be tested without a server and
reused by any future front end. Everything here is derived from a plan or the
catalog; nothing mutates state except :func:`apply_tag`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

from ..catalog import Catalog
from ..config import RootConfig
from ..grid import format_clock, format_wall
from ..paths import Paths
from ..pipeline import BuildError, day_config_for
from ..plan import Plan, PlanContext, build_plan
from ..sidecars import POSITIONS
from ..sidecars import BumpTag
from ..sidecars import write as write_tag

# A subject is a show id: lowercase, hyphen-separated (``aqua-teen-hunger-force``).
_SUBJECT_PATTERN = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


@dataclass(frozen=True, slots=True)
class ItemView:
    kind: str
    clock: str
    duration_s: float
    title: str
    subtitle: str
    note: str


@dataclass(frozen=True, slots=True)
class NowPlaying:
    day: date
    now_clock: str
    current: ItemView | None
    next_episodes: tuple[ItemView, ...]
    generated_share: float
    fallbacks: int


def _view(plan: Plan, item, zone, titles: dict[str, str] | None = None) -> ItemView:
    title = item.title
    if titles and item.kind == "bump":
        title = titles.get(item.show_id or "", title)
    return ItemView(
        kind=item.kind,
        clock=format_clock(item.start_ms, zone),
        duration_s=item.duration_ms / 1000,
        title=title,
        subtitle=item.subtitle,
        note=item.note,
    )


_PLAN_CACHE: dict[tuple, Plan] = {}
"""Most recent day plan per (day, config fingerprint).

Building a day's plan takes several seconds against a real library, and the page
was doing it on *every* request -- including the container healthcheck, which
polled faster than a rebuild completes and so never reported healthy. The plan for
a given day and config never changes, so caching it turns the page from a
multi-second wait into an instant one.
"""


def _config_fingerprint(config: RootConfig) -> int:
    """A hash of everything that can change a plan.

    Content matters rather than identity: saving a schedule from the web page
    rebuilds ``config`` into a new object, so an ``id()`` check would go stale in
    exactly the case the page exists to serve.
    """
    return hash(config.model_dump_json())


def plan_for_day(config: RootConfig, catalog: Catalog, paths: Paths, day: date) -> Plan:
    """The plan for *day*, cached until the config or the catalog changes."""
    key = (day, _config_fingerprint(config), _catalog_stamp(catalog))
    cached = _PLAN_CACHE.get(key)
    if cached is not None:
        return cached

    context = PlanContext.load(
        config, catalog, config_dir=paths.config_dir, media_root=paths.media_root
    )
    try:
        plan = build_plan(day, day_config_for(config, day), context)
    except BuildError as exc:
        raise LookupError(str(exc)) from exc

    # One entry is enough: the page only ever shows today, and keeping older days
    # would leak memory for no benefit.
    _PLAN_CACHE.clear()
    _PLAN_CACHE[key] = plan
    return plan


def _catalog_stamp(catalog: Catalog) -> object:
    """Cheap signal that the library changed, so a rescan invalidates the cache.

    There is no scan counter in the database to read, so this hashes the file
    mtimes. A ``scan`` rewrites the rows and therefore touches the file, while
    the scheduler's six-hourly ``emit`` commits too -- neither should serve a
    stale lineup, and both are far cheaper to detect than a rebuild.
    """
    import os

    try:
        info = os.stat(catalog.path)
        return (info.st_mtime_ns, info.st_size)
    except OSError:
        return None


def now_playing(
    config: RootConfig,
    catalog: Catalog,
    paths: Paths,
    *,
    now: datetime | None = None,
) -> NowPlaying:
    """What is airing *now*, plus the next few programmes."""
    zone = config.channel.grid.timezone
    moment = now or datetime.now(tz=__import__("zoneinfo").ZoneInfo(zone))
    today = moment.date()

    plan = plan_for_day(config, catalog, paths, today)

    now_ms = int(moment.timestamp() * 1000)
    titles = {show_id: show.title for show_id, show in config.shows.items()}
    current = next((item for item in plan.items if item.start_ms <= now_ms < item.end_ms), None)
    # A mid-roll break splits one episode into two plan items. Listing both would
    # show the same programme twice in "up next", so only the first part is kept.
    upcoming: list = []
    for item in plan.items:
        if item.start_ms <= now_ms or item.kind != "episode":
            continue
        if upcoming and upcoming[-1].asset_key == item.asset_key:
            continue
        upcoming.append(item)

    bumps = [item for item in plan.items if item.kind == "bump"]
    generated = [
        item for item in bumps if item.asset_key.startswith("gen:") or "generated" in item.note
    ]

    return NowPlaying(
        day=today,
        now_clock=format_wall(now_ms, config.channel.grid)[11:19],
        current=_view(plan, current, config.channel.grid, titles) if current else None,
        next_episodes=tuple(
            _view(plan, item, config.channel.grid, titles) for item in upcoming[:6]
        ),
        generated_share=round(len(generated) / len(bumps), 3) if bumps else 0.0,
        fallbacks=len(plan.bump_fallbacks),
    )


@dataclass(frozen=True, slots=True)
class PendingTag:
    key: str
    filename: str
    duration_s: float
    subject: str | None
    position: str | None
    source: str
    confidence: float


def pending_tags(catalog: Catalog, *, limit: int | None = None) -> list[PendingTag]:
    """Bumps whose subject has not been confirmed."""
    rows = catalog.tags_pending_review()
    out: list[PendingTag] = []
    for row in rows:
        path = Path(row["path"])
        # Show the parent folder in the UI: for bumps/<show>/after-01.mkv the
        # filename alone is meaningless, but the folder is the actual hint.
        parent = path.parent.name
        label = path.name if parent in ("bumps", "") else f"{parent}/{path.name}"
        out.append(
            PendingTag(
                key=row["key"],
                filename=label,
                duration_s=(row["duration_ms"] or 0) / 1000,
                subject=row["subject"],
                position=row["position"],
                source=row["tag_source"] or "unknown",
                confidence=row["confidence"] or 0.0,
            )
        )
        if limit and len(out) >= limit:
            break
    return out


def apply_tag(
    catalog: Catalog,
    key: str,
    *,
    subject: str | None,
    position: str | None = None,
    era: int | None = None,
) -> Path:
    """Write a confirmed tag to both the database and the file's sidecar.

    The sidecar is the durable copy: it survives a wiped catalog and travels with
    the media. The database row keeps the UI fast.
    """
    row = catalog.media_info(key)
    if row is None:
        raise LookupError(f"no asset with key {key!r}")

    # Validate here rather than trusting the caller. A bad position silently
    # written to a sidecar becomes a scan warning on every future run and the tag
    # is unusable, so it is better to fail the request and leave the file alone.
    if position is not None:
        position = position.strip().upper()
        if position == "":
            position = None
        elif position not in POSITIONS:
            raise ValueError(
                f"position must be one of {', '.join(POSITIONS)}; got {position!r}"
            )
    if subject is not None:
        subject = subject.strip() or None
        if subject is not None and not _SUBJECT_PATTERN.match(subject):
            raise ValueError(f"subject {subject!r} is not a valid show id")

    tag = BumpTag(
        subject=subject,
        position=position,
        era=era,
        era_to=era,
        source="manual",
        confidence=1.0,
        confirmed=True,
    )
    sidecar = write_tag(Path(row["path"]), tag)
    catalog.set_tag(
        key,
        subject=subject,
        position=position,
        era_from=era,
        era_to=era,
        tag_source="manual",
        confidence=1.0,
        confirmed=True,
    )
    return sidecar


__all__ = [
    "ItemView",
    "NowPlaying",
    "PendingTag",
    "apply_tag",
    "now_playing",
    "pending_tags",
]
