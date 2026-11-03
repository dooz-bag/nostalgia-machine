"""Playback telemetry maps HLS requests to per-item observed starts."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from types import SimpleNamespace

from nostalgia.catalog import Catalog
from nostalgia.playback import (
    PlaybackStore,
    ScheduleMap,
    activate_schedule_map,
    parse_hls_segment_times,
    schedule_rows,
    write_schedule_map,
)


def test_parse_hls_segment_times_uses_program_date_time() -> None:
    playlist = """#EXTM3U
#EXT-X-PROGRAM-DATE-TIME:2026-11-03T15:00:00.000+09:00
#EXTINF:4.004,
live000100.ts
#EXTINF:3.996,
live000101.ts
"""

    times = parse_hls_segment_times(playlist)

    assert times == {
        "live000100.ts": 1_793_685_600_000,
        "live000101.ts": 1_793_685_604_004,
    }


def test_schedule_map_finds_item_for_channel_and_timestamp(tmp_path: Path) -> None:
    path = tmp_path / "rotation-map.json"
    write_schedule_map(
        path,
        [
            {
                "channel": "1",
                "item_id": "day-ep1",
                "asset_key": "show:S01E01",
                "kind": "episode",
                "start_ms": 1000,
                "end_ms": 2000,
            },
            {
                "channel": "2",
                "item_id": "other-ep1",
                "asset_key": "other:S01E01",
                "kind": "episode",
                "start_ms": 1000,
                "end_ms": 2000,
            },
        ],
    )
    schedule = ScheduleMap(path)

    assert schedule.find("1", 1500)["item_id"] == "day-ep1"
    assert schedule.find("2", 1500)["item_id"] == "other-ep1"
    assert schedule.find("1", 2000) is None


def test_schedule_rows_include_each_underlying_item() -> None:
    first = SimpleNamespace(
        item_id="day-ep1",
        asset_key="show:S01E01",
        kind="episode",
        start_ms=1000,
        end_ms=2000,
    )
    bump = SimpleNamespace(
        item_id="day-bump",
        asset_key="bump:1",
        kind="bump",
        start_ms=2000,
        end_ms=2100,
    )

    rows = schedule_rows(
        [SimpleNamespace(channel_number="1", items=[first, bump])]
    )

    # Bumps are mapped too: a segment landing on one is explained, not a mismatch.
    assert [row["item_id"] for row in rows] == ["day-ep1", "day-bump"]


def test_playback_store_counts_an_item_once_and_tracks_segment_requests(
    tmp_path: Path,
) -> None:
    path = tmp_path / "catalog.sqlite"
    with Catalog(path):
        pass
    store = PlaybackStore(path)
    item = {
        "channel": "1",
        "item_id": "day-ep1",
        "asset_key": "show:S01E01",
        "kind": "episode",
        "start_ms": 1000,
    }

    assert store.record_request(item, requested_ms=1100)
    assert not store.record_request(item, requested_ms=1200)

    report = store.airing_counts(
        "episode", since_ms=None, before_ms=2000
    )
    assert report["total_airings"] == 1
    assert report["items"][0]["airings"] == 1
    assert report["items"][0]["segment_requests"] == 2
    # "Last segment request" is the most recent one, not the first.
    assert report["items"][0]["last_aired_ms"] == 1200


def test_playback_store_does_not_double_count_a_replanned_item(
    tmp_path: Path,
) -> None:
    """A replan moves an item's start; that is the same airing, not a new one."""
    path = tmp_path / "catalog.sqlite"
    with Catalog(path):
        pass
    store = PlaybackStore(path)
    item = {
        "channel": "1",
        "item_id": "day-ep1",
        "asset_key": "show:S01E01",
        "kind": "episode",
        "start_ms": 1000,
    }

    assert store.record_request(item, requested_ms=1100)
    assert not store.record_request({**item, "start_ms": 1500}, requested_ms=1600)

    report = store.airing_counts("episode", since_ms=None, before_ms=2000)
    assert report["total_airings"] == 1
    assert report["items"][0]["segment_requests"] == 2


