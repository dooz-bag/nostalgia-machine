"""Ad pods must land on their target duration, to the millisecond."""

from __future__ import annotations

import pytest

from nostalgia.ads import BLACK_CARD_MIN_MS, PodError, fill_gap, fill_pod
from nostalgia.assets import Asset


def _ad(key: str, seconds: float) -> Asset:
    return Asset(key=key, path=f"/media/{key}.mkv", kind="ad", duration_ms=round(seconds * 1000))


@pytest.fixture()
def pool() -> list[Asset]:
    return [_ad(f"ad:30:{index}", 30) for index in range(4)] + [
        _ad(f"ad:15:{index}", 15) for index in range(4)
    ]


@pytest.mark.parametrize("target", [30_000, 60_000, 90_000, 45_000, 75_000, 120_000])
def test_exact_sum_is_exact(pool: list[Asset], target: int) -> None:
    fill = fill_pod(pool, target_ms=target, trim_slop_ms=1500)
    assert fill.exact
    assert sum(item.duration_ms for item in fill.items) == target
    assert fill.synthetic_ms == 0


def test_no_asset_is_used_twice(pool: list[Asset]) -> None:
    fill = fill_pod(pool, target_ms=120_000, trim_slop_ms=1500)
    keys = [item.asset.key for item in fill.items]
    assert len(keys) == len(set(keys))


def test_cooldown_is_respected_when_possible(pool: list[Asset]) -> None:
    cooling = {f"ad:30:{index}" for index in range(4)}
    fill = fill_pod(pool, target_ms=60_000, on_cooldown=cooling, trim_slop_ms=1500)
    assert all(item.asset.key not in cooling for item in fill.items)


def test_cooldown_is_relaxed_when_nothing_else_fits(pool: list[Asset]) -> None:
    cooling = {asset.key for asset in pool if asset.duration_ms == 15_000}
    fill = fill_pod(pool, target_ms=45_000, on_cooldown=cooling, trim_slop_ms=1500)
    assert fill.exact
    assert "relaxed" in fill.note


def test_trim_is_the_last_resort() -> None:
    # Two 45s spots can only reach 90s, so 89s needs one shortened by 1s.
    pool = [_ad("ad:a", 45), _ad("ad:b", 45)]
    fill = fill_pod(pool, target_ms=89_000, trim_slop_ms=1500)
    assert fill.exact
    assert sum(item.duration_ms for item in fill.items) == 89_000
    assert "trimmed" in fill.note


def test_unreachable_target_falls_back_to_whole_spots() -> None:
    # 88s would need a 2s cut, but the slop is 1.5s. A pod target is a booking
    # length, so the nearest whole-spot combination is used and the slot's tail
    # absorbs the remainder -- nothing is trimmed past the slop.
    pool = [_ad("ad:a", 45), _ad("ad:b", 45)]
    fill = fill_pod(pool, target_ms=88_000, trim_slop_ms=1500)
    assert not fill.exact
    assert "nearest whole spots" in fill.note
    assert all(item.out_point_ms is None for item in fill.items)
    assert abs(fill.achieved_ms - 88_000) <= 2_000


def test_a_spot_longer_than_the_pod_is_refused_not_cut() -> None:
    """The lurch this fixes: a 70 s file in a 60 s pod.

    Before ``max_overshoot_ms`` the longest candidate won and was then cut
    mid-word. Refusing it makes the pool pick two 30 s spots, which is what the
    break was booked for.
    """
    pool = [_ad("ad:long", 70), _ad("ad:a", 30), _ad("ad:b", 30)]
    fill = fill_pod(pool, target_ms=60_000, max_overshoot_ms=500)
    assert fill.exact
    assert "ad:long" not in {item.asset.key for item in fill.items}
    assert all(item.out_point_ms is None for item in fill.items)


def test_overshoot_allowance_is_honoured() -> None:
    """A spot inside the allowance is used whole rather than cut.

    The pool is chosen so the near spot is the *only* way to reach 60 s: an exact
    sum always wins first, and rightly so.
    """
    pool = [_ad("ad:near", 60.4), _ad("ad:a", 45)]
    fill = fill_pod(pool, target_ms=60_000, max_overshoot_ms=500)
    assert "ad:near" in {item.asset.key for item in fill.items}
    assert all(item.out_point_ms is None for item in fill.items)
    assert fill.achieved_ms == 60_400


def test_overshoot_allowance_is_a_ceiling_not_a_target() -> None:
    """Raising it must not make the pod overshoot by more than it allows."""
    pool = [_ad("ad:long", 70), _ad("ad:a", 30), _ad("ad:b", 30)]
    for allowance in (0, 500, 2_000):
        fill = fill_pod(pool, target_ms=60_000, max_overshoot_ms=allowance)
        assert fill.achieved_ms <= 60_000 + allowance, allowance


def test_a_pool_of_only_oversized_spots_still_fills_the_pod() -> None:
    """Refusing the overshoot must not mean failing when nothing else exists."""
    pool = [_ad("ad:long", 70)]
    fill = fill_pod(pool, target_ms=60_000, max_overshoot_ms=500)
    assert fill.items
    assert fill.achieved_ms > 0


def test_target_larger_than_the_pool_uses_everything_available() -> None:
    fill = fill_pod([_ad("ad:only", 30)], target_ms=90_000, trim_slop_ms=0)
    assert fill.achieved_ms == 30_000
    assert len(fill.items) == 1


def test_empty_pool_raises() -> None:
    with pytest.raises(PodError, match="no commercials"):
        fill_pod([], target_ms=30_000)


