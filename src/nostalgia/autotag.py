"""Advisory bump auto-tagging.

[adult swim] bumps are frequently abstract: a black card with one line of text, a
still frame, a logo. OCR on a four-second clip is easy to get wrong, so this module
is deliberately **advisory**. It proposes a subject, position and era, writes them
at low confidence, and leaves the confirmation to a human in the review queue.

Design consequences:

* **Never overwrites a confirmed tag.** The whole point is to reduce review work,
  not to create silent errors.
* **Every proposal carries provenance.** `source: ocr` plus the confidence score,
  so a wrong guess is visible rather than authoritative.
* **Degrades to nothing.** With no ffmpeg or no Tesseract it returns an empty
  result instead of failing a scan or an emit.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Sequence

from .catalog import Catalog
from .naming import normalise_for_match, year_from_name
from .sidecars import BumpTag
from .sidecars import write as write_tag

#: Words that appear on bumps but are never the subject.
STOPWORDS = {
    "adult",
    "swim",
    "next",
    "coming",
    "up",
    "we",
    "now",
    "return",
    "to",
    "stay",
    "tuned",
    "on",
    "the",
    "a",
    "tonight",
    "later",
    "and",
    "you",
    "are",
    "watching",
    "back",
    "right",
    "after",
    "these",
    "messages",
}

#: Text that identifies which moment a card describes.
#:
#: ``OUT`` and ``AFTER`` deliberately share no cue. "We now return to" only ever
#: appears on a mid-break card -- it is a promise that the show is coming back --
#: so reading it as ``AFTER`` (after the episode has ended) tags the card for a
#: slot it would be a lie in, and leaves it unusable where it belongs.
POSITION_CUES: dict[str, tuple[str, ...]] = {
    "NEXT": ("next", "coming up", "up next", "later"),
    "OUT": ("we now return", "return to", "back to", "right back"),
    "AFTER": ("you just watched", "just watched", "you watched"),
    "INTO": ("stay tuned", "coming up", "up next", "next"),
}


@dataclass(frozen=True, slots=True)
class Suggestion:
    key: str
    subject: str | None
    position: str | None
    era: int | None
    confidence: float
    text: str = ""
    reasons: list[str] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class AutoTagResult:
    examined: int = 0
    proposed: int = 0
    skipped_confirmed: int = 0
    skipped_no_tools: bool = False
    suggestions: tuple[Suggestion, ...] = ()


def available() -> bool:
    """Whether OCR is actually usable here."""
    if shutil.which("ffmpeg") is None:
        return False
    try:
        import pytesseract  # noqa: F401
    except ImportError:
        return False
    try:
        import pytesseract

        pytesseract.get_tesseract_version()
    except Exception:  # noqa: BLE001 - binary missing or unreadable
        return False
    return True


def sample_text(path: Path, *, frames: int = 3, ffmpeg: str | None = None) -> str:
    """OCR a few frames spread across the clip, and concatenate what it finds."""
    try:
        import pytesseract
        from PIL import Image
    except ImportError:
        return ""

    binary = ffmpeg or shutil.which("ffmpeg")
    if binary is None or not path.is_file():
        return ""

    duration = _duration_seconds(path) or 4.0
    texts: list[str] = []
    for index in range(max(1, frames)):
        moment = duration * (index + 1) / (frames + 1)
        frame = _grab_frame(binary, path, moment)
        if frame is None:
            continue
        try:
            with Image.open(frame) as image:
                # Cards are light text on dark ground; inverting helps Tesseract.
                grey = image.convert("L")
                texts.append(pytesseract.image_to_string(grey) or "")
        except Exception:  # noqa: BLE001 - OCR is best-effort by design
            continue
    return " ".join(texts)


def propose(
    *,
    key: str,
    text: str,
    show_titles: dict[str, str],
    duration_ms: int,
) -> Suggestion:
    """Turn OCR text into a subject/position/era suggestion."""
    cleaned = " ".join(text.split())
    normalised = normalise_for_match(cleaned)
    reasons: list[str] = []

    subject: str | None = None
    best_score = 0.0
    for show_id, title in show_titles.items():
        for candidate in {show_id.replace("-", " "), title}:
            needle = normalise_for_match(candidate)
            if not needle:
                continue
            if needle in normalised:
                # Longer titles are more specific matches.
                score = len(needle) / max(1, len(normalised))
                if score > best_score:
                    best_score = score
                    subject = show_id
                    reasons.append(f"title {candidate!r} in text")
            else:
                words = [word for word in needle.split() if word not in STOPWORDS and len(word) > 3]
                hits = sum(1 for word in words if word in normalised)
                if words and hits == len(words):
                    score = 0.5 * len(words) / max(1, len(normalised.split()))
                    if score > best_score:
                        best_score = score
                        subject = show_id
                        reasons.append(f"all words of {candidate!r} in text")

    position = None
    for candidate, cues in POSITION_CUES.items():
        if any(cue in normalised for cue in cues):
            position = candidate
            reasons.append(f"position cue for {candidate}")
            break

    era = year_from_name(cleaned)
    if era:
        reasons.append(f"year {era} in text")

    # A four-second card with no legible show name is a neutral or abstract card;
    # say so instead of guessing wildly.
    confidence = round(min(0.95, best_score * 2.0), 2)
    if subject is None and position is None:
        confidence = 0.0

    return Suggestion(
        key=key,
        subject=subject,
        position=position,
        era=era,
        confidence=confidence,
        text=cleaned[:200],
        reasons=reasons,
    )


def run(
    catalog: Catalog,
    *,
    show_titles: dict[str, str],
    min_confidence: float = 0.3,
    limit: int | None = None,
    write: bool = False,
    ffmpeg: str | None = None,
) -> AutoTagResult:
    """Propose tags for unconfirmed bumps.

    With ``write=False`` (the default) this changes nothing: it is a report.
    """
    if not available():
        return AutoTagResult(skipped_no_tools=True)

    pending = catalog.tags_pending_review()
    suggestions: list[Suggestion] = []
    examined = 0
    skipped = 0

    for row in pending:
        if limit is not None and examined >= limit:
            break
        if row["confirmed"]:
            skipped += 1
            continue
        path = Path(row["path"])
        examined += 1
        text = sample_text(path, ffmpeg=ffmpeg)
        if not text.strip():
            continue
        suggestion = propose(
            key=row["key"],
            text=text,
            show_titles=show_titles,
            duration_ms=row["duration_ms"] or 0,
        )
        suggestions.append(suggestion)
        if write and suggestion.confidence >= min_confidence:
            write_tag(
                path,
                BumpTag(
                    subject=suggestion.subject,
                    position=suggestion.position,
                    era=suggestion.era,
                    era_to=suggestion.era,
                    source="ocr",
                    confidence=suggestion.confidence,
                    # Not confirmed: it still belongs in the review queue.
                    confirmed=False,
                ),
            )

    return AutoTagResult(
        examined=examined,
        proposed=len(suggestions),
        skipped_confirmed=skipped,
        suggestions=tuple(suggestions),
    )


# -- helpers ---------------------------------------------------------------


def _duration_seconds(path: Path) -> float | None:
    from .probe import ProbeError, find_ffprobe, probe

    if find_ffprobe() is None:
        return None
    try:
        return probe(path).duration_ms / 1000
    except ProbeError:
        return None


def _grab_frame(binary: str, path: Path, moment: float) -> Path | None:
    target = path.with_name(f".{path.stem}.ocr.png")
    try:
        result = subprocess.run(
            [
                binary,
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-ss",
                f"{moment:.3f}",
                "-i",
                str(path),
                "-frames:v",
                "1",
                str(target),
            ],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
    except OSError:
        return None
    if result.returncode != 0 or not target.is_file():
        return None
    return target


def cleanup(frames: Iterable[Path]) -> None:
    for frame in frames:
        frame.unlink(missing_ok=True)


__all__ = [
    "AutoTagResult",
    "STOPWORDS",
    "Suggestion",
    "available",
    "propose",
    "run",
    "sample_text",
]
