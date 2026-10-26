"""Filing a downloaded show into the library layout.

The scanner refuses to guess a season or episode, because airing episodes out of
order is worse than not airing them. That makes the importer the one place where
guessing is allowed -- so it has to be a dry run by default, it must never
overwrite a file the owner curated, and it must report what it could not do
rather than dropping it.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from nostalgia import mediaimport
from nostalgia.config import ShowConfig


def _shows() -> dict[str, ShowConfig]:
    return {
        "futurama": ShowConfig(
            title="Futurama", folder="shows/futurama", aliases=["Fry"]
        ),
        "aqua-teen-hunger-force": ShowConfig(
            title="Aqua Teen Hunger Force",
            folder="shows/aqua-teen-hunger-force",
            aliases=["ATHF", "Aqua Teen"],
        ),
    }


def _write(path: Path, size: int) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as handle:
        handle.write(b"\0" * size)
    return path


@pytest.fixture()
def drop(tmp_path) -> Path:
    """A download shaped like a real one: quality noise, mixed naming, junk."""
    root = tmp_path / "incoming"
    _write(root / "Futurama 1080p WEB-DL" / "Season 2" / "Futurama.2x05.mkv", 100)
    _write(root / "Futurama 1080p WEB-DL" / "Season 2" / "Futurama S02E06.mkv", 100)
    _write(root / "Futurama 1080p WEB-DL" / "Season 2" / "sample.mkv", 10)
    _write(root / "aqua.teen.hunger.force" / "ATHF - S01E03.mkv", 100)
    return root


def test_aliases_and_quality_noise_resolve_to_the_roster_id(drop, tmp_path) -> None:
    candidates = mediaimport.plan_import(drop, _shows(), tmp_path / "shows", min_bytes=0)
    by_source = {c.source.name: c for c in candidates}

    assert by_source["Futurama.2x05.mkv"].show_id == "futurama"
    assert by_source["ATHF - S01E03.mkv"].show_id == "aqua-teen-hunger-force"


def test_targets_use_the_library_naming(drop, tmp_path) -> None:
    candidates = mediaimport.plan_import(drop, _shows(), tmp_path / "shows", min_bytes=0)
    ready = {c.key: c.target for c in candidates if c.actionable}

    assert ready["futurama:s02e005"].as_posix().endswith(
        "shows/futurama/Season 02/futurama - S02E05.mkv"
    )


def test_junk_and_undersized_files_are_skipped(drop, tmp_path) -> None:
    """Neither is imported, but they are reported differently on purpose.

    ``sample.mkv`` is an *extra*: a real video that is deliberately not an
    episode, and saying so is more useful than "too small", which reads like a
    failed download.
    """
    candidates = mediaimport.plan_import(drop, _shows(), tmp_path / "shows", min_bytes=50)
    by_source = {c.source.name: c for c in candidates}

    assert by_source["sample.mkv"].status == mediaimport.EXTRA
    assert not by_source["sample.mkv"].actionable


def test_an_unknown_show_is_reported_not_filed(tmp_path) -> None:
    root = tmp_path / "incoming"
    _write(root / "Some Other Show" / "Whatever S01E01.mkv", 100)

    candidates = mediaimport.plan_import(root, _shows(), tmp_path / "shows", min_bytes=0)
    assert [c.status for c in candidates] == [mediaimport.UNKNOWN_SHOW]


def test_a_nameless_file_is_unparsed_not_guessed(tmp_path) -> None:
    root = tmp_path / "incoming"
    _write(root / "futurama" / "no numbers here.mkv", 100)

    candidates = mediaimport.plan_import(root, _shows(), tmp_path / "shows", min_bytes=0)
    assert [c.status for c in candidates] == [mediaimport.UNPARSED]


def test_an_existing_target_is_a_collision_not_an_overwrite(drop, tmp_path) -> None:
    dest = tmp_path / "shows"
    existing = dest / "futurama" / "Season 02" / "futurama - S02E05.mkv"
    _write(existing, 7)

    candidates = mediaimport.plan_import(drop, _shows(), dest, min_bytes=0)
    collision = next(c for c in candidates if c.source.name == "Futurama.2x05.mkv")
    assert collision.status == mediaimport.COLLISION

    mediaimport.apply_import(candidates)
    assert existing.read_bytes() == b"\0" * 7, "a curated file was overwritten"


def test_two_files_claiming_one_episode_are_flagged(tmp_path) -> None:
    root = tmp_path / "incoming"
    _write(root / "futurama" / "a" / "S01E01.mkv", 100)
    _write(root / "futurama" / "b" / "S01E01.mkv", 100)

    candidates = mediaimport.plan_import(root, _shows(), tmp_path / "shows", min_bytes=0)
    statuses = sorted(c.status for c in candidates)
    assert statuses == sorted([mediaimport.OK, mediaimport.DUPLICATE])


def test_apply_copies_by_default_and_leaves_the_source(drop, tmp_path) -> None:
    dest = tmp_path / "shows"
    candidates = mediaimport.plan_import(drop, _shows(), dest, min_bytes=0)
    report = mediaimport.apply_import(candidates)

    assert report.copied == 3
    assert report.moved == 0
    assert (drop / "Futurama 1080p WEB-DL" / "Season 2" / "Futurama.2x05.mkv").is_file()
    assert (dest / "futurama" / "Season 02" / "futurama - S02E05.mkv").is_file()


def test_apply_can_move_instead(drop, tmp_path) -> None:
    dest = tmp_path / "shows"
    candidates = mediaimport.plan_import(drop, _shows(), dest, min_bytes=0)
    report = mediaimport.apply_import(candidates, move=True)

    assert report.moved == 3
    assert not (drop / "Futurama 1080p WEB-DL" / "Season 2" / "Futurama.2x05.mkv").exists()


def test_force_show_files_an_unrecognised_folder(tmp_path) -> None:
    root = tmp_path / "incoming"
    _write(root / "Some Other Show" / "Whatever S01E01.mkv", 100)

    candidates = mediaimport.plan_import(
        root, _shows(), tmp_path / "shows", min_bytes=0, default_show="futurama"
    )
    assert [c.status for c in candidates] == [mediaimport.OK]
    assert candidates[0].show_id == "futurama"


def test_the_plan_is_deterministic(drop, tmp_path) -> None:
    first = mediaimport.plan_import(drop, _shows(), tmp_path / "shows", min_bytes=0)
    second = mediaimport.plan_import(drop, _shows(), tmp_path / "shows", min_bytes=0)
    assert [(c.status, c.key) for c in first] == [(c.status, c.key) for c in second]


@pytest.mark.parametrize(
    ("minutes", "expected"),
    [(11, "short"), (13, "short"), (22, "half"), (25, "half"), (44, "hour"), (90, "hour")],
)
def test_suggest_slot_is_arithmetic_on_the_length(minutes, expected) -> None:
    assert mediaimport.suggest_slot(minutes * 60_000) == expected


def test_shows_yaml_snippet_is_paste_ready() -> None:
    snippet = mediaimport.shows_yaml_snippet("home-movies", "Home Movies", slot="half")
    assert snippet.startswith("  home-movies:")
    assert 'folder: shows/home-movies' in snippet
    assert "slot: half" in snippet
    # Indented for the shows: mapping, so it can be pasted without editing.
    assert all(line.startswith("    ") for line in snippet.splitlines()[1:])


def test_shows_yaml_snippet_emits_a_window() -> None:
    snippet = mediaimport.shows_yaml_snippet(
        "home-movies", "Home Movies", eligibility="window", airing_from="2004-11-01"
    )
    assert "eligibility: window" in snippet
    assert 'airing_from: "2004-11-01"' in snippet
