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

import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Mapping, Sequence

from .naming import normalise_for_match, parse_episode, season_from_dir
from .probe import VIDEO_EXTENSIONS
from .rotation import EpisodeRef

OK = "ok"
UNPARSED = "unparsed"
COLLISION = "collision"
DUPLICATE = "duplicate"
TOO_SMALL = "too-small"
UNKNOWN_SHOW = "unknown-show"
ALREADY = "already"

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
)


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
    """
    token = normalise_for_match(name)
    if not token:
        return None
    if token in aliases:
        return aliases[token]
    compact = token.replace(" ", "")
    if compact in aliases:
        return aliases[compact]

    best: tuple[int, str] | None = None
    for alias, show_id in aliases.items():
        if len(alias) < 3:
            continue
        if token.startswith(alias) or alias.startswith(token):
            if best is None or len(alias) > best[0]:
                best = (len(alias), show_id)
    return best[1] if best else None


def is_junk(path: Path, *, min_bytes: int) -> tuple[bool, str]:
    """Whether a file is certainly not an episode."""
    if path.suffix.lower() not in VIDEO_EXTENSIONS:
        return True, "not a video file"
    lowered = path.name.lower()
    for token in _JUNK_TOKENS:
        if token in lowered:
            return True, f"looks like {token}"
    try:
        size = path.stat().st_size
    except OSError as exc:
        return True, str(exc)
    if min_bytes and size < min_bytes:
        return True, f"only {size // 1024} KB"
    return False, ""


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
    default_show: str | None = None,
    recursive: bool = True,
) -> list[Candidate]:
    """Work out where every file would go, without touching anything.

    ``default_show`` forces every file under one show id, which is what you want
    when you already know you are importing a single series and the folder name
    is unhelpful.
    """
    source_root = Path(source_root)
    dest_shows_root = Path(dest_shows_root)
    if not source_root.is_dir():
        raise NotADirectoryError(f"not a folder: {source_root}")

    aliases = build_alias_index(shows)
    files = sorted(
        path
        for path in (source_root.rglob("*") if recursive else source_root.glob("*"))
        if path.is_file()
    )

    candidates: list[Candidate] = []
    taken: dict[str, Path] = {}
    for path in files:
        size = 0
        try:
            size = path.stat().st_size
        except OSError:
            pass

        junk, reason = is_junk(path, min_bytes=min_bytes)
        if junk:
            candidates.append(
                Candidate(path, None, None, None, None, TOO_SMALL, reason, size)
            )
            continue

        named = default_show or _source_show_name(path, source_root)
        show_id = match_show(named, aliases)
        if show_id is None:
            show_id = match_show(path.stem, aliases)
        if show_id is None:
            candidates.append(
                Candidate(
                    path,
                    None,
                    None,
                    None,
                    None,
                    UNKNOWN_SHOW,
                    f"no roster match for {named!r}",
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

        target = (
            dest_shows_root
            / show_id
            / f"Season {parsed.season:02d}"
            / target_name(show_id, parsed.season, parsed.episode, path.suffix)
        )
        key = f"{show_id}:s{parsed.season:02d}e{parsed.episode:03d}"

        if key in taken:
            # Two files claim the same slot. Keeping the larger one is a guess,
            # so it is reported rather than silently applied.
            candidates.append(
                Candidate(
                    path,
                    show_id,
                    parsed.season,
                    parsed.episode,
                    target,
                    DUPLICATE,
                    f"same episode as {taken[key].name}",
                    size,
                )
            )
            continue
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
        taken[key] = path
        candidates.append(
            Candidate(
                path, show_id, parsed.season, parsed.episode, target, OK, "", size
            )
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
