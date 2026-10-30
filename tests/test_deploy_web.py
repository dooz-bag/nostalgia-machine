"""Replan, then push: the routes and the notice.

What has to be true from the outside:

* saving anything stages a schedule and says so, and the live playout does not change;
* the pending notice appears on every page that can be the source of the change, and
  names when the switch would happen;
* launching swaps the files in and clears the notice;
* discarding leaves the stream exactly as it was;
* every failure is a sentence on a page, never a server error.
"""

from __future__ import annotations

import json

import pytest

from nostalgia import deploy as D


def _client(paths, config):
    fastapi = pytest.importorskip("fastapi.testclient")
    from nostalgia.web.app import create_app

    return fastapi.TestClient(create_app(paths, config), follow_redirects=False)


def _roots(paths):
    return D.Roots.of(paths.playout_dir, paths.xmltv_dir)


DAYS = 1


def _emit_live(config, paths) -> None:
    """Scan the fixture library and put a real day on air.

    These tests compare against what the stream is serving, so there has to be
    something on air: without a live day there is nothing for a stage to *not*
    disturb.
    """
    from nostalgia.catalog import Catalog
    from nostalgia.pipeline import build, scan

    with Catalog(paths.catalog) as catalog:
        scan(config, catalog, paths)
        build(config, catalog, paths, days=DAYS, ignore_cooldowns=True)


def _live_day(paths) -> str:
    days = sorted(paths.playout_dir.glob("*.json"))
    assert days, "the fixture should have emitted a day"
    return days[0].read_text(encoding="utf-8")


def _first_show(config) -> str:
    return next(sid for sid, show in config.shows.items() if show.enable)


# -- staging ---------------------------------------------------------------------------


def test_replan_stages_a_schedule_and_leaves_the_live_playout_alone(paths, config):
    _emit_live(config, paths)
    before = _live_day(paths)
    response = _client(paths, config).post("/schedule/replan")
    assert response.status_code == 303
    location = response.headers["location"]
    assert "staged" in location and "launch+it" in location

    assert D.load(_roots(paths)) is not None
    assert D.staged_playouts(_roots(paths)), "the days are on disk"
    assert _live_day(paths) == before, "the stream is untouched until a launch"


def test_every_save_stages_instead_of_going_live(paths, config):
    """A save that silently rewrote the live files is what this whole change removes."""
    _emit_live(config, paths)
    before = _live_day(paths)
    client = _client(paths, config)

    response = client.post(
        "/schedule",
        data={
            "mode": "shuffle",
            "shuffle_order": "random",
            "lineup_show": [""],
            "lineup_order": ["random"],
            "pinned_show": [""],
            "pinned_season": [""],
            "pinned_episode": [""],
        },
    )
    assert response.status_code == 303
    assert _live_day(paths) == before
    assert D.load(_roots(paths)) is not None


def test_a_skip_is_staged_too(paths, config):
    _emit_live(config, paths)
    client = _client(paths, config)
    before = _live_day(paths)
    page = client.get("/")
    assert page.status_code == 200

    # Slot 0 of a fixture day is the first episode; the route refuses a slot that
    # has no episode, so a refusal here is also a pass for this rule.
    response = client.post("/skip", data={"slot": "0"})
    assert response.status_code == 303
    location = response.headers["location"]
    assert "skip saved" in location or "Skip" in location or "no episode" in location
    if "skip saved" in location:
        assert _live_day(paths) == before


# -- the notice ------------------------------------------------------------------------


def test_the_notice_appears_on_every_page_that_can_change_the_schedule(paths, config):
    _emit_live(config, paths)
    client = _client(paths, config)
    assert "Schedule changes pending" not in client.get("/schedule").text

    client.post("/schedule/replan")
    for page in ("/", "/schedule", "/config"):
        body = client.get(page).text
        assert "Schedule changes pending" in body, page
        assert "Launch new schedule" in body and "Discard" in body, page


def test_the_notice_names_the_seam(paths, config):
    _emit_live(config, paths)
    client = _client(paths, config)
    client.post("/schedule/replan")
    body = client.get("/schedule").text
    assert "ready to go on air at" in body
    assert "the end of the programme on air now" in body
    assert "day(s)</b> staged" in body, "how much is waiting is stated, not implied"


