"""The web UI must not rebuild a day's plan on every request.

Building a plan takes seconds against a real library. Doing it per request made
the page a six-second wait and made the container healthcheck time out faster
than a rebuild completes, so the service reported itself unhealthy while working
perfectly well.
"""

from __future__ import annotations

from datetime import date

from nostalgia.web import view


def test_the_same_day_is_only_planned_once(config, catalog, paths) -> None:
    view._PLAN_CACHE.clear()
    day = date(2003, 6, 16)

    first = view.plan_for_day(config, catalog, paths, day)
    second = view.plan_for_day(config, catalog, paths, day)

    # Identity, not equality: a rebuild would produce an equal-but-new object.
    assert second is first


def test_a_changed_config_replans(config, catalog, paths) -> None:
    """Saving a schedule from the web page must take effect immediately."""
    view._PLAN_CACHE.clear()
    day = date(2003, 6, 16)

    first = view.plan_for_day(config, catalog, paths, day)
    changed = config.model_copy(update={"media_root": "media-elsewhere"})
    second = view.plan_for_day(changed, catalog, paths, day)

    assert second is not first


def test_now_playing_serves_a_cached_plan(config, catalog, paths) -> None:
    from datetime import datetime
    from zoneinfo import ZoneInfo

    view._PLAN_CACHE.clear()
    zone = ZoneInfo(config.channel.grid.timezone)
    moment = datetime(2003, 6, 16, 20, 0, tzinfo=zone)

    first = view.now_playing(config, catalog, paths, now=moment)
    second = view.now_playing(config, catalog, paths, now=moment)

    assert first == second
    assert len(view._PLAN_CACHE) == 1, "the cache should hold one day, not grow"


def test_playback_telemetry_does_not_invalidate_the_plan(config, catalog, paths) -> None:
    """Telemetry writes the catalog file constantly; that must not cost a rebuild.

    The cache key used to be the catalog file's mtime. Playback telemetry writes
    ``playback_events`` every few seconds, so the key changed on every request and
    the cache never hit -- every page load rebuilt the whole day, ~19s instead of
    instant. The key now reads only the tables a plan is built from.
    """
    view._PLAN_CACHE.clear()
    day = date(2003, 6, 16)

    first = view.plan_for_day(config, catalog, paths, day)

    catalog.connect().execute(
        "INSERT INTO playback_events (channel, item_id, asset_key, kind, "
        "scheduled_start_ms, first_requested_ms, last_requested_ms) "
        "VALUES ('ch', 'item', 'asset', 'episode', 1, 1, 1)"
    )
    catalog.connect().commit()

    second = view.plan_for_day(config, catalog, paths, day)
    assert second is first, "a telemetry write must not throw the plan away"


def test_a_library_change_invalidates_the_plan(config, catalog, paths) -> None:
    """Disabling a spot changes what airs, so the cached plan must be dropped."""
    view._PLAN_CACHE.clear()
    day = date(2003, 6, 16)

    first = view.plan_for_day(config, catalog, paths, day)

    catalog.connect().execute(
        "UPDATE media SET disabled = 1 WHERE key = (SELECT key FROM media LIMIT 1)"
    )
    catalog.connect().commit()

    second = view.plan_for_day(config, catalog, paths, day)
    assert second is not first, "a library change must force a replan"
