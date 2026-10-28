"""The commercials page's smoothing surface: conform status and whole-source switches.

The unit that goes bad in a recorded-break library is the *recording* -- a worn
tape, a station logo in the corner -- not the individual spot, because every slice
shares the fault. So the page groups spots by source and switches the group.
"""

from __future__ import annotations

import pytest

from nostalgia.assets import Asset
from nostalgia.catalog import Catalog
from nostalgia.pipeline import scan
from nostalgia.web.commercials import (
    MIN_FAMILY_SIZE,
    ad_family,
    commercials_body,
    families,
    set_family_enabled,
)

from fixtures import _stub


def _asset(path: str) -> Asset:
    return Asset(key=f"ad:{path}", path=path, kind="ad", duration_ms=30_000)


# -- grouping spots by the recording they came from -----------------------------


@pytest.mark.parametrize(
    "path, expected",
    [
        ("/m/commercials/2000/cartoon-network-jan-18-2000-ads-013.mp4",
         "2000/cartoon-network-jan-18-2000-ads"),
        ("/m/commercials/2000/cartoon-network-jan-18-2000-ads-087.mp4",
         "2000/cartoon-network-jan-18-2000-ads"),
        ("/m/commercials/2002/vhs_commercials_fox_5_2002-05-19_54.mp4",
         "2002/vhs_commercials_fox_5_2002-05-19"),
        ("C:\\m\\commercials\\2001\\fox-classics-promo-break-004.mp4",
         "2001/fox-classics-promo-break"),
    ],
)
def test_slices_of_one_recording_share_a_family(path, expected):
    assert ad_family(_asset(path)) == expected


def test_the_same_filename_in_a_different_year_is_a_different_recording():
    one = ad_family(_asset("/m/commercials/2000/tape-001.mp4"))
    two = ad_family(_asset("/m/commercials/2001/tape-001.mp4"))
    assert one != two


def test_a_handful_of_spots_is_not_a_compilation():
    """Below the threshold, 'switch off the family' is just switching off a spot."""
    few = [_asset(f"/m/c/2000/tape-{i:03d}.mp4") for i in range(MIN_FAMILY_SIZE - 1)]
    enough = [_asset(f"/m/c/2001/tape-{i:03d}.mp4") for i in range(MIN_FAMILY_SIZE)]
    assert set(families(few + enough)) == {"2001/tape"}


# -- on the page ---------------------------------------------------------------


@pytest.fixture()
def compiled(library, config, paths, catalog):
    """Six numbered slices of one recording, catalogued."""
    for index in range(1, 7):
        _stub(library.media / "commercials" / "2003" / f"worn-tape-{index:03d}.mkv", 30_000)
    scan(config, catalog, paths)
    return catalog


def _keys(catalog: Catalog) -> list[str]:
    return [a.key for a in catalog.assets(("ad",), include_disabled=True) if "worn-tape" in a.path]


def test_the_page_lists_a_compilation_with_its_size(config, compiled):
    body = commercials_body(config, compiled)
    assert "switch off a whole source" in body
    assert "2003/worn-tape" in body
    assert "Switch all off" in body


def test_switching_a_source_off_takes_every_slice_out_of_the_pool(config, compiled):
    message = set_family_enabled(compiled, "2003/worn-tape", enabled=False)
    assert "6 spots" in message and "switched off" in message

    live = {a.key for a in compiled.assets(("ad",))}
    assert not set(_keys(compiled)) & live, "disabled spots must leave the pool at once"
    # and nothing else went with them
    assert len(live) == len(compiled.assets(("ad",), include_disabled=True)) - 6


def test_it_is_reversible_and_the_button_flips(config, compiled):
    set_family_enabled(compiled, "2003/worn-tape", enabled=False)
    body = commercials_body(config, compiled)
    assert "all off" in body and "Switch all on" in body

    set_family_enabled(compiled, "2003/worn-tape", enabled=True)
    assert set(_keys(compiled)) <= {a.key for a in compiled.assets(("ad",))}
    assert "Switch all off" in commercials_body(config, compiled)


def test_a_partly_switched_off_source_says_how_many(config, compiled):
    compiled.set_disabled(_keys(compiled)[0], True)
    body = commercials_body(config, compiled)
    assert "1 off" in body
    assert "Switch all off" in body, "something is still on, so offer to switch the rest off"


def test_an_unknown_source_is_an_error_not_a_silent_success(compiled):
    with pytest.raises(LookupError):
        set_family_enabled(compiled, "1999/never-existed", enabled=False)


def test_the_route_switches_a_source_and_replans(paths, config, compiled):
    pytest.importorskip("fastapi.testclient")
    from fastapi.testclient import TestClient

    from nostalgia.web.app import create_app

    client = TestClient(create_app(paths, config))
    response = client.post(
        "/commercials/family/disable", data={"family": "2003/worn-tape"}, follow_redirects=False
    )
    assert response.status_code == 303 and "saved=" in response.headers["location"]

    with Catalog(paths.catalog) as fresh:
        assert not set(_keys(fresh)) & {a.key for a in fresh.assets(("ad",))}


def test_the_route_rejects_a_missing_or_bad_request(paths, config, compiled):
    pytest.importorskip("fastapi.testclient")
    from fastapi.testclient import TestClient

    from nostalgia.web.app import create_app

    client = TestClient(create_app(paths, config))
    missing = client.post("/commercials/family/disable", data={}, follow_redirects=False)
    assert "error=" in missing.headers["location"]
    assert client.post("/commercials/family/explode", data={"family": "x"}).status_code == 404


# -- conform status ----------------------------------------------------------


def test_an_unconformed_library_says_so_and_how_to_fix_it(config, catalog):
    body = commercials_body(config, catalog)
    assert "Not conformed yet" in body
    assert "nostalgia conform" in body


def test_a_conformed_library_reports_progress_and_merging_state(config, catalog, tmp_path):
    ads = catalog.assets(("ad",))
    copy = tmp_path / "c.mkv"
    copy.write_bytes(b"x")
    from nostalgia.pipeline import conform_params

    signature = conform_params(config).signature()
    for asset in ads[:3]:
        catalog.set_conformed(asset.key, copy, 1000, signature)
    body = commercials_body(config, catalog)
    assert "3 of" in body and "break files conformed" in body
    assert "merging is <b>off</b>" in body
    assert "only merge where every piece is conformed" in body


def test_stale_copies_are_called_out(config, catalog, tmp_path):
    copy = tmp_path / "c.mkv"
    copy.write_bytes(b"x")
    catalog.set_conformed(catalog.assets(("ad",))[0].key, copy, 1000, "an-older-recipe")
    assert "out of date" in commercials_body(config, catalog)
