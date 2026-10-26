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