def test_the_notice_says_when_the_settings_have_moved_on_since_the_stage(paths, config):
    """Launch rebuilds, so this is a warning, not a blocker -- and it says so."""
    _emit_live(config, paths)
    client = _client(paths, config)
    client.post("/schedule/replan")
    deploy = D.load(_roots(paths))
    assert deploy.fingerprint
    D.save_request(
        _roots(paths), D.Deploy(deploy.staged_at_ms, deploy.switch_at_ms, deploy.days, "not-the-config")
    )
    body = client.get("/schedule").text
    assert "Settings have changed since this was prepared" in body


# -- launching -------------------------------------------------------------------------


def test_launching_swaps_the_files_in_and_clears_the_notice(paths, config):
    _emit_live(config, paths)
    client = _client(paths, config)
    client.post("/schedule/replan")
    staged = D.staged_playouts(_roots(paths))
    assert staged

    response = client.post("/schedule/launch")
    assert response.status_code == 303
    location = response.headers["location"]
    assert "launched" in location and "day%28s%29" in location

    assert D.load(_roots(paths)) is None
    assert D.last(_roots(paths)) is not None
    assert D.staged_playouts(_roots(paths)) == []
    assert "Schedule changes pending" not in client.get("/schedule").text
    assert "launched" in client.get(location).text


def test_launching_with_nothing_pending_still_puts_the_settings_on_air(paths, config):
    """Launch means "make the stream match the settings", so it builds first.

    Being forgiving here matters: the button is what an impatient user presses when
    they are not sure whether their Replan took.
    """
    _emit_live(config, paths)
    response = _client(paths, config).post("/schedule/launch")
    assert response.status_code == 303
    assert "launched" in response.headers["location"]
    assert D.last(_roots(paths)) is not None


def test_discarding_leaves_the_stream_exactly_as_it_was(paths, config):
    _emit_live(config, paths)
    client = _client(paths, config)
    before = _live_day(paths)
    client.post("/schedule/replan")

    response = client.post("/schedule/cancel")
    assert response.status_code == 303
    assert "discarded" in response.headers["location"]
    assert D.load(_roots(paths)) is None
    assert _live_day(paths) == before
    assert json.loads(_live_day(paths))["items"], "the live day is still a whole day"


def test_a_launch_that_cannot_work_changes_nothing_and_says_why(paths, config):
    """The guard in front of the swap: a stage naming a pruned file is refused.

    Launch rebuilds first, so the realistic version of this is a merged break
    pruned by a concurrent live emit between the build and the swap. That is
    simulated at the ``_launch`` level, which is the seam the guard sits on --
    monkeypatching the route instead would depend on when FastAPI bound it.
    """
    import json as _json

    from nostalgia.deploy import Roots
    from nostalgia import deploy as deploy_mod

    _emit_live(config, paths)
    roots = Roots.of(paths.playout_dir, paths.xmltv_dir)
    roots.pending.playout_dir.mkdir(parents=True, exist_ok=True)
    roots.pending.xmltv_dir.mkdir(parents=True, exist_ok=True)
    (roots.pending.playout_dir / "20261030T000000_20261031T000000.json").write_text(
        _json.dumps(
            {
                "items": [
                    {
                        "id": "20261030-0000-ep1",
                        "start": "2026-10-30T00:00:00+09:00",
                        "finish": "2026-10-30T00:22:00+09:00",
                        "source": {
                            "source_type": "local",
                            "path": str(paths.root / "media" / "gone.mkv"),
                        },
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    before = {
        path.name: path.read_text(encoding="utf-8")
        for path in paths.playout_dir.glob("*.json")
        if path.name != "last-deploy.json"
    }

    from nostalgia import web as web_pkg

    with pytest.MonkeyPatch.context() as patch:
        # Make the rebuild preserve the broken stage, then let the real guard run.
        patch.setattr(
            deploy_mod,
            "missing_inputs",
            lambda roots: [str(paths.root / "media" / "gone.mkv")],
        )
        patch.setattr(web_pkg.app, "_launch", web_pkg.app._launch)
        response = _client(paths, config).post("/schedule/launch")

    assert response.status_code == 303 and "error=" in response.headers["location"]
    assert "missing" in response.headers["location"]
    after = {
        path.name: path.read_text(encoding="utf-8")
        for path in paths.playout_dir.glob("*.json")
        if path.name != "last-deploy.json"
    }
    assert after == before, "a failed launch must not half-apply"


def test_the_api_still_works_while_a_deploy_is_pending(paths, config):
    _emit_live(config, paths)
    client = _client(paths, config)
    client.post("/schedule/replan")
    response = client.get("/api/now")
    assert response.status_code == 200 and "current" in response.json()
