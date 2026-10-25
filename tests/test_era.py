"""Commercials must respect the era window they are filed under.

``ads.era_drift_years`` was documented as governing commercials but was only ever
read for bump scoring, so a 2000 spot could air on a 2003 day. A period channel
that plays the wrong decade's ads is not the channel anyone asked for.
"""

from __future__ import annotations

from datetime import date

from nostalgia.assets import Asset
from nostalgia.plan import MIN_ERA_POOL, era_ad_pool
from nostalgia.pipeline import day_config_for
from nostalgia.plan import PlanContext, build_plan


def _ad(key: str, era: int | None) -> Asset:
    return Asset(
        key=f"ad:{key}",
        path=f"/media/commercials/{era or 'unknown'}/{key}.mp4",
        kind="ad",
        duration_ms=30_000,
        era_from=era,
        era_to=era,
    )


def test_spots_outside_the_window_are_excluded() -> None:
    pool = [_ad(f"a{index}", 2003) for index in range(20)] + [_ad("old", 2000)]
    eligible, widened = era_ad_pool(pool, 2003, drift=2)
    assert not widened
    assert all(asset.era_from == 2003 for asset in eligible)


def test_drift_widens_the_window() -> None:
    pool = [_ad(f"a{index}", 2005) for index in range(20)]

    # Drift 2 reaches 2005 from 2003, so the whole pool is in era.
    eligible, widened = era_ad_pool(pool, 2003, drift=2)
    assert len(eligible) == 20
    assert not widened

    # Drift 0 matches nothing, so the pool has to widen: a slot with no
    # commercials at all is a worse failure than a wrong-era spot.
    eligible, widened = era_ad_pool(pool, 2003, drift=0)
    assert widened
    assert len(eligible) == 20


def test_untagged_spots_stay_eligible() -> None:
    """No year folder means "unknown", not "wrong"."""
    pool = [_ad(f"a{index}", 2003) for index in range(15)] + [_ad("untagged", None)]
    eligible, _ = era_ad_pool(pool, 2003, drift=0)
    assert any(asset.era_from is None for asset in eligible)


def test_a_thin_pool_is_widened_rather_than_repeating() -> None:
    """A handful of spots on repeat sounds less like TV than the wrong era."""
    pool = [_ad("only", 2003)] + [_ad(f"b{index}", 1995) for index in range(30)]
    eligible, widened = era_ad_pool(pool, 2003, drift=0)
    assert widened
    assert len(eligible) == len(pool)
    assert len(pool) > MIN_ERA_POOL


def test_an_untagged_library_is_not_filtered() -> None:
    """Before anything is era-tagged, filtering cannot mean anything."""
    pool = [_ad(f"a{index}", None) for index in range(5)]
    eligible, widened = era_ad_pool(pool, 2003, drift=0)
    assert len(eligible) == 5
    assert not widened


def test_plan_never_airs_a_spot_from_outside_the_window(config, catalog) -> None:
    context = PlanContext.load(config, catalog)
    day = date(2003, 6, 16)
    plan = build_plan(day, day_config_for(config, day), context)

    airing = {item.asset_key for item in plan.items if item.kind == "ad"}
    by_key = {asset.key: asset for asset in context.ads}
    drift = config.ads.era_drift_years
    for key in airing:
        asset = by_key[key]
        if asset.era_from is None:
            continue
        assert asset.era_covers(day.year, drift), (
            f"{key} (era {asset.era_from}) aired on a {day.year} day "
            f"with drift {drift}"
        )
