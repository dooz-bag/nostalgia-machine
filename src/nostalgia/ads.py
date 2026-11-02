"""Ad pods.

A pod is a *target duration*, not a "fill until it looks about right". Commercial
pod lengths in the era were 30/60/90 seconds, and the difference between a 58 s and
a 60 s break is the difference between a channel that feels broadcast and one that
feels like a playlist.

Filling is exact-sum selection (subset-sum DP) over eligible spots, so the pod
lands on its target to the millisecond. Two kinds of compromise are allowed, in
strict order of preference:

1. **Shorten** a real spot, capped by ``trim_slop_ms`` -- used for the odd second
   a pod cannot sum to.
2. **Insert a black card** of the exact remaining length -- used for tail
   remainders shorter than the shortest commercial, where admitting a 2-second
   black frame is far better than chopping a 30-second spot to 2 seconds.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from typing import Iterable, Sequence

from .assets import Asset, synthetic_filler
from .rng import rng_for


@dataclass(frozen=True, slots=True)
class PodItem:
    asset: Asset
    out_point_ms: int | None = None
    """Set when the asset must be cut short to hit the pod target."""

    @property
    def duration_ms(self) -> int:
        if self.out_point_ms is not None:
            return min(self.out_point_ms, self.asset.duration_ms)
        return self.asset.duration_ms


@dataclass(frozen=True, slots=True)
class PodFill:
    items: tuple[PodItem, ...]
    target_ms: int
    achieved_ms: int
    note: str = ""
    synthetic_ms: int = 0
    """Milliseconds covered by synthetic filler rather than a real spot."""

    @property
    def exact(self) -> bool:
        return self.achieved_ms == self.target_ms


class PodError(ValueError):
    pass


#: A spot is never shaved below this, however small the leftover.
MIN_SPOT_MS = 1_000

#: A leftover below this is never covered by a black card.
#:
#: A card this short is not a card -- it is a discontinuity between two spots, and
#: it reads as a flash. At 30 fps a 900 ms card is 27 frames of black between two
#: commercials, which is not a station ident, it is a glitch. The threshold is a
#: full second because that is the shortest interval a viewer reads as *content*
#: rather than as a cut: below it, the honest options are to absorb the leftover
#: into the neighbouring item or to leave the break a few milliseconds short.
BLACK_CARD_MIN_MS = 1_000

#: Upper bound on subset-sum iterations per pod. Large enough to find exact
#: combinations for real 30/60/90 s targets, small enough that a pathological
#: pool (dozens of spots with jittery real durations) cannot stall a build.
_SEARCH_BUDGET = 250_000


def fill_pod(
    pool: Iterable[Asset],
    *,
    target_ms: int,
    on_cooldown: set[str] | None = None,
    tolerance_ms: int = 0,
    trim_slop_ms: int = 0,
    seed_parts: Sequence[object] = (),
    max_items: int = 10,
    #: Measured on a 211-spot library: 40 candidates -> 13s per day, 14 -> 3.1s,
    #: with identical variety (164 vs 165 unique spots, 13x vs 14x max repeat).
    #: A pod is 15-25 spots; searching more than ~14 candidates per single pod was
    #: buying nothing but CPU.
    max_candidates: int = 14,
    #: A single spot may not overshoot the pod target by more than this.
    #:
    #: A pod target is a booking length, and a 70-second file dropped into a
    #: 60-second pod is what makes playback lurch: it cannot fit, so it is either
    #: cut (audibly, mid-word) or it steals the budget from the pods after it and
    #: they get cut instead. Refusing the overshoot makes the pool pick two 30s
    #: spots, which is what the break was for.
    max_overshoot_ms: int = 500,
) -> PodFill:
    """Select whole spots that come as close to *target_ms* as possible.

    A pod target is a *booking* length, not a millisecond contract: real spots are
    30.02 s or 14.98 s, and a station plays them as they are. What must be exact is
    the **slot**, and the planner's tail absorbs whatever the pods leave over. So:

    1. Any combination whose total is within *tolerance_ms* of the target is
       accepted **untrimmed** -- this is the normal path.
    2. Otherwise an exact sum, if one exists.
    3. Otherwise one spot shortened by at most *trim_slop_ms*.

    ``achieved_ms`` therefore reports what will actually air, and ``exact`` is
    only true when it equals the target.
    """
    if target_ms <= 0:
        raise PodError("pod target must be positive")

    cooling = on_cooldown or set()
    ads = _ads(pool)
    if not ads:
        raise PodError("no commercials in the pool")

    # Shuffle first so different breaks draw different combinations rather than
    # always taking the same greedy first match. Deterministic per pod.
    order = list(ads)
    rng_for("pod-order", *seed_parts).shuffle(order)

    # Cap the working set. The subset-sum search is O(states x candidates), and a
    # real library easily reaches 200+ spots in one era window; searching all of
    # them per pod made a day's build take 25s. A shuffled, budgeted window keeps
    # variety (the shuffle changes per pod) while bounding the work.
    # Anything longer than target + overshoot cannot be part of this pod: it would
    # have to be cut, or it would swallow the slot.
    ceiling = target_ms + max(0, max_overshoot_ms)
    preferred_order = [asset for asset in order if asset.key not in cooling]
    cooling_order = [asset for asset in order if asset.key in cooling]
    choice_rng = rng_for("pod-choice", *seed_parts)

    # Start with a non-cooling window. If the mixed window would need a repeat,
    # widen the non-cooling search before accepting it; normal pods still use the
    # original small search window.
    preferred_windows = list(
        _candidate_windows(preferred_order, target_ms, max_candidates)
    )
    preferred_fill = (
        _within_tolerance(
            preferred_windows[0],
            target_ms=target_ms,
            tolerance_ms=tolerance_ms,
            ceiling_ms=ceiling,
            max_items=max_items,
            rng=choice_rng,
        )
        if preferred_windows
        else None
    )
    if preferred_fill is not None:
        return preferred_fill

    mixed_window = next(
        iter(_candidate_windows(order, target_ms, max_candidates)), []
    )
    mixed_fill = _within_tolerance(
        mixed_window,
        target_ms=target_ms,
        tolerance_ms=tolerance_ms,
        ceiling_ms=ceiling,
        max_items=max_items,
        rng=choice_rng,
        suffix=" (cooldown relaxed)",
        relaxed_keys=cooling,
    )
    if mixed_fill is not None and "cooldown relaxed" in mixed_fill.note:
        for candidate_window in preferred_windows[1:]:
            fill = _within_tolerance(
                candidate_window,
                target_ms=target_ms,
                tolerance_ms=tolerance_ms,
                ceiling_ms=ceiling,
                max_items=max_items,
                rng=choice_rng,
            )
            if fill is not None:
                return fill

            if trim_slop_ms > 0:
                bounded = [
                    asset
                    for asset in candidate_window
                    if asset.duration_ms <= ceiling
                ]
                trimmed = _exact_sum_trimmed(
                    bounded, target_ms, trim_slop_ms
                )
                if trimmed:
                    items = [PodItem(asset, cut) for asset, cut in trimmed]
                    trimmed_asset, trimmed_to = trimmed[-1]
                    return PodFill(
                        items=tuple(items),
                        target_ms=target_ms,
                        achieved_ms=target_ms,
                        note=f"trimmed {trimmed_asset.key} to {trimmed_to}ms",
                    )
        return mixed_fill
    if mixed_fill is not None:
        return mixed_fill

    # A trimmed preferred spot is still better than airing one inside cooldown.
    if trim_slop_ms > 0 and preferred_windows:
        bounded = [
            asset
            for asset in preferred_windows[0]
            if asset.duration_ms <= ceiling
        ]
        trimmed = _exact_sum_trimmed(bounded, target_ms, trim_slop_ms)
        if trimmed:
            items = [PodItem(asset, cut) for asset, cut in trimmed]
            trimmed_asset, trimmed_to = trimmed[-1]
            return PodFill(
                items=tuple(items),
                target_ms=target_ms,
                achieved_ms=target_ms,
                note=f"trimmed {trimmed_asset.key} to {trimmed_to}ms",
            )

    # Nothing lands on the target: use the nearest whole-spot combination rather
    # than failing. A pod target is a booking length, and real spots come in
    # 30.02 s and 14.98 s, so "77.5 s" is simply not reachable. The slot's tail
    # absorbs the difference, which is how a station actually operates.
    candidates = preferred_order or cooling_order
    closest = None
    closest_delta: int | None = None
    for candidate_window in _candidate_windows(candidates, target_ms, max_candidates):
        bounded = [asset for asset in candidate_window if asset.duration_ms <= ceiling]
        if not bounded:
            bounded = candidate_window
        fill_items = _closest_to(bounded, target_ms, max_items)
        if not fill_items:
            continue
        total = sum(asset.duration_ms for asset in fill_items)
        delta = abs(total - target_ms)
        if closest_delta is None or delta < closest_delta:
            closest = fill_items
            closest_delta = delta
        elif delta == closest_delta and choice_rng.randrange(2) == 0:
            closest = fill_items

    if not closest:
        raise PodError(f"no commercial fits a {target_ms}ms pod")
    total = sum(asset.duration_ms for asset in closest)
    return PodFill(
        items=tuple(PodItem(asset) for asset in closest),
        target_ms=target_ms,
        achieved_ms=total,
        note=f"nearest whole spots ({total - target_ms:+d}ms)",
    )


def _candidate_windows(
    order: Sequence[Asset], target_ms: int, limit: int
) -> Iterable[list[Asset]]:
    """Yield the normal candidate window and one wider fallback window."""
    if limit <= 0 or len(order) <= limit:
        if order:
            yield list(order)
        return
    yield _budgeted_window(order, target_ms, limit)
    if len(order) > limit * 2:
        yield _budgeted_window(order, target_ms, limit * 2)
    else:
        yield list(order)


def _within_tolerance(
    candidates: Sequence[Asset],
    *,
    target_ms: int,
    tolerance_ms: int,
    ceiling_ms: int,
    max_items: int,
    rng: random.Random,
    suffix: str = "",
    relaxed_keys: set[str] | None = None,
) -> PodFill | None:
    bounded = [asset for asset in candidates if asset.duration_ms <= ceiling_ms]
    if not bounded:
        return None
    reachable = _reachable_totals(
        bounded,
        ceiling_ms,
        max_items,
        alternatives=2,
        rng=rng,
        alternate_totals=range(
            max(1, target_ms - tolerance_ms),
            min(ceiling_ms, target_ms + tolerance_ms) + 1,
        ),
    )
    totals = [
        total
        for total in reachable
        if total > 0 and abs(total - target_ms) <= tolerance_ms
    ]
    if not totals:
        return None
    total = rng.choice(totals)
    options = reachable[total]
    chosen = rng.choice(options)
    selected = _to_assets(bounded, chosen)
    note_suffix = (
        suffix
        if relaxed_keys is None or any(asset.key in relaxed_keys for asset in selected)
        else ""
    )
    return PodFill(
        items=tuple(PodItem(asset) for asset in selected),
        target_ms=target_ms,
        achieved_ms=total,
        note=f"whole spots{note_suffix}",
    )


def multi_spot_candidates(
    pool: Iterable[Asset],
    *,
    longest_pod_ms: int,
    max_overshoot_ms: int = 500,
) -> list[Asset]:
    """Spots too long to ever play whole in this channel's longest pod.

    This is the only honest automatic discriminator available. Scene-cut counts
    and silence detection were both tried and neither separates a 70-second
    recorded break from a 70-second single spot: a real spot has cuts too, and a
    recorded break's seams are often *quieter* than its own audio. Duration is
    different in kind -- it is arithmetic. A file longer than the longest pod the
    channel books cannot be played whole, so it will be trimmed or it will steal
    the budget from the pods after it. Either way it needs a human to look at it.

    Deliberately a *suggestion*: this returns candidates and changes nothing. A
    real 45-second spot in a 30/60/90 channel is long, not broken, and disabling
    it automatically would quietly shrink the pool.
    """
    ceiling = longest_pod_ms + max(0, max_overshoot_ms)
    return [asset for asset in pool if asset.duration_ms > ceiling]


def fill_gap(
    pool: Iterable[Asset],
    *,
    target_ms: int,
    on_cooldown: set[str] | None = None,
    trim_slop_ms: int = 0,
    seed_parts: Sequence[object] = (),
    max_items: int = 10,
    short_pool: Iterable[Asset] = (),
    must_fill: bool = False,
) -> PodFill:
    """Fill a gap of up to *target_ms*, always succeeding.

    Used for the tail of a slot, where the remainder is whatever the template and
    the episode length left behind -- frequently shorter than one commercial, and
    almost never a whole multiple of the spots in the pool.

    *short_pool* (promos, generic bumps, idents) is drawn on once the commercials
    no longer fit. A break that comes up short is topped up with promos, which is
    exactly what a station does; a black card covers only what nothing can fill.

    *must_fill* decides what a **sub-second** leftover means, and the two answers
    are genuinely different:

    * ``True`` -- this gap runs to the end of the slot, so nothing follows to
      absorb a shortfall. A card is emitted, because the timeline must be
      contiguous and a slot cannot end early.
    * ``False`` -- more items follow inside the same break, so they simply start a
      few milliseconds earlier. Nothing is emitted: a 1 ms card *between* two spots
      is a single frame of black at a cut, which reads as a flash, and that was
      happening several times an hour.
    """
    if target_ms <= 0:
        raise PodError("gap target must be positive")

    # A gap is the leftover after pods, so it must be hit *exactly* -- there is
    # nothing after it to absorb a surplus.
    try:
        exact = fill_pod(
            pool,
            target_ms=target_ms,
            on_cooldown=on_cooldown,
            tolerance_ms=0,
            trim_slop_ms=trim_slop_ms,
            seed_parts=seed_parts,
            max_items=max_items,
        )
        if exact.exact:
            return exact
    except PodError:
        pass

    ads = _ads(pool)
    cooling = on_cooldown or set()
    rng = rng_for("gap-order", *seed_parts)

    # A gap has nothing after it to absorb a surplus, so it needs enough spots to
    # reach the target. Capping at a fixed count leaves a few seconds of black at
    # the end of every break, which is the artefact this module exists to avoid.
    filter_ads = [asset for asset in ads if asset.duration_ms > 0]
    if filter_ads:
        shortest = min(asset.duration_ms for asset in filter_ads)
        span = (target_ms + shortest - 1) // shortest + 2
        max_items = max(max_items, min(96, span))

    # Keep drawing whole spots until the gap closes. One subset-sum pass against
    # a large remainder can leave a huge hole (or, worse, a 3-minute black card),
    # so this repeats with a shrinking target and only ever gives up when a single
    # spot no longer fits -- at which point a black card covers the last few
    # seconds, which is the honest way to land on the grid boundary.
    remaining = target_ms
    items: list[PodItem] = []
    note_parts: list[str] = []
    rounds = 0
    placed: set[str] = set()

    while remaining > 0 and rounds < 32:
        rounds += 1
        fits = [asset for asset in ads if asset.duration_ms <= remaining]
        if not fits:
            break

        # Three passes, weakest last. `placed` is what stops one round re-picking
        # a spot the previous round just used: the loop re-draws from the whole
        # pool every time, so without it a single gap could air the same
        # commercial twice running, which is the most obvious "this is a
        # playlist" tell there is.
        order = (
            [a for a in fits if a.key not in cooling and a.key not in placed]
            or [a for a in fits if a.key not in placed]
            or fits
        )
        order = list(order)
        rng.shuffle(order)

        used = _closest_under(order, remaining, max_items)
        if not used:
            break

        consumed = sum(asset.duration_ms for asset in used)
        if consumed <= 0:
            break
        items.extend(PodItem(asset) for asset in used)
        placed.update(asset.key for asset in used)
        remaining -= consumed

    # A short break is topped up with promos rather than left black: a station
    # rounds a break out with a promo, it does not sit on black. Drawn repeatedly
    # with a shrinking target, because one promo almost never lands exactly.
    shorts = sorted(
        (asset for asset in short_pool if asset.duration_ms > 0),
        key=lambda asset: (asset.duration_ms, asset.key),
        reverse=True,
    )
    if remaining > 0 and shorts:
        used = 0
        taken: set[str] = set()
        while remaining > 0 and used < 24:
            # `taken` stops one promo filling two adjacent slots. Without it the
            # loop re-drew from the whole pool every round, so a break could end
            # on the same card twice running, which reads as a frozen stream. Only
            # when every remaining card is too long does it fall back.
            fits = [
                asset
                for asset in shorts
                if asset.duration_ms <= remaining
                and asset.key not in taken
                and asset.key not in placed
            ]
            if not fits:
                fits = [
                    asset
                    for asset in shorts
                    if asset.duration_ms <= remaining and asset.key not in taken
                ]
            if not fits:
                fits = [asset for asset in shorts if asset.duration_ms <= remaining]
            if not fits:
                break
            order = list(fits)
            rng.shuffle(order)
            items.append(PodItem(order[0]))
            taken.add(order[0].key)
            remaining -= order[0].duration_ms
            used += 1
        if used:
            note_parts.append(f"{used} promo(s)")

    # A few milliseconds of leftover is not worth a card. Emitting one put a
    # 6-millisecond black frame between two spots -- invisible as a card, but the
    # *discontinuity* it creates is a visible flash, and it was happening on 20 of
    # the day's breaks. A leading `_exact_sum_trimmed` pass would have taken this
    # off a real spot, so reaching here means no spot is long enough to give back
    # the leftover. It is a few hundred milliseconds: the slot's tail absorbs it,
    # and that is a far smaller artefact than a card.
    #
    # This has to be decided *before* the "nothing fit" branch below. A gap of
    # 1 ms has no spots that fit, so `not items` was true and a 1 ms card was
    # emitted anyway -- the round-off was being undone one line later. When
    # something follows, returning an empty fill is the honest answer: the next
    # item starts a few ms early and nothing flashes. When nothing follows, the
    # slot would end short and the timeline would lose its contiguity, so the card
    # stays.
    if 0 < remaining <= BLACK_CARD_MIN_MS:
        if not must_fill:
            # Something follows inside the same break and absorbs the shortfall by
            # starting a few milliseconds early. A card here would be a single
            # frame of black at a cut -- a flash -- and it buys nothing at all.
            note_parts.append(f"{remaining}ms short")
            return PodFill(
                items=tuple(items),
                target_ms=target_ms,
                achieved_ms=sum(item.duration_ms for item in items),
                note=" + ".join(note_parts),
                synthetic_ms=sum(
                    item.duration_ms for item in items if item.asset.is_synthetic
                ),
            )
        # Nothing follows: the slot must land exactly. A card this short is a
        # single frame of black at the boundary -- a flash, not a card. Giving the
        # leftover back to a spot that was trimmed earlier is invisible (it is a
        # fraction of a frame at 30 fps) and lands exactly, which is what a station
        # does when a break runs a hair short.
        #
        # If nothing was trimmed there is nothing to give back, so `remaining` is
        # deliberately left alone: the card branch below covers it. Rounding it off
        # here would leave a hole in the timeline, which is worse than the flash.
        extended = _extend_last_spot(items, remaining)
        if extended is not None:
            note_parts.append(f"{remaining}ms given back to {extended}")
            remaining = 0

    if not items or remaining > 0:
        if remaining > 0:
            items.append(PodItem(synthetic_filler(remaining / 1000)))
            note_parts.append(f"{remaining}ms black card")
        if not items:
            items.append(PodItem(synthetic_filler(target_ms / 1000)))
            note_parts = [f"{target_ms}ms black card"]
        elif not note_parts:
            note_parts.append("whole spots")

    # Report what will *actually* air, not the target. A rounded-off remainder
    # means the break is a few milliseconds short, and the planner has to be told
    # so it can put that slack in the slot's tail -- claiming `target_ms` here
    # while emitting less is how a gap appears and the timeline loses its
    # contiguity.
    achieved = sum(item.duration_ms for item in items)
    return PodFill(
        items=tuple(items),
        target_ms=target_ms,
        achieved_ms=achieved,
        note=" + ".join(note_parts) if note_parts else f"{len(items)} spot(s)",
        synthetic_ms=sum(item.duration_ms for item in items if item.asset.is_synthetic),
    )


def _extend_last_spot(items: list[PodItem], ms: int) -> str | None:
    """Give *ms* back to the last spot that was trimmed, in place.

    Used only for a sub-second shortfall at the very end of a slot, where the
    timeline must land exactly and a card would be a single frame of black. A spot
    that was cut short still has its own tail to give back, so extending it by a
    few milliseconds is invisible -- and unlike a card it does not flash.

    Refuses when no spot was trimmed (every item is already playing its full file)
    or when the extension would run past the file's own length.
    """
    for index in range(len(items) - 1, -1, -1):
        item = items[index]
        if item.asset.is_synthetic or item.out_point_ms is None:
            continue
        extended_ms = item.duration_ms + ms
        if extended_ms > item.asset.duration_ms:
            return None
        items[index] = PodItem(item.asset, extended_ms)
        return item.asset.key
    return None


def _budgeted_window(order: Sequence[Asset], target_ms: int, limit: int) -> list[Asset]:
    """A deterministic subset of *order* that can plausibly fill *target_ms*.

    Two pools are merged: short spots (≤ target, which is what actually composes a
    pod) and a slice of longer ones (needed when the target is bigger than a single
    spot). Taking a prefix of the already-shuffled order rather than sorting keeps
    each pod drawing a different set, so capping the search does not flatten the
    rotation into the same few commercials.

    Spots much longer than the whole target are dropped: they cannot contribute,
    and they are what makes the state space wide for no benefit.
    """
    if len(order) <= limit:
        return list(order)

    short = [asset for asset in order if asset.duration_ms <= target_ms]
    long = [asset for asset in order if asset.duration_ms > target_ms]
    # Long spots can still matter (a 90s pod from 60s spots), but only the first
    # few are ever useful; the rest just widen the search.
    keep_short = max(1, int(limit * 0.8))
    window = short[:keep_short] + long[: max(0, limit - keep_short)]
    return window or list(order[:limit])


#: Asset kinds that may fill a pod.
#:
#: Filler is not always a commercial. `kind: ident` segments and the extras pool
#: both hand this module a pool that holds something else, and the original
#: `kind == "ad"` test silently rejected every one of them: an idents-only pod fell
#: through every branch and produced a 60-second black card, which the planner then
#: labelled "ident". Anything that is real footage and has a duration can fill a
#: pod, so the kind is not the right test -- `is_synthetic` is.
FILLER_KINDS: frozenset[str] = frozenset({"ad", "ident", "extra"})


def _ads(pool: Iterable[Asset]) -> list[Asset]:
    return [
        asset
        for asset in pool
        if not asset.is_synthetic and asset.duration_ms > 0
    ]


def _to_assets(candidates: Sequence[Asset], indices: Iterable[int]) -> list[Asset]:
    return [candidates[index] for index in sorted(indices)]


def _exact_sum(candidates: Sequence[Asset], target: int, max_items: int) -> list[Asset] | None:
    """First subset (in *candidates* order) whose durations sum to *target*."""
    reachable = _reachable_totals(candidates, target, max_items)
    chosen = reachable.get(target)
    return None if chosen is None else _to_assets(candidates, chosen[0])


def _exact_sum_trimmed(
    candidates: Sequence[Asset],
    target: int,
    slop: int,
) -> list[tuple[Asset, int | None]] | None:
    """Exact sum where the final spot may be shortened by at most *slop* ms."""
    reachable = _reachable_totals(candidates, target - 1, 8)

    best: tuple[int, frozenset[int], int] | None = None
    for subtotal, options in reachable.items():
        for chosen in options:
            need = target - subtotal
            if need <= 0:
                continue
            for index, asset in enumerate(candidates):
                if index in chosen or asset.duration_ms < need:
                    continue
                if asset.duration_ms - need <= slop:
                    # Prefer the combination that uses the most real spots.
                    if best is None or len(chosen) > len(best[1]):
                        best = (need, chosen, index)
    if best is None:
        return None
    need, chosen, index = best
    trimmed = candidates[index]
    return [(asset, None) for asset in _to_assets(candidates, chosen)] + [(trimmed, need)]


def _closest_under(candidates: Sequence[Asset], target: int, max_items: int) -> list[Asset]:
    """The subset that gets closest to *target* without exceeding it."""
    reachable = _reachable_totals(candidates, target, max_items)
    return _to_assets(candidates, reachable[max(reachable)][0])


def _closest_to(
    candidates: Sequence[Asset],
    target: int,
    max_items: int,
) -> list[Asset] | None:
    """The subset whose total is nearest *target*, over or under.

    The search window is one spot beyond the target, and the *caller* is
    responsible for excluding spots that cannot belong to this pod at all -- see
    ``max_overshoot_ms`` in :func:`fill_pod`.
    """
    longest = max((asset.duration_ms for asset in candidates), default=0)
    limit = target + max(longest, 1)
    reachable = _reachable_totals(
        candidates,
        limit,
        max_items,
    )
    if not reachable:
        return None
    best = min(reachable, key=lambda total: (abs(total - target), total))
    return _to_assets(candidates, reachable[best][0])


def _reachable_totals(
    candidates: Sequence[Asset],
    limit: int,
    max_items: int,
    *,
    alternatives: int = 1,
    rng: random.Random | None = None,
    alternate_totals: range | None = None,
) -> dict[int, list[frozenset[int]]]:
    """Totals up to *limit*, with the indices that reach each one.

    Two things make this cheap enough to call per break:

    * **Sets of indices, not lists of assets.** ``Asset`` is a dataclass with a
      ``dict`` field, so ``asset in chosen`` is a deep dict comparison at every
      node. A real library has spots of 15.015 s, 15.021 s, 30.021 s and 60.26 s
      rather than round numbers, which multiplies the state space; membership on a
      frozenset of ints is O(1) and was the difference between seconds and minutes.

    * **A bounded number of combinations per total.** Most callers need only one
      path. Pod selection can retain a small randomized sample, so equally good
      duration totals do not always resolve to the same exact-duration spots.
    """
    alternatives = max(1, alternatives)
    reachable: dict[int, list[frozenset[int]]] = {0: [frozenset()]}
    seen: dict[int, int] = {0: 1}
    durations = [asset.duration_ms for asset in candidates]

    # A hard budget, in "states grown". The loop is O(states x candidates), so
    # without a cap a 6-minute tail over 52 spots is tens of millions of
    # iterations -- which is exactly what took minutes before. Exceeding the
    # budget just means the answer is approximate, and the caller's tolerance and
    # the slot's tail already absorb that.
    budget = _SEARCH_BUDGET
    work = 0

    for _round in range(max_items):
        grew = False
        snapshot = [
            (subtotal, tuple(options))
            for subtotal, options in sorted(reachable.items())
        ]
        for subtotal, options in snapshot:
            for chosen in options:
                for index in range(len(candidates)):
                    work += 1
                    if work > budget:
                        return reachable
                    if index in chosen:
                        continue
                    total = subtotal + durations[index]
                    if total > limit:
                        continue
                    option = chosen | {index}
                    choices = reachable.get(total)
                    if choices is None:
                        reachable[total] = [option]
                        seen[total] = 1
                        grew = True
                        continue
                    if option in choices:
                        continue
                    seen[total] = seen.get(total, 0) + 1
                    capacity = (
                        alternatives
                        if alternate_totals is None or total in alternate_totals
                        else 1
                    )
                    if len(choices) < capacity:
                        choices.append(option)
                        grew = True
                    elif rng is not None and capacity > 1:
                        # Reservoir sampling retains a varied, bounded sample of
                        # equally good combinations for this duration total.
                        replacement = rng.randrange(seen[total])
                        if replacement < capacity:
                            choices[replacement] = option
                            grew = True
        if not grew:
            break
    return reachable


def dedupe(items: Iterable[Asset]) -> list[Asset]:
    seen: set[str] = set()
    out: list[Asset] = []
    for asset in items:
        if asset.key in seen:
            continue
        seen.add(asset.key)
        out.append(asset)
    return out


__all__ = [
    "PodError",
    "PodFill",
    "PodItem",
    "dedupe",
    "fill_gap",
    "fill_pod",
    "multi_spot_candidates",
]
