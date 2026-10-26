"""A downloaded show that no roster entry points at must not be silent.

The scanner only walks folders named in ``shows.yaml``, which is correct -- it
stops a stray directory becoming a channel -- but it also means a show can sit
fully downloaded under ``media/shows/`` and never air, with a green ``doctor``.
That is the most expensive way for this project to fail, because it looks fine.
"""

from __future__ import annotations

from pathlib import Path

from nostalgia.pipeline import doctor


def _write(path: Path, data: bytes = b"x") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def _library_messages(config, catalog, paths) -> list[str]:
    return [
        message
        for _level, area, message in doctor(config, catalog, paths)
        if area == "library"
    ]


def test_an_unrostered_folder_with_video_is_reported(config, catalog, paths) -> None:
    _write(paths.media_root / "shows" / "home-movies" / "Season 01" / "S01E01.mkv")

    messages = _library_messages(config, catalog, paths)
    assert any(
        "home-movies" in message and "never scanned" in message for message in messages
    ), messages


def test_an_empty_unrostered_folder_is_reported_too(config, catalog, paths) -> None:
    (paths.media_root / "shows" / "the-brak-show").mkdir(parents=True, exist_ok=True)

    messages = _library_messages(config, catalog, paths)
    assert any("the-brak-show" in message for message in messages), messages


def test_a_rostered_folder_is_not_reported(config, catalog, paths) -> None:
    _write(paths.media_root / "shows" / "futurama" / "Season 01" / "S01E01.mkv")

    messages = _library_messages(config, catalog, paths)
    assert not any("never scanned" in message for message in messages), messages


def test_an_absolute_show_folder_is_understood(config, catalog, paths, tmp_path) -> None:
    """`folder:` may be absolute, and then the name is still the roster's."""
    outside = tmp_path / "elsewhere" / "futurama"
    _write(outside / "Season 01" / "S01E01.mkv")

    messages = _library_messages(config, catalog, paths)
    assert not any("elsewhere" in message for message in messages), messages
