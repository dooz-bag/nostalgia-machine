"""Break construction -- the "traffic department".

A slot is a fixed budget. The episode is a fixed cost. Everything else has to fit
in the difference *exactly*, to the millisecond, or the channel drifts and the
:00/:30 grid collapses.

Rather than hoping the numbers line up, this module spends the budget in a fixed
priority order and lets the **tail pod absorb the remainder**. That is both how
the algorithm stays exact and how the channel stays resilient: a show whose
episodes run long simply gets a shorter tail break instead of overrunning the
next slot.

Bumps are never trimmed to fit. If a bump does not fit a slot's bump budget it is
skipped, because a bump cut off mid-word looks broken while a missing bump does
not.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Sequence

from .config import FILLER_KINDS, ContinuitySegment, ContinuityTemplate

MIN_TAIL_POD_MS = 10_000
"""Remainders smaller than this are folded into the previous break instead."""


@dataclass(frozen=True, slots=True)
class SegmentSpec:
    """A slot segment *before* an asset has been chosen."""

    kind: str
    """``episode`` | ``bump`` | ``ad_pod`` | ``ident`` | ``extra``."""

    # bump intent
    subject: str = "this"
    position: str = "ANY"
    max_seconds: float | None = None

    # pod intent
    seconds: int | None = None
    pod: str | None = None
    share: float | None = None
    """Fraction of the remaining break budget; resolved by the planner."""

    guide: bool = False

    # episode slicing
    part: int = 0
    of_parts: int = 1
    offset_ms: int = 0
    duration_ms: int = 0

    resumes: bool = False
    """True for an `INTO` that returns from a mid-episode break.

    Two `INTO` segments exist in a slot and they describe different moments: the
    one before the episode announces it, the one inside `mid:` says it is
    continuing. Only the second may carry "we now return to".
    """

    in_mid: bool = False
    """True for every segment that came from a ``mid:`` block.

    Needed because a mid-episode break is a *different* break from the ones
    around the programme: it interrupts the show rather than preceding or
    following it. The extras swap consults this -- a 4-minute music video is a
    fine replacement for the break before a show and a bad one for the break in
    the middle of it.
    """


@dataclass
class SlotScript:
    """Ordered specs for one slot, with the episode already sliced."""

    specs: list[SegmentSpec] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def bumps(self) -> list[SegmentSpec]:
        return [spec for spec in self.specs if spec.kind == "bump"]

    def reserved_ms(self) -> int:
        """Explicitly-sized segments; bumps are not included (unknown until chosen).

        ``seconds`` is *seconds*, so it is converted. Returning the raw value
        counted a 90-second pod as 90 **milliseconds**, which made every budget
        derived from it wrong by three orders of magnitude: the plan still tiled
        because the tail absorbed the error, so the only visible symptom was that
        a template's pod lengths were quietly ignored.
        """
        return (
            sum(
                spec.seconds
                for spec in self.specs
                if spec.kind in FILLER_KINDS and spec.seconds
            )
            * 1000
        )

    def has_shares(self) -> bool:
        return any(spec.share for spec in self.specs if spec.kind in FILLER_KINDS)


@dataclass(frozen=True, slots=True)
class EpisodeSplit:
    """How a single episode is divided across mid-breaks."""

    parts: tuple[tuple[int, int], ...]
    """``(offset_ms, duration_ms)`` per part."""

    @property
    def count(self) -> int:
        return len(self.parts)

    @property
    def total_ms(self) -> int:
        return sum(duration for _, duration in self.parts)


def split_episode(duration_ms: int, break_points: Sequence[float]) -> EpisodeSplit:
    """Slice an episode at percentage points, rounded to whole milliseconds.

    The slices always sum back to *duration_ms* exactly, so a mid-break never
    changes the episode's contribution to the slot budget.
    """
    if duration_ms <= 0:
        raise ValueError("episode duration must be positive")

    points = sorted(point for point in break_points if 0 < point < 100)
    boundaries = [0]
    for point in points:
        offset = round(duration_ms * point / 100)
        if boundaries[-1] < offset < duration_ms:
            boundaries.append(offset)
    boundaries.append(duration_ms)

    parts = tuple(
        (start, finish - start) for start, finish in zip(boundaries, boundaries[1:]) if finish > start
    )
    if not parts:
        return EpisodeSplit(((0, duration_ms),))
    return EpisodeSplit(parts)


def build_script(template: ContinuityTemplate, *, episode_ms: int) -> SlotScript:
    """Expand a template into ordered specs for one slot.

    ``episode`` placeholders are replaced by one spec per episode part; mid-break
    segments are interleaved between the parts in declared order.
    """
    script = SlotScript()
    break_points = [slot.after_percent for slot in template.mid]
    split = split_episode(episode_ms, break_points) if template.mid else EpisodeSplit(((0, episode_ms),))

    mid_index = 0
    for segment in template.segments:
        if segment.kind == "episode":
            for part_index, (offset, length) in enumerate(split.parts):
                if part_index > 0 and mid_index < len(template.mid):
                    script.specs.extend(
                        _specs_from(template.mid[mid_index].segments, resuming=True)
                    )
                    mid_index += 1
                script.specs.append(
                    SegmentSpec(
                        kind="episode",
                        part=part_index,
                        of_parts=split.count,
                        offset_ms=offset,
                        duration_ms=length,
                        guide=True,
                    )
                )
            continue
        script.specs.extend(_specs_from([segment]))

    if mid_index < len(template.mid):
        script.warnings.append(
            f"{template.name}: {len(template.mid) - mid_index} mid-break(s) unused because the "
            f"template has fewer episode segments"
        )
    return script


def _specs_from(
    segments: Sequence[ContinuitySegment], *, resuming: bool = False
) -> list[SegmentSpec]:
    """Expand segment config into specs.

    *resuming* marks a ``mid:`` block, whose leading ``INTO`` is the episode
    coming back rather than a new show about to start -- a distinction the bump
    matcher needs in order to choose a truthful card.
    """
    return [
        SegmentSpec(
            kind=segment.kind,
            subject=segment.subject,
            position=segment.position,
            max_seconds=segment.max_seconds,
            seconds=segment.seconds,
            pod=segment.pod,
            share=segment.share,
            guide=segment.guide,
            resumes=resuming and segment.kind == "bump" and segment.position == "INTO",
            in_mid=resuming,
        )
        for segment in segments
    ]


@dataclass(frozen=True, slots=True)
class Budget:
    """The outcome of spending one slot's budget."""

    slack_ms: int
    """Slot length minus episode length."""

    reserved_ms: int
    """Explicitly-sized segments (pods and idents)."""

    bump_budget_ms: int
    """What is left for bumps, before any actual bump is chosen."""

    tail_ms: int
    """Remainder handed to the tail pod after bumps are picked."""

    degraded: tuple[str, ...] = ()
    """Human-readable notes about segments that were dropped or shrunk."""


