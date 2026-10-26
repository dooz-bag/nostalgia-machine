"""Bring a downloaded show into the library layout.

A fresh download is rarely shaped like the library: it is one flat folder per
series, names vary between ``S01E01``, ``1x01`` and ``Episode 1``, and there are
extras mixed in. The scanner is deliberately strict -- it will not guess a season
or episode, because airing episodes out of order is worse than not airing them --
so a messy drop means a show that silently never appears.

This module does the guessing *once*, up front, where a human can look at the
proposal before any file moves. Two rules make that safe:

* **It never overwrites.** A target that already exists is a collision, reported
  and skipped, because the file already there is library content that a person
  curated.
* **It defaults to copy.** The download stays where it is until the owner is
  satisfied, and ``-Move`` is opt-in.

Everything here is a pure function of the source tree plus the roster, so the
same input always produces the same target paths -- which is what makes a dry run
worth reading.
"""

from __future__ import annotations

import re
import shutil
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Iterable, Mapping, Sequence

from .naming import identity_prefix, normalise_for_match, parse_episode, season_from_dir
from .probe import VIDEO_EXTENSIONS
from .rotation import EpisodeRef

#: Path parts, relative to the import root.
Parts = tuple[str, ...]

OK = "ok"
UNPARSED = "unparsed"
COLLISION = "collision"
DUPLICATE = "duplicate"
TOO_SMALL = "too-small"
UNKNOWN_SHOW = "unknown-show"
ALREADY = "already"
EXTRA = "extra"
"""A real video below the episode floor: a webisode, an extra, a bumper.

Kept apart from ``too-small`` so that "this file is not an episode" and "this file
cannot be identified" stay distinguishable when reading a dry run.
"""

#: Names that are never part of the season, whatever their size.
_JUNK_TOKENS = (
    "sample",
    "trailer",
    "teaser",
    "featurette",
    "behind the scenes",
    "behindthescenes",
    "interview",
    "commentary",
    "nfo",
    # Filename fragments that mark disc extras rather than episodes.
    "deleted scene",
    "animatic",
    "storyboard",
    "music video",
    "fake ending",
    "easter egg",
)

#: Folder names that only ever hold disc extras. Matched against whole path
#: components, so a show whose title contains "xtras" is not disqualified.
#:
#: "Specials" is deliberately absent: a ``Specials`` folder here holds
#: properly-labelled ``S00E01 - Pitch Pilot.mkv`` files, which are real content
#: the schedule can air. Only folders that are extras by construction are listed.
_EXTRA_DIRS = {
    "xtras",
    "extras",
    "bonus",
    "featurettes",
    "deleted scenes",
    "animatics",
    "storyboards",
}


@dataclass(frozen=True, slots=True)
class Candidate:
    """One source file and what would happen to it."""

    source: Path
    show_id: str | None
    season: int | None
    episode: int | None
    target: Path | None
    status: str
    detail: str = ""
    size_bytes: int = 0

    @property
    def key(self) -> str | None:
        if self.show_id is None or self.season is None or self.episode is None:
            return None
        return f"{self.show_id}:s{self.season:02d}e{self.episode:03d}"

    @property
    def actionable(self) -> bool:
        return self.status == OK


