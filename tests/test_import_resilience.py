"""Importing a real library, which is messier than a tidy download.

Every test here comes from a failure found importing an actual 60 GB collection.
A byte floor is a poor proxy for "is this an episode": 240p TV rips of 11-minute
[Aquanet] shows are 9 MB, while a 4K promo is larger. Judging size before
season/episode throws away real content, so the order of the checks is the thing
being pinned down.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from nostalgia import mediaimport
from nostalgia.config import ShowConfig
from nostalgia.naming import identity_prefix, parse_episode


def _shows() -> dict[str, ShowConfig]:
    return {
        "the-brak-show": ShowConfig(
            title="The Brak Show", folder="shows/the-brak-show", aliases=["Brak"]
        ),
        "king-of-the-hill": ShowConfig(
            title="King of the Hill",
            folder="shows/king-of-the-hill",
            aliases=["KotH"],
        ),
        "aqua-teen-hunger-force": ShowConfig(
            title="Aqua Teen Hunger Force",
            folder="shows/aqua-teen-hunger-force",
            aliases=["ATHF", "Aqua Teen"],
        ),
    }


def _video(path: Path, size: int, *, duration_ms: int | None = None) -> Path:
    """A stub video, optionally with the duration sidecar the scanner writes."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\0" * size)
    if duration_ms is not None:
        path.with_suffix(path.suffix + ".json").write_text(
            json.dumps({"duration_ms": duration_ms}), encoding="utf-8"
        )
    return path


# -- absolute episode numbers -------------------------------------------------


def test_leading_absolute_number_uses_the_season_folder():
    """``1001 - Title.mkv`` inside ``Season 10`` is episode 1, not episode 100.

    Read naively that filename is season 10 episode 100, which filed 231 King of
    the Hill episodes against episode numbers that do not exist.
    """
    parsed = parse_episode("1001 - Hank's On Board.mkv", season_dir="Season 10")
    assert parsed is not None
    assert (parsed.season, parsed.episode) == (10, 1)
    assert parsed.title == "Hank's On Board"


def test_absolute_number_disagreeing_with_the_folder_is_not_guessed():
    """A multi-season folder must be reported, not silently mis-filed."""
    assert parse_episode("1001 - Title.mkv", season_dir="Season 2") is None


def test_absolute_number_without_a_season_folder_is_not_guessed():
    assert parse_episode("1001 - Title.mkv") is None


def test_a_leading_year_is_not_read_as_a_season():
    """``2001 - Title`` would otherwise parse as season 20 episode 01."""
    assert parse_episode("2001 - A Space Odyssey.mkv", season_dir="Season 1") is None


# -- disc extras --------------------------------------------------------------


def test_a_featurette_folder_disqualifies_an_innocuous_filename(tmp_path):
    """``2) Hank Hill.mkv`` in a drawing-featurette folder is not S02E00."""
    path = _video(
        tmp_path
        / "king-of-the-hill"
        / "Featurettes"
        / "Season 2"
        / "The Arlen School of Drawing"
        / "2) Hank Hill.mkv",
        4_000_000,
    )
    candidates = mediaimport.plan_import(
        tmp_path, _shows(), tmp_path / "shows", min_bytes=1_000_000
    )
    statuses = {c.source.name: c.status for c in candidates}
    assert statuses["2) Hank Hill.mkv"] == mediaimport.EXTRA


def test_a_deleted_scene_beside_an_episode_does_not_win_the_slot(tmp_path):
    """A longer deleted-scene cut must not displace the broadcast episode."""
    root = tmp_path
    _video(
        root / "king-of-the-hill" / "Season 01" / "104 - Hank's Got the Willies.mkv",
        40_000_000,
    )
    _video(
        root
        / "king-of-the-hill"
        / "Featurettes"
        / "Season 1"
        / "Deleted and Extended Scenes"
        / "S01E04 - Hank's Got the Willies.mkv",
        74_000_000,
    )
    candidates = mediaimport.plan_import(
        root, _shows(), tmp_path / "shows", min_bytes=1_000_000
    )
    ready = [c for c in candidates if c.actionable]
    assert len(ready) == 1
    assert ready[0].source.name == "104 - Hank's Got the Willies.mkv"


