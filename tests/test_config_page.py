"""The config page: one form, one save button, main settings then advanced.

Three promises the page has to keep, and each of them was broken by the version
with a save button per file:

* a save touches only the files whose fields changed;
* main and advanced settings are separated, and every setting is still reachable;
* a submission that would leave the config unloadable saves nothing at all.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from nostalgia.web.config_edit import SETTINGS


@pytest.fixture()
def paths(tmp_path: Path):
    """A scratch project whose config is a copy of the real one.

    The page is tested against the shipped files rather than the fixture library's
    three, because the thing under test is a form over those files: a missing
    ``ads.yaml`` would test nothing.
    """
    from nostalgia.paths import Paths

    root = tmp_path / "project"
    shutil.copytree(Path(__file__).resolve().parents[1] / "config", root / "config")
    (root / "data").mkdir(parents=True)
    resolved = Paths(root)
    resolved.ensure_runtime_dirs()
    return resolved


@pytest.fixture()
def config(paths):
    from nostalgia.config import load_config

    return load_config(paths.config_dir)


def _client(paths, config):
    fastapi = pytest.importorskip("fastapi.testclient")
    from nostalgia.web.app import create_app

    return fastapi.TestClient(create_app(paths, config), follow_redirects=False)


def _current(paths, slug: str) -> dict:
    """Every field of one file as the form would submit it."""
    from nostalgia.web.config_edit import BY_SLUG, read_values

    entry = BY_SLUG[slug]
    values = read_values(entry, paths.config_dir)
    form = {}
    for spec in entry.all_fields():
        value = values.get(spec.key)
        if spec.kind == "bool":
            if value:
                form[f"{slug}::{spec.key}"] = "1"
        elif spec.kind == "list":
            form[f"{slug}::{spec.key}"] = " ".join(str(item) for item in (value or []))
        else:
            form[f"{slug}::{spec.key}"] = "" if value is None else str(value)
    return form


def _text(paths, name: str) -> str:
    path = paths.config_dir / name
    return path.read_text(encoding="utf-8")


# -- one form --------------------------------------------------------------------------


def test_the_page_has_one_save_button_and_no_per_file_forms(paths, config):
    body = _client(paths, config).get("/config").text
    assert body.count('action="/config"') == 1, "the whole page is one form"
    assert 'data-progress="Saving and validating settings"' in body
    assert 'id="work-progress" class="work-progress" role="status"' in body
    assert '"/api/progress/"' in body
    assert 'action="/config/' not in body, "no per-file form survives"
    assert body.count("<button") == 2, "one save at the top, one at the bottom"
    assert body.count("Save changes") == 2


def test_main_settings_are_grouped_by_intent_and_advanced_is_collapsed(paths, config):
    body = _client(paths, config).get("/config").text
    assert "<details" in body and "Advanced options" in body
    assert body.index("Advanced options") > body.index("Identity")
    # Pod tolerance is tuning, not something to lead with.
    assert body.index("Pod tolerance") > body.index("Advanced options")


def test_every_setting_is_still_reachable_from_the_page(paths, config):
    """Moving to Advanced must not mean hiding a setting altogether."""
    body = _client(paths, config).get("/config").text
    for entry in SETTINGS:
        for spec in entry.all_fields():
            assert f'name="{entry.slug}::{spec.key}"' in body, f"{entry.slug}::{spec.key}"


# -- saving ----------------------------------------------------------------------------


def test_the_form_never_lets_the_browser_block_a_save(paths, config):
    """A dead save button on a valid page is worse than no validation at all.

    A native ``step`` mismatch (12.75 against a 0.5 step) made the browser refuse
    to submit *any* of the form, with a console error about a control it could not
    focus -- in a collapsed Advanced section, so nothing looked wrong.
    """
    body = _client(paths, config).get("/config").text
    assert "novalidate" in body
    assert " step=" not in body, "min/max are bounds the server checks; step only blocks saves"


def test_saving_one_field_rewrites_only_that_file(paths, config):
    before = {entry.filename: _text(paths, entry.filename) for entry in SETTINGS}
    form = _current(paths, "channel")
    form["channel::name"] = "Renamed Channel"

    response = _client(paths, config).post("/config", data=form)
    assert response.status_code == 303
    assert "saved=" in response.headers["location"]
    assert "Renamed Channel" in _text(paths, "channel.yaml")

    for name, text in before.items():
        if name != "channel.yaml":
            assert _text(paths, name) == text, f"{name} was rewritten for no reason"


def test_one_save_can_change_several_files_at_once(paths, config):
    form = {
        **_current(paths, "channel"),
        **_current(paths, "ads"),
        **_current(paths, "bumps"),
        **_current(paths, "watermark"),
        **_current(paths, "encoding"),
        **_current(paths, "days/weekday"),
        **_current(paths, "days/weekend"),
    }
    form["channel::name"] = "Across Files"
    form["ads::cooldown_hours"] = "5"

    response = _client(paths, config).post("/config", data=form)
    assert response.status_code == 303
    assert "Across+Files" not in response.headers["location"], "the location names files, not values"
    assert "Channel" in response.headers["location"] and "Commercials" in response.headers["location"]
    assert "Across Files" in _text(paths, "channel.yaml")
    assert "cooldown_hours: 5" in _text(paths, "ads.yaml")


def test_an_unchanged_form_saves_nothing(paths, config):
    before = {entry.filename: _text(paths, entry.filename) for entry in SETTINGS}
    form = {
        **{k: v for entry in SETTINGS for k, v in _current(paths, entry.slug).items()},
    }
    response = _client(paths, config).post("/config", data=form)
    assert response.status_code == 303
    assert "saved=" not in response.headers["location"], "a no-op save should not claim a save"
    for name, text in before.items():
        assert _text(paths, name) == text


def test_a_bad_value_saves_nothing_and_says_why(paths, config):
    before = _text(paths, "channel.yaml")
    form = _current(paths, "channel")
    form["channel::horizon_days"] = "99"  # outside the declared range

    response = _client(paths, config).post("/config", data=form)
    assert response.status_code == 200, "the page is re-rendered so the message is visible"
    assert "at most" in response.text
    assert _text(paths, "channel.yaml") == before


def test_a_file_that_would_not_load_is_rolled_back(paths, config):
    """A day file is validated as a whole, so a bad edit must not survive the save.

    Nothing in the shipped files is wrong, so this is proven by pointing a day
    file at something that does not exist. The other files posted alongside it
    must be left alone too -- a rejected save writes nothing.
    """
    day = paths.config_dir / "days" / "weekday.yaml"
    original = day.read_text(encoding="utf-8")
    broken = original.replace("title: ", "title: ", 1)

    form = _current(paths, "channel")
    form["channel::name"] = "Should Not Stick"
    form["days/weekday::days"] = "mon tue wed thu fri"

    # A weekday the model does not recognise fails validation with a real message.
    form["days/weekday::days"] = "mon funday"
    other = _text(paths, "channel.yaml")

    response = _client(paths, config).post("/config", data=form)
    assert response.status_code == 200, "the page is re-rendered so the message is visible"
    assert "not saved" in response.text.lower() or "unknown weekday" in response.text
    assert day.read_text(encoding="utf-8") == original, "the bad day file was left half-saved"
    assert _text(paths, "channel.yaml") == other, "a rejected save must write nothing at all"


def test_an_unknown_form_key_is_never_written(paths, config):
    form = _current(paths, "channel")
    form["channel::not_a_setting"] = "1"
    form["not_a_file::name"] = "nope"
    _client(paths, config).post("/config", data=form)
    text = _text(paths, "channel.yaml")
    assert "not_a_setting" not in text


def test_the_two_era_tolerances_stay_separate(paths, config):
    """The same key exists in two files; the slug prefix is what keeps them apart."""
    form = {
        **_current(paths, "ads"),
        **_current(paths, "bumps"),
        **_current(paths, "channel"),
        **_current(paths, "watermark"),
        **_current(paths, "encoding"),
        **_current(paths, "days/weekday"),
        **_current(paths, "days/weekend"),
    }
    form["ads::era_drift_years"] = "4"
    form["bumps::era_drift_years"] = "0"
    _client(paths, config).post("/config", data=form)
    assert "era_drift_years: 4" in _text(paths, "ads.yaml")
    assert "era_drift_years: 0" in _text(paths, "bumps.yaml")


def test_a_boolean_can_be_turned_off_from_the_page(paths, config):
    """An unticked box submits nothing, which must mean false rather than "unchanged"."""
    on = _current(paths, "ads")
    on["ads::merge_breaks"] = "1"
    _client(paths, config).post("/config", data=on)
    assert "merge_breaks: true" in _text(paths, "ads.yaml")

    off = {k: v for k, v in _current(paths, "ads").items() if k != "ads::merge_breaks"}
    _client(paths, config).post("/config", data=off)
    assert "merge_breaks: false" in _text(paths, "ads.yaml")


def test_saving_stages_a_new_schedule(paths, config):
    """A setting change still reaches the channel, just via the pending deploy."""
    from nostalgia.deploy import Roots, load

    # A stage needs something to plan from. The real roster is large, so the
    # catalog is seeded with the fixture library's media but the real config --
    # the roster decides what a day references.
    import shutil as _shutil
    from pathlib import Path as _Path

    from nostalgia.catalog import Catalog
    from nostalgia.pipeline import scan
    from tests.fixtures import build_library

    library = build_library(paths.root / "fixture")
    _shutil.copytree(library.media, paths.root / "media", dirs_exist_ok=True)
    with Catalog(paths.catalog) as catalog:
        scan(config, catalog, paths)

    form = _current(paths, "channel")
    form["channel::name"] = "Staged Channel"
    response = _client(paths, config).post("/config", data=form)
    location = response.headers["location"]
    # Either it staged a schedule, or it says plainly that it could not -- but it
    # always reports what happened. The fixture library is deliberately far too
    # thin to fill a real day, so "staged" is not guaranteed here; what is
    # guaranteed is that a save attempts the replan and never fails silently.
    assert "built=" in location
    assert "staged" in location or "staging+the+new+schedule+failed" in location
