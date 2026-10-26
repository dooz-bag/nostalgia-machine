"""`doctor` must describe the schedule that is actually in use.

In a hand-programmed mode the day files are not read at all, so "N day files" and
nothing else describes a file the channel ignores. The dangerous case is a lineup
naming shows with no media: it plans to nothing, and the channel goes black. That
has to be a blocking error, not a warning.
"""

from __future__ import annotations

from nostalgia.config import ProgrammingConfig
from nostalgia.pipeline import doctor


def _with(config, **programming):
    return config.model_copy(
        update={"programming": ProgrammingConfig(**programming)}
    )


def _schedule(findings):
    return [f for f in findings if f[1] == "schedule"]


def test_dayparts_mode_is_reported(config, catalog, paths):
    findings = _schedule(doctor(config, catalog, paths))
    assert any("dayparts" in message for _, _, message in findings)
    assert all(level == "ok" for level, _, _ in findings)


def test_lineup_mode_is_reported_instead_of_the_day_files(config, catalog, paths):
    root = _with(config, mode="lineup", lineup=[{"show_id": "futurama"}])
    findings = _schedule(doctor(root, catalog, paths))
    assert any("lineup" in message and "futurama" not in message
               for _, _, message in findings)
    assert any("1 show(s)" in message for _, _, message in findings)


def test_shuffle_mode_reports_the_whole_roster(config, catalog, paths):
    root = _with(config, mode="shuffle")
    findings = _schedule(doctor(root, catalog, paths))
    assert any("whole roster" in message for _, _, message in findings)


def test_a_lineup_with_nothing_catalogued_is_a_blocking_error(config, catalog, paths):
    """This is the failure that silently produces a black channel."""
    root = _with(config, mode="lineup", lineup=[{"show_id": "ghost-show"}])
    findings = _schedule(doctor(root, catalog, paths))
    assert any(level == "error" for level, _, _ in findings), findings


def test_a_lineup_naming_an_empty_show_warns(config, catalog, paths):
    """A real roster entry with no media is a warning, not an error: the rest of
    the lineup still airs."""
    root = _with(
        config,
        mode="lineup",
        lineup=[{"show_id": "futurama"}, {"show_id": "sealab-2021"}],
    )
    findings = _schedule(doctor(root, catalog, paths))
    assert not any(level == "error" for level, _, _ in findings), findings


def test_daypart_warnings_are_suppressed_in_a_programmed_mode(
    config, catalog, paths, library
):
    """A broken day file is irrelevant when the day files are not in charge."""
    # A roster entry with no media, referenced by every daypart, which is exactly
    # what the daypart check warns about.
    (library.config / "shows.yaml").write_text(
        (library.config / "shows.yaml").read_text(encoding="utf-8")
        + "  empty-show:\n    folder: shows/empty-show\n    slot: half\n",
        encoding="utf-8",
    )
    from nostalgia.config import load_config

    broken = load_config(library.config)
    broken = broken.model_copy(
        update={
            "days": [
                day.model_copy(
                    update={
                        "dayparts": [
                            part.model_copy(
                                update={"shows": ["empty-show"]}
                            )
                            for part in day.dayparts
                        ]
                    }
                )
                for day in broken.days
            ]
        }
    )
    # The day files alone do warn about it.
    assert any(
        "has no episodes" in message
        for _, _, message in _schedule(doctor(broken, catalog, paths))
    )

    # In a hand-programmed mode they are not consulted, so they do not warn.
    programmed = _with(broken, mode="shuffle")
    messages = [
        message for _, _, message in _schedule(doctor(programmed, catalog, paths))
    ]
    assert not any("has no episodes" in message for message in messages), messages
