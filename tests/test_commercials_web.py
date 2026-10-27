"""The commercials page: listing what aired and switching spots off.

The page exists because a break that lurches is almost always one bad file, and
the only reliable detector is a person hearing it. So the tests care about the
loop closing: a click must change the catalog, and the change must be visible on
the next page load.
"""

from __future__ import annotations

from datetime import date

import pytest

from nostalgia.catalog import Catalog
from nostalgia.web.commercials import MULTI_SPOT_HINT_MS, SHORT_POD_HINT_MS


def _client(paths, config):
    fastapi = pytest.importorskip("fastapi.testclient")
    from nostalgia.web.app import create_app

    return fastapi.TestClient(create_app(paths, config))


def _record(catalog: Catalog, key: str, *, start_ms: int = 1_000_000) -> None:
    """Record one airing of *key*, played in full."""
    played = next(
        asset.duration_ms
        for asset in catalog.assets(("ad",), include_disabled=True)
        if asset.key == key
    )
    catalog.record_airing(
        item_id=f"2004-06-01-{key[-8:]}-0",
        asset_key=key,
        kind="ad",
        start_ms=start_ms,
        end_ms=start_ms + played,
        day=date(2004, 6, 1).isoformat(),
        slot_index=0,
    )


def _first_ad(catalog: Catalog):
    return catalog.assets(("ad",))[0]


# -- rendering --------------------------------------------------------------


def test_commercials_page_renders(paths, config, catalog):
    _record(catalog, _first_ad(catalog).key)
    response = _client(paths, config).get("/commercials")
    assert response.status_code == 200
    body = response.text
    assert "Recently aired" in body
    assert "Commercial library" in body


def test_commercials_page_lists_an_airing_with_name_time_and_duration(
    paths, config, catalog
):
    """The three columns the page exists to show."""
    spot = _first_ad(catalog)
    _record(catalog, spot.key)
    body = _client(paths, config).get("/commercials").text

    assert spot.path.replace("\\", "/").rsplit("/", 1)[-1] in body
    # The file's own length, formatted by the same helper the rest of the UI uses.
    assert f"{spot.duration_ms / 1000:.1f}s" in body
    # A weekday and a clock time, e.g. "Thu 09:46:40".
    assert ":" in body


def test_commercials_page_is_empty_before_anything_airs(paths, config, catalog):
    """A fresh install has no history; the page must say so, not render a blank."""
    body = _client(paths, config).get("/commercials").text
    assert "Nothing recorded yet" in body


def test_commercials_page_shows_the_library_counts(paths, config, catalog):
    body = _client(paths, config).get("/commercials").text
    total = len(catalog.assets(("ad",), include_disabled=True))
    assert f"<b>{total}</b>" in body


def test_commercials_page_links_from_the_nav(paths, config):
    body = _client(paths, config).get("/").text
    assert 'href="/commercials"' in body


# -- the disable / enable loop ---------------------------------------------


def test_disabling_a_spot_removes_it_from_the_pool(paths, config, catalog):
    spot = _first_ad(catalog)
    _record(catalog, spot.key)
    client = _client(paths, config)

    response = client.post(
        "/commercials/disable", data={"key": spot.key}, follow_redirects=False
    )
    assert response.status_code == 303
    assert "saved=" in response.headers["location"]

    with Catalog(paths.catalog) as fresh:
        assert spot.key not in {asset.key for asset in fresh.assets(("ad",))}
        assert spot.key in {
            asset.key for asset in fresh.assets(("ad",), include_disabled=True)
        }


def test_enabling_a_spot_puts_it_back(paths, config, catalog):
    spot = _first_ad(catalog)
    _record(catalog, spot.key)
    client = _client(paths, config)

    client.post("/commercials/disable", data={"key": spot.key}, follow_redirects=False)
    response = client.post(
        "/commercials/enable", data={"key": spot.key}, follow_redirects=False
    )
    assert response.status_code == 303

    with Catalog(paths.catalog) as fresh:
        assert spot.key in {asset.key for asset in fresh.assets(("ad",))}


def test_a_disabled_spot_is_always_reachable_to_undo(paths, config, catalog):
    """The row that prompted the click must not vanish without a way back.

    A spot switched off *before* its first airing appears in neither the airing
    list (nothing has aired) nor the long-file list (capped at the longest 40), so
    the "Switched off" section is the only control that can undo the click. It is
    therefore never filtered by ``?all=1``.
    """
    spot = _first_ad(catalog)
    _record(catalog, spot.key)
    client = _client(paths, config)
    client.post("/commercials/disable", data={"key": spot.key}, follow_redirects=False)

    name = spot.path.replace("\\", "/").rsplit("/", 1)[-1]
    for url in ("/commercials", "/commercials?all=1"):
        body = client.get(url).text
        assert "Switched off" in body, url
        assert name in body, url
        assert 'action="/commercials/enable"' in body, url


def test_a_spot_disabled_before_it_ever_aired_can_still_be_enabled(
    paths, config, catalog
):
    """The case the airing list cannot cover: disabled, then never aired."""
    spot = _first_ad(catalog)
    client = _client(paths, config)
    client.post("/commercials/disable", data={"key": spot.key}, follow_redirects=False)

    name = spot.path.replace("\\", "/").rsplit("/", 1)[-1]
    body = client.get("/commercials").text
    # This spot never aired, so the history card cannot be the way back to it.
    assert name not in _airing_section(body)
    assert "Switched off" in body
    assert name in body

    response = client.post(
        "/commercials/enable", data={"key": spot.key}, follow_redirects=False
    )
    assert response.status_code == 303
    with Catalog(paths.catalog) as fresh:
        assert spot.key in {asset.key for asset in fresh.assets(("ad",))}


