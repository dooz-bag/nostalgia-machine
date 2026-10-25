"""Bump tag sidecars.

Folder convention (``bumps/aqua-teen/foo.mp4``) covers most libraries, but it
cannot express *position* or *era*, and it breaks for folders that are not shows
(``bumps/generated/``, ``bumps/_review/``). A sidecar next to the file can say
everything::

    bumps/generated/2003-next-futurama.mp4
    bumps/generated/2003-next-futurama.mp4.tags.json
    {
      "subject": "futurama",
      "position": "NEXT",
      "era": 2003,
      "source": "generated",
      "confidence": 1.0
    }

This is also the file the web review queue writes and what ``nostalgia tag``
produces, so there is exactly one on-disk representation of "what this bump is".
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

SIDECAR_SUFFIX = ".tags.json"

POSITIONS = ("INTO", "OUT", "NEXT", "AFTER", "ANY")
SOURCES = ("folder", "ocr", "manual", "generated", "unknown")

#: Folder names under ``media/bumps`` that are not cheat-sheet show folders.
RESERVED_BUMP_FOLDERS = {"_review", "generated", "neutral", "_incoming"}


class SidecarError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class BumpTag:
    subject: str | None = None
    position: str | None = None
    era: int | None = None
    era_to: int | None = None
    family: str | None = None
    source: str = "manual"
    confidence: float = 1.0
    brand: str | None = None
    confirmed: bool = True

    def to_json(self) -> dict:
        payload: dict = {"source": self.source, "confidence": self.confidence}
        if self.subject is not None:
            payload["subject"] = self.subject
        if self.position is not None:
            payload["position"] = self.position
        if self.era is not None:
            payload["era"] = self.era
        if self.era_to is not None:
            payload["era_to"] = self.era_to
        if self.family is not None:
            payload["family"] = self.family
        if self.brand is not None:
            payload["brand"] = self.brand
        if self.confirmed:
            payload["confirmed"] = True
        return payload

    @classmethod
    def from_json(cls, payload: dict, *, default_source: str = "manual") -> "BumpTag":
        if not isinstance(payload, dict):
            raise SidecarError("tag sidecar must be a JSON object")
        position = payload.get("position")
        if position is not None:
            position = str(position).upper()
            if position not in POSITIONS:
                raise SidecarError(
                    f"position {position!r} must be one of {', '.join(POSITIONS)}"
                )
        source = str(payload.get("source", default_source))
        if source not in SOURCES:
            raise SidecarError(f"source {source!r} must be one of {', '.join(SOURCES)}")
        era = payload.get("era")
        return cls(
            subject=_clean_subject(payload.get("subject")),
            position=position,
            era=int(era) if era is not None else None,
            era_to=int(payload["era_to"]) if payload.get("era_to") is not None else None,
            family=payload.get("family"),
            source=source,
            confidence=float(payload.get("confidence", 1.0)),
            brand=payload.get("brand"),
            confirmed=bool(payload.get("confirmed", True)),
        )


def _clean_subject(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text or text.lower() in ("-", "neutral", "none", "null"):
        return "neutral" if text.lower() == "neutral" else None
    return text


def path_for(media_path: Path) -> Path:
    return media_path.with_name(media_path.name + SIDECAR_SUFFIX)


def read(media_path: Path) -> BumpTag | None:
    """Read a sidecar if one exists. Unreadable sidecars are reported, not ignored."""
    sidecar = path_for(media_path)
    if not sidecar.is_file():
        return None
    try:
        payload = json.loads(sidecar.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SidecarError(f"{sidecar.name}: {exc}") from exc
    return BumpTag.from_json(payload)


def write(media_path: Path, tag: BumpTag) -> Path:
    sidecar = path_for(media_path)
    sidecar.write_text(
        json.dumps(tag.to_json(), indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return sidecar


def subject_from_folder(folder_name: str | None) -> str | None:
    """The show id implied by a bump's folder, or ``None`` if it is not a show."""
    if not folder_name:
        return None
    if folder_name.lower() in RESERVED_BUMP_FOLDERS:
        return None
    return folder_name


__all__ = [
    "BumpTag",
    "POSITIONS",
    "RESERVED_BUMP_FOLDERS",
    "SIDECAR_SUFFIX",
    "SOURCES",
    "SidecarError",
    "path_for",
    "read",
    "subject_from_folder",
    "write",
]
