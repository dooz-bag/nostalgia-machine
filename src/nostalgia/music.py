"""The sound under a generated bump.

A real [adult swim] card was never silent: a few seconds of a music bed -- ambient,
electronic, odd -- under the text. A silent generated card is the biggest tell that
it is not the real thing.

This project does not ship music and will not fetch any: the tracks on those cards
are other people's work, and the repository is meant to be shared. There are three
sources, all of them yours and all kept under ``media/`` (which is never
committed):

* **Harvested.** The real bumps in your own archive are mostly show footage, but a
  minority are text on black with a music bed and nothing else. :func:`harvest`
  finds those and extracts just the audio. They are, by construction, the sound the
  real thing used.
* **The bumps themselves.** :func:`load_bump_beds` reads the audio straight out of
  the archive bumps, so a card can be bedded even when nothing was harvested. This
  is the widest source: harvesting only ever found beds in text-on-black cards,
  while every bump has audio. Beds carry the show they came from, and a card only
  borrows from its own show or from a bump that names no show.
* **Dropped in.** Anything you put in ``media/music/`` -- a track you own, a library
  loop -- is used as is.

Either way a bed is chosen deterministically per card, so a re-render never quietly
changes what a card sounds like.
"""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Sequence

from .conform import ConformParams, measure_loudness
from .rng import rng_for

AUDIO_EXTENSIONS = frozenset({".mp3", ".wav", ".flac", ".m4a", ".aac", ".ogg", ".opus", ".mka", ".wma"})

#: Beds sit under text, not over it. Quieter than the programme loudness, so the
#: streamer's normaliser (which lifts everything towards -16) still leaves the card
#: reading as a card rather than a music video.
BED_LUFS = -18.0

#: A bed shorter than this cannot cover a card, and looping a one-second blip under
#: four seconds of text sounds like a fault.
MIN_BED_SECONDS = 3.0

#: A dark frame is one whose average luma is this low (0-255).
DARK_LUMA = 24
#: A "text card" is at least this fraction dark frames.
DARK_SHARE = 0.60
#: Below this an audio track is silence, not a bed.
BED_FLOOR_LUFS = -50.0

INDEX_NAME = "index.json"


@dataclass(frozen=True, slots=True)
class Bed:
    path: Path
    duration_s: float
    era: int | None = None
    source: str = "folder"
    subject: str | None = None
    """The show this bed came from, when it came from a real bump.

    A card may borrow the sound of a bump about its *own* show, or of a neutral
    one. A bed from a different show is never used: those bumps are mostly a
    character talking, and a voice naming another programme under a card about
    this one is a worse lie than silence. ``None`` means "fits any card".
    """


@dataclass
class MusicLibrary:
    root: Path
    beds: list[Bed] = field(default_factory=list)

    def __bool__(self) -> bool:
        return bool(self.beds)

    def pick(
        self,
        *,
        year: int | None,
        seed_parts: Sequence[object] = (),
        subject: str | None = None,
        allow_other: bool = False,
    ) -> Bed | None:
        """A bed for one card. Same inputs, same bed -- always.

        Preference order: a bed from the card's own show, then a neutral bed, then
        (only with *allow_other*) anything. Within a tier an era match wins, so a
        2003 card sounds like 2003, and untagged beds fit anywhere.

        Only the *best non-empty* tier is drawn from, and any bed in it is equally
        likely. Falling through the tiers per card would mean a channel with show
        bumps never used a neutral one, and neutral beds are what a real break
        mostly sounded like anyway.

        ``None`` when nothing is eligible: a card about a show with no bumps of its
        own and no neutral beds falls back to silence rather than borrowing a
        different show's dialogue.
        """
        candidates = self.candidates(
            year=year, seed_parts=seed_parts, subject=subject, allow_other=allow_other
        )
        if not candidates:
            return None
        tier = candidates[0][0]
        pool = [bed for tier_of, bed in candidates if tier_of == tier]
        return rng_for("bed", year or 0, *seed_parts, "tier", tier).choice(pool)

    def candidates(
        self,
        *,
        year: int | None,
        seed_parts: Sequence[object] = (),
        subject: str | None = None,
        allow_other: bool = False,
    ) -> list[tuple[str, Bed]]:
        """Every eligible bed for a card as ``(tier, bed)``, best first.

        The list is what a re-render walks when the first bed turns out to be silent
        in the stretch it would need: the order is deterministic, so the fallback is
        reproducible rather than a different card on every run.
        """
        usable = [bed for bed in self.beds if bed.duration_s >= MIN_BED_SECONDS]
        if not usable:
            return []
        if year is not None:
            near = [b for b in usable if b.era is None or abs(b.era - year) <= 2]
            usable = near or usable

        tiers: list[tuple[str, list[Bed]]] = []
        if subject:
            tiers.append(("show", [b for b in usable if b.subject == subject]))
        tiers.append(("neutral", [b for b in usable if b.subject is None]))
        if allow_other:
            tiers.append(
                (
                    "other",
                    [
                        b
                        for b in usable
                        if b.subject is not None and b.subject != subject
                    ],
                )
            )

        ordered: list[tuple[str, Bed]] = []
        for name, tier in tiers:
            if not tier:
                continue
            pool = sorted(tier, key=lambda bed: str(bed.path))
            rng_for("bed-order", name, year or 0, *seed_parts).shuffle(pool)
            ordered.extend((name, bed) for bed in pool)
        return ordered

    def offset_for(self, bed: Bed, length_s: float, seed_parts: Sequence[object] = ()) -> float:
        """Where in the bed to start, so two cards on one track do not match."""
        room = max(0.0, bed.duration_s - length_s)
        if room <= 0:
            return 0.0
        return round(rng_for("bed-offset", str(bed.path), *seed_parts).uniform(0, room), 2)


