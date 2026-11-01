"""The schedule page: rendering and the ``programming.yaml`` round-trip.

The page is the only place a user changes the schedule without editing YAML, so
the tests care about two things beyond "it renders": that saving preserves what
the page does not own, and that a rejected form does not damage the file.
"""

from __future__ import annotations

from datetime import date

import pytest

from nostalgia.config import ProgrammingConfig, load_config
from nostalgia.web.schedule import (
    ScheduleForm,
    ScheduleFormError,
    load_schedule,
    parse_form,
    write_schedule,
)


@pytest.fixture()
def programming_yaml(library):
    """The real file the page writes, pre-seeded so tests can compare before/after."""
    path = library.config / "programming.yaml"
    path.write_text("programming:\n  mode: dayparts\n", encoding="utf-8")
    return path


# -- parse_form -------------------------------------------------------------


def test_parse_form_pairs_lineup_rows():
    form = parse_form(
        mode="lineup",
        lineup_show=["futurama", "sealab-2021"],
        lineup_order=["sequential", "by_season"],
    )
    assert [(entry.show_id, entry.order) for entry in form.lineup] == [
        ("futurama", "sequential"),
        ("sealab-2021", "by_season"),
    ]


def test_parse_form_keeps_start_points_for_multiple_shows():
    form = parse_form(
        mode="lineup",
        lineup_show=["futurama", "sealab-2021"],
        lineup_order=["sequential", "random"],
        lineup_start_season=["2", "1"],
        lineup_start_episode=["3", ""],
        lineup_start_date=["2026-11-01", ""],
    )
    assert [
        (
            entry.show_id,
            entry.start_season,
            entry.start_episode,
            entry.start_date,
        )
        for entry in form.lineup
    ] == [
        ("futurama", 2, 3, date(2026, 11, 1)),
        ("sealab-2021", 1, None, None),
    ]


def test_parse_form_skips_blank_lineup_rows():
    """The page always renders one empty row for adding; it must not become an entry."""
    form = parse_form(
        mode="lineup",
        lineup_show=["futurama", ""],
        lineup_order=["random", "random"],
    )
    assert [entry.show_id for entry in form.lineup] == ["futurama"]


def test_parse_form_rejects_mismatched_lineup_columns():
    with pytest.raises(ScheduleFormError, match="mismatched"):
        parse_form(mode="lineup", lineup_show=["a", "b"], lineup_order=["random"])


def test_parse_form_rejects_episode_start_without_season():
    with pytest.raises(ScheduleFormError, match="needs a starting season"):
        parse_form(
            mode="lineup",
            lineup_show=["futurama"],
            lineup_order=["sequential"],
            lineup_start_episode=["2"],
        )


def test_parse_form_renders_pinned_keys():
    form = parse_form(
        mode="lineup",
        pinned_show=["aqua-teen-hunger-force"],
        pinned_season=["2"],
        pinned_episode=["3"],
    )
    assert form.pinned == ["aqua-teen-hunger-force:s02e003"]


def test_parse_form_skips_half_filled_pinned_rows():
    """A blank season is a typo, not a request to pin episode zero."""
    form = parse_form(
        mode="lineup",
        pinned_show=["futurama"],
        pinned_season=[""],
        pinned_episode=["4"],
    )
    assert form.pinned == []


def test_parse_form_rejects_non_numeric_pinned_code():
    with pytest.raises(ScheduleFormError, match="numeric"):
        parse_form(
            mode="lineup",
            pinned_show=["futurama"],
            pinned_season=["x"],
            pinned_episode=["1"],
        )


def test_parse_form_drops_empty_shuffle_shows():
    form = parse_form(mode="shuffle", shuffle_shows=["", "futurama", ""])
    assert form.shuffle_shows == ["futurama"]


# -- write/load round-trip --------------------------------------------------


def test_write_then_load_round_trips(programming_yaml, library):
    form = ScheduleForm(
        mode="lineup",
        lineup=[
            {
                "show_id": "futurama",
                "order": "sequential",
                "start_season": 2,
                "start_episode": 3,
                "start_date": date(2026, 11, 1),
            }
        ],
        pinned=["futurama:s01e002"],
        shuffle_shows=["futurama"],
        shuffle_order="by_season",
    )
    write_schedule(programming_yaml, form)

    config = load_config(library.config)
    assert config.programming.mode == "lineup"
    assert config.programming.lineup[0].show_id == "futurama"
    assert config.programming.lineup[0].order == "sequential"
    assert config.programming.lineup[0].start_season == 2
    assert config.programming.lineup[0].start_episode == 3
    assert config.programming.lineup[0].start_date == date(2026, 11, 1)
    assert config.programming.pinned == ["futurama:s01e002"]
    assert config.programming.shuffle_shows == ["futurama"]
    assert config.programming.shuffle_order == "by_season"


def test_write_preserves_keys_the_page_does_not_own(programming_yaml):
    """The file is meant to be hand-edited too, so a page save must not prune it."""
    programming_yaml.write_text(
        "programming:\n"
        "  mode: dayparts\n"
        "  future_setting: 42\n"
        "other_section:\n"
        "  keep: me\n",
        encoding="utf-8",
    )
    write_schedule(programming_yaml, ScheduleForm(mode="shuffle"))

    text = programming_yaml.read_text(encoding="utf-8")
    assert "future_setting: 42" in text
    assert "other_section:" in text
    assert "keep: me" in text


