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

from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Iterator, Sequence

from .ads import PodError, PodFill, fill_gap, fill_pod
from .assets import NEUTRAL, Asset
from .bumps import BumpRequest, choose
from .catalog import Catalog
from .config import DayConfig, RootConfig
from .cooldown import CooldownTracker
from .continuity import (
    SegmentSpec,
    build_script,
    describe_script,
    plan_budget,
)
from .grid import Slot, build_day, day_bounds_ms, format_wall
from .rotation import NoEpisodesError, RotationRing, build_rings, select
from .schedule import AssignedSlot, assign_day
from .watermark import WatermarkRule


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

    @property
    def duration_ms(self) -> int:
        return self.end_ms - self.start_ms


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
    era_file: Path | None = None
    """``config/bumps-era.yaml``, used by the generated-bump renderer."""

    generated_dir: Path | None = None
    """Where rendered cards are cached (``media/bumps/generated`` by default)."""

    @classmethod
    def load(
        cls,
        root: RootConfig,
        catalog: Catalog,
        *,
        config_dir: Path | None = None,
        media_root: Path | None = None,
    ) -> "PlanContext":
        all_assets = catalog.assets()
        bumps = [asset for asset in all_assets if asset.kind == "bump"]
        ads = [asset for asset in all_assets if asset.kind == "ad"]
        idents = [asset for asset in all_assets if asset.kind == "ident"]
        orders = {show_id: show.order for show_id, show in root.shows.items()}
        rings = build_rings(catalog.episodes(), orders, seed_salt=root.channel.name)

        era_file = (config_dir / "bumps-era.yaml") if config_dir else None
        if era_file is not None and not era_file.is_file():
            era_file = None

        generated_dir = None
        if media_root is not None:
            generated_dir = media_root / "bumps" / "generated"

        return cls(
            root=root,
            catalog=catalog,
            rings=rings,
            bumps=bumps,
            ads=ads,
            idents=idents,
            last_aired=catalog.last_aired(),
            era_file=era_file,
            generated_dir=generated_dir,
        )