# -- the index -----------------------------------------------------------------------


def _read_index(root: Path) -> dict:
    try:
        return json.loads((root / INDEX_NAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _write_index(root: Path, index: dict) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / INDEX_NAME).write_text(json.dumps(index, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _duration(path: Path, ffprobe: str | None) -> float:
    if not ffprobe:
        return 0.0
    run = subprocess.run(
        [ffprobe, "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
        capture_output=True, text=True, check=False,
    )
    try:
        return max(0.0, float(run.stdout.strip()))
    except ValueError:
        return 0.0


def music_roots(media_root: Path) -> list[Path]:
    """Where beds live: the folder you drop files in, and where harvests are written.

    Two places because the media mount is read-only to the app, apart from
    ``derived/``. A harvest can only be written there, and your own files stay put.
    """
    return [media_root / "music", media_root / "derived" / "music"]


def _subject_from_source(source: object, path: Path, root: Path) -> str | None:
    """Which show a bed came from, if any.

    A harvest records the bump it was taken from (``harvested:bump:<show>/<file>``),
    which is the only reliable record: the bed itself is written flat into
    ``harvested/`` so its own folder says nothing. Files you drop in have no
    subject unless you put them in a folder named after a show.
    """
    from .sidecars import subject_from_folder

    text = str(source or "")
    if text.startswith("harvested:"):
        key = text.split("harvested:", 1)[1]
        _, separator, rest = key.partition(":")  # "bump" then the relative path
        if separator:
            head = rest.split("/", 1)[0]
            return subject_from_folder(head)
    try:
        head = path.relative_to(root).parts[0]
    except (ValueError, IndexError):
        return None
    return subject_from_folder(head) if head != path.name else None


def load_bump_beds(
    assets: Iterable, *, min_seconds: float = MIN_BED_SECONDS
) -> list[Bed]:
    """Beds taken from the real bumps themselves.

    The archive bumps *are* the sound the channel is trying to reproduce, and using
    the files directly needs no extraction step and no second index to go stale:
    ffmpeg reads the audio of a video input as readily as of an ``.m4a``.

    Only real cards are considered (a generated card has nothing of its own to
    borrow) and only ones long enough to cover a card. Each bed remembers the show
    it came from, so a card about a show is never scored with another show's
    dialogue.
    """
    beds: list[Bed] = []
    seen: set[Path] = set()
    for asset in assets:
        if getattr(asset, "is_generated", False):
            continue
        path = Path(str(getattr(asset, "path", "") or ""))
        if not path.is_file() or path in seen:
            continue
        seconds = float(getattr(asset, "duration_ms", 0) or 0) / 1000.0
        if seconds < min_seconds:
            continue
        seen.add(path)
        beds.append(
            Bed(
                path=path,
                duration_s=seconds,
                era=getattr(asset, "era_from", None),
                source=f"bump:{getattr(asset, 'key', path.name)}",
                # A neutral bump is stored with the literal subject "neutral", which
                # is a *category*, not a show: it fits any card.
                subject=_show_of(asset),
            )
        )
    return beds


def _show_of(asset: object) -> str | None:
    """The show a bump belongs to, or ``None`` when it names no show."""
    subject = getattr(asset, "subject", None)
    if not subject or str(subject).lower() in ("neutral", "none", "null", "-"):
        return None
    return str(subject)


def load_library(
    root: Path | Sequence[Path] | None, *, ffprobe: str | None = None
) -> MusicLibrary:
    """Every bed under *root* (one folder or several). Missing folders are empty."""
    if root is None:
        return MusicLibrary(root=Path("."))
    roots = [Path(root)] if isinstance(root, (str, Path)) else [Path(r) for r in root]
    beds: list[Bed] = []
    for one in roots:
        beds.extend(_load_root(one, ffprobe))
    return MusicLibrary(root=roots[0] if roots else Path("."), beds=beds)


def _load_root(root: Path, ffprobe: str | None) -> list[Bed]:
    if not root.is_dir():
        return []
    index = _read_index(root)
    files = index.setdefault("files", {})
    beds: list[Bed] = []
    changed = False
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.suffix.lower() not in AUDIO_EXTENSIONS:
            continue
        relative = path.relative_to(root).as_posix()
        stat = path.stat()
        entry = files.get(relative) or {}
        if entry.get("size") != stat.st_size or not entry.get("duration"):
            entry = {**entry, "size": stat.st_size, "duration": round(_duration(path, ffprobe), 3)}
            files[relative] = entry
            changed = True
        if entry["duration"] <= 0:
            continue
        beds.append(
            Bed(path=path, duration_s=float(entry["duration"]), era=entry.get("era"),
                source=entry.get("source", "folder"),
                subject=_subject_from_source(entry.get("source"), path, root))
        )
    if changed:
        try:
            _write_index(root, index)
        except OSError:
            pass  # a read-only folder just re-probes next time
    return beds


# -- laying a bed under a card ----------------------------------------------------------


def segment_filter(
    path: Path,
    offset_s: float,
    length_s: float,
    ffmpeg: str,
    *,
    fade_in_s: float = 0.15,
    fade_out_s: float = 0.6,
) -> str | None:
    """An audio filter chain that sits *length_s* of a bed at the right level.

    Measured on the *segment* rather than the whole track, because that is the part
    that is heard. ``None`` when the segment is silent: a card with a silent bed is
    better off with no bed than with hiss turned up.
    """
    params = ConformParams(target_lufs=BED_LUFS, true_peak=-2.0, lra=7.0)
    loud = measure_loudness(path, ffmpeg, params, start=offset_s, duration=length_s)
    if loud is None or loud["input_i"] < BED_FLOOR_LUFS:
        return None
    fade_out_s = min(fade_out_s, length_s / 2)
    return ",".join(
        [
            "aresample=48000",
            "aformat=sample_fmts=fltp:channel_layouts=stereo",
            f"loudnorm=I={params.target_lufs}:TP={params.true_peak}:LRA={params.lra}"
            f":measured_I={loud['input_i']}:measured_TP={loud['input_tp']}"
            f":measured_LRA={loud['input_lra']}:measured_thresh={loud['input_thresh']}"
            f":offset={loud['target_offset']}:linear=true",
            "aresample=48000",
            f"afade=t=in:st=0:d={fade_in_s:.3f}",
            f"afade=t=out:st={max(0.0, length_s - fade_out_s):.3f}:d={fade_out_s:.3f}",
        ]
    )


# -- harvesting beds from your own bumps ------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CardAnalysis:
    dark_share: float
    loudness: float | None

    @property
    def is_text_card_with_bed(self) -> bool:
        return (
            self.dark_share >= DARK_SHARE
            and self.loudness is not None
            and self.loudness > BED_FLOOR_LUFS
        )


def analyse_bump(path: Path, ffmpeg: str) -> CardAnalysis:
    """How dark the picture is, and whether there is a bed to take."""
    video = subprocess.run(
        [ffmpeg, "-hide_banner", "-nostats", "-i", str(path), "-an", "-vf",
         "scale=160:-2,signalstats,metadata=print:key=lavfi.signalstats.YAVG", "-f", "null", "-"],
        capture_output=True, text=True, check=False,
    ).stderr
    lumas = [float(x) for x in re.findall(r"YAVG=([\d.]+)", video)]
    dark = (sum(1 for y in lumas if y < DARK_LUMA) / len(lumas)) if lumas else 0.0
    loud = measure_loudness(path, ffmpeg, ConformParams(target_lufs=BED_LUFS, true_peak=-2.0, lra=7.0))
    return CardAnalysis(dark_share=dark, loudness=loud["input_i"] if loud else None)


@dataclass
class HarvestReport:
    scanned: int = 0
    taken: list[str] = field(default_factory=list)
    skipped: int = 0
    failed: list[tuple[str, str]] = field(default_factory=list)


def harvest(
    bumps: Iterable,
    out_dir: Path,
    *,
    ffmpeg: str,
    ffprobe: str | None = None,
    limit: int | None = None,
    on_progress: Callable[[int, int], None] | None = None,
) -> HarvestReport:
    """Extract the audio bed from every text-on-black bump in *bumps*.

    Takes only what is clearly a text card carrying a bed. Show footage is skipped
    on purpose: its audio is dialogue and effects, and laying a character's voice
    under a card about a different show would be worse than silence.

    Each bed is level-matched on the way out, so every file in the library sits at
    the same loudness and a card does not depend on which one it drew.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    index = _read_index(out_dir.parent)
    files = index.setdefault("files", {})
    report = HarvestReport()
    todo = [asset for asset in bumps if not getattr(asset, "is_generated", False)]
    if limit:
        todo = todo[:limit]

    params = ConformParams(target_lufs=BED_LUFS, true_peak=-2.0, lra=7.0)
    for number, asset in enumerate(todo, start=1):
        report.scanned += 1
        if on_progress:
            on_progress(number, len(todo))
        source = Path(asset.path)
        stem = re.sub(r"[^A-Za-z0-9._-]+", "_", source.stem)[:70]
        target = out_dir / f"{stem}.m4a"
        relative = target.relative_to(out_dir.parent).as_posix()
        if target.is_file() and relative in files:
            report.skipped += 1
            continue
        try:
            card = analyse_bump(source, ffmpeg)
            if not card.is_text_card_with_bed:
                report.skipped += 1
                continue
            loud = measure_loudness(source, ffmpeg, params)
            if loud is None:
                report.skipped += 1
                continue
            filt = (
                f"loudnorm=I={params.target_lufs}:TP={params.true_peak}:LRA={params.lra}"
                f":measured_I={loud['input_i']}:measured_TP={loud['input_tp']}"
                f":measured_LRA={loud['input_lra']}:measured_thresh={loud['input_thresh']}"
                f":offset={loud['target_offset']}:linear=true,aresample=48000"
            )
            partial = target.with_suffix(".part.m4a")
            run = subprocess.run(
                [ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin", "-y", "-i", str(source),
                 "-vn", "-af", filt, "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2",
                 "-map_metadata", "-1", str(partial)],
                capture_output=True, text=True, check=False,
            )
            if run.returncode != 0 or not partial.is_file():
                partial.unlink(missing_ok=True)
                report.failed.append((asset.key, (run.stderr or "ffmpeg failed").strip()[-160:]))
                continue
            partial.replace(target)
            files[relative] = {
                "size": target.stat().st_size,
                "duration": round(_duration(target, ffprobe), 3),
                "era": getattr(asset, "era_from", None),
                "source": f"harvested:{asset.key}",
            }
            report.taken.append(asset.key)
        except (OSError, ValueError) as exc:
            report.failed.append((asset.key, f"{type(exc).__name__}: {exc}"))
    _write_index(out_dir.parent, index)
    return report


__all__ = [
    "AUDIO_EXTENSIONS",
    "BED_LUFS",
    "Bed",
    "CardAnalysis",
    "HarvestReport",
    "MIN_BED_SECONDS",
    "MusicLibrary",
    "analyse_bump",
    "harvest",
    "load_bump_beds",
    "load_library",
    "music_roots",
    "segment_filter",
]
