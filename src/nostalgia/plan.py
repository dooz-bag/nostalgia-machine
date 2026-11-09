"""The planner: slots in, an exact timeline out.

This is the only module that knows about *all* the parts at once, and it is the
single source of truth for both the stream and the EPG -- :mod:`nostalgia.emit`
serialises the same :class:`Plan` to playout JSON and XMLTV, so the guide can
never disagree with what is on screen.

Invariants the planner enforces, in order of severity:

* **Contiguity.** Item ``n`` starts exactly when item ``n-1`` finishes.
* **Slot alignment.** Every slot starts on its grid boundary and ends on the next.
* **Non-overflow.** No item crosses a slot boundary.
* **Determinism.** Same config + same date + same library = same plan, byte for byte.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING, Iterator, Mapping, Sequence

from . import programming
from .ads import BLACK_CARD_MIN_MS, PodError, PodFill, PodItem, fill_gap, fill_pod
from .assets import NEUTRAL, Asset
from .bumps import BumpRequest, choose
from .catalog import Catalog
from .config import FILLER_KINDS, DayConfig, RootConfig
from .cooldown import CooldownTracker
from .continuity import (
    SegmentSpec,
    build_script,
    describe_script,
    plan_budget,
)
from .grid import Slot, build_day, day_bounds_ms, format_wall
from .rng import rng_for
from .rotation import NoEpisodesError, RotationRing, build_rings, select
from .schedule import AssignedSlot, PlayCounts, assign_day
from .watermark import WatermarkRule

if TYPE_CHECKING:
    from .skip import Skip


class PlanError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class PlannedItem:
    """One item on the timeline, ready to serialise."""

    item_id: str
    kind: str
    """``episode`` | ``bump`` | ``ad`` | ``ident`` | ``filler``."""

    start_ms: int
    end_ms: int
    asset_key: str
    path: str

    title: str = ""
    subtitle: str = ""
    description: str = ""
    show_id: str | None = None
    slot_index: int = 0

    in_point_ms: int | None = None
    out_point_ms: int | None = None

    guide: bool = False
    """Whether this item appears in the EPG."""

    note: str = ""

    lavfi_params: str | None = None
    """Set when the item is rendered by ffmpeg instead of read from a file."""

    watermark: bool = False
    """True when the streamer should composite the corner bug over this item.

    Only ever set in overlay mode. Burn mode replaces the *path* instead, because
    a pre-composited file is just a file and needs no cooperation from the
    streamer.
    """

    conformed: bool = False
    """True when ``path`` is a conformed copy that may be joined to its neighbours
    by stream copy (see :mod:`nostalgia.mergebreaks`)."""

    stretched: bool = False
    """True when the slot gave this item more time than its file has (see
    ``_absorb_remainder``). Its file is shorter than its entry, so joining it to a
    neighbour would move that shortfall to the end of the whole run."""

    @property
    def duration_ms(self) -> int:
        return self.end_ms - self.start_ms

    @property
    def mergeable(self) -> bool:
        """Whether this item can be folded into a merged break file.

        All four conditions are load-bearing: an item that is cut (``in``/``out``
        point) would lose its dip, a ``lavfi`` source has no file to join, an
        unconformed file has different stream parameters and cannot be copied, and
        a programme is never merged -- it is the one thing worth keeping separate.
        A stretched item is left out too: it has to end where its file ends.
        """
        return (
            self.conformed
            and self.kind != "episode"
            and not self.lavfi_params
            and self.in_point_ms is None
            and self.out_point_ms is None
            and not self.stretched
        )


@dataclass
class Plan:
    day: date
    channel_name: str
    channel_number: str
    tvg_id: str
    items: list[PlannedItem] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    bump_fallbacks: list[str] = field(default_factory=list)
    """Item ids where a neutral bump stepped in for a missing show-specific one.

    These are *emitted* item ids, so a caller can look the card up in ``items``.
    """

    bump_fallback_reasons: list[str] = field(default_factory=list)
    """Why each fallback happened, in the same order as :attr:`bump_fallbacks`."""

    extras_cooled: int = 0
    """Breaks the extras roll chose but no piece was eligible for.

    Counted rather than warned per break: the pool is small by design, so with a
    24h cooldown most breaks after the first are in this state and repeating the
    line for each one buries every other warning in the day.
    """

    day_start_ms: int = 0
    day_end_ms: int = 0

    def episode_items(self) -> list[PlannedItem]:
        return [item for item in self.items if item.kind == "episode"]

    def guide_items(self) -> list[PlannedItem]:
        return [item for item in self.items if item.guide]

    def duration_ms(self) -> int:
        return sum(item.duration_ms for item in self.items)

    def validate(self) -> list[str]:
        """Check the timeline invariants. Returns a list of problems."""
        problems: list[str] = []
        if not self.items:
            return ["plan is empty"]
        if self.items[0].start_ms != self.day_start_ms:
            problems.append(
                f"first item starts at {self.items[0].start_ms}, "
                f"day starts at {self.day_start_ms}"
            )
        for previous, current in zip(self.items, self.items[1:]):
            if current.start_ms != previous.end_ms:
                problems.append(
                    f"{current.item_id} starts at {current.start_ms} but "
                    f"{previous.item_id} ends at {previous.end_ms}"
                )
            if current.end_ms <= current.start_ms:
                problems.append(f"{current.item_id} has non-positive duration")
        last = self.items[-1]
        if last.end_ms != self.day_end_ms:
            problems.append(
                f"last item ends at {last.end_ms} but day ends at {self.day_end_ms}"
            )
        return problems


@dataclass
class PlanContext:
    """Everything the planner needs, loaded once."""

    root: RootConfig
    catalog: Catalog
    rings: dict[str, RotationRing]
    bumps: list[Asset]
    ads: list[Asset]
    idents: list[Asset]
    last_aired: dict[str, int]
    play_counts: PlayCounts = field(init=False, repr=False)
    """Per-show play counts from the day files, for the rotation ordinal.

    Built from ``root`` in :meth:`__post_init__` rather than passed in, so every
    construction path gets one and no caller has to remember.
    """

    era_file: Path | None = None
    """``config/bumps-era.yaml``, used by the generated-bump renderer."""

    generated_dir: Path | None = None
    """Where rendered cards are cached (``media/bumps/generated`` by default)."""

    lines_file: Path | None = None
    """``config/bump-lines.yaml``: the approved sentences generated cards say."""

    music_dirs: tuple[Path, ...] = ()
    """``media/music/`` and ``media/derived/music/``: beds laid under generated cards.

    The real bumps are a third source, read straight from the catalog by the
    renderer, so a channel with nothing in either folder still gets beds.
    """

    extras: list[Asset] = field(default_factory=list)
    """Long-form filler: music videos, DVD extras, anything that is not a spot.

    Separate from ``ads`` because it is a different promise to the viewer and a
    different length class -- these run for minutes, so they can only ever fill a
    whole break, never a 30-second pod.
    """

    def __post_init__(self) -> None:
        self.play_counts = PlayCounts(self.root)

    @classmethod
    def load(
        cls,
        root: RootConfig,
        catalog: Catalog,
        *,
        config_dir: Path | None = None,
        media_root: Path | None = None,
    ) -> "PlanContext":
        all_assets = catalog.assets(("bump", "ad", "ident", "extra"))
        bumps = [asset for asset in all_assets if asset.kind == "bump"]
        ads = [asset for asset in all_assets if asset.kind == "ad"]
        idents = [asset for asset in all_assets if asset.kind == "ident"]
        extras = [asset for asset in all_assets if asset.kind == "extra"]
        orders = {show_id: show.order for show_id, show in root.shows.items()}
        rings = build_rings(catalog.episodes(), orders, seed_salt=root.channel.name)

        era_file = (config_dir / "bumps-era.yaml") if config_dir else None
        if era_file is not None and not era_file.is_file():
            era_file = None

        generated_dir = None
        music_dirs: tuple[Path, ...] = ()
        if media_root is not None:
            from .music import music_roots

            generated_dir = media_root / "bumps" / "generated"
            music_dirs = tuple(music_roots(media_root))
        lines_file = (config_dir / "bump-lines.yaml") if config_dir else None

        return cls(
            lines_file=lines_file,
            music_dirs=music_dirs,
            root=root,
            catalog=catalog,
            rings=rings,
            bumps=bumps,
            ads=ads,
            idents=idents,
            last_aired=catalog.last_aired(),
            era_file=era_file,
            generated_dir=generated_dir,
            extras=extras,
        )


def build_plan(
    day: date,
    day_config: DayConfig,
    context: PlanContext,
    *,
    ignore_cooldowns: bool = False,
    allow_generated: bool | None = None,
    skips: Mapping[int, "Skip"] | None = None,
    last_episodes: Mapping[str, str] | None = None,
) -> Plan:
    """Build one day's timeline.

    ``skips`` are slot indices whose programme should be replaced by the show's
    next episode. See :mod:`nostalgia.skip` for why that is a plan override rather
    than a player command.
    """
    root = context.root
    channel = root.channel
    day_start, day_end = day_bounds_ms(day, channel.grid)

    plan = Plan(
        day=day,
        channel_name=channel.name,
        channel_number=channel.number,
        tvg_id=channel.tvg_id,
        day_start_ms=day_start,
        day_end_ms=day_end,
    )

    # Two ways to decide what airs, and they produce the same AssignedSlot shape
    # so everything downstream -- bumps, pods, era filtering, the watermark --
    # works without knowing which mode produced the schedule.
    if root.programming.active():
        try:
            slots, placed = programming.build_programmed_day(
                root,
                context.catalog.episodes(),
                day=day,
                last_episodes=last_episodes,
            )
        except programming.ProgrammingError as exc:
            raise PlanError(f"{day}: {exc}") from exc
        assignments = _assignments_from_programming(placed)
    else:
        slots = build_day(day, day_config, channel.grid)
        schedule = assign_day(slots, day_config, root, day)
        plan.warnings.extend(schedule.warnings)
        assignments = {slot_key(a.slot): a for a in schedule.slots}

    generated_enabled = (
        root.bumps.generated.enabled if allow_generated is None else allow_generated
    )
    # Generated cards are kept out of the *real* pool so they can never beat a
    # real asset, and so `--no-generated` is a true switch rather than a filter.
    real_bumps = [asset for asset in context.bumps if not asset.is_generated]
    generated_pool = (
        [asset for asset in context.bumps if asset.is_generated] if generated_enabled else []
    )
    budget = GeneratedBudget(
        max_slots=_generated_allowance(len(slots), root.bumps.generated.max_share)
        if generated_pool
        else 0
    )

    # Cooldowns advance as items are placed. Seeded from catalogued airings so a
    # plan that continues yesterday does not immediately reuse yesterday's spots.
    bump_tracker = CooldownTracker.from_history(
        context.last_aired,
        cooldown_ms=root.bumps.cooldown_hours * 3_600_000,
        min_gap=4,
        disabled=ignore_cooldowns,
    )
    ad_tracker = CooldownTracker.from_history(
        context.last_aired,
        cooldown_ms=root.ads.cooldown_hours * 3_600_000,
        min_gap=2,
        disabled=ignore_cooldowns,
    )
    # Extras are long and few, so they need a much wider separation than a spot --
    # the same music video three times in an evening is the clearest possible sign
    # that a program assembled the schedule.
    extra_tracker = CooldownTracker.from_history(
        context.last_aired,
        cooldown_ms=root.ads.extras_cooldown_hours * 3_600_000,
        min_gap=20,
        disabled=ignore_cooldowns,
    )

    mark = watermark_rule(root)
    ordered = sorted(assignments.values(), key=lambda a: a.slot.start_ms)

    # A skipped slot has to be planned twice: once to learn what the playing
    # programme actually lasts -- its duration is what decides how much break
    # surrounds it, so it cannot be guessed from the slot length -- and again with
    # the following episode substituted. Only the second pass survives, so this
    # costs planning time and changes nothing else about the day.
    if skips:
        ordered = _apply_skips(
            plan,
            ordered,
            context,
            dict(skips),
            bump_tracker=bump_tracker,
            ad_tracker=ad_tracker,
            real_bumps=real_bumps,
            generated_pool=generated_pool,
            generated_budget=budget,
            mark=mark,
            extra_tracker=extra_tracker,
        )

    for position, assigned in enumerate(ordered):
        nxt = ordered[position + 1] if position + 1 < len(ordered) else None
        prev = ordered[position - 1] if position > 0 else None
        _plan_slot(
            plan,
            assigned,
            nxt,
            prev,
            context,
            bump_tracker=bump_tracker,
            ad_tracker=ad_tracker,
            real_bumps=real_bumps,
            generated_pool=generated_pool,
            generated_budget=budget,
            mark=mark,
            extra_tracker=extra_tracker,
        )

    if not plan.items:
        raise PlanError(f"{day}: nothing could be scheduled")

    if plan.extras_cooled:
        aired = sum(1 for item in plan.items if item.kind == "extra")
        plan.warnings.append(
            f"{day}: {plan.extras_cooled} break(s) rolled extras but every piece was "
            f"cooling down ({root.ads.extras_cooldown_hours}h); {aired} aired -- "
            f"add more files to media/extras/"
        )

    problems = plan.validate()
    if problems:
        raise PlanError(f"{day}: timeline is invalid\n  - " + "\n  - ".join(problems))
    return plan


def _apply_skips(
    plan: Plan,
    ordered: list[AssignedSlot],
    context: PlanContext,
    skips: Mapping[int, "Skip"],
    *,
    bump_tracker,
    ad_tracker,
    real_bumps: Sequence,
    generated_pool: Sequence,
    generated_budget: GeneratedBudget,
    mark: "WatermarkRule | None",
    extra_tracker: CooldownTracker,
) -> list[AssignedSlot]:
    """Rewrite the running order so skipped slots air the next episode.

    A skip is *not* "cut this episode out". Removing a programme and keeping its
    neighbours' start times would leave a hole the length of a show, and ErsatzTV
    plays a gap as black -- on a live channel that is a visibly dead stream. So the
    replacement keeps ownership of the same wall-clock span, and the timeline stays
    exactly as long as it was.

    The replacement episode must fit the slot it inherits. It usually does, because
    episodes of one show land in the same slot family, but a 23-minute episode does
    exist in a 15-minute show's library, and asking the planner for 23 minutes of a
    22-minute slot is how the whole day gets refused. When it does not fit, the
    show's rotation is walked forward to the first episode that does -- which is
    still "the next one you can watch", not a silent no-op.

    Only the running order is rewritten, and the caller plans it once, so a skipped
    day costs a normal day plus one probe slot.
    """
    by_index = {assigned.slot.index: assigned for assigned in ordered}

    for slot_index, skip in sorted(skips.items()):
        assigned = by_index.get(slot_index)
        if assigned is None:
            plan.warnings.append(
                f"{plan.day} slot {slot_index}: skip ignored, that slot is not in today's plan"
            )
            continue

        # Plan the slot as it stands, to learn which episode is airing. Its length
        # cannot be read off the slot: a 21-minute and a 23-minute episode occupy
        # the same 30-minute slot and leave very different room for break.
        probe = Plan(
            day=plan.day,
            channel_name=plan.channel_name,
            channel_number=plan.channel_number,
            tvg_id=plan.tvg_id,
            day_start_ms=plan.day_start_ms,
            day_end_ms=plan.day_end_ms,
        )
        _plan_slot(
            probe,
            assigned,
            None,
            None,
            context,
            bump_tracker=bump_tracker,
            ad_tracker=ad_tracker,
            real_bumps=real_bumps,
            generated_pool=generated_pool,
            generated_budget=generated_budget,
            mark=mark,
            extra_tracker=extra_tracker,
        )
        playing = next((item for item in probe.items if item.kind == "episode"), None)
        if playing is None:
            plan.warnings.append(
                f"{plan.day} slot {slot_index}: skip ignored, nothing is airing there"
            )
            continue
        if skip.episode_key and playing.asset_key != skip.episode_key:
            # The plan moved underneath the request (a rebuild, a config change).
            # Applying it now would swap out an episode nobody asked to skip.
            plan.warnings.append(
                f"{plan.day} slot {slot_index}: skip ignored, "
                f"{skip.episode_key} is no longer the episode airing there"
            )
            continue

        replacement = _next_episode(assigned, playing, context)
        if replacement is None:
            plan.warnings.append(
                f"{plan.day}: skip ignored, {assigned.show_id} has no other episode "
                f"that fits a {assigned.slot.length_ms / 60_000:.0f}-minute slot"
            )
            continue

        by_index[slot_index] = replace(assigned, episode=replacement)
        plan.warnings.append(
            f"{plan.day} slot {slot_index}: skipped to {assigned.show_id} "
            f"S{replacement.season:02d}E{replacement.episode:02d}"
        )

    return sorted(by_index.values(), key=lambda a: a.slot.start_ms)


def _next_episode(assigned: AssignedSlot, playing: PlannedItem, context: PlanContext):
    """The episode after *playing* in its show's rotation, if it fits the slot.

    Walked forward rather than taken at ``ordinal + 1``: episodes in one show are
    not guaranteed equal in length, and "next" that cannot air is not a skip.
    """
    ring = context.rings.get(assigned.show_id)
    if ring is None or len(ring) < 2:
        return None
    start = next(
        (index for index, episode in enumerate(ring.episodes) if episode.key == playing.asset_key),
        None,
    )
    if start is None:
        return None
    limit = assigned.slot.length_ms
    for step in range(1, len(ring) + 1):
        candidate = ring.episodes[(start + step) % len(ring)]
        if candidate.key == playing.asset_key:
            continue
        if candidate.duration_ms < limit:
            return candidate
    return None


def _assignments_from_programming(placed: Sequence) -> dict[str, AssignedSlot]:
    """Turn hand-programmed slots into the map the planner consumes.

    ``of_same_show`` comes from the actual line-up rather than being assumed to be
    one: a show appearing three times in a day must not restart its episode ring
    at each appearance, or a marathon would play episode one three times. The
    episode is carried through directly, because the point of a hand-built lineup
    is that a human said exactly what plays.
    """
    totals: dict[str, int] = {}
    for entry in placed:
        totals[entry.show_id] = totals.get(entry.show_id, 0) + 1

    seen: dict[str, int] = {}
    assignments: dict[str, AssignedSlot] = {}
    for entry in placed:
        position = seen.get(entry.show_id, 0)
        seen[entry.show_id] = position + 1
        assignments[slot_key(entry.slot)] = AssignedSlot(
            slot=entry.slot,
            show_id=entry.show_id,
            slot_in_day=position,
            of_same_show=totals[entry.show_id],
            episode=entry.episode,
        )
    return assignments


def watermark_rule(root: RootConfig) -> "WatermarkRule | None":
    """The active overlay rule, or ``None`` when the streamer should not be asked.

    Only ``overlay`` mode returns a rule: in ``burn`` mode the file on disk
    already carries the mark, so asking the streamer to add a second one would
    double it, and in ``off`` mode there is nothing to do.
    """
    config = root.watermark
    if config.mode != "overlay":
        return None
    return WatermarkRule(
        enabled=True,
        shows=tuple(config.shows),
        except_shows=tuple(config.except_shows),
        # Honoured in overlay mode too. This used to be forced False, which meant
        # an episode that already carried the network's own bug got a second one
        # stamped on top -- visible immediately on any Harvey Birdman or Brak Show
        # rip, and reported as such.
        skip_when_burned_in=config.skip_when_burned_in,
    )


def slot_key(slot: Slot) -> str:
    return f"{slot.start_ms}"


@dataclass
class GeneratedBudget:
    """Caps how much of the channel is synthetic.

    ``max_share`` is a promise to the viewer as much as a config knob: generated
    cards fill gaps, they do not become the channel's voice.
    """

    max_slots: int
    used: int = 0

    def available(self) -> bool:
        return self.used < self.max_slots

    def take(self) -> None:
        self.used += 1


def _generated_allowance(slot_count: int, max_share: float) -> int:
    if max_share <= 0 or slot_count <= 0:
        return 0
    # Each slot has several bump positions; the share is of *bump slots*.
    bump_slots = slot_count * 3
    return int(bump_slots * max_share)


#: Below this many era-appropriate spots, a strict filter would repeat so heavily
#: that a break stops sounding like a real one, so the pool is widened instead.
MIN_ERA_POOL = 12

#: Fewer usable bump cards than this means the pool really is too thin and the
#: skip is worth reporting. Above it, picking a long card and falling back to the
#: next is ordinary operation, not a fault.
MIN_BUMP_ALTERNATIVES = 5


def era_ad_pool(ads: Sequence[Asset], year: int, drift: int) -> tuple[list[Asset], bool]:
    """Commercials whose era covers *year*, within *drift* years.

    ``ads.era_drift_years`` was documented as governing commercials but was only
    ever consulted for bump scoring, so a 2000 spot could air on a 2003 day.

    Untagged spots (no year folder) stay eligible: the owner has not said when
    they are from, which is different from saying they are wrong.

    Returns ``(pool, widened)``. ``widened`` is true when the era-appropriate
    selection was too thin to fill breaks and the whole pool was used instead.
    """
    if not any(asset.era_from is not None for asset in ads):
        # Nothing is era-tagged, so filtering cannot mean anything yet.
        return list(ads), False

    in_era = [
        asset
        for asset in ads
        if asset.era_from is None or asset.era_covers(year, drift)
    ]
    if len(in_era) >= MIN_ERA_POOL:
        return in_era, False
    return list(ads), True


def render_generated_bumps(
    context: PlanContext,
    *,
    out_dir: Path | None = None,
    style_pack: str | None = None,
    ffmpeg: str | None = None,
    force: bool = False,
) -> list[Asset]:
    """Render the generated-bump pool for this channel's roster.

    One card per ``(era style, position, show)``, which is a few dozen small files
    for a whole channel. Cards go through the normal catalog on the next ``scan``,
    so the planner and ``--no-generated`` work without special cases.
    """
    from .bumpgen import BumpGenError, load_era_packs, render_pool, specs_for_schedule

    era_file = context.era_file
    if era_file is None or not era_file.is_file():
        raise PlanError(f"missing era style file: {era_file}")

    pack = load_era_packs(era_file)
    if style_pack and style_pack not in pack.styles:
        available = ", ".join(sorted(pack.styles))
        raise PlanError(f"unknown style pack {style_pack!r}; available: {available}")

    titles = {show_id: show.title for show_id, show in context.root.shows.items()}
    # The era decides the look. Never "now": a nostalgia channel that renders
    # this year's cards for a 2003 season is the wrong channel.
    year = context.root.era_year
    generated = context.root.bumps.generated

    # What a card says, and what it sounds like. Both are optional and both degrade
    # to exactly the old plain, silent card, so a channel with no bank and no music
    # renders what it always did.
    from .bumplines import load_bank
    from .music import MusicLibrary, load_bump_beds, load_library

    bank = load_bank(context.lines_file) if generated.lines else None
    music = None
    if generated.music:
        from .conform import find_tools

        roots = context.music_dirs or ()
        beds = load_library(roots, ffprobe=find_tools(ffmpeg)[1]).beds if roots else []
        if generated.music_from_bumps:
            # The real cards are the sound being reproduced, so they are the better
            # source; a dropped-in track stays eligible beside them.
            beds = beds + load_bump_beds(context.bumps)
        library = MusicLibrary(root=roots[0] if roots else Path("."), beds=beds)
        music = library if library else None

    positions = ("INTO", "AFTER", "NEXT", "OUT") if bank else ("INTO", "AFTER", "NEXT")

    specs = specs_for_schedule(
        shows=[show_id for show_id, show in context.root.shows.items() if show.enable],
        titles=titles,
        year=year,
        positions=positions,
        max_seconds=12,
        bank=bank,
        music=music,
        variants=generated.variants,
        hold_s=pack.style_for(year).hold_ms / 1000,
    )
    target = Path(out_dir) if out_dir else context.generated_dir
    try:
        return render_pool(
            specs,
            pack=pack,
            out_dir=target,
            width=context.root.encoding.width,
            height=context.root.encoding.height,
            ffmpeg=ffmpeg or context.root.channel.ffmpeg_path,
            force=force,
            music=music,
            # A caller-supplied folder may hold real media; only the channel's own
            # cache is safe to prune.
            prune=out_dir is None,
        )
    except BumpGenError as exc:
        raise PlanError(str(exc)) from exc


def _plan_slot(
    plan: Plan,
    assigned: AssignedSlot,
    next_assigned: AssignedSlot | None,
    prev_assigned: AssignedSlot | None,
    context: PlanContext,
    *,
    bump_tracker: CooldownTracker,
    ad_tracker: CooldownTracker,
    real_bumps: Sequence[Asset],
    generated_pool: Sequence[Asset],
    generated_budget: GeneratedBudget,
    mark: "WatermarkRule | None" = None,
    extra_tracker: CooldownTracker | None = None,
) -> None:
    """Resolve one slot in two passes.

    Pass 1 chooses the bump *assets*, because their exact durations are what the
    pods must fit around. Pass 2 spends the remaining budget on pods and emits
    everything in template order.

    Doing it the other way round -- emitting as we go -- is what leaves the
    closing "up next" card with no room, or dumps the leftover after it.
    """
    root = context.root
    slot = assigned.slot
    prefix = f"{plan.day} slot {slot.index}"

    episode = _episode_for(plan, assigned, context)
    if episode is None:
        return

    # An episode longer than its slot cannot be made to fit by any arrangement of
    # breaks: the timeline would cross the boundary and take every later slot with
    # it. A long-running show accumulates a double-length episode eventually, so
    # this is not a config error to refuse over -- it is one episode that cannot
    # air in this slot. Try another that fits; only if none does, give the slot to
    # the pod. Letting it through took the whole day down with "timeline is
    # invalid", which named neither the show nor the fix.
    if episode.duration_ms >= slot.length_ms:
        replacement = _fitting_episode(assigned, episode, context)
        if replacement is None:
            _fill_bare_slot(plan, assigned, context, ad_tracker, mark)
            plan.warnings.append(
                f"{prefix}: no {assigned.show_id} episode fits a "
                f"{slot.length_ms / 60_000:.0f}-minute slot (the one selected ran "
                f"{episode.duration_ms / 60_000:.1f} min); slot filled with breaks -- "
                f"give the show a larger 'slot' family in shows.yaml"
            )
            return
        plan.warnings.append(
            f"{prefix}: {assigned.show_id} S{episode.season:02d}E{episode.episode:02d} "
            f"runs {episode.duration_ms / 60_000:.1f} min and cannot fit "
            f"{slot.length_ms / 60_000:.0f} minutes; airing "
            f"S{replacement.season:02d}E{replacement.episode:02d} instead"
        )
        episode = replacement

    # A slot that is mostly break time means the episode length does not match
    # the slot family. That is a config mistake, and silently airing minutes of
    # commercials hides it.
    slack = slot.length_ms - episode.duration_ms
    if slack > slot.length_ms * 0.55:
        plan.warnings.append(
            f"{prefix}: {assigned.show_id} episode is "
            f"{episode.duration_ms / 1000:.0f}s in a {slot.length_ms / 1000:.0f}s slot, "
            f"leaving {slack / 1000:.0f}s of break -- check the show's 'slot' family "
            f"or the episode length"
        )

    template = _template_for(assigned, context)
    script = build_script(template, episode_ms=episode.duration_ms)
    plan.warnings.extend(f"{prefix}: {warning}" for warning in script.warnings)

    # -- the extras roll ------------------------------------------------------
    # Roughly one break in N airs something from media/extras/ instead of spots.
    # Rolled per *pod*, not per slot: a template puts two or three pods in a slot
    # and they are separate breaks, so "1 in 5 breaks" has to count breaks. Seeded
    # from (day, slot, pod) so it is independent of the template's shape -- editing
    # a template cannot reshuffle which breaks are special -- and so a rebuild
    # reproduces the day exactly.
    extra_pool: list[Asset] = []
    # `extras_rate` is 0.0 whenever the master switch is off, so there is no
    # separate "is it enabled" test here to drift out of step with the config.
    if context.extras and extra_tracker is not None and root.ads.extras_rate > 0:
        cooling = extra_tracker.cooling([], slot.start_ms)
        extra_pool = [asset for asset in context.extras if asset.key not in cooling]
        rolled = _roll_extras(
            plan.day, slot.index, script.specs, chance=root.ads.extras_rate
        )
        if any(spec.kind == "extra" for spec in rolled):
            if extra_pool:
                script.specs = rolled
            else:
                # The roll fired but there is nothing to air. Counted rather than
                # warned per slot: with a small pool and a 24h cooldown almost
                # every later break hits this, and 67 identical lines bury the one
                # warning that matters. build_plan reports the total once.
                plan.extras_cooled += 1

    show = root.shows.get(assigned.show_id)
    # The timeline's date is a real date so the stream is consumable today, but
    # era matching is about the year being *recreated*. Using plan.day.year here
    # would score every 2003 card as 23 years stale.
    era_year = root.era_year
    request_base = dict(
        this_show=assigned.show_id,
        next_show=next_assigned.show_id if next_assigned else None,
        prev_show=prev_assigned.show_id if prev_assigned else None,
        year=era_year,
        family=show.family if show else None,
        # The bump pool's own tolerance. This used to be ads.era_drift_years,
        # which meant changing how loose the commercials were silently loosened
        # the continuity cards too.
        era_drift=root.bumps.era_drift_years,
        allow_other_shows=root.bumps.allow_other_shows,
    )

    # -- pass 1: resolve bumps -------------------------------------------------
    bump_assets: dict[int, Asset] = {}
    bump_reasons: dict[int, str] = {}
    # Cards chosen earlier *in this slot*. The cooldown tracker cannot answer
    # this: it is only written as items are emitted, which happens in pass 2, so
    # every spec here used to see identical state and a template with two "up
    # next" segments picked the same card twice, back to back.
    #
    # A cooldown is the wrong tool even if it were current: `choose` deliberately
    # resurrects a cooling card when it beats what would air instead, which is
    # right for a *later* slot and wrong for something 5 seconds later.
    chosen_here: set[str] = set()
    # A wrong-subject *promo* is now intended behaviour, not a gap in the library,
    # so it is no longer logged as a fallback: only a general card standing in for
    # a show-specific one is.
    for index, spec in enumerate(script.specs):
        if spec.kind != "bump":
            continue
        request = BumpRequest(
            subject=spec.subject,
            position=spec.position,
            resumes=spec.resumes,
            **request_base,
        )
        allowed = tuple(
            asset for asset in generated_pool if asset.key not in chosen_here
        ) if generated_budget.available() else ()
        # A card that already aired in this break is removed from the pool rather
        # than merely cooled, so no ladder step -- including the cooled-upgrade
        # one -- can hand it back.
        fresh = [asset for asset in real_bumps if asset.key not in chosen_here]
        choice = choose(
            fresh,
            request,
            min_score=root.bumps.min_score,
            strictness=root.bumps.strictness,
            # Cooldown is evaluated per chosen card below, so a decision here does
            # not depend on where in the slot the card ends up.
            on_cooldown=bump_tracker.cooling([], slot.start_ms),
            seed_parts=(plan.day.isoformat(), slot.index, index),
            generated=allowed,
            promo_share=root.bumps.promo_share,
        )
        asset = choice.asset
        if asset is None:
            plan.warnings.append(
                f"{prefix}: no bump available ({spec.subject}:{spec.position})"
            )
            continue
        if spec.max_seconds and asset.duration_ms > spec.max_seconds * 1000:
            # Only worth a warning when the *only* cards that matched are too
            # long. A pool with plenty of alternatives that simply picked a long
            # one is normal operation, and warning about it buried the real
            # problems (a genuinely missing subject) under 77 lines of noise.
            shortlist = [candidate for candidate in real_bumps if candidate.duration_ms <= spec.max_seconds * 1000]
            if len(shortlist) < MIN_BUMP_ALTERNATIVES:
                plan.warnings.append(
                    f"{prefix}: only {len(shortlist)} bump(s) fit the "
                    f"{spec.max_seconds}s cap; {asset.key} was {asset.duration_ms}ms. "
                    f"Raise max_seconds in continuity.yaml or add shorter cards"
                )
            continue
        chosen_here.add(asset.key)
        bump_assets[index] = asset
        bump_reasons[index] = choice.reason
        if choice.fallback:
            # The same id the item is emitted under below, so a caller can
            # actually find the card that fell back.
            plan.bump_fallbacks.append(
                f"{plan.day:%Y%m%d}-{slot.index:04d}-b{index:02d}"
            )
            plan.bump_fallback_reasons.append(
                f"{plan.day} slot {slot.index}: {spec.subject}:{spec.position} "
                f"had no show-specific card ({choice.reason})"
            )
        if asset.is_generated:
            generated_budget.take()

    # Cards already placed in *this slot*, bumped and filler alike. Passed to every
    # filler call so a promo cannot repeat the closing card it was topping up
    # after -- both pools draw on the same short library, so they have to agree on
    # what has already been used here.
    used_here: set[str] = set(chosen_here)
    bump_total = sum(asset.duration_ms for asset in bump_assets.values())

    # -- pass 2: spend the rest on pods, then emit ----------------------------
    pod_budget = slot.length_ms - episode.duration_ms - bump_total
    if pod_budget < 0:
        # Too many bumps for the break: drop the lowest-priority trailing ones
        # rather than overrun the slot.
        for index in sorted(bump_assets, reverse=True):
            if pod_budget >= 0:
                break
            pod_budget += bump_assets[index].duration_ms
            plan.warnings.append(
                f"{prefix}: dropped bump {bump_assets[index].key} to fit the slot"
            )
            bump_assets.pop(index, None)
            bump_reasons.pop(index, None)

    # Bumps appear at known positions in the template, so the pod segments can be
    # sized against a budget that is already free of them.
    shares = [
        spec.share
        for spec in script.specs
        if spec.kind in FILLER_KINDS and spec.share
    ]
    fixed = sum(
        spec.seconds
        for spec in script.specs
        if spec.kind in FILLER_KINDS and spec.seconds
    )
    shared_budget = max(0, pod_budget - fixed)

    pod_targets = {target.seconds: target.count for target in root.ads.pod_targets}

    # A pod may spend everything except the room that bumps *later* in the
    # template still need. Without this the final pod eats the closing "up next"
    # card, which is the single most recognisable thing on the channel.
    bump_after_ms: dict[int, int] = {}
    running = 0
    for index in reversed(range(len(script.specs))):
        bump_after_ms[index] = running
        asset = bump_assets.get(index)
        if asset is not None:
            running += asset.duration_ms

    cursor_ms = slot.start_ms
    emitted = 0

    # Only spots whose era covers the channel's era may air, so a 2000 tape does
    # not turn up in a 2008 break. Untagged spots stay eligible.
    ad_pool, widened = era_ad_pool(context.ads, era_year, root.ads.era_drift_years)
    # Merging joins whole conformed files by stream copy, which cannot cut a spot
    # short: a trimmed spot would lose its dip and could not join its neighbours.
    # So when every spot in the pool is conformed, trimming is off and a pod lands
    # on the nearest whole spots, with the tail absorbing the difference. Until the
    # library is conformed, behaviour is exactly what it was.
    trim_slop = (
        0
        if root.ads.merge_breaks and ad_pool and all(asset.is_conformed for asset in ad_pool)
        else root.ads.trim_slop_ms
    )
    if widened and not any("match era" in warning for warning in plan.warnings):
        matching = sum(
            1
            for asset in context.ads
            if asset.era_from is not None
            and asset.era_covers(era_year, root.ads.era_drift_years)
        )
        plan.warnings.append(
            f"{prefix}: only {matching} commercial(s) match era {era_year} "
            f"+/-{root.ads.era_drift_years}y; widened to the whole pool -- add "
            f"spots under media/commercials/{era_year}/"
        )

    for index, spec in enumerate(script.specs):
        if spec.kind == "episode":
            part_len = spec.duration_ms or episode.duration_ms
            _append(
                plan,
                PlannedItem(
                    item_id=f"{plan.day:%Y%m%d}-{slot.index:04d}-ep{spec.part + 1}",
                    kind="episode",
                    start_ms=cursor_ms,
                    end_ms=cursor_ms + part_len,
                    asset_key=episode.key,
                    path=episode.path,
                    title=show.title if show else assigned.show_id,
                    subtitle=f"S{episode.season:02d}E{episode.episode:02d}",
                    description=episode.title,
                    show_id=assigned.show_id,
                    slot_index=slot.index,
                    in_point_ms=spec.offset_ms if spec.of_parts > 1 else None,
                    out_point_ms=(spec.offset_ms + part_len) if spec.of_parts > 1 else None,
                    guide=True,
                    watermark=_wants_mark(mark, assigned.show_id, root, episode),
                ),
            )
            cursor_ms += part_len
            emitted += 1
            continue

        if spec.kind == "bump":
            asset = bump_assets.get(index)
            if asset is None:
                continue
            if cursor_ms + asset.duration_ms > slot.end_ms:
                plan.warnings.append(
                    f"{prefix}: skipped {asset.key}, no room left"
                )
                continue
            _append(
                plan,
                PlannedItem(
                    item_id=f"{plan.day:%Y%m%d}-{slot.index:04d}-b{index:02d}",
                    kind="bump",
                    start_ms=cursor_ms,
                    end_ms=cursor_ms + asset.duration_ms,
                    asset_key=asset.key,
                    path=asset.playable_path,
                    conformed=asset.is_conformed,
                    title=_show_title(root, asset.subject) or "bump",
                    subtitle=f"[adult swim] {spec.position.lower()}",
                    show_id=asset.subject if asset.subject not in (None, NEUTRAL) else None,
                    slot_index=slot.index,
                    note=bump_reasons.get(index, ""),
                ),
            )
            bump_tracker.record(asset.key, cursor_ms)
            cursor_ms += asset.duration_ms
            emitted += 1
            continue

        if spec.kind in FILLER_KINDS:
            # `remaining` excludes the bumps that still have to air after this
            # break. An extras block is sized exactly like the pod it replaced --
            # it is the same break, so it must occupy the same span, which is what
            # keeps every later programme starting when it would have anyway.
            reserve = bump_after_ms.get(index, 0)
            remaining = slot.end_ms - cursor_ms - reserve
            if remaining <= 0:
                continue
            if spec.share:
                target = round(shared_budget * spec.share)
            else:
                target = _pod_target_ms(spec, pod_targets, remaining)
            # The final real break fills up to (but not past) the reserved bumps;
            # any sliver left over is covered afterwards, so the closing card still
            # lands last and the next slot starts on its boundary.
            last = _is_last_filler(script, spec)
            if last:
                target = remaining
            target = min(target, remaining)
            if target <= 0:
                continue
            must_fill = not _has_item_after(script, spec)

            filler: PodFill | None = None
            if spec.kind == "extra":
                filler = _fill_extras_block(
                    plan,
                    prefix=prefix,
                    extras=extra_pool,
                    fallback=ad_pool,
                    target_ms=target,
                    used=used_here,
                    on_cooldown=ad_tracker.cooling([], cursor_ms),
                    seed_parts=(plan.day.isoformat(), slot.index, "extra", cursor_ms),
                    root=root,
                    must_fill=must_fill,
                )
                if filler is not None:
                    cursor_ms = _emit_pod(
                        plan,
                        filler,
                        cursor_ms=cursor_ms,
                        slot=slot,
                        suffix="e",
                        tracker=ad_tracker,
                        root=root,
                        kind="extra",
                        used=used_here,
                        # The extras pool has its own, much longer cooldown; a spot
                        # topping up a short block still belongs on the ad
                        # tracker.
                        extra_tracker=extra_tracker,
                    )
                    emitted += 1
                    continue
                # Nothing in the pool fits this break. Fall through to the
                # commercials it was going to air: a replaced break that cannot be
                # filled must never become a hole in the timeline.
                plan.warnings.append(
                    f"{prefix}: no extra fits a {target // 1000}s break; "
                    f"airing commercials"
                )

            if spec.kind == "ident":
                pool = context.idents or context.ads
            else:
                # A spot that has already aired in this slot is removed rather
                # than merely cooled. A cooldown can be overridden by the
                # tolerance/nearest-combination fallbacks, and on a small era pool
                # that is enough to put the same commercial on twice in one break.
                pool = [asset for asset in ad_pool if asset.key not in used_here] or ad_pool
            if last:
                filler = fill_gap(
                    pool,
                    target_ms=target,
                    on_cooldown=ad_tracker.cooling([], cursor_ms),
                    trim_slop_ms=trim_slop,
                    seed_parts=(plan.day.isoformat(), slot.index, "tail", cursor_ms),
                    short_pool=_promo_pool(
                        real_bumps, generated_pool, context.idents, exclude=used_here
                    ),
                    # A mid-break pod is followed by more material, which absorbs
                    # any shortfall by starting a few ms early. Only the closing pod
                    # runs to the slot boundary and has to land exactly.
                    must_fill=must_fill,
                )
            else:
                try:
                    filler = fill_pod(
                        pool,
                        target_ms=target,
                        on_cooldown=ad_tracker.cooling([], cursor_ms),
                        tolerance_ms=root.ads.pod_tolerance_ms,
                        trim_slop_ms=trim_slop,
                        seed_parts=(plan.day.isoformat(), slot.index, cursor_ms),
                    )
                except PodError as exc:
                    plan.warnings.append(f"{prefix}: {exc}")
                    continue
            cursor_ms = _emit_pod(
                plan,
                filler,
                cursor_ms=cursor_ms,
                slot=slot,
                suffix="a",
                tracker=ad_tracker,
                root=root,
                kind="ident" if spec.kind == "ident" else "ad",
                used=used_here,
            )
            emitted += 1

    if cursor_ms < slot.end_ms:
        # Trailing dead air means the template under-budgeted the break. Cover it
        # exactly rather than letting the next slot start early.
        remainder = slot.end_ms - cursor_ms
        # A sub-second remainder is a rounding artefact, not a gap. Emitting a card
        # for it puts a single frame of black at the slot boundary -- a flash, and
        # it was happening several times an hour. Absorbing it into the item just
        # before is invisible: the durations are integer milliseconds, so a file
        # recorded as 5349 ms is really 5.3493 s and giving back 1 ms is correcting
        # the rounding rather than inventing footage.
        if remainder <= BLACK_CARD_MIN_MS and _absorb_remainder(plan, remainder):
            cursor_ms = slot.end_ms
        else:
            try:
                filler = fill_gap(
                    [asset for asset in ad_pool if asset.key not in used_here] or ad_pool,
                    target_ms=remainder,
                    on_cooldown=ad_tracker.cooling([], cursor_ms),
                    trim_slop_ms=trim_slop,
                    seed_parts=(plan.day.isoformat(), slot.index, "trail", cursor_ms),
                    short_pool=_promo_pool(
                        real_bumps, generated_pool, context.idents, exclude=used_here
                    ),
                    # Nothing follows this: it runs to the slot boundary. A
                    # shortfall here is a gap in the timeline, so it must land
                    # exactly even when the leftover is only a few milliseconds.
                    must_fill=True,
                )
                cursor_ms = _emit_pod(
                    plan,
                    filler,
                    cursor_ms=cursor_ms,
                    slot=slot,
                    suffix="t",
                    tracker=ad_tracker,
                    root=root,
                    used=used_here,
                )
            except PodError as exc:
                plan.warnings.append(f"{prefix}: {exc}")

    if cursor_ms != slot.end_ms:
        plan.warnings.append(
            f"{prefix}: ended {cursor_ms - slot.end_ms}ms "
            f"{'late' if cursor_ms > slot.end_ms else 'early'}"
        )
    if emitted == 0:
        plan.warnings.append(f"{prefix}: produced no items")

def _wants_mark(
    mark: "WatermarkRule | None",
    show_id: str,
    root: RootConfig,
    episode,
) -> bool:
    """Whether the streamer should composite a bug over this episode.

    Three things have to hold: the rule covers this show, and the episode is not
    already carrying a bug of its own. An episode that has never been examined
    counts as *not* carrying one, because the alternative -- refusing to mark
    everything until a full rescan completes -- would switch the bug off for the
    whole channel. ``nostalgia scan --detect-marks`` fills the gaps in.
    """
    if mark is None:
        return False
    if not mark.applies_to(kind="episode", show_id=show_id, era_year=root.era_year):
        return False
    if mark.skip_when_burned_in and getattr(episode, "carries_bug", None):
        return False
    return True


def _fitting_episode(
    assigned: AssignedSlot, rejected, context: PlanContext
):
    """Another episode of the same show that fits this slot, or ``None``.

    Deterministic: the ring is walked in order from the rejected episode, so the
    same day always makes the same substitution. A show whose episodes are all
    too long for its declared family gets ``None`` and the slot falls back to
    breaks, which is visible in the warnings rather than silent.
    """
    ring = context.rings.get(assigned.show_id)
    if ring is None or not ring.episodes:
        return None
    limit = assigned.slot.length_ms
    start = 0
    for index, candidate in enumerate(ring.episodes):
        if candidate.key == rejected.key:
            start = index + 1
            break
    for offset in range(len(ring.episodes)):
        candidate = ring.episodes[(start + offset) % len(ring.episodes)]
        if candidate.duration_ms < limit:
            return candidate
    return None


def _fill_bare_slot(
    plan: Plan,
    assigned: AssignedSlot,
    context: PlanContext,
    ad_tracker: CooldownTracker,
    mark: "WatermarkRule | None",
) -> None:
    """Cover a slot that has no programme with commercials and cards.

    A gap in the timeline is not an option -- every later slot would shift -- so
    the slot is filled with break material. It is a poor hour of television, but
    it is a *continuous* hour, and the warning says why.
    """
    slot = assigned.slot
    root = context.root
    try:
        filler = fill_gap(
            context.ads,
            target_ms=slot.length_ms,
            on_cooldown=ad_tracker.cooling([], slot.start_ms),
            trim_slop_ms=root.ads.trim_slop_ms,
            seed_parts=(plan.day.isoformat(), slot.index, "bare", slot.start_ms),
            short_pool=_promo_pool(context.bumps, (), context.idents),
        )
    except PodError as exc:
        plan.warnings.append(f"{plan.day} slot {slot.index}: {exc}")
        return
    _emit_pod(
        plan,
        filler,
        cursor_ms=slot.start_ms,
        slot=slot,
        suffix="x",
        tracker=ad_tracker,
        root=root,
    )


def _fill_extras_block(
    plan: Plan,
    *,
    prefix: str,
    extras: Sequence[Asset],
    fallback: Sequence[Asset],
    target_ms: int,
    used: set[str],
    on_cooldown: set[str],
    seed_parts: Sequence[object],
    root: RootConfig,
    must_fill: bool,
) -> PodFill | None:
    """Fill a replaced break with long-form material.

    An extra is one piece, not a pod. A music video is 3-5 minutes and a break is
    2-7, so the honest options are "play one and let the remainder be covered" or
    "play one and top up with a spot". Both happen; what never happens is cutting
    a music video down to 47 seconds to hit a booking length, which is exactly the
    kind of violent edit this feature exists to avoid.

    So: take the longest extra that fits the target, then top up with other extras
    while they still fit, and only then fall back to ``fill_gap`` on commercials.

    Returns ``None`` when nothing could be placed. The caller treats that as "air
    the commercials after all" rather than as a fatal error: a replaced break that
    cannot be filled must never become a hole in the timeline.
    """
    cooling = set(on_cooldown)
    candidates = [
        asset for asset in extras if asset.duration_ms > 0 and asset.key not in used
    ]
    if not candidates:
        return None

    # Prefer something that is not cooling, but never refuse to air an extra just
    # because everything is cooling: a blank break would be worse than a repeat.
    fresh = [asset for asset in candidates if asset.key not in cooling]
    pool = sorted(fresh or candidates, key=lambda asset: (asset.duration_ms, asset.key))

    fitting = [asset for asset in reversed(pool) if asset.duration_ms <= target_ms]
    if not fitting:
        # Every extra is longer than the break. Rather than cut one mid-song --
        # the exact "jumping around" this is meant to remove -- return None and let
        # the caller air the spots the break was going to have anyway.
        return None

    items: list[PodItem] = [PodItem(fitting[0])]
    remaining = target_ms - fitting[0].duration_ms

    # Deliberately at most one more piece: two music videos back to back is a music
    # channel, not a commercial break.
    while remaining > 0 and len(items) < 2:
        taken = {item.asset.key for item in items}
        fits = [
            asset
            for asset in pool
            if asset.duration_ms <= remaining and asset.key not in taken
        ]
        if not fits:
            break
        pick = max(fits, key=lambda asset: (asset.duration_ms, asset.key))
        items.append(PodItem(pick))
        remaining -= pick.duration_ms

    if remaining > BLACK_CARD_MIN_MS:
        # Top up with spots, which is what a station does when a piece runs short.
        try:
            top_up = fill_gap(
                [asset for asset in fallback if asset.key not in used] or fallback,
                target_ms=remaining,
                on_cooldown=cooling,
                trim_slop_ms=root.ads.trim_slop_ms,
                seed_parts=(*seed_parts, "topup"),
                must_fill=must_fill,
            )
            items.extend(top_up.items)
        except PodError as exc:
            plan.warnings.append(f"{prefix}: {exc}")

    achieved = sum(item.duration_ms for item in items)
    return PodFill(
        items=tuple(items),
        target_ms=target_ms,
        achieved_ms=achieved,
        note=f"extras block ({achieved}ms of {target_ms}ms)",
        synthetic_ms=sum(item.duration_ms for item in items if item.asset.is_synthetic),
    )


def _emit_pod(
    plan: Plan,
    fill: PodFill,
    *,
    cursor_ms: int,
    slot: Slot,
    suffix: str,
    tracker: CooldownTracker,
    root: RootConfig,
    kind: str = "ad",
    used: set[str] | None = None,
    extra_tracker: CooldownTracker | None = None,
) -> int:
    """Append a filled pod's items and return the new cursor.

    Items that would cross the slot boundary are dropped with a warning rather
    than allowed to overrun: a late item pushes everything after it and the
    channel loses the grid permanently.

    *used* collects every asset placed in this slot, so the caller can keep filler
    from repeating a card the template already aired.

    *extra_tracker* is used for items whose kind is ``extra``; everything else
    goes on *tracker*. The two pools have deliberately different cooldowns, and a
    commercial topping up a short extras block was being recorded on the extras
    tracker -- so it stayed eligible on the very next break, which is why the same
    spot could appear twice within minutes of a replaced break.
    """
    for index, pod_item in enumerate(fill.items):
        if cursor_ms + pod_item.duration_ms > slot.end_ms:
            plan.warnings.append(
                f"{plan.day} slot {slot.index}: dropped {pod_item.asset.key}, "
                f"it would cross the slot boundary"
            )
            break
        # A promo inside a pod keeps its own kind, so the guide never claims a
        # bumper aired as a commercial.
        if pod_item.asset.kind in ("bump", "ident"):
            item_kind = pod_item.asset.kind
        elif kind == "extra" and pod_item.asset.kind == "ad":
            # A spot topping up a short extras block is still a commercial. Folding
            # it into "extra" would have the guide claim four minutes of music video
            # when two of them were spots.
            item_kind = "ad"
        else:
            item_kind = kind
        if pod_item.asset.is_synthetic:
            subtitle = "black"
        elif item_kind == "bump":
            subtitle = "promo"
        elif item_kind == "extra":
            subtitle = "extra"
        else:
            subtitle = "commercial"
        _append(
            plan,
            PlannedItem(
                item_id=(
                    f"{plan.day:%Y%m%d}-{slot.index:04d}-{suffix}"
                    f"{cursor_ms - slot.start_ms:07d}-{index}"
                ),
                kind=item_kind,
                start_ms=cursor_ms,
                end_ms=cursor_ms + pod_item.duration_ms,
                asset_key=pod_item.asset.key,
                path=pod_item.asset.playable_path,
                conformed=pod_item.asset.is_conformed,
                title=(
                    _show_title(root, pod_item.asset.subject)
                    or pod_item.asset.brand
                    # An extra has no brand and no subject, so the filename is the
                    # only label that means anything -- and it is what the owner
                    # named it.
                    or (Path(pod_item.asset.path).stem if item_kind == "extra" else None)
                    or "commercial"
                ),
                subtitle=subtitle,
                slot_index=slot.index,
                out_point_ms=pod_item.out_point_ms,
                # Only the synthetic item is annotated: repeating the pod note on
                # every item in the break made the timeline unreadable.
                note="" if not pod_item.asset.is_synthetic else fill.note,
                lavfi_params=pod_item.asset.lavfi_params,
            ),
        )
        if not pod_item.asset.is_synthetic:
            if item_kind == "extra" and extra_tracker is not None:
                extra_tracker.record(pod_item.asset.key, cursor_ms)
            else:
                tracker.record(pod_item.asset.key, cursor_ms)
        if used is not None:
            used.add(pod_item.asset.key)
        cursor_ms += pod_item.duration_ms
    return cursor_ms


def _episode_for(plan: Plan, assigned: AssignedSlot, context: PlanContext):
    # Hand programming already chose the exact episode, so re-deriving it from the
    # ring here would discard the lineup (and make "random overall" reproducible
    # in the wrong way).
    if assigned.episode is not None:
        return assigned.episode

    ring = context.rings.get(assigned.show_id)
    if ring is None or not ring.episodes:
        plan.warnings.append(
            f"{assigned.show_id}: no episodes catalogued; slot {assigned.slot.index} skipped"
        )
        return None
    try:
        return select(
            ring,
            day=plan.day,
            anchor=context.root.channel.grid.anchor,
            slots_per_day=max(1, assigned.of_same_show),
            slot_in_day=assigned.slot_in_day,
            cumulative=context.play_counts.plays_before(
                assigned.show_id, context.root.channel.grid.anchor
            ),
        )
    except NoEpisodesError:
        return None


def _template_for(assigned: AssignedSlot, context: PlanContext):
    """Resolve which break structure a slot uses.

    Order matters: a template named on the *daypart* is an explicit statement
    about this block of the schedule and wins; otherwise the show's slot family
    picks the default. Previously the daypart's choice was validated at load time
    and then never read, so a custom break length could not take effect at all.
    """
    root = context.root
    explicit = assigned.slot.template
    if explicit and explicit in root.continuity:
        return root.continuity[explicit]
    show_id = assigned.show_id
    family = root.shows[show_id].slot if show_id in root.shows else assigned.slot.family
    name = f"default-{family}"
    if name not in root.continuity:
        name = f"default-{assigned.slot.family}"
    return root.continuity[name]


def _extras_roll(day: date, slot_index: int, chance: float, pod_index: int = 0) -> bool:
    """Whether one break is replaced by something from ``extras``.

    Seeded from ``(day, slot index, pod index)`` -- every break's own identity.
    Deliberately *not* from the template or the show: the same wall-clock break
    should always make the same decision, so editing a template cannot silently
    reshuffle which breaks are special, and a rebuild reproduces the day exactly.
    """
    if chance <= 0:
        return False
    if chance >= 1:
        return True
    return rng_for("extras", day.isoformat(), slot_index, pod_index).random() < chance


def _roll_extras(
    day: date,
    slot_index: int,
    specs: Sequence[SegmentSpec],
    *,
    chance: float,
) -> list[SegmentSpec]:
    """Turn *some* commercial pods in a slot into extras blocks.

    Rolled per pod, because a template puts several pods in a slot and they are
    separate breaks: ``extras_chance`` is the chance of one *break* being
    replaced, so two pods must not share a single coin flip.

    A replaced pod keeps its own ``share``/``seconds`` and only changes what fills
    it, so the break's length -- and therefore the slot's -- is unchanged.

    Bump segments are never touched: they are continuity, not filler, and the
    positional rules depend on them staying where the template put them. Nor is a
    pod inside a ``mid:`` block, because a break that interrupts a show is a
    different moment from the ones around it -- a four-minute music video there is
    a bigger interruption than the show itself.

    Seeded from ``(day, slot, pod)``, which is every break's own identity. It
    therefore does not change when a template is edited: adding a bump to a slot
    does not reshuffle which breaks are special.
    """
    out: list[SegmentSpec] = []
    pod_index = 0
    for spec in specs:
        if spec.kind != "ad_pod":
            out.append(spec)
            continue
        if not spec.in_mid and _extras_roll(day, slot_index, chance, pod_index):
            out.append(replace(spec, kind="extra"))
        else:
            out.append(spec)
        pod_index += 1
    return out


def _bump_cap(script, context: PlanContext) -> int:
    bump_count = len(script.bumps())
    longest = max((asset.duration_ms for asset in context.bumps), default=10_000)
    return bump_count * longest


def _promo_pool(
    real_bumps: Sequence[Asset],
    generated_pool: Sequence[Asset],
    idents: Sequence[Asset],
    *,
    exclude: set[str] | None = None,
) -> list[Asset]:
    """Short assets used to round out a break.

    [adult swim] closed the last seconds of a pod with a card rather than sitting
    on black, so this deliberately draws on the whole short library, not just the
    neutral cards. Only the template's own slots are spoken for; extra cards are
    filler, and any card can be filler.

    *exclude* drops cards that have already aired inside the slot being filled.
    Filler draws on the same library as the template's own cards, so without it a
    break could end on the same card twice running -- the closing "up next" being
    "topped up" with a copy of itself.
    """
    skip = exclude or set()
    pool: list[Asset] = []
    for asset in (*idents, *generated_pool, *real_bumps):
        if asset.is_synthetic or asset.duration_ms <= 0 or asset.duration_ms > 15_000:
            continue
        if asset.key in skip:
            continue
        pool.append(asset)
    return pool


# About one frame at 29.97 fps. A stretch this small is rounding, not a hole, so the
# item may still be joined to its neighbours.
STRETCH_TOLERANCE_MS = 40


def _absorb_remainder(plan: Plan, ms: int) -> bool:
    """Stretch the last item by *ms* so the slot lands exactly. True on success.

    Only ever called for a sub-second remainder at a slot boundary, where the
    alternative is a black card one frame long. A card that short is not a card: it
    is a discontinuity at the cut, and it reads as a flash. Stretching the previous
    item by the same few milliseconds is invisible -- durations are integer
    milliseconds, so a file recorded as 5349 ms is really 5.3493 s and giving back
    1 ms corrects the rounding rather than inventing footage.

    Refuses for a synthetic item (a card cannot be stretched into a longer card
    without changing what it is) and for an episode (its length is the file's, and
    the guide would then disagree with the timeline).
    """
    if ms <= 0 or not plan.items:
        return False
    last = plan.items[-1]
    if last.kind not in ("ad", "bump", "ident", "extra", "filler"):
        return False
    plan.items[-1] = replace(
        last, end_ms=last.end_ms + ms, stretched=ms > STRETCH_TOLERANCE_MS
    )
    return True


def _has_item_after(script, spec: SegmentSpec) -> bool:
    """Whether anything at all follows *spec* inside the same slot.

    A pod followed by more material -- another episode part, or the closing bump --
    is mid-break: whatever comes next starts a few milliseconds early and absorbs
    the shortfall, so a sub-second leftover needs no card. Only the very last pod of
    a slot has nothing after it, and only that one must land exactly.
    """
    seen = False
    for candidate in script.specs:
        if candidate is spec:
            seen = True
            continue
        if seen:
            return True
    return False


def _is_last_filler(script, spec: SegmentSpec) -> bool:
    """Whether *spec* is the final filler block of the slot.

    Only that one is allowed to grow to absorb the slot's slack. Every earlier pod
    honours its booked target, so the template's structure survives.
    """
    fillers = [candidate for candidate in script.specs if candidate.kind in FILLER_KINDS]
    return bool(fillers) and fillers[-1] is spec


def _reserve_for_bumps(specs: Sequence[SegmentSpec], *, bump_reserve_ms: int) -> int:
    """Milliseconds that must be held back for bumps appearing after *specs*.

    Used to cap each pod: a pod may spend everything except the room the
    remaining bumps still need. That is what stops a 60-second pod from consuming
    the slot and leaving the trailing "up next" bump with nowhere to go.
    """
    remaining_bumps = sum(1 for spec in specs if spec.kind == "bump")
    return remaining_bumps * bump_reserve_ms


def _average_bump_ms(bumps: Sequence[Asset]) -> int:
    durations = [asset.duration_ms for asset in bumps if asset.duration_ms > 0]
    if not durations:
        return 4_500
    # Only short cards are used for breaks; a long-form oddity would inflate this.
    short = [value for value in durations if value <= 15_000] or durations
    return round(sum(short) / len(short))


def _show_title(root: RootConfig, show_id: str | None) -> str | None:
    """Display title for a show id, falling back to a humanised slug."""
    if not show_id:
        return None
    show = root.shows.get(show_id)
    if show is not None:
        return show.title
    return show_id.replace("-", " ").title()


def _pod_target_ms(spec: SegmentSpec, targets: dict[int, int], remaining: int) -> int:
    if spec.seconds:
        return spec.seconds * 1000
    if spec.pod:
        try:
            return int(spec.pod) * 1000
        except ValueError:
            pass
    # Default: largest pod target that fits.
    for seconds in sorted(targets, reverse=True):
        if seconds * 1000 <= remaining:
            return seconds * 1000
    return 0


def _append(plan: Plan, item: PlannedItem) -> None:
    plan.items.append(item)


def describe_plan(plan: Plan, context: PlanContext) -> Iterator[str]:
    """Human-readable timeline, used by ``nostalgia plan --show``."""
    zone_cfg = context.root.channel.grid
    for item in plan.items:
        clock = format_wall(item.start_ms, zone_cfg)[11:19]
        length = item.duration_ms / 1000
        yield f"{clock}  {length:6.1f}s  {item.kind:<8} {item.title}" + (
            f"  ({item.note})" if item.note else ""
        )


__all__ = [
    "Plan",
    "PlanContext",
    "PlanError",
    "PlannedItem",
    "build_plan",
    "describe_plan",
    "describe_script",
    "slot_key",
]