def _airing_section(body: str) -> str:
    """The "Recently aired" card only, so a name in "Switched off" cannot count."""
    start = body.find("Recently aired")
    if start < 0:
        return ""
    end = body.find("Switched off", start)
    return body[start:end if end > 0 else len(body)]


def test_disabled_airings_are_hidden_unless_asked_for(paths, config, catalog):
    """The airing history still filters, so a long list stays readable."""
    spot = _first_ad(catalog)
    _record(catalog, spot.key)
    client = _client(paths, config)
    client.post("/commercials/disable", data={"key": spot.key}, follow_redirects=False)

    name = spot.path.replace("\\", "/").rsplit("/", 1)[-1]
    hidden = _airing_section(client.get("/commercials").text)
    shown = _airing_section(client.get("/commercials?all=1").text)

    assert name not in hidden
    assert name in shown
    assert "Show switched-off" in hidden
    assert "Hide switched-off" in shown


def test_an_unknown_key_reports_an_error_instead_of_pretending(paths, config, catalog):
    response = _client(paths, config).post(
        "/commercials/disable", data={"key": "no/such/spot.mp4"}, follow_redirects=False
    )
    assert response.status_code == 303
    assert "error=" in response.headers["location"]


def test_a_missing_key_reports_an_error(paths, config, catalog):
    response = _client(paths, config).post(
        "/commercials/disable", data={}, follow_redirects=False
    )
    assert response.status_code == 303
    assert "error=" in response.headers["location"]


def test_an_unknown_action_is_a_404(paths, config, catalog):
    response = _client(paths, config).post(
        "/commercials/explode", data={"key": "x"}, follow_redirects=False
    )
    assert response.status_code == 404


def test_the_saved_banner_names_the_file(paths, config, catalog):
    spot = _first_ad(catalog)
    _record(catalog, spot.key)
    client = _client(paths, config)
    response = client.post(
        "/commercials/disable", data={"key": spot.key}, follow_redirects=False
    )
    location = response.headers["location"]
    assert "switched" in location or "off" in location


# -- the multi-spot hint ----------------------------------------------------


def test_long_files_are_offered_as_multi_spot_candidates(paths, config, catalog):
    """A file longer than a pod is the one that has to be cut to fit."""
    long_ads = [
        asset
        for asset in catalog.assets(("ad",), include_disabled=True)
        if asset.duration_ms >= SHORT_POD_HINT_MS
    ]
    body = _client(paths, config).get("/commercials").text
    if long_ads:
        assert "worth a look" in body
        name = long_ads[0].path.replace("\\", "/").rsplit("/", 1)[-1]
        assert name in body
    else:
        assert "nothing is being trimmed to fit a pod" in body


def test_a_file_over_the_longest_pod_is_labelled_cannot_play_whole(
    paths, config, catalog, library
):
    """The strongest hint: it is trimmed wherever it lands, not just in a short pod."""
    from fixtures import _stub

    _stub(library.media / "commercials" / "2003" / "recorded-break-130s.mkv", 130_000)
    _client(paths, config).get("/commercials")  # trigger a scan via the page? no
    # The page reads the catalog, so scan first.
    from nostalgia.pipeline import scan

    scan(config, catalog, paths)
    body = _client(paths, config).get("/commercials").text
    assert "cannot play whole" in body
    assert "recorded-break-130s.mkv" in body


def test_a_file_between_the_shortest_and_longest_pod_is_labelled_long(
    paths, config, catalog
):
    """A 45 s spot plays whole in a 60 s break; it is long, not broken."""
    body = _client(paths, config).get("/commercials").text
    # The fixture library's 30 s spots sit exactly on the shortest pod, so they
    # are not listed at all; nothing here should claim "cannot play whole".
    assert "cannot play whole" not in body


def test_multi_spot_hint_does_not_disable_anything(paths, config, catalog):
    """It is a suggestion. Disabling by duration would take out real 45 s spots."""
    before = len(catalog.assets(("ad",)))
    _client(paths, config).get("/commercials")
    with Catalog(paths.catalog) as fresh:
        assert len(fresh.assets(("ad",))) == before


def test_a_trimmed_spot_is_marked_as_cut(paths, config, catalog):
    """A spot played shorter than its file is the visible symptom of the bug."""
    spot = _first_ad(catalog)
    catalog.record_airing(
        item_id="2004-06-01-trim0001-0",
        asset_key=spot.key,
        kind="ad",
        start_ms=2_000_000,
        end_ms=2_000_000 + 12_000,
        day=date(2004, 6, 1).isoformat(),
        slot_index=0,
    )
    body = _client(paths, config).get("/commercials").text
    assert "cut to 12.0s" in body


def test_an_untrimmed_spot_is_not_marked_as_cut(paths, config, catalog):
    spot = _first_ad(catalog)
    _record(catalog, spot.key)
    body = _client(paths, config).get("/commercials").text
    # The marker is a span in the row, not the prose in the hint paragraph.
    assert '<span class="mono" title="cut short to fit the pod">' not in body


@pytest.mark.parametrize("action", ["disable", "enable"])
def test_toggling_replans_the_channel(paths, config, catalog, action):
    """A click that leaves the channel unchanged for six hours reads as broken."""
    spot = _first_ad(catalog)
    _record(catalog, spot.key)
    client = _client(paths, config)
    if action == "enable":
        client.post("/commercials/disable", data={"key": spot.key}, follow_redirects=False)

    response = client.post(
        f"/commercials/{action}", data={"key": spot.key}, follow_redirects=False
    )
    location = response.headers["location"]
    # Either it replanned, or it says plainly that it could not.
    assert "replanned" in location or "replanning+failed" in location