def build_plan(
    day: date,
    day_config: DayConfig,
    context: PlanContext,
    *,
    ignore_cooldowns: bool = False,
    allow_generated: bool | None = None,
) -> Plan:
    """Build one day's timeline."""
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

    slots = build_day(day, day_config, channel.grid)
    schedule = assign_day(slots, day_config, root, day)
    plan.warnings.extend(schedule.warnings)

    assignments: dict[str, AssignedSlot] = {}
    for assigned in schedule.slots:
        assignments[slot_key(assigned.slot)] = assigned

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

    mark = watermark_rule(root)
    ordered = sorted(assignments.values(), key=lambda a: a.slot.start_ms)
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
        )

    if not plan.items:
        raise PlanError(f"{day}: nothing could be scheduled")

    problems = plan.validate()
    if problems:
        raise PlanError(f"{day}: timeline is invalid\n  - " + "\n  - ".join(problems))
    return plan


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
        skip_when_burned_in=False,
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
    specs = specs_for_schedule(
        shows=[show_id for show_id, show in context.root.shows.items() if show.enable],
        titles=titles,
        year=year,
        max_seconds=12,
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
        era_drift=root.ads.era_drift_years,
    )

    # -- pass 1: resolve bumps -------------------------------------------------
    bump_assets: dict[int, Asset] = {}
    bump_reasons: dict[int, str] = {}
    for index, spec in enumerate(script.specs):
        if spec.kind != "bump":
            continue
        request = BumpRequest(subject=spec.subject, position=spec.position, **request_base)
        allowed = generated_pool if generated_budget.available() else ()
        choice = choose(
            real_bumps,
            request,
            min_score=root.bumps.min_score,
            strictness=root.bumps.strictness,
            # Cooldown is evaluated per chosen card below, so a decision here does
            # not depend on where in the slot the card ends up.
            on_cooldown=bump_tracker.cooling([], slot.start_ms),
            seed_parts=(plan.day.isoformat(), slot.index, index),
            generated=allowed,
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
        if spec.kind in ("ad_pod", "ident") and spec.share
    ]
    fixed = sum(
        spec.seconds
        for spec in script.specs
        if spec.kind in ("ad_pod", "ident") and spec.seconds
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
                    watermark=bool(
                        mark is not None
                        and mark.applies_to(
                            kind="episode",
                            show_id=assigned.show_id,
                            era_year=root.era_year,
                        )
                    ),
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
                    path=asset.path,
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

        if spec.kind in ("ad_pod", "ident"):
            # `remaining` excludes the bumps that still have to air after this pod.
            reserve = bump_after_ms.get(index, 0)
            remaining = slot.end_ms - cursor_ms - reserve
            if remaining <= 0:
                continue
            if spec.share:
                target = round(shared_budget * spec.share)
            else:
                target = _pod_target_ms(spec, pod_targets, remaining)
            target = min(target, remaining)
            if target <= 0:
                continue

            if spec.kind == "ident":
                pool = context.idents or context.ads
            else:
                pool = ad_pool
            # The final real pod fills up to (but not past) the reserved bumps; any
            # sliver left over is covered afterwards, so the closing card still
            # lands last and the next slot starts on its boundary.
            if _is_last_filler(script, spec):
                target = remaining
                filler = fill_gap(
                    pool,
                    target_ms=target,
                    on_cooldown=ad_tracker.cooling([], cursor_ms),
                    trim_slop_ms=root.ads.trim_slop_ms,
                    seed_parts=(plan.day.isoformat(), slot.index, "tail", cursor_ms),
                    short_pool=_promo_pool(real_bumps, generated_pool, context.idents),
                )
            else:
                try:
                    filler = fill_pod(
                        pool,
                        target_ms=target,
                        on_cooldown=ad_tracker.cooling([], cursor_ms),
                        tolerance_ms=root.ads.pod_tolerance_ms,
                        trim_slop_ms=root.ads.trim_slop_ms,
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
            )
            emitted += 1

    if cursor_ms < slot.end_ms:
        # Trailing dead air means the template under-budgeted the break. Cover it
        # exactly rather than letting the next slot start early.
        remainder = slot.end_ms - cursor_ms
        try:
            filler = fill_gap(
                context.ads,
                target_ms=remainder,
                on_cooldown=ad_tracker.cooling([], cursor_ms),
                trim_slop_ms=root.ads.trim_slop_ms,
                seed_parts=(plan.day.isoformat(), slot.index, "trail", cursor_ms),
                short_pool=_promo_pool(real_bumps, generated_pool, context.idents),
            )
            cursor_ms = _emit_pod(
                plan,
                filler,
                cursor_ms=cursor_ms,
                slot=slot,
                suffix="t",
                tracker=ad_tracker,
                root=root,
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
) -> int:
    """Append a filled pod's items and return the new cursor.

    Items that would cross the slot boundary are dropped with a warning rather
    than allowed to overrun: a late item pushes everything after it and the
    channel loses the grid permanently.
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
        item_kind = (
            pod_item.asset.kind if pod_item.asset.kind in ("bump", "ident") else kind
        )
        subtitle = (
            "black"
            if pod_item.asset.is_synthetic
            else ("promo" if item_kind == "bump" else "commercial")
        )
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
                path=pod_item.asset.path,
                title=(
                    _show_title(root, pod_item.asset.subject)
                    or pod_item.asset.brand
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
            tracker.record(pod_item.asset.key, cursor_ms)
        cursor_ms += pod_item.duration_ms
    return cursor_ms


def _episode_for(plan: Plan, assigned: AssignedSlot, context: PlanContext):
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
        )
    except NoEpisodesError:
        return None


def _template_for(assigned: AssignedSlot, context: PlanContext):
    root = context.root
    show_id = assigned.show_id
    family = root.shows[show_id].slot if show_id in root.shows else assigned.slot.family
    name = f"default-{family}"
    if name not in root.continuity:
        name = f"default-{assigned.slot.family}"
    return root.continuity[name]


def _bump_cap(script, context: PlanContext) -> int:
    bump_count = len(script.bumps())
    longest = max((asset.duration_ms for asset in context.bumps), default=10_000)
    return bump_count * longest


def _promo_pool(
    real_bumps: Sequence[Asset],
    generated_pool: Sequence[Asset],
    idents: Sequence[Asset],
) -> list[Asset]:
    """Short assets used to round out a break.

    [adult swim] closed the last seconds of a pod with a card rather than sitting
    on black, so this deliberately draws on the whole short library, not just the
    neutral cards. Only the template's own slots are spoken for; extra cards are
    filler, and any card can be filler.
    """
    pool: list[Asset] = []
    for asset in (*idents, *generated_pool, *real_bumps):
        if asset.is_synthetic or asset.duration_ms <= 0 or asset.duration_ms > 15_000:
            continue
        pool.append(asset)
    return pool


def _is_last_filler(script, spec: SegmentSpec) -> bool:
    """Whether *spec* is the final filler block of the slot.

    Only that one is allowed to grow to absorb the slot's slack. Every earlier pod
    honours its booked target, so the template's structure survives.
    """
    fillers = [candidate for candidate in script.specs if candidate.kind in ("ad_pod", "ident")]
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