def test_a_specials_folder_is_not_treated_as_extras(tmp_path):
    """``Specials/S01E01 - Pitch Pilot.mkv`` is real content that can air."""
    _video(
        tmp_path / "the-brak-show" / "Specials" / "The Brak Show S00E01 - Pilot.mkv",
        8_000_000,
    )
    candidates = mediaimport.plan_import(
        tmp_path, _shows(), tmp_path / "shows", min_bytes=1_000_000
    )
    assert [c.status for c in candidates] == [mediaimport.OK]


# -- size versus runtime ------------------------------------------------------


def test_a_low_bitrate_episode_is_imported_despite_the_size_floor(tmp_path):
    """The bug that silently discarded 28 Brak Show episodes.

    A 240p TV rip of an 11-minute episode is 9 MB -- under any sane byte floor --
    while a 30-second promo can be larger. Runtime is the discriminator.
    """
    _video(
        tmp_path
        / "the-brak-show"
        / "The Brak Show S01 (240p re-tvrip)"
        / "The Brak Show S01E04 Gimme a Brak, War Next Door.mp4",
        9_000_000,
        duration_ms=683_000,
    )
    candidates = mediaimport.plan_import(
        tmp_path, _shows(), tmp_path / "shows", min_bytes=20_000_000
    )
    assert [c.status for c in candidates] == [mediaimport.OK]


def test_a_short_clip_is_rejected_even_when_it_is_large(tmp_path):
    """A 78-second "Brak Sings" video is not an 11-minute episode."""
    _video(
        tmp_path / "the-brak-show" / "The Brak Show S01E04 Big Clip.mp4",
        12_000_000,
        duration_ms=78_480,
    )
    candidates = mediaimport.plan_import(
        tmp_path, _shows(), tmp_path / "shows", min_bytes=20_000_000
    )
    assert [c.status for c in candidates] == [mediaimport.TOO_SMALL]
    assert "too short" in candidates[0].detail


def test_an_unknown_runtime_under_the_floor_is_kept_rather_than_discarded(tmp_path):
    """With no ffprobe and no sidecar, keeping it is the safer error.

    A stray promo in the library is visible and removable; a discarded episode is
    invisible.
    """
    _video(tmp_path / "the-brak-show" / "The Brak Show S01E04 Untitled.mp4", 8_000_000)
    candidates = mediaimport.plan_import(
        tmp_path, _shows(), tmp_path / "shows", min_bytes=20_000_000
    )
    assert [c.status for c in candidates] == [mediaimport.OK]


def test_a_tiny_file_with_no_runtime_is_still_rejected(tmp_path):
    _video(tmp_path / "the-brak-show" / "The Brak Show S01E04 tiny.mp4", 200_000)
    candidates = mediaimport.plan_import(
        tmp_path, _shows(), tmp_path / "shows", min_bytes=20_000_000
    )
    assert [c.status for c in candidates] == [mediaimport.TOO_SMALL]


# -- duplicates ---------------------------------------------------------------


def test_the_larger_of_two_claims_on_one_episode_wins(tmp_path):
    """A cut and a full-length copy of the same episode are not an ambiguity.

    Refusing to choose made a real library unimportable: Space Ghost had
    ``01 Chambraigne.mkv`` beside ``01 Chambraigne-00.00.00.000-00.00.55.322.m4v``.
    """
    _video(tmp_path / "the-brak-show" / "S01E01 short.mp4", 4_000_000)
    _video(tmp_path / "the-brak-show" / "S01E01 long.mp4", 12_000_000)
    candidates = mediaimport.plan_import(
        tmp_path, _shows(), tmp_path / "shows", min_bytes=1_000_000
    )
    ready = [c for c in candidates if c.actionable]
    assert len(ready) == 1
    assert ready[0].source.name == "S01E01 long.mp4"
    losers = [c for c in candidates if c.status == mediaimport.DUPLICATE]
    assert len(losers) == 1
    assert "S01E01 long.mp4" in losers[0].detail


