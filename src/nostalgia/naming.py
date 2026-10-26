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
    # ``S03X01`` -- the way releases label extras and webisodes that sit outside
    # the numbered run. Treating them as episodes is better than dropping them:
    # an extra that never airs looks identical to a failed import.
    re.compile(r"(?<![a-z0-9])s(?P<s>\d{1,2})[\s._-]*x(?P<e>\d{1,3})(?!\d)", re.IGNORECASE),
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

    A leading absolute episode number -- ``1001 - Title.mkv`` in ``Season 10`` --
    is also resolved. Read naively that is season 10 episode 100; the convention
    is a two-digit season followed by a two-digit episode, which is why the
    season *folder* is used to split the digits rather than assuming a width.
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

    numbered = _from_absolute_number(stem, season)
    if numbered is not None:
        return numbered
    if _LEADING_ABSOLUTE.match(stem):
        # A leading 3-4 digit number that the season folder *disagrees* with.
        # Falling through would let the bare-number rule read "1001 - Title" as
        # episode 100, filing a whole season against numbers that cannot exist.
        # A multi-season folder is a real thing and has to be reported instead.
        return None


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


def _from_absolute_number(stem: str, season: int | None) -> ParsedEpisode | None:
    """``Season 10/1001 - Title.mkv`` -> season 10, episode 1.

    Only trusted when the digits agree with the season folder. A disagreement
    means the release packs multiple seasons into one folder, and guessing there
    would file the episodes under a season that does not exist -- better to
    report it as unparsed, which ``doctor`` surfaces.
    """
    if season is None:
        return None
    match = _LEADING_ABSOLUTE.match(stem)
    if not match:
        return None
    digits = match.group("a")
    parsed_season, parsed_episode = int(digits[:-2]), int(digits[-2:])
    if parsed_season != season or parsed_episode == 0:
        return None
    return ParsedEpisode(
        parsed_season, parsed_episode, clean_title(stem[match.end() :])
    )


#: ``1001 - Title`` / ``1001. Title`` / ``1001 Title``. Deliberately not
#: anchored to a three-or-four-digit number of any shape: a leading year
#: (``2001 - Title``) would otherwise be read as season 20.
_LEADING_ABSOLUTE = re.compile(r"^(?P<a>\d{3,4})\s*[-–.]?\s+")


def identity_prefix(filename: str) -> str:
    """The part of a filename before any episode marker.

    ``Show.Name.S01E01.Title.mkv`` -> ``Show Name``. Used when the folder does
    not name the show, so the show has to be read off the filename. Cutting at
    the episode marker matters: a *title* often contains the show's name, and
    letting it into the match would attribute every file in a folder to whichever
    show the title happened to mention.
    """
    stem = filename
    for extension in VIDEO_EXTENSIONS:
        if stem.lower().endswith(extension):
            stem = stem[: -len(extension)]
            break
    cut = len(stem)
    for pattern in (*_SEASON_EPISODE, *_EPISODE_ONLY):
        for match in pattern.finditer(stem):
            cut = min(cut, match.start())
    return clean_title(stem[:cut])


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
    "identity_prefix",
    "is_media_name",
    "normalise_for_match",
    "parse_episode",
    "season_from_dir",
    "show_id_from_dir",
    "year_from_name",
]