def test_write_produces_a_loadable_file(programming_yaml, library):
    """Every saved file must be re-loadable; otherwise the channel breaks at plan time."""
    write_schedule(
        programming_yaml,
        ScheduleForm(
            mode="lineup",
            lineup=[{"show_id": "futurama", "order": "by_season"}],
            pinned=["futurama:s01e001"],
        ),
    )
    assert load_config(library.config).programming.mode == "lineup"


def test_write_replaces_a_corrupt_file_rather_than_failing(programming_yaml, library):
    """A file the user broke by hand must not lock them out of the editor."""
    programming_yaml.write_text("programming: [unclosed\n", encoding="utf-8")
    write_schedule(programming_yaml, ScheduleForm(mode="dayparts"))
    assert load_config(library.config).programming.mode == "dayparts"


def test_load_schedule_matches_the_config(programming_yaml):
    config = load_config(programming_yaml.parent)
    form = load_schedule(config)
    assert form.mode == config.programming.mode
    assert form.pinned == list(config.programming.pinned)


def test_form_to_config_validates(config):
    """An empty lineup is a config error, and the form must surface it as one."""
    with pytest.raises(ValueError, match="lineup"):
        ScheduleForm(mode="lineup").to_config()


def test_config_rejects_unknown_order():
    with pytest.raises(ValueError):
        ProgrammingConfig(
            mode="lineup",
            lineup=[{"show_id": "futurama", "order": "shuffle"}],
        )


# -- page rendering ---------------------------------------------------------


def _client(paths, config):
    fastapi = pytest.importorskip("fastapi.testclient")
    from nostalgia.web.app import create_app

    return fastapi.TestClient(create_app(paths, config))


def test_schedule_page_renders_every_mode(paths, config):
    client = _client(paths, config)
    response = client.get("/schedule")
    assert response.status_code == 200
    body = response.text
    for mode in ("dayparts", "lineup", "shuffle"):
        assert f'value="{mode}"' in body
    assert 'action="/schedule"' in body
    assert 'data-progress="Saving and staging the schedule"' in body


def test_schedule_page_lists_the_roster_with_episode_counts(paths, config):
    client = _client(paths, config)
    body = client.get("/schedule").text
    assert "futurama" in body
    # The count is what tells the user a show is actually playable.
    assert "(6)" in body


def test_schedule_page_exposes_multiple_lineup_rows_and_start_fields(paths, config):
    body = _client(paths, config).get("/schedule").text
    assert 'data-lineup-add' in body
    assert 'data-lineup-remove' in body
    assert 'name="lineup_start_season"' in body
    assert 'name="lineup_start_episode"' in body


def test_schedule_page_warns_about_a_show_with_no_media(paths, library):
    """A rostered show with nothing downloaded would otherwise fail silently."""
    (library.config / "shows.yaml").write_text(
        (library.config / "shows.yaml").read_text(encoding="utf-8")
        + "  empty-show:\n    folder: shows/empty-show\n    slot: half\n",
        encoding="utf-8",
    )
    config = load_config(library.config)
    client = _client(paths, config)
    body = client.get("/schedule").text
    assert "empty-show" in body
    assert "cannot air" in body


def test_saving_a_lineup_persists_and_replans(paths, config):
    client = _client(paths, config)
    response = client.post(
        "/schedule",
        data={
            "mode": "lineup",
            "lineup_show": ["futurama", "sealab-2021"],
            "lineup_order": ["sequential", "random"],
            "lineup_start_season": ["2", ""],
            "lineup_start_episode": ["3", ""],
            "lineup_start_date": ["", ""],
            "shuffle_order": "random",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"].startswith("/schedule?saved=1")

    saved = load_config(paths.config_dir)
    assert saved.programming.mode == "lineup"
    assert saved.programming.lineup[0].show_id == "futurama"
    assert saved.programming.lineup[0].start_season == 2
    assert saved.programming.lineup[0].start_episode == 3
    assert saved.programming.lineup[0].start_date is not None
    assert saved.programming.lineup[0].start_date == date.today()
    assert len(saved.programming.lineup) == 2
    assert saved.programming.lineup[1].show_id == "sealab-2021"


def test_a_rejected_form_leaves_the_file_untouched(paths, config, programming_yaml):
    before = programming_yaml.read_text(encoding="utf-8")
    client = _client(paths, config)
    response = client.post(
        "/schedule",
        data={
            "mode": "lineup",
            # No shows: the config layer rejects an empty lineup.
            "lineup_show": [""],
            "lineup_order": ["random"],
        },
        follow_redirects=False,
    )
    assert response.status_code == 200
    assert "lineup" in response.text.lower()
    assert programming_yaml.read_text(encoding="utf-8") == before


def test_saved_flag_shows_a_confirmation(paths, config):
    client = _client(paths, config)
    body = client.get("/schedule?saved=1&built=1+day(s)+replanned").text
    assert "Schedule saved" in body