def test_duplicate_resolution_does_not_depend_on_scan_order(tmp_path):
    """The same tree always yields the same winner, whatever the filesystem says."""
    _video(tmp_path / "the-brak-show" / "a" / "S01E01.mkv", 4_000_000)
    _video(tmp_path / "the-brak-show" / "b" / "S01E01.mkv", 12_000_000)
    first = mediaimport.plan_import(
        tmp_path, _shows(), tmp_path / "shows", min_bytes=1_000_000
    )
    second = mediaimport.plan_import(
        tmp_path, _shows(), tmp_path / "shows", min_bytes=1_000_000
    )
    assert [c.status for c in first] == [c.status for c in second]
    assert [str(c.source) for c in first] == [str(c.source) for c in second]


# -- show identity ------------------------------------------------------------


def test_a_single_letter_folder_does_not_match_a_show(tmp_path):
    """``a`` resolved to ``ATHF`` and filed a whole season under Aqua Teen."""
    _video(tmp_path / "futurama" / "a" / "S01E01.mkv", 100)
    shows = {"aqua-teen-hunger-force": _shows()["aqua-teen-hunger-force"]}
    candidates = mediaimport.plan_import(
        tmp_path, shows, tmp_path / "shows", min_bytes=0
    )
    assert [c.status for c in candidates] == [mediaimport.UNKNOWN_SHOW]


def test_a_flat_release_names_the_show_in_the_filename(tmp_path):
    """Harvey Birdman ships flat, so the folder cannot identify it."""
    shows = {
        "harvey-birdman": ShowConfig(
            title="Harvey Birdman, Attorney at Law",
            folder="shows/harvey-birdman",
            aliases=["Harvey Birdman"],
        )
    }
    _video(
        tmp_path
        / "Harvey.Birdman.Attorney.At.Law.Complete.720P.WEB-DL"
        / "Season 1"
        / "Harvey.Birdman.S01E01.Bannon.Custody.Battle.720P.WEB-DL.mp4",
        1_000,
    )
    candidates = mediaimport.plan_import(
        tmp_path, shows, tmp_path / "shows", min_bytes=0
    )
    assert [c.show_id for c in candidates] == ["harvey-birdman"]


def test_a_web_series_in_an_extras_folder_is_not_imported(tmp_path):
    """``Carl's Pissed`` sits in an ATHF folder but is a different show.

    It resolves to Aqua Teen's *extras*, which is the honest answer -- but the
    folder is a disc-extras folder, so it is reported and never filed as an
    episode of anything.
    """
    _video(
        tmp_path
        / "athf"
        / "ATHF Xtras"
        / "Carl's Pissed (2007)"
        / "Carl's Pissed E01 Memories of Joe.mp4",
        1_000,
    )
    shows = {
        "aqua-teen-hunger-force": ShowConfig(
            title="Aqua Teen Hunger Force",
            folder="shows/aqua-teen-hunger-force",
            aliases=["ATHF"],
        )
    }
    candidates = mediaimport.plan_import(
        tmp_path, shows, tmp_path / "shows", min_bytes=0
    )
    assert [c.status for c in candidates] == [mediaimport.EXTRA]
    assert not any(c.actionable for c in candidates)


def test_identity_prefix_stops_at_the_episode_marker():
    assert identity_prefix("ATHF S01E01 Rabbot.mp4") == "ATHF"
    assert (
        identity_prefix(
            "Harvey.Birdman.S01E01.Bannon.Custody.Battle.720P.WEB-DL.mp4"
        )
        == "Harvey Birdman"
    )
