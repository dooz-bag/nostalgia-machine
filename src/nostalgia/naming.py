"""Filename → episode identity.

Libraries in the wild are messy: ``S01E02``, ``s1e2``, ``1x02``, ``Season 1/Episode
2``, ``- 102 -``, ``Ep 2``. The parser is forgiving on purpose and returns
``None`` rather than guessing when it cannot find numbers, so an unrecognised file
is reported by ``doctor`` instead of silently airing out of order.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .probe import VIDEO_EXTENSIONS

_SEASON_EPISODE = [
    re.compile(r"(?<![a-z0-9])s(?P<s>\d{1,2})[\s._-]*e(?P<e>\d{1,3})(?!\d)", re.IGNORECASE),
    re.compile(r"(?<![a-z0-9])(?P<s>\d{1,2})x(?P<e>\d{2,3})(?!\d)", re.IGNORECASE),
    re.compile(r"(?<![a-z0-9])season[\s._-]*(?P<s>\d{1,2})[\s._-]*episode[\s._-]*(?P<e>\d{1,3})", re.IGNORECASE),
]

_EPISODE_ONLY = [
    re.compile(r"(?<![a-z0-9])episode[\s._-]*(?P<e>\d{1,3})(?!\d)", re.IGNORECASE),
    re.compile(r"(?<![a-z0-9])ep[\s._-]*(?P<e>\d{1,3})(?!\d)", re.IGNORECASE),
]

_SEASON_DIR = re.compile(r"^(?:season|series)[\s._-]*(?P<s>\d{1,2})$", re.IGNORECASE)
_ABSOLUTE = re.compile(r"^(?P<a>\d{3,4})$")

_JUNK = re.compile(r"[._]+")
_MULTISPACE = re.compile(r"\s{2,}")


@dataclass(frozen=True, slots=True)
class ParsedEpisode:
    season: int
    episode: int
    title: str

    @property
    def key_suffix(self) -> str:
        return f"s{self.season:02d}e{self.episode:03d}"


def is_media_name(name: str) -> bool:
    return any(name.lower().endswith(extension) for extension in VIDEO_EXTENSIONS)


def clean_title(raw: str) -> str:
    text = _JUNK.sub(" ", raw)
    text = _MULTISPACE.sub(" ", text).strip(" -")
    return text


def parse_episode(filename: str, *, season_dir: str | None = None) -> ParsedEpisode | None:
    """Extract ``(season, episode)`` from a filename.

    *season_dir* is used when the filename carries only an episode number, which
    is common inside a ``Season 01`` folder.
    """
    stem = filename
    for extension in VIDEO_EXTENSIONS:
        if stem.lower().endswith(extension):
            stem = stem[: -len(extension)]
            break
    stem = stem.strip()

    for pattern in _SEASON_EPISODE:
        match = pattern.search(stem)
        if match:
            title = _strip_identity(stem, match)
            return ParsedEpisode(int(match.group("s")), int(match.group("e")), title)

    season = _season_from_dir(season_dir)

    for pattern in _EPISODE_ONLY:
        match = pattern.search(stem)
        if match and season is not None:
            return ParsedEpisode(season, int(match.group("e")), _strip_identity(stem, match))

    stripped = stem.strip(" -")
    absolute = _ABSOLUTE.match(stripped)
    if absolute and season is not None:
        number = absolute.group("a")
        return ParsedEpisode(season, int(number), "")

    if season is not None:
        # "Season 01/02 - Episode Title.mkv"
        leading = re.match(r"^(?P<e>\d{1,3})\s*(?:[-–]\s*)?(?P<rest>.*)$", stripped)
        if leading:
            return ParsedEpisode(season, int(leading.group("e")), clean_title(leading.group("rest")))

    return None


def _strip_identity(stem: str, match: re.Match[str]) -> str:
    leftover = stem[: match.start()] + " " + stem[match.end() :]
    title = clean_title(leftover)
    title = re.sub(r"^(?:[-–]\s*)", "", title)
    title = re.sub(r"\s*[-–]$", "", title)
    return clean_title(title)


def season_from_dir(path_name: str) -> int | None:
    match = _SEASON_DIR.match(path_name.strip())
    return int(match.group("s")) if match else None


def _season_from_dir(season_dir: str | None) -> int | None:
    if not season_dir:
        return None
    return season_from_dir(season_dir)


def show_id_from_dir(name: str) -> str:
    """``Aqua Teen Hunger Force`` → ``aqua-teen-hunger-force``."""
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return slug or "unknown"


def normalise_for_match(text: str) -> str:
    """Lowercase, strip punctuation and collapse spaces, for title matching."""
    plain = re.sub(r"[^a-z0-9]+", " ", text.lower())
    return _MULTISPACE.sub(" ", plain).strip()


def year_from_name(name: str) -> int | None:
    match = re.search(r"\(?(19[5-9]\d|20[0-4]\d)\)?", name)
    return int(match.group(1)) if match else None


__all__ = [
    "ParsedEpisode",
    "clean_title",
    "is_media_name",
    "normalise_for_match",
    "parse_episode",
    "season_from_dir",
    "show_id_from_dir",
    "year_from_name",
]