def test_zero_target_raises(pool: list[Asset]) -> None:
    with pytest.raises(PodError):
        fill_pod(pool, target_ms=0)


def test_gap_never_fails_and_always_lands_exactly(pool: list[Asset]) -> None:
    """A gap that must be filled lands exactly, whatever the target."""
    for target in (1_000, 2_400, 7_500, 13_333, 46_000, 137_400):
        fill = fill_gap(pool, target_ms=target, trim_slop_ms=1500, must_fill=True)
        assert fill.exact, target
        assert sum(item.duration_ms for item in fill.items) == target


def test_a_mid_break_gap_may_fall_short_rather_than_flash() -> None:
    """A sub-second gap between two spots is left short, not covered by a card."""
    pool = [_ad("ad:30", 30)]
    fill = fill_gap(pool, target_ms=1_000, trim_slop_ms=1500, must_fill=False)
    assert fill.items == ()
    assert fill.synthetic_ms == 0
    assert "short" in fill.note


def test_gap_prefers_real_spots_over_a_black_card(pool: list[Asset]) -> None:
    fill = fill_gap(pool, target_ms=60_000, trim_slop_ms=1500)
    assert fill.synthetic_ms == 0


def test_gap_uses_one_black_card_for_a_tiny_remainder(pool: list[Asset]) -> None:
    fill = fill_gap(pool, target_ms=2_400, trim_slop_ms=0)
    assert fill.exact
    assert fill.synthetic_ms == 2_400
    assert len(fill.items) == 1
    assert fill.items[0].asset.is_synthetic


def test_gap_with_no_pool_still_lands_exactly() -> None:
    fill = fill_gap([], target_ms=5_000)
    assert fill.exact
    assert fill.synthetic_ms == 5_000


def test_a_sub_second_gap_emits_no_card_at_all() -> None:
    """Regression: a 1 ms gap produced a 1 ms black card.

    The round-off set ``remaining = 0`` and then the "nothing fit" branch below it
    saw ``not items`` and emitted a card anyway, undoing the round-off one line
    later. Twenty-seven of a day's breaks carried one. An empty fill is the honest
    answer: the break runs a few ms short and the next slot starts on its own
    boundary regardless.
    """
    fill = fill_gap([_ad("ad:30", 30)], target_ms=1)
    assert fill.items == ()
    assert fill.synthetic_ms == 0
    assert fill.achieved_ms == 0
    assert "short" in fill.note


def test_a_gap_shorter_than_the_shortest_spot_emits_no_card() -> None:
    """Same case at the top of the round-off window."""
    fill = fill_gap([_ad("ad:30", 30)], target_ms=BLACK_CARD_MIN_MS)
    assert fill.items == ()
    assert fill.synthetic_ms == 0


def test_a_gap_just_over_the_round_off_still_gets_a_card() -> None:
    """The round-off must not swallow a gap that genuinely needs covering."""
    fill = fill_gap([_ad("ad:30", 30)], target_ms=BLACK_CARD_MIN_MS + 1)
    assert fill.synthetic_ms == BLACK_CARD_MIN_MS + 1
    assert fill.exact


def test_a_trailing_shortfall_extends_a_trimmed_spot() -> None:
    """A slot must land exactly, but a 1 ms card is a flash, not a card.

    A spot that was cut short still has its own tail to give back, so extending it
    by a few milliseconds is a fraction of a frame at 30 fps -- invisible -- and
    lands exactly.
    """
    # 45 + 45 cannot reach 89.999s, so one spot is trimmed and the leftover must
    # come back out of that same spot.
    pool = [_ad("ad:a", 45), _ad("ad:b", 45)]
    fill = fill_gap(pool, target_ms=89_999, trim_slop_ms=1500, must_fill=True)
    assert fill.exact
    assert fill.synthetic_ms == 0
    trimmed = [item for item in fill.items if item.out_point_ms is not None]
    assert len(trimmed) == 1


def test_a_trailing_leftover_with_no_trimmed_spot_falls_back_to_a_card() -> None:
    """Every spot is already playing its full file, so nothing can give it back."""
    fill = fill_gap([], target_ms=1, must_fill=True)
    assert fill.exact
    assert fill.synthetic_ms == 1


def test_an_extension_never_runs_past_the_file() -> None:
    """Giving back more than a spot was trimmed by would invent footage."""
    pool = [_ad("ad:a", 45), _ad("ad:b", 45)]
    fill = fill_gap(pool, target_ms=89_999, trim_slop_ms=1500, must_fill=True)
    for item in fill.items:
        assert item.duration_ms <= item.asset.duration_ms


def test_a_mid_break_leftover_emits_nothing() -> None:
    """With more items following, the next one simply starts a few ms early."""
    pool = [_ad("ad:a", 30)]
    fill = fill_gap(pool, target_ms=30_001, must_fill=False)
    assert fill.synthetic_ms == 0
    assert fill.achieved_ms == 30_000
    assert "short" in fill.note


def test_different_pods_draw_different_spots(pool: list[Asset]) -> None:
    first = fill_pod(pool, target_ms=60_000, seed_parts=("pod", 1)).items
    second = fill_pod(pool, target_ms=60_000, seed_parts=("pod", 2)).items
    assert [item.asset.key for item in first] != [item.asset.key for item in second]


def test_same_seed_is_repeatable(pool: list[Asset]) -> None:
    first = fill_pod(pool, target_ms=60_000, seed_parts=("pod", 1)).items
    second = fill_pod(pool, target_ms=60_000, seed_parts=("pod", 1)).items
    assert [item.asset.key for item in first] == [item.asset.key for item in second]
