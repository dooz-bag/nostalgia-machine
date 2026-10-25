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


def fill_pod(
    pool: Iterable[Asset],
    *,
    target_ms: int,
    on_cooldown: set[str] | None = None,
    tolerance_ms: int = 0,
    trim_slop_ms: int = 0,
    seed_parts: Sequence[object] = (),
    max_items: int = 10,
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

    preferred = [asset for asset in order if asset.key not in cooling]
    for candidates, suffix in ((preferred, ""), (order, " (cooldown relaxed)")):
        if not candidates:
            continue

        closest = _closest_to(candidates, target_ms, max_items)
        if closest is not None:
            total = sum(asset.duration_ms for asset in closest)
            if abs(total - target_ms) <= tolerance_ms:
                return PodFill(
                    items=tuple(PodItem(asset) for asset in closest),
                    target_ms=target_ms,
                    achieved_ms=total,
                    note=f"whole spots{suffix}",
                )

        selected = _exact_sum(candidates, target_ms, max_items)
        if selected:
            return PodFill(
                items=tuple(PodItem(asset) for asset in selected),
                target_ms=target_ms,
                achieved_ms=target_ms,
                note=f"exact{suffix}",
            )

    if trim_slop_ms > 0:
        trimmed = _exact_sum_trimmed(preferred or order, target_ms, trim_slop_ms)
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
    closest = _closest_to(preferred or order, target_ms, max_items)
    if not closest:
        raise PodError(f"no commercial fits a {target_ms}ms pod")
    total = sum(asset.duration_ms for asset in closest)
    return PodFill(
        items=tuple(PodItem(asset) for asset in closest),
        target_ms=target_ms,
        achieved_ms=total,
        note=f"nearest whole spots ({total - target_ms:+d}ms)",
    )


def fill_gap(
    pool: Iterable[Asset],
    *,
    target_ms: int,
    on_cooldown: set[str] | None = None,
    trim_slop_ms: int = 0,
    seed_parts: Sequence[object] = (),
    max_items: int = 10,
    short_pool: Iterable[Asset] = (),
) -> PodFill:
    """Fill a gap of exactly *target_ms*, always succeeding.

    Used for the tail of a slot, where the remainder is whatever the template and
    the episode length left behind -- frequently shorter than one commercial, and
    almost never a whole multiple of the spots in the pool.

    *short_pool* (promos, generic bumps, idents) is drawn on once the commercials
    no longer fit. A break that comes up short is topped up with promos, which is
    exactly what a station does; a black card covers only what nothing can fill.
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

    while remaining > 0 and rounds < 32:
        rounds += 1
        fits = [asset for asset in ads if asset.duration_ms <= remaining]
        if not fits:
            break

        order = [asset for asset in fits if asset.key not in cooling] or fits
        order = list(order)
        rng.shuffle(order)

        used = _closest_under(order, remaining, max_items)
        if not used:
            break

        consumed = sum(asset.duration_ms for asset in used)
        if consumed <= 0:
            break
        items.extend(PodItem(asset) for asset in used)
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
        while remaining > 0 and used < 24:
            fits = [asset for asset in shorts if asset.duration_ms <= remaining]
            if not fits:
                break
            order = list(fits)
            rng.shuffle(order)
            items.append(PodItem(order[0]))
            remaining -= order[0].duration_ms
            used += 1
        if used:
            note_parts.append(f"{used} promo(s)")

    if not items or remaining > 0:
        if remaining > 0:
            items.append(PodItem(synthetic_filler(remaining / 1000)))
            note_parts.append(f"{remaining}ms black card")
        if not items:
            items.append(PodItem(synthetic_filler(target_ms / 1000)))
            note_parts = [f"{target_ms}ms black card"]
        elif not note_parts:
            note_parts.append("whole spots")

    return PodFill(
        items=tuple(items),
        target_ms=target_ms,
        achieved_ms=target_ms,
        note=" + ".join(note_parts) if note_parts else f"{len(items)} spot(s)",
        synthetic_ms=remaining if remaining > 0 else 0,
    )


def _ads(pool: Iterable[Asset]) -> list[Asset]:
    return [asset for asset in pool if asset.kind == "ad" and asset.duration_ms > 0]


def _exact_sum(candidates: Sequence[Asset], target: int, max_items: int) -> list[Asset] | None:
    """First subset (in *candidates* order) whose durations sum to *target*."""
    reachable: dict[int, list[Asset]] = {0: []}
    for _round in range(max_items):
        snapshot = sorted(reachable.items())
        grew = False
        for subtotal, chosen in snapshot:
            for asset in candidates:
                if asset in chosen:
                    continue
                total = subtotal + asset.duration_ms
                if total > target or total in reachable:
                    continue
                reachable[total] = chosen + [asset]
                grew = True
                if total == target:
                    return reachable[target]
        if not grew:
            break
    return None


def _exact_sum_trimmed(
    candidates: Sequence[Asset],
    target: int,
    slop: int,
) -> list[tuple[Asset, int | None]] | None:
    """Exact sum where the final spot may be shortened by at most *slop* ms."""
    reachable: dict[int, list[Asset]] = {0: []}
    for _round in range(8):
        snapshot = sorted(reachable.items())
        grew = False
        for subtotal, chosen in snapshot:
            for asset in candidates:
                if asset in chosen:
                    continue
                total = subtotal + asset.duration_ms
                if total >= target or total in reachable:
                    continue
                reachable[total] = chosen + [asset]
                grew = True
        if not grew:
            break

    best: tuple[int, list[Asset], Asset] | None = None
    for subtotal, chosen in reachable.items():
        need = target - subtotal
        if need <= 0:
            continue
        for asset in candidates:
            if asset in chosen or asset.duration_ms < need:
                continue
            if asset.duration_ms - need <= slop:
                # Prefer the combination that uses the most real spots.
                if best is None or len(chosen) > len(best[1]):
                    best = (need, chosen, asset)
    if best is None:
        return None
    need, chosen, asset = best
    return [(item, None) for item in chosen] + [(asset, need)]


def _closest_under(candidates: Sequence[Asset], target: int, max_items: int) -> list[Asset]:
    """The subset that gets closest to *target* without exceeding it."""
    reachable = _reachable_totals(candidates, target, max_items)
    return reachable[max(reachable)]


def _closest_to(candidates: Sequence[Asset], target: int, max_items: int) -> list[Asset] | None:
    """The subset whose total is nearest *target*, over or under."""
    reachable = _reachable_totals(candidates, int(target * 1.5) + 1, max_items)
    if not reachable:
        return None
    best = min(reachable, key=lambda total: (abs(total - target), total))
    return reachable[best]


def _reachable_totals(
    candidates: Sequence[Asset], limit: int, max_items: int
) -> dict[int, list[Asset]]:
    reachable: dict[int, list[Asset]] = {0: []}
    for _round in range(max_items):
        snapshot = sorted(reachable.items())
        grew = False
        for subtotal, chosen in snapshot:
            for asset in candidates:
                if asset in chosen:
                    continue
                total = subtotal + asset.duration_ms
                if total > limit or total in reachable:
                    continue
                reachable[total] = chosen + [asset]
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
]
