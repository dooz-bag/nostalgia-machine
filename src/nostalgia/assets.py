"""Assets: the things that can air.

A bump, a commercial, an ident and an episode all reduce to the same shape once
catalogued, which is what lets one scoring function serve every break slot.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Literal, Sequence

AssetKind = Literal["bump", "ad", "ident", "episode"]

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
    )


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
