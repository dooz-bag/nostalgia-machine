"""The disable/multi-spot flags and the airing history they feed.

These are the two levers the commercials page pulls, so they are tested at the
catalog layer where the page's routes end up. The page itself is covered in
``test_commercials_web.py``.
"""

from __future__ import annotations

from datetime import date

import pytest

from nostalgia.catalog import Catalog


def _ads(catalog: Catalog, **kwargs):
    return catalog.assets(("ad",), **kwargs)


def test_assets_hides_disabled_by_default(catalog: Catalog) -> None:
    """The pool is the only door into the planner, so the filter lives here."""
    ads = _ads(catalog)
    assert ads, "fixture library should contain commercials"
    victim = ads[0]

    assert catalog.set_disabled(victim.key, disabled=True) is True

    assert victim.key not in {asset.key for asset in _ads(catalog)}
    assert victim.key in {asset.key for asset in _ads(catalog, include_disabled=True)}


def test_assets_include_disabled_keeps_the_flag_visible(catalog: Catalog) -> None:
    """The history page has to show *why* a spot vanished from the pool."""
    victim = _ads(catalog)[0]
    catalog.set_disabled(victim.key, disabled=True)

    found = {asset.key: asset for asset in _ads(catalog, include_disabled=True)}
    assert found[victim.key].disabled is True


def test_disabling_is_reversible(catalog: Catalog) -> None:
    victim = _ads(catalog)[0]
    catalog.set_disabled(victim.key, disabled=True)
    assert catalog.set_disabled(victim.key, disabled=False) is True
    assert victim.key in {asset.key for asset in _ads(catalog)}


def test_set_disabled_reports_an_unknown_key(catalog: Catalog) -> None:
    """A click that reports success and changes nothing is worse than an error."""
    assert catalog.set_disabled("no/such/spot.mp4", disabled=True) is False


def test_multi_spot_flag_round_trips(catalog: Catalog) -> None:
    victim = _ads(catalog)[0]
    assert catalog.set_multi_spot(victim.key, True) is True

    found = {asset.key: asset for asset in _ads(catalog, include_disabled=True)}
    assert found[victim.key].contains_multiple_spots is True

    # None clears the flag rather than storing "unknown" as a third state.
    assert catalog.set_multi_spot(victim.key, None) is True
    found = {asset.key: asset for asset in _ads(catalog, include_disabled=True)}
    assert found[victim.key].contains_multiple_spots is None


def test_multi_spot_does_not_remove_a_spot_from_the_pool(catalog: Catalog) -> None:
    """It is a hint for the page, not a switch. Only `disabled` filters."""
    victim = _ads(catalog)[0]
    catalog.set_multi_spot(victim.key, True)
    assert victim.key in {asset.key for asset in _ads(catalog)}


def test_commercial_stats_counts_flags(catalog: Catalog) -> None:
    ads = _ads(catalog)
    before = catalog.commercial_stats()
    assert before["total"] == len(ads)
    assert before["disabled"] == 0

    catalog.set_disabled(ads[0].key, disabled=True)
    catalog.set_multi_spot(ads[1].key, True)

    after = catalog.commercial_stats()
    assert after["total"] == len(ads)
    assert after["disabled"] == 1
    assert after["multi_spot"] == 1


def test_record_airing_then_read_it_back(catalog: Catalog) -> None:
    """The page's whole data source: what aired, when, and for how long."""
    spot = _ads(catalog)[0]
    day = date(2004, 6, 1)
    catalog.record_airing(
        item_id="2004-06-01-a0000001-7",
        asset_key=spot.key,
        kind="ad",
        start_ms=1_000_000,
        end_ms=1_030_000,
        day=day.isoformat(),
        slot_index=7,
    )

    rows = catalog.commercial_airings()
    assert len(rows) == 1
    row = rows[0]
    assert row["key"] == spot.key
    assert row["aired_ms"] == 1_000_000
    assert row["played_ms"] == 30_000
    assert row["slot_index"] == 7
    assert row["name"] == "ad-01-15s.mkv"


