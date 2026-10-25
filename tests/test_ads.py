"""Ad pods must land on their target duration, to the millisecond."""

from __future__ import annotations

import pytest

from nostalgia.ads import PodError, fill_gap, fill_pod
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
    for target in (1_000, 2_400, 7_500, 13_333, 46_000, 137_400):
        fill = fill_gap(pool, target_ms=target, trim_slop_ms=1500)
        assert fill.exact, target
        assert sum(item.duration_ms for item in fill.items) == target


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


def test_different_pods_draw_different_spots(pool: list[Asset]) -> None:
    first = fill_pod(pool, target_ms=60_000, seed_parts=("pod", 1)).items
    second = fill_pod(pool, target_ms=60_000, seed_parts=("pod", 2)).items
    assert [item.asset.key for item in first] != [item.asset.key for item in second]


def test_same_seed_is_repeatable(pool: list[Asset]) -> None:
    first = fill_pod(pool, target_ms=60_000, seed_parts=("pod", 1)).items
    second = fill_pod(pool, target_ms=60_000, seed_parts=("pod", 1)).items
    assert [item.asset.key for item in first] == [item.asset.key for item in second]