def plan_budget(
    script: SlotScript,
    *,
    slot_ms: int,
    episode_ms: int,
    bump_cap_ms: int,
    drop_order: Sequence[int] | None = None,
) -> Budget:
    """Spend the slot budget.

    *bump_cap_ms* is a defensive ceiling (``max bumps × longest bump``) so a
    template that asks for more bumps than the slot can hold fails at plan time
    instead of producing a negative remainder.
    """
    slack = slot_ms - episode_ms
    if slack < 0:
        raise ValueError(
            f"episode ({episode_ms}ms) is longer than its slot ({slot_ms}ms)"
        )

    reserved = script.reserved_ms()
    degraded: list[str] = []

    if reserved > slack:
        # Shrink explicit pods from the end until they fit; this keeps the grid
        # exact rather than letting the slot overflow. `SegmentSpec` is frozen, so
        # the trimmed value is *rebuilt*: assigning to `spec.seconds` raised
        # FrozenInstanceError, which made this entire branch dead code and left
        # an over-long pod to be silently shrunk nowhere.
        excess = reserved - slack
        for index in reversed(range(len(script.specs))):
            if excess <= 0:
                break
            spec = script.specs[index]
            if spec.kind not in FILLER_KINDS or not spec.seconds:
                continue
            # Cut in whole seconds: `seconds` is an int booking length, and
            # trimming it to a fraction would give the planner a pod target it
            # could never fill with whole spots.
            cut_ms = min(spec.seconds * 1000, excess)
            cut_seconds = -(-cut_ms // 1000)
            remaining = spec.seconds - cut_seconds
            script.specs[index] = replace(
                spec, seconds=remaining if remaining > 0 else None
            )
            excess -= cut_seconds * 1000
            degraded.append(f"{spec.kind} cut {cut_seconds}s to fit the slot")
        reserved = script.reserved_ms()

    bump_budget = max(0, min(slack - reserved, bump_cap_ms))
    tail = max(0, slack - reserved - bump_budget)

    if 0 < tail < MIN_TAIL_POD_MS:
        # Too small to be a real break: give it to bumps instead, which are
        # seconds long, so the remainder is absorbed rather than spawned as a
        # 3-second "commercial break".
        bump_budget += tail
        tail = 0

    return Budget(
        slack_ms=slack,
        reserved_ms=reserved,
        bump_budget_ms=bump_budget,
        tail_ms=tail,
        degraded=tuple(degraded),
    )


def describe_script(script: SlotScript) -> str:
    """One-line summary for logs and ``nostalgia plan --explain``."""
    parts = []
    for spec in script.specs:
        if spec.kind == "episode":
            parts.append(f"EP{spec.part + 1}/{spec.of_parts}")
        elif spec.kind == "bump":
            parts.append(f"BUMP[{spec.subject}:{spec.position}]")
        elif spec.kind == "ad_pod":
            target = f"{spec.seconds}s" if spec.seconds else (spec.pod or "?")
            parts.append(f"POD[{target}]")
        elif spec.kind == "extra":
            target = f"{spec.seconds}s" if spec.seconds else (spec.pod or "?")
            parts.append(f"EXTRA[{target}]")
        else:
            parts.append(spec.kind.upper())
    return " ".join(parts)


__all__ = [
    "Budget",
    "EpisodeSplit",
    "MIN_TAIL_POD_MS",
    "SegmentSpec",
    "SlotScript",
    "build_script",
    "describe_script",
    "plan_budget",
    "split_episode",
]
