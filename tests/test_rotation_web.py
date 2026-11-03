"""The rotation report summarizes recorded episode and commercial airings."""

from __future__ import annotations

import time
from datetime import date
from pathlib import Path

import pytest

from nostalgia.catalog import Catalog
from nostalgia.playback import PlaybackStore


def _record(
    catalog: Catalog,
    *,
    key: str,
    kind: str,
    start_ms: int,
    index: int,
) -> None:
    asset = next(
        asset
        for asset in (
            catalog.episodes()
            if kind == "episode"
            else catalog.assets((kind,), include_disabled=True)
        )
        if asset.key == key
    )
    duration_ms = asset.duration_ms
    catalog.record_airing(
        item_id=f"rotation-{kind}-{index}",
        asset_key=key,
        kind=kind,
        start_ms=start_ms,
        end_ms=start_ms + duration_ms,
        day=date.today().isoformat(),
        slot_index=index,
    )


def _client(paths, config):
    fastapi = pytest.importorskip("fastapi.testclient")
    from nostalgia.web.app import create_app

    return fastapi.TestClient(create_app(paths, config))


def _record_observed(
    catalog: Catalog,
    *,
    key: str,
    kind: str,
    start_ms: int,
    index: int,
) -> None:
    PlaybackStore(catalog.path).record_request(
        {
            "channel": "1",
            "item_id": f"observed-{kind}-{index}",
            "asset_key": key,
            "kind": kind,
            "start_ms": start_ms,
        },
        requested_ms=start_ms + 2_000,
    )


def test_rotation_page_shows_episode_and_commercial_airing_counts(
    paths, config, catalog: Catalog
) -> None:
    now_ms = int(time.time() * 1000)
    episode = catalog.episodes()[0]
    commercial = catalog.assets(("ad",))[0]
    for index in range(3):
        _record(
            catalog,
            key=episode.key,
            kind="episode",
            start_ms=now_ms - (index + 1) * 60_000,
            index=index,
        )
        _record_observed(
            catalog,
            key=episode.key,
            kind="episode",
            start_ms=now_ms - (index + 1) * 60_000,
            index=index,
        )
    _record(
        catalog,
        key=episode.key,
        kind="episode",
        start_ms=now_ms + 60 * 60 * 1000,
        index=3,
    )
    _record(
        catalog,
        key=commercial.key,
        kind="ad",
        start_ms=now_ms - 30_000,
        index=10,
    )
    _record_observed(
        catalog,
        key=commercial.key,
        kind="ad",
        start_ms=now_ms - 30_000,
        index=10,
    )

    response = _client(paths, config).get("/rotation?period=7d")

    assert response.status_code == 200
    assert "Rotation report" in response.text
    assert "Episodes" in response.text
    assert "Commercials" in response.text
    assert episode.title in response.text
    assert Path(commercial.path).name in response.text
    assert "3</td>" in response.text
    assert "does not prove a" in response.text
    assert "Scheduled" in response.text


def test_rotation_page_filters_airings_by_selected_period(
    paths, config, catalog: Catalog
) -> None:
    now_ms = int(time.time() * 1000)
    episode = catalog.episodes()[0]
    _record(
        catalog,
        key=episode.key,
        kind="episode",
        start_ms=now_ms - 2 * 24 * 60 * 60 * 1000,
        index=1,
    )
    _record_observed(
        catalog,
        key=episode.key,
        kind="episode",
        start_ms=now_ms - 2 * 24 * 60 * 60 * 1000,
        index=1,
    )
    _record(
        catalog,
        key=episode.key,
        kind="episode",
        start_ms=now_ms - 10 * 24 * 60 * 60 * 1000,
        index=2,
    )
    _record_observed(
        catalog,
        key=episode.key,
        kind="episode",
        start_ms=now_ms - 10 * 24 * 60 * 60 * 1000,
        index=2,
    )

    client = _client(paths, config)
    recent = client.get("/rotation?period=7d").text
    all_history = client.get("/rotation?period=all").text

    assert "1</td>" in recent
    assert "2</td>" in all_history


def test_rotation_page_compares_against_scheduled_counts_outside_the_top_rows(
    paths, config, catalog: Catalog
) -> None:
    """A rarely-aired item must not be compared against a misleading zero."""
    now_ms = int(time.time() * 1000)
    episodes = catalog.episodes()
    observed, unobserved = episodes[0], episodes[1]
    _record(
        catalog,
        key=observed.key,
        kind="episode",
        start_ms=now_ms - 60_000,
        index=1,
    )
    _record_observed(
        catalog,
        key=observed.key,
        kind="episode",
        start_ms=now_ms - 60_000,
        index=1,
    )
    for index in range(3):
        _record(
            catalog,
            key=unobserved.key,
            kind="episode",
            start_ms=now_ms - (index + 2) * 60_000,
            index=10 + index,
        )

    body = _client(paths, config).get("/rotation?period=7d").text

    # The observed row reports its own scheduled count (1), not the page total (4).
    row = body.split("<tbody>", 1)[1].split("</tr>", 1)[0]
    cells = [cell.strip() for cell in row.split("</td>")]
    assert cells[2].endswith(">1")  # observed starts
    assert cells[3].endswith(">1")  # scheduled airings for this item
    assert ">4<" in body  # the page total still counts every scheduled airing


def test_rotation_page_is_in_the_navigation(paths, config) -> None:
    response = _client(paths, config).get("/")

    assert response.status_code == 200
    assert 'href="/rotation"' in response.text


def test_rotation_page_rejects_an_unknown_period(paths, config) -> None:
    response = _client(paths, config).get("/rotation?period=forever")

    assert response.status_code == 400