def test_commercial_airings_is_newest_first(catalog: Catalog) -> None:
    ads = _ads(catalog)
    day = date(2004, 6, 1)
    for index, spot in enumerate(ads[:3]):
        catalog.record_airing(
            item_id=f"2004-06-01-a000000{index}-{index}",
            asset_key=spot.key,
            kind="ad",
            start_ms=1_000_000 + index * 60_000,
            end_ms=1_030_000 + index * 60_000,
            day=day.isoformat(),
            slot_index=index,
        )

    rows = catalog.commercial_airings()
    starts = [row["aired_ms"] for row in rows]
    assert starts == sorted(starts, reverse=True)


def test_commercial_airings_ignores_bumps(catalog: Catalog) -> None:
    """The page lists commercials; a bump in the list would be noise."""
    bump = catalog.assets(("bump",))[0]
    catalog.record_airing(
        item_id="2004-06-01-b0000001-1",
        asset_key=bump.key,
        kind="bump",
        start_ms=5_000,
        end_ms=8_000,
        day=date(2004, 6, 1).isoformat(),
        slot_index=1,
    )
    assert catalog.commercial_airings() == []


def test_commercial_airings_survives_a_deleted_file(catalog: Catalog) -> None:
    """A row whose file is gone still renders, with a blank path.

    Dropping it would make the history lie about what aired, which is exactly the
    case where the history matters most.
    """
    spot = _ads(catalog)[0]
    catalog.record_airing(
        item_id="2004-06-01-a0000002-2",
        asset_key=spot.key,
        kind="ad",
        start_ms=9_000,
        end_ms=39_000,
        day=date(2004, 6, 1).isoformat(),
        slot_index=2,
    )
    with catalog.transaction() as connection:
        connection.execute("DELETE FROM media WHERE key = ?", (spot.key,))

    rows = catalog.commercial_airings()
    assert len(rows) == 1
    assert rows[0]["path"] == ""
    assert rows[0]["played_ms"] == 30_000


def test_record_day_airings_replaces_the_day(catalog: Catalog) -> None:
    """A replan must not leave yesterday's stale rows behind."""
    spot = _ads(catalog)[0]
    day = date(2004, 6, 1)
    catalog.record_airing(
        item_id="2004-06-01-a0000003-3",
        asset_key=spot.key,
        kind="ad",
        start_ms=1_000,
        end_ms=2_000,
        day=day.isoformat(),
        slot_index=3,
    )
    assert len(catalog.commercial_airings()) == 1

    catalog.clear_day(day.isoformat())
    assert catalog.commercial_airings() == []


def test_clear_airings_empties_the_table(catalog: Catalog) -> None:
    spot = _ads(catalog)[0]
    catalog.record_airing(
        item_id="2004-06-01-a0000004-4",
        asset_key=spot.key,
        kind="ad",
        start_ms=1_000,
        end_ms=2_000,
        day=date(2004, 6, 1).isoformat(),
        slot_index=4,
    )
    catalog.clear_airings()
    assert catalog.commercial_airings() == []


def test_last_aired_seeds_cooldowns(catalog: Catalog) -> None:
    """Cooldowns are only meaningful for what has already aired."""
    spot = _ads(catalog)[0]
    catalog.record_airing(
        item_id="2004-06-01-a0000005-5",
        asset_key=spot.key,
        kind="ad",
        start_ms=4_242,
        end_ms=5_000,
        day=date(2004, 6, 1).isoformat(),
        slot_index=5,
    )
    assert catalog.last_aired(("ad",)).get(spot.key) == 4_242


def test_get_state_round_trips(catalog: Catalog) -> None:
    assert catalog.get_state("last_build") is None
    catalog.set_state("last_build", "2004-06-01T00:00:00")
    assert catalog.get_state("last_build") == "2004-06-01T00:00:00"
    assert catalog.get_state("missing", "fallback") == "fallback"


@pytest.mark.parametrize("limit", [1, 2, 5])
def test_commercial_airings_respects_limit(catalog: Catalog, limit: int) -> None:
    ads = _ads(catalog)
    for index, spot in enumerate(ads[:5]):
        catalog.record_airing(
            item_id=f"2004-06-01-a000000{index}-{index}",
            asset_key=spot.key,
            kind="ad",
            start_ms=1_000 + index * 1_000,
            end_ms=2_000 + index * 1_000,
            day=date(2004, 6, 1).isoformat(),
            slot_index=index,
        )
    assert len(catalog.commercial_airings(limit=limit)) == min(limit, 5)
