"""Pod filling must stay fast on a *realistic* library.

Regression: the subset-sum search compared ``Asset`` dataclasses for membership,
and ``Asset`` carries a ``dict`` field, so every candidate check was a deep dict
comparison. With round demo durations (exactly 15.000 s, 30.000 s) that was fine;
with a real library full of 15.015 s / 15.021 s / 60.26 s spots the state space
grew until a single tail pod took minutes per slot and a day's build never
finished.

The budgets here are generous enough not to be flaky and tight enough to catch a
return to that behaviour.
"""

from __future__ import annotations

import time

import pytest

from nostalgia.ads import BLACK_CARD_MIN_MS, fill_gap, fill_pod
from nostalgia.assets import Asset


def _ad(key: str, ms: int) -> Asset:
    return Asset(key=f"ad:{key}", path=f"/media/{key}.mp4", kind="ad", duration_ms=ms)


@pytest.fixture()
def real_pool() -> list[Asset]:
    """Durations copied from spots actually imported off archive.org breaks.

    The ragged values are the point: they are what made the brute-force search
    explode.
    """
    durations = [
        14114, 15015, 15021, 15021, 15021, 15033, 15098, 15349, 15499, 15516,
        29763, 30013, 30021, 30021, 30021, 30063, 30083, 30200, 30864, 31045,
        31045, 38583, 44349, 57360, 58301, 59026, 59359, 59626, 59993, 60093,
        60160, 60260, 61897,
    ]
    pool = [_ad(f"r{index}", ms) for index, ms in enumerate(durations)]
    # Pad to a realistic pool size so the state space is genuinely large.
    pool += [_ad(f"p{index}", ms) for index, ms in enumerate(durations)]
    return pool


@pytest.mark.parametrize("target", [60_000, 120_000, 200_000, 300_000, 400_000])
def test_gap_filling_is_fast_on_a_ragged_pool(real_pool, target: int) -> None:
    started = time.perf_counter()
    fill = fill_gap(real_pool, target_ms=target)
    elapsed = time.perf_counter() - started
    # A gap may fall a few hundred milliseconds short rather than emit a
    # sub-second black card -- see BLACK_CARD_MIN_MS. It may never *overrun*:
    # there is nothing after it to absorb a surplus.
    assert target - BLACK_CARD_MIN_MS <= fill.achieved_ms <= target
    assert elapsed < 3.0, f"fill_gap({target}) took {elapsed:.2f}s"


@pytest.mark.parametrize("target", [30_000, 60_000, 90_000, 300_000])
def test_pod_filling_is_fast_on_a_ragged_pool(real_pool, target: int) -> None:
    started = time.perf_counter()
    fill_pod(real_pool, target_ms=target, tolerance_ms=250)
    elapsed = time.perf_counter() - started
    assert elapsed < 3.0, f"fill_pod({target}) took {elapsed:.2f}s"


def test_round_trip_fill_still_sums_exactly(real_pool) -> None:
    """The speed-up must not have cost correctness on ordinary targets."""
    for target in (30_000, 60_000, 90_000, 120_000):
        fill = fill_gap(real_pool, target_ms=target)
        assert abs(fill.achieved_ms - target) <= BLACK_CARD_MIN_MS, target
        assert sum(item.duration_ms for item in fill.items) == fill.achieved_ms


def test_a_gap_never_emits_a_sub_second_black_card(real_pool) -> None:
    """Regression: 20 of a day's breaks contained a 0.0 s card.

    A card of a few milliseconds is not a card. It renders as a single frame of
    black between two spots, and the discontinuity it introduces reads as a flash
    -- worse than the few hundred milliseconds of unfilled tail it replaced.
    """
    for target in (30_000, 45_500, 60_000, 75_250, 90_000, 120_000):
        fill = fill_gap(real_pool, target_ms=target)
        for item in fill.items:
            if not item.asset.is_synthetic:
                continue
            assert item.duration_ms > BLACK_CARD_MIN_MS, (target, item.duration_ms)


def test_a_pod_refuses_a_spot_that_cannot_fit_it(real_pool) -> None:
    """A 70-second file in a 60-second pod is the lurch, not the fix.

    Previously the longest candidate won and was then cut mid-word, or stole the
    budget from the pods after it.
    """
    fill = fill_pod(real_pool, target_ms=60_000, max_overshoot_ms=500)
    assert sum(item.duration_ms for item in fill.items) <= 60_500
    assert all(item.out_point_ms is None for item in fill.items)


def test_no_spot_is_used_twice(real_pool) -> None:
    fill = fill_gap(real_pool, target_ms=180_000)
    keys = [item.asset.key for item in fill.items if not item.asset.is_synthetic]
    assert len(keys) == len(set(keys))
