"""``scan --detect-spots``: the one honest multi-spot discriminator.

Scene-cut counting and silence detection were both tried and rejected: neither
separates a 70-second recorded break from a 70-second single spot. Duration is
arithmetic -- a file longer than the longest pod the channel books cannot play
whole -- so it is the only signal offered. It stays advisory, because a real 45 s
spot in a 30/60/90 channel is long, not broken.
"""

from __future__ import annotations

import pytest

from nostalgia.ads import multi_spot_candidates
from nostalgia.assets import Asset
from nostalgia.catalog import Catalog
from nostalgia.config import load_config
from nostalgia.pipeline import scan


def _ad(key: str, seconds: float) -> Asset:
    return Asset(
        key=key, path=f"/media/{key}.mkv", kind="ad", duration_ms=round(seconds * 1000)
    )


# -- the pure predicate -----------------------------------------------------


def test_a_file_longer_than_the_longest_pod_is_a_candidate() -> None:
    pool = [_ad("ad:short", 30), _ad("ad:long", 120)]
    found = multi_spot_candidates(pool, longest_pod_ms=90_000)
    assert [asset.key for asset in found] == ["ad:long"]


def test_a_file_that_fits_is_not_a_candidate() -> None:
    pool = [_ad("ad:exact", 90), _ad("ad:under", 45)]
    assert multi_spot_candidates(pool, longest_pod_ms=90_000) == []


def test_the_overshoot_allowance_widens_the_ceiling() -> None:
    """A 90.4 s file plays whole in a 90 s pod when 500 ms is allowed."""
    pool = [_ad("ad:near", 90.4)]
    assert multi_spot_candidates(pool, longest_pod_ms=90_000, max_overshoot_ms=500) == []
    assert multi_spot_candidates(pool, longest_pod_ms=90_000, max_overshoot_ms=0)


def test_an_empty_pool_yields_nothing() -> None:
    assert multi_spot_candidates([], longest_pod_ms=90_000) == []


def test_candidates_are_ordered_as_given() -> None:
    """Determinism: the CLI prints these, and a reshuffle would churn the output."""
    pool = [_ad("ad:b", 120), _ad("ad:a", 100)]
    assert [a.key for a in multi_spot_candidates(pool, longest_pod_ms=90_000)] == [
        "ad:b",
        "ad:a",
    ]


# -- through a real scan ----------------------------------------------------


def _add_long_ad(library, seconds: float) -> str:
    """Drop a stub commercial into the fixture library and rescan it in.

    The stock fixtures are all 15 s and 30 s, so a multi-spot candidate has to be
    added: the whole point of the detector is a file longer than the pod.
    """
    from fixtures import _stub

    path = library.media / "commercials" / "2003" / f"recorded-break-{seconds:g}s.mkv"
    _stub(path, round(seconds * 1000))
    return path.name


def test_scan_detect_spots_flags_long_commercials(config, paths, catalog, library) -> None:
    """The flag writes the hint and reports what it found."""
    _add_long_ad(library, 130)
    scan(config, catalog, paths)

    longest = max(target.seconds * 1000 for target in config.ads.pod_targets)
    long_ads = [
        asset
        for asset in catalog.assets(("ad",), include_disabled=True)
        if asset.duration_ms > longest
    ]
    assert long_ads, "the added 130 s file should be over the longest pod"

    report = scan(config, catalog, paths, detect_spots=True)
    assert report.multi_spot == len(long_ads)
    assert set(report.multi_spot_keys) == {asset.key for asset in long_ads}

    flagged = {
        asset.key
        for asset in catalog.assets(("ad",), include_disabled=True)
        if asset.contains_multiple_spots
    }
    assert set(report.multi_spot_keys) <= flagged


def test_scan_detect_spots_disables_nothing(config, paths, catalog, library) -> None:
    """The whole point of keeping it advisory."""
    _add_long_ad(library, 130)
    scan(config, catalog, paths)
    before = {asset.key for asset in catalog.assets(("ad",))}
    scan(config, catalog, paths, detect_spots=True)
    after = {asset.key for asset in catalog.assets(("ad",))}
    assert before == after


def test_scan_without_the_flag_leaves_the_hint_alone(config, paths, catalog, library) -> None:
    """A plain scan must not quietly mark files; the hint is opt-in."""
    _add_long_ad(library, 130)
    scan(config, catalog, paths, detect_spots=False)
    flagged = [
        asset
        for asset in catalog.assets(("ad",), include_disabled=True)
        if asset.contains_multiple_spots
    ]
    assert flagged == []


def test_scan_detect_spots_is_idempotent(config, paths, catalog, library) -> None:
    _add_long_ad(library, 130)
    first = scan(config, catalog, paths, detect_spots=True)
    second = scan(config, catalog, paths, detect_spots=True)
    assert first.multi_spot == second.multi_spot
    assert first.multi_spot > 0


def test_the_ceiling_comes_from_the_configured_pods(library, paths) -> None:
    """A channel booking 120 s pods must not flag a 100 s spot."""
    (library.config / "ads.yaml").write_text(
        "ads:\n  pod_targets:\n    - seconds: 120\n      count: 1\n", encoding="utf-8"
    )
    config = load_config(library.config)
    assert max(target.seconds for target in config.ads.pod_targets) == 120

    with Catalog(paths.catalog) as instance:
        scan(config, instance, paths, detect_spots=True)
        long_ads = [
            asset
            for asset in instance.assets(("ad",), include_disabled=True)
            if asset.duration_ms > 120_500
        ]
        flagged = [
            asset
            for asset in instance.assets(("ad",), include_disabled=True)
            if asset.contains_multiple_spots
        ]
    assert len(flagged) == len(long_ads)
