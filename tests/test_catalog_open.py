"""Opening the catalog must not write to it.

``_migrate`` runs on every connection and used to end with an unconditional
``INSERT OR REPLACE`` of the schema version. That writes even when the value is
unchanged, so merely opening the catalog bumped the file's mtime. The web page
keyed its plan cache on that mtime, so the cache never hit and every page load
rebuilt the whole day's plan.
"""

from __future__ import annotations

import sqlite3

from nostalgia.catalog import SCHEMA_VERSION, Catalog


def test_opening_the_catalog_does_not_write_to_it(catalog) -> None:
    """A read-only open must issue no writes at all.

    Asserted on ``total_changes`` rather than the file's mtime: WAL defers the
    write into the ``-wal`` sidecar, so the main file's mtime can stay put even
    when a write happened. The change counter sees it either way.
    """
    path = catalog.path
    for _ in range(3):
        reopened = Catalog(path)
        reopened.connect()
        reopened.episodes()
        assert reopened.connect().total_changes == 0, "opening the catalog wrote to it"
        reopened.close()


def test_a_missing_schema_version_is_written(catalog) -> None:
    path = catalog.path
    connection = sqlite3.connect(path)
    connection.execute("DELETE FROM meta WHERE key = 'schema_version'")
    connection.commit()
    connection.close()

    with Catalog(path) as reopened:
        reopened.episodes()

    connection = sqlite3.connect(path)
    row = connection.execute(
        "SELECT value FROM meta WHERE key = 'schema_version'"
    ).fetchone()
    connection.close()
    assert row is not None and row[0] == str(SCHEMA_VERSION)


def test_a_stale_schema_version_is_bumped(catalog) -> None:
    path = catalog.path
    connection = sqlite3.connect(path)
    connection.execute("UPDATE meta SET value = '1' WHERE key = 'schema_version'")
    connection.commit()
    connection.close()

    with Catalog(path) as reopened:
        reopened.episodes()

    connection = sqlite3.connect(path)
    row = connection.execute(
        "SELECT value FROM meta WHERE key = 'schema_version'"
    ).fetchone()
    connection.close()
    assert row is not None and row[0] == str(SCHEMA_VERSION)


def test_plan_stamp_ignores_telemetry_but_sees_the_library(catalog) -> None:
    """The stamp is the plan cache key, so it must track plans, not playback."""
    base = catalog.plan_stamp()

    catalog.connect().execute(
        "INSERT INTO playback_events (channel, item_id, asset_key, kind, "
        "scheduled_start_ms, first_requested_ms, last_requested_ms) "
        "VALUES ('ch', 'item', 'asset', 'episode', 1, 1, 1)"
    )
    catalog.connect().commit()
    assert catalog.plan_stamp() == base, "playback history must not change the stamp"

    catalog.connect().execute(
        "UPDATE media SET disabled = 1 WHERE key = (SELECT key FROM media LIMIT 1)"
    )
    catalog.connect().commit()
    assert catalog.plan_stamp() != base, "a library change must change the stamp"