@dataclass
class ImportReport:
    candidates: list[Candidate] = field(default_factory=list)
    moved: int = 0
    copied: int = 0
    skipped: int = 0
    bytes_done: int = 0
    created_dirs: list[Path] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def by_status(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for item in self.candidates:
            counts[item.status] = counts.get(item.status, 0) + 1
        return counts

    def of(self, *statuses: str) -> list[Candidate]:
        return [item for item in self.candidates if item.status in statuses]


def build_alias_index(shows: Mapping[str, object]) -> dict[str, str]:
    """Map every acceptable spelling of a show onto its roster id.

    A download folder is named by a human or by a release group, so it will be
    ``Aqua Teen Hunger Force``, ``aqua.teen.hunger.force`` or ``ATHF``. Matching
    against id + title + aliases is what stops the importer creating a second
    folder for a show that is already in the roster.
    """
    index: dict[str, str] = {}

    def add(name: str | None, show_id: str) -> None:
        if not name:
            return
        token = normalise_for_match(name)
        if token:
            index.setdefault(token, show_id)
        # Also index the unpunctuated form so "ATHF" matches "a t h f"-ish noise.
        compact = token.replace(" ", "")
        if compact:
            index.setdefault(compact, show_id)

    for show_id, show in shows.items():
        add(show_id, show_id)
        add(show_id.replace("-", " "), show_id)
        for attribute in ("title", "aliases"):
            value = getattr(show, attribute, None)
            if value is None:
                continue
            if isinstance(value, str):
                add(value, show_id)
            else:
                for alias in value:
                    add(alias, show_id)
    return index


def match_show(name: str, aliases: Mapping[str, str]) -> str | None:
    """Resolve a folder name to a roster id, or ``None``.

    Falls back to a prefix match because downloads often append quality noise
    (``Futurama 1080p WEB-DL``). Longest alias wins so "Aqua Teen" cannot shadow
    "Aqua Teen Hunger Force".

    Very short names are refused outright. A prefix rule otherwise lets a
    one-letter folder match every alias that starts with that letter -- a folder
    called ``a`` resolved to ``ATHF`` and filed an entire season under Aqua Teen.
    """
    token = normalise_for_match(name)
    if len(token.replace(" ", "")) < 3:
        return None
    if token in aliases:
        return aliases[token]
    compact = token.replace(" ", "")
    if compact in aliases:
        return aliases[compact]

    best: tuple[int, str] | None = None
    for alias, show_id in aliases.items():
        if len(alias.replace(" ", "")) < 3:
            continue
        if token.startswith(alias) or alias.startswith(token):
            if best is None or len(alias) > best[0]:
                best = (len(alias), show_id)
    return best[1] if best else None


def is_junk(path: Path, *, min_bytes: int) -> tuple[bool, str]:
    """Whether a file is certainly not an episode.

    The whole path is searched, not just the filename. Featurettes are usually
    identified by their *folder* while every filename inside looks ordinary --
    ``Featurettes/Season 1/Easter Egg - Boomhauer Clips.mkv`` -- and a filename
    like ``2) Hank Hill.mkv`` inside ``The Arlen School of Drawing`` parses as
    season 2 episode 0 unless the folder is what disqualifies it.
    """
    if path.suffix.lower() not in VIDEO_EXTENSIONS:
        return True, "not a video file"
    lowered = str(path).lower().replace("\\", "/")
    for token in _JUNK_TOKENS:
        if token in lowered:
            return True, f"looks like {token}"
    try:
        size = path.stat().st_size
    except OSError as exc:
        return True, str(exc)
    if min_bytes and size < min_bytes:
        return True, f"only {size / (1024 * 1024):.1f} MB"
    return False, ""


#: Shorter than this and the file is not a programme. A 240p TV rip of an
#: 11-minute [adult swim] show can be 9 MB, so *size* cannot be the test -- a
#: 30-second promo can be nearly as large. Runtime can.
MIN_EPISODE_MS = 4 * 60_000


def episode_duration_ms(
    path: Path, *, ffprobe: str | None = None
) -> int | None:
    """Runtime of *path*, or ``None`` when it cannot be determined.

    Only used to separate an episode from a promo when a file is small enough to
    be suspicious, so a missing ffprobe is not fatal: the size floor stands.
    """
    from .probe import sidecar_duration_ms

    sidecar = sidecar_duration_ms(path)
    if sidecar:
        return sidecar
    from .probe import ProbeError, probe

    try:
        return probe(path, ffprobe).duration_ms
    except (ProbeError, OSError):
        return None


def _source_show_name(path: Path, source_root: Path) -> str:
    """The folder that names the show, relative to *source_root*.

    Download roots are inconsistent: sometimes each show is a folder
    (``Downloads/Shows/Futurama/S01E01.mkv``), sometimes the show name is in the
    filename and there is no folder at all. A file sitting directly in the root
    is treated as its own show name so ``Futurama.S01E01.mkv`` still resolves.
    """
    try:
        relative = path.relative_to(source_root)
    except ValueError:
        return path.parent.name
    if len(relative.parts) > 1:
        return relative.parts[0]
    return path.stem


def _show_id_for(
    path: Path,
    relative: Parts,
    aliases: Mapping[str, str],
    default_show: str | None,
) -> str | None:
    """Work out which show a file belongs to.

    Naming a show is not the same as *holding* one. ``ATHF Xtras/Soul Quest
    Overdrive S01E01`` sits under a folder that names Aqua Teen but is a different
    show entirely, so the innermost folder wins and the top-level folder is only
    a fallback.

    The filename is consulted before the top-level folder too, because a release
    that parks twelve series in one root has to be identified by its files; only
    the part before the episode marker is used, or a title containing a show's
    name would pull in the whole folder.
    """
    if default_show:
        return default_show
    candidates = [path.parent.name, identity_prefix(path.name), path.stem]
    if relative:
        candidates.append(relative[0])
    for name in candidates:
        resolved = match_show(name, aliases)
        if resolved is not None:
            return resolved
    return None


def target_name(show_id: str, season: int, episode: int, extension: str) -> str:
    """The library's own naming, derived rather than guessed.

    ``SxxExx`` is the one form every downstream parser agrees on, so the importer
    emits it exactly rather than preserving whatever the release used.
    """
    return f"{show_id} - S{season:02d}E{episode:02d}{extension.lower()}"


def plan_import(
    source_root: Path | str,
    shows: Mapping[str, object],
    dest_shows_root: Path | str,
    *,
    min_bytes: int = 20 * 1024 * 1024,
    min_bytes_extra: int | None = 1 * 1024 * 1024,
    default_show: str | None = None,
    recursive: bool = True,
    ffprobe: str | None = None,
    show_roots: Mapping[str, str] | None = None,
) -> list[Candidate]:
    """Work out where every file would go, without touching anything.

    ``default_show`` forces every file under one show id, which is what you want
    when you already know you are importing a single series and the folder name
    is unhelpful.

    ``show_roots`` maps a top-level source folder to the show it holds. Some
    releases are structured so the folder genuinely does not name the show --
    ``King of the Hill/King of the Hill/Season 01/101 - Pilot.mkv`` has an
    ordinary inner folder name, and a flat Harvey Birdman release names the show
    only in a mangled filename. Declaring it beats adding a fragile alias.

    ``min_bytes_extra`` is the size floor for files the roster will not use at
    all. The episode floor is far too aggressive for a 3 MB webisode or a 0.3 MB
    bumper, and those are exactly the files most worth keeping for a later
    import, so they are reported as extras instead of rubbish. ``None`` disables
    the distinction.
    """
    source_root = Path(source_root)
    dest_shows_root = Path(dest_shows_root)
    if not source_root.is_dir():
        raise NotADirectoryError(f"not a folder: {source_root}")

    aliases = build_alias_index(shows)
    # Non-video files are not candidates at all. Subtitles and .nfo sit beside
    # every episode in a real download, and reporting each one as "not a video
    # file" buried the actual problems under hundreds of lines of noise.
    files = sorted(
        path
        for path in (source_root.rglob("*") if recursive else source_root.glob("*"))
        if path.is_file() and path.suffix.lower() in VIDEO_EXTENSIONS
    )

    candidates: list[Candidate] = []
    #: ``(candidate index, episode key, source, size)`` for every file wanting an
    #: episode slot. Duplicates are resolved once the whole tree has been seen, so
    #: the winner does not depend on the order the filesystem returns.
    competing: list[tuple[int, str, Path, int]] = []

    for path in files:
        size = 0
        try:
            size = path.stat().st_size
        except OSError:
            pass

        # Rejection by name comes first: a trailer or a disc extra is never an
        # episode, whatever it weighs or how long it runs.
        named, why = _named_extra(path)
        if named:
            candidates.append(
                Candidate(path, None, None, None, None, EXTRA, why, size)
            )
            continue

        relative = path.relative_to(source_root).parts
        declared = (show_roots or {}).get(relative[0]) if relative else None
        show_id = declared or _show_id_for(path, relative, aliases, default_show)
        if show_id is None:
            candidates.append(
                Candidate(
                    path,
                    None,
                    None,
                    None,
                    None,
                    UNKNOWN_SHOW,
                    f"no roster match for {_source_show_name(path, source_root)!r}"
                    f" or {path.name!r}",
                    size,
                )
            )
            continue

        parsed = parse_episode(path.name, season_dir=path.parent.name)
        if parsed is None:
            # Last chance: a bare episode number in a folder with no season name.
            parsed = parse_episode(path.name, season_dir="Season 1")
        if parsed is None:
            candidates.append(
                Candidate(
                    path,
                    show_id,
                    None,
                    None,
                    None,
                    UNPARSED,
                    "cannot determine season/episode from the filename",
                    size,
                )
            )
            continue

        # Only now, with a real season and episode in hand, does size mean
        # anything. Judging earlier threw away 28 genuine episodes: a 240p TV rip
        # of an 11-minute show is 9 MB, and the floor rejected it before anyone
        # checked whether it was an episode at all.
        accepted, verdict = _episode_size_verdict(
            path, size, min_bytes, min_bytes_extra, ffprobe
        )
        if not accepted:
            candidates.append(
                Candidate(
                    path,
                    show_id,
                    parsed.season,
                    parsed.episode,
                    None,
                    verdict[0],
                    verdict[1],
                    size,
                )
            )
            continue

        target = (
            dest_shows_root
            / show_id
            / f"Season {parsed.season:02d}"
            / target_name(show_id, parsed.season, parsed.episode, path.suffix)
        )
        if target.exists() and target.resolve() != path.resolve():
            candidates.append(
                Candidate(
                    path,
                    show_id,
                    parsed.season,
                    parsed.episode,
                    target,
                    COLLISION,
                    "target already exists",
                    size,
                )
            )
            continue

        competing.append(
            (
                len(candidates),
                f"{show_id}:s{parsed.season:02d}e{parsed.episode:03d}",
                path,
                size,
            )
        )
        candidates.append(
            Candidate(
                path, show_id, parsed.season, parsed.episode, target, OK, "", size
            )
        )

    return _resolve_duplicates(candidates, competing)


def _named_extra(path: Path) -> tuple[bool, str]:
    """Whether the *name* says this is not an episode.

    Checked before anything else, because a trailer or a disc extra is never an
    episode however large it is. The whole path is searched: featurettes are
    usually identified by their folder while every filename inside looks
    ordinary, and ``2) Hank Hill.mkv`` inside ``The Arlen School of Drawing``
    parses as season 2 episode 0 unless the folder disqualifies it.
    """
    if path.suffix.lower() not in VIDEO_EXTENSIONS:
        return True, "not a video file"
    lowered = str(path).lower().replace("\\", "/")
    for token in _JUNK_TOKENS:
        if token in lowered:
            return True, f"looks like {token}"
    # Directory names that mark a disc-extras folder. Real folders are decorated
    # ("ATHF Xtras", "Season 2 Featurettes"), so the last word is what is
    # compared -- matching any word would disqualify a show whose title happens
    # to contain "extras".
    for part in path.parts[:-1]:
        words = re.findall(r"[a-z]+", part.lower())
        if words and words[-1] in _EXTRA_DIRS:
            return True, f"inside a {part!r} folder"
    return False, ""


def _episode_size_verdict(
    path: Path,
    size: int,
    min_bytes: int,
    min_bytes_extra: int | None,
    ffprobe: str | None,
) -> tuple[bool, tuple[str, str]]:
    """Whether a parsed episode is big enough to be one.

    A byte floor cannot tell an episode from a bumper. The Brak Show's 240p TV
    rips are 9-17 MB for a full 11-minute episode, while a 30-second promo can be
    larger, so ``--min-mb`` alone silently discarded 28 real episodes. Runtime
    settles it, and is measured only for files the floor already flagged, so an
    ordinary import does not probe the whole tree.

    Returns ``(accepted, (status, detail))``.
    """
    if size >= (min_bytes or 0):
        return True, ("", "")
    duration = episode_duration_ms(path, ffprobe=ffprobe)
    if duration is not None and duration >= MIN_EPISODE_MS:
        return True, ("", "")
    if duration is not None:
        return False, (
            TOO_SMALL,
            f"{duration / 1000:.0f}s runtime -- too short to be an episode",
        )
    if min_bytes_extra is not None and size >= min_bytes_extra:
        # Runtime is unknowable here (no ffprobe, no sidecar). Keeping it is the
        # safer error: a stray promo in the library is visible and removable,
        # whereas a discarded episode is invisible.
        return True, ("", "")
    return False, (TOO_SMALL, f"only {size / (1024 * 1024):.1f} MB")


def _resolve_duplicates(
    candidates: list[Candidate], competing: Sequence[tuple[int, str, Path, int]]
) -> list[Candidate]:
    """Keep the largest file per episode and report the rest.

    Refusing to choose made a real library unimportable: ``01 Chambraigne.mkv``
    beside ``01 Chambraigne-00.00.00.000-00.00.55.322.m4v`` is a cut and a
    full-length copy of the *same* episode, not an ambiguity. The bigger file is
    the episode. Nothing disappears quietly -- every loser is named.
    """
    by_key: dict[str, list[tuple[int, Path, int]]] = {}
    for index, key, path, size in competing:
        by_key.setdefault(key, []).append((index, path, size))

    for claims in by_key.values():
        if len(claims) < 2:
            continue
        winner_index, winner_path, winner_size = max(
            claims, key=lambda claim: (claim[2], str(claim[1]))
        )
        for index, path, size in claims:
            if index == winner_index:
                continue
            candidates[index] = replace(
                candidates[index],
                status=DUPLICATE,
                detail=(
                    f"same episode as {winner_path.name} "
                    f"({winner_size / (1024 * 1024):.1f} MB against "
                    f"{size / (1024 * 1024):.1f} MB); not imported"
                ),
            )
    return candidates



def apply_import(
    candidates: Sequence[Candidate],
    *,
    move: bool = False,
    overwrite: bool = False,
) -> ImportReport:
    """Copy or move the ``OK`` candidates into place. Never destructive by default."""
    report = ImportReport(candidates=list(candidates))
    for item in candidates:
        if not item.actionable and not (overwrite and item.status == COLLISION):
            if item.status in (OK,):
                continue
            report.skipped += 1
            continue
        assert item.target is not None
        try:
            if not item.target.parent.exists():
                item.target.parent.mkdir(parents=True, exist_ok=True)
                report.created_dirs.append(item.target.parent)
            if item.target.resolve() == item.source.resolve():
                continue
            if move:
                shutil.move(str(item.source), str(item.target))
                report.moved += 1
            else:
                shutil.copy2(item.source, item.target)
                report.copied += 1
            report.bytes_done += item.size_bytes
        except OSError as exc:
            report.errors.append(f"{item.source}: {exc}")
    return report


def catalog_keys(catalog) -> set[str]:
    """Episode keys already present, so an import can report what is genuinely new."""
    return {episode.key for episode in catalog.episodes()}


def season_episode_index(episodes: Iterable[EpisodeRef]) -> dict[str, set[int]]:
    """``{show_id: {episode numbers}}`` for a quick "did this land?" check."""
    index: dict[str, set[int]] = {}
    for episode in episodes:
        index.setdefault(episode.show_id, set()).add(episode.episode)
    return index


def suggest_slot(duration_ms: int) -> str:
    """The tightest grid family that holds an episode without long dead air.

    Not a guess about the show: it is arithmetic on the file's own length, which
    is what the planner's "slot is mostly break" warning is complaining about.
    """
    minutes = duration_ms / 60_000
    if minutes <= 13:
        return "short"
    if minutes <= 25:
        return "half"
    return "hour"


def shows_yaml_snippet(
    show_id: str,
    title: str,
    *,
    slot: str = "half",
    eligibility: str = "always",
    airing_from: str | None = None,
    weight: float = 1.0,
) -> str:
    """A paste-ready ``shows.yaml`` entry, indented for the ``shows:`` mapping."""
    lines = [
        f"  {show_id}:",
        f'    title: "{title}"',
        f"    folder: shows/{show_id}",
        f"    family: {slot}",
        f"    slot: {slot}",
    ]
    if eligibility == "window" and airing_from:
        lines.append(f'    eligibility: window')
        lines.append(f'    airing_from: "{airing_from}"')
    else:
        lines.append("    eligibility: always")
    lines.append("    aliases: []")
    lines.append(f"    weight: {weight:g}")
    return "\n".join(lines)


__all__ = [
    "ALREADY",
    "COLLISION",
    "Candidate",
    "DUPLICATE",
    "EXTRA",
    "ImportReport",
    "OK",
    "TOO_SMALL",
    "UNKNOWN_SHOW",
    "UNPARSED",
    "apply_import",
    "build_alias_index",
    "catalog_keys",
    "is_junk",
    "match_show",
    "plan_import",
    "season_episode_index",
    "shows_yaml_snippet",
    "suggest_slot",
    "target_name",
]
