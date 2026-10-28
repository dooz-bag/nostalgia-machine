"""Assets: the things that can air.

A bump, a commercial, an ident and an episode all reduce to the same shape once
catalogued, which is what lets one scoring function serve every break slot.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Literal, Sequence

AssetKind = Literal["bump", "ad", "ident", "episode", "extra"]

NEUTRAL = "neutral"


@dataclass(frozen=True, slots=True)
class Asset:
    key: str
    path: str
    kind: AssetKind
    duration_ms: int

    subject: str | None = None
    """Show id a bump names; ``None``/``neutral`` for show-agnostic bumps."""

    position: str | None = None
    """``INTO``/``OUT``/``NEXT``/``AFTER``/``ANY`` when known."""

    era_from: int | None = None
    era_to: int | None = None
    family: str | None = None
    """Show family the bump is styled for, e.g. ``sitcom`` or ``anime``."""

    tag_source: str = "folder"
    """``folder``, ``ocr``, ``manual`` or ``generated``."""

    confidence: float = 1.0
    brand: str | None = None
    """Commercial advertiser, used only for display."""

    extra: dict[str, str] = field(default_factory=dict, compare=False)

    lavfi_params: str | None = None
    """Set for synthetic assets rendered by ffmpeg (``lavfi``) instead of a file."""

    disabled: bool = False
    """Excluded from planning because it was marked bad on the web page.

    A disabled asset stays catalogued and keeps its tags and history: the file is
    still on disk, and "this spot is broken" is a judgement about the *print*, not
    about the media. Hiding it from ``assets()`` is what takes it out of the pool
    without a rescan, so a bad commercial stops airing the moment it is clicked.
    """

    contains_multiple_spots: bool | None = None
    """Whether the file actually holds several commercials back to back.

    ``None`` means not examined. A "commercial" that is really a whole recorded
    break is the main source of jolting playback: it airs as a single item, so a
    60-second pod becomes one 70-second clip and has to be chopped to fit.
    """

    @property
    def is_neutral_bump(self) -> bool:
        return self.kind == "bump" and self.subject in (None, "", NEUTRAL)

    @property
    def is_generated(self) -> bool:
        return self.tag_source == "generated"

    @property
    def is_synthetic(self) -> bool:
        """True for assets with no file on disk, e.g. a black filler card."""
        return bool(self.lavfi_params)

    @property
    def is_multi_spot(self) -> bool:
        """True when this "commercial" is really a whole recorded break.

        Such a file has no single product in it, so the pod arithmetic treats it
        as one 70-second spot and has to cut it. Skipping these is the difference
        between a break that flows and one that lurches.
        """
        return bool(self.contains_multiple_spots)

    def era_covers(self, year: int, drift: int = 0) -> bool:
        """Whether this asset may air in *year* (``drift`` widens the window)."""
        if self.era_from is None:
            return True
        end = self.era_to if self.era_to is not None else self.era_from
        return self.era_from - drift <= year <= end + drift


def asset_from_row(row) -> Asset:
    """Build an :class:`Asset` from a catalog row (``sqlite3.Row`` or mapping)."""
    get = row.__getitem__ if not hasattr(row, "get") else row.get
    return Asset(
        key=get("key"),
        path=get("path"),
        kind=get("kind"),
        duration_ms=int(get("duration_ms") or 0),
        subject=get("subject"),
        position=get("position"),
        era_from=get("era_from"),
        era_to=get("era_to"),
        family=get("family"),
        tag_source=get("tag_source") or "folder",
        confidence=float(get("confidence") if get("confidence") is not None else 1.0),
        brand=get("brand"),
        extra={},
        disabled=flag_from_row(get, "disabled"),
        contains_multiple_spots=flag_from_row(get, "contains_multiple_spots"),
    )


def flag_from_row(get, name: str) -> bool | None:
    """Read a nullable INTEGER flag without raising on an un-migrated catalog."""
    try:
        value = get(name)
    except (IndexError, KeyError, TypeError):
        return None
    return None if value is None else bool(value)


def synthetic_filler(seconds: float) -> Asset:
    """A black, silent card of exactly *seconds*.

    A tail remainder of, say, 1.4 s cannot be filled from real 15/30-second spots,
    and chopping a commercial down to 1.4 s looks broken. A short black card is
    what a real station does here, and it is the only item that lands on the
    required length exactly.
    """
    duration_ms = round(seconds * 1000)
    return Asset(
        key=f"synthetic:black:{duration_ms}",
        path="",
        kind="ident",
        duration_ms=duration_ms,
        lavfi_params=f"color=c=black:s=640x480:r=30000/1001:d={duration_ms / 1000:.3f}",
        tag_source="generated",
    )


def total_duration(assets: Iterable[Asset]) -> int:
    return sum(asset.duration_ms for asset in assets)


def index_by_key(assets: Sequence[Asset]) -> dict[str, Asset]:
    return {asset.key: asset for asset in assets}


__all__ = [
    "Asset",
    "AssetKind",
    "NEUTRAL",
    "asset_from_row",
    "index_by_key",
    "synthetic_filler",
    "total_duration",
]