def test_playback_store_migrates_a_table_keyed_by_scheduled_start(
    tmp_path: Path,
) -> None:
    path = tmp_path / "catalog.sqlite"
    with Catalog(path):
        pass
    connection = sqlite3.connect(path)
    connection.execute("DROP TABLE playback_events")
    connection.execute(
        """
        CREATE TABLE playback_events (
            channel TEXT NOT NULL, item_id TEXT NOT NULL, asset_key TEXT NOT NULL,
            kind TEXT NOT NULL, scheduled_start_ms INTEGER NOT NULL,
            first_requested_ms INTEGER NOT NULL, last_requested_ms INTEGER NOT NULL,
            segment_requests INTEGER NOT NULL DEFAULT 1,
            PRIMARY KEY (channel, item_id, asset_key, scheduled_start_ms)
        )
        """
    )
    connection.executemany(
        "INSERT INTO playback_events VALUES (?,?,?,?,?,?,?,?)",
        [
            ("1", "day-ep1", "show:S01E01", "episode", 1000, 1100, 1100, 1),
            ("1", "day-ep1", "show:S01E01", "episode", 1500, 1600, 1600, 1),
            ("1", "day-ad1", "ad:one", "ad", 2000, 2100, 2100, 3),
            ("1", "day-ad2", "ad:two", "ad", 3000, 3100, 3100, 1),
        ],
    )
    # The old table carried this index, and a rename keeps it under the same name.
    connection.execute(
        "CREATE INDEX playback_events_kind_time "
        "ON playback_events(kind, first_requested_ms)"
    )
    connection.commit()
    connection.close()

    store = PlaybackStore(path)

    episodes = store.airing_counts("episode", since_ms=None, before_ms=4000)
    assert episodes["total_airings"] == 1
    assert episodes["items"][0]["segment_requests"] == 2
    # Distinct items survive the rebuild; only the duplicate airing is collapsed.
    commercials = store.airing_counts("ad", since_ms=None, before_ms=4000)
    assert commercials["total_airings"] == 2
    assert commercials["distinct_assets"] == 2


def test_playback_store_recovers_a_migration_that_failed_part_way(
    tmp_path: Path,
) -> None:
    """SQLite does not roll back DDL, so a half-done upgrade must still finish."""
    path = tmp_path / "catalog.sqlite"
    with Catalog(path):
        pass
    connection = sqlite3.connect(path)
    connection.execute("DROP TABLE playback_events")
    connection.execute(
        """
        CREATE TABLE playback_events_old (
            channel TEXT NOT NULL, item_id TEXT NOT NULL, asset_key TEXT NOT NULL,
            kind TEXT NOT NULL, scheduled_start_ms INTEGER NOT NULL,
            first_requested_ms INTEGER NOT NULL, last_requested_ms INTEGER NOT NULL,
            segment_requests INTEGER NOT NULL DEFAULT 1,
            PRIMARY KEY (channel, item_id, asset_key, scheduled_start_ms)
        )
        """
    )
    connection.executemany(
        "INSERT INTO playback_events_old VALUES (?,?,?,?,?,?,?,?)",
        [
            ("1", "day-ep1", "show:S01E01", "episode", 1000, 1100, 1100, 1),
            ("1", "day-ep1", "show:S01E01", "episode", 1500, 1600, 1600, 1),
        ],
    )
    connection.execute(
        """
        CREATE TABLE playback_events (
            channel TEXT NOT NULL, item_id TEXT NOT NULL, asset_key TEXT NOT NULL,
            kind TEXT NOT NULL, scheduled_start_ms INTEGER NOT NULL,
            first_requested_ms INTEGER NOT NULL, last_requested_ms INTEGER NOT NULL,
            segment_requests INTEGER NOT NULL DEFAULT 1,
            PRIMARY KEY (channel, item_id)
        )
        """
    )
    connection.execute(
        "INSERT INTO playback_events VALUES "
        "('1','day-ep1','show:S01E01','episode',1500,1700,1700,2)"
    )
    connection.commit()
    connection.close()

    store = PlaybackStore(path)

    report = store.airing_counts("episode", since_ms=None, before_ms=2000)
    assert report["total_airings"] == 1
    assert report["items"][0]["segment_requests"] == 4
    assert report["items"][0]["last_aired_ms"] == 1700
    with sqlite3.connect(path) as check:
        assert check.execute(
            "SELECT 1 FROM sqlite_master "
            "WHERE type='table' AND name='playback_events_old'"
        ).fetchone() is None


def test_activate_schedule_map_keeps_current_item_and_shifts_overlap(
    tmp_path: Path,
) -> None:
    live = tmp_path / "rotation-map.json"
    pending = tmp_path / "rotation-map.pending.json"
    write_schedule_map(
        live,
        [
            {
                "channel": "1",
                "item_id": "current",
                "asset_key": "show:S01E01",
                "kind": "episode",
                "start_ms": 900,
                "end_ms": 1100,
            }
        ],
    )
    write_schedule_map(
        pending,
        [
            {
                "channel": "1",
                "item_id": "next",
                "asset_key": "show:S01E02",
                "kind": "episode",
                "start_ms": 1000,
                "end_ms": 1200,
            },
            {
                "channel": "1",
                "item_id": "after",
                "asset_key": "show:S01E03",
                "kind": "episode",
                "start_ms": 1200,
                "end_ms": 1300,
            },
        ],
    )

    activate_schedule_map(live, pending, swap_ms=1000, now_ms=950)

    schedule = ScheduleMap(live)
    assert schedule.find("1", 1050)["item_id"] == "current"
    assert schedule.find("1", 1150)["item_id"] == "next"
    assert schedule.find("1", 1250)["item_id"] == "after"
    assert not pending.exists()
