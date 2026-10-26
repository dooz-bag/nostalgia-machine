"""Generated bumps -- Phase 2, Tier 1.

The 2003-2007 [adult swim] ident bumps are *typographic black cards*: a line of
white text, a lot of negative space, occasionally a still behind it. There is no
photoreal imagery to synthesize, which is good news: a template renderer gets
almost all of the look with no model at all.

Two design rules keep this honest:

1. **Deterministic.** Styles are keyed by era and every parameter is a constant,
   so a rendered card is byte-identical across runs. Generated bumps therefore
   obey the same "same date, same channel" guarantee as the rest of the plan.
2. **Real always wins.** Generated cards enter the bump pool flagged
   ``source: generated`` and are only consulted when nothing real clears the
   score. They fill gaps; they never displace an asset you provided.
"""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Sequence

import yaml

from .assets import Asset
from .probe import find_ffprobe, probe
from .sidecars import BumpTag
from .sidecars import write as write_tag


class BumpGenError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class StylePack:
    """A single era's look, expressed as fractions of the frame."""

    key: str
    name: str
    font: str = "Arial"
    font_size: float = 0.058
    colour: str = "white"
    uppercase: bool = True
    tracking: float = 0.06
    margin_x: float = 0.14
    margin_y: float = 0.48
    align: str = "left"
    background: str = "black"
    letterbox: bool = False
    vignette: float = 0.0
    grain: float = 0.0
    hold_ms: int = 4200
    fade_ms: int = 500
    show_position_word: bool = True

    # -- the "this was not in the original run" mark ------------------------
    artifact: bool = True
    """Whether to stamp generated cards.

    A generated card sits in the same pool as 600 archive bumps and, if it is
    indistinguishable, it quietly rewrites history -- you can no longer tell what
    actually aired from what the renderer invented. The mark is deliberately
    small, low-contrast and in the corner the real cards never used, so it reads
    as broadcast grime to a viewer but is unmistakable when you look for it.
    """
    artifact_text: str = "SG"  
    """Wordmark in the artifact. SG = Space Ghost, the [adult swim] mascot."""
    artifact_opacity: float = 0.32
    """Low enough to look like a ghost of the picture rather than a caption."""
    artifact_size: float = 0.030
    """Fraction of frame height."""

    artifact_margin_x: float = 0.035
    artifact_margin_y: float = 0.115
    """Top-right by default: the real [adult swim] cards put their text left and
    kept the bottom-right for the network bug, so a mark up here is one the
    original run never had."""

    artifact_box: bool = True
    """Draw a thin box around the mark.

    The "askew" quality: a bordered tag in the corner reads as broadcast grime
    at a glance, but no archive card ever carried one, so it is unmistakable
    once you know to look. A drawn box rather than an image, so it costs nothing
    and cannot be lost in a transcode.
    """

    @classmethod
    def from_mapping(cls, key: str, raw: dict) -> "StylePack":
        return cls(
            key=key,
            name=str(raw.get("name", key)),
            font=str(raw.get("font", "Arial")),
            font_size=float(raw.get("font_size", 0.058)),
            colour=str(raw.get("colour", "white")),
            uppercase=bool(raw.get("uppercase", True)),
            tracking=float(raw.get("tracking", 0.06)),
            margin_x=float(raw.get("margin_x", 0.14)),
            margin_y=float(raw.get("margin_y", 0.48)),
            align=str(raw.get("align", "left")),
            background=str(raw.get("background", "black")),
            letterbox=bool(raw.get("letterbox", False)),
            vignette=float(raw.get("vignette", 0.0)),
            grain=float(raw.get("grain", 0.0)),
            hold_ms=int(raw.get("hold_ms", 4200)),
            fade_ms=int(raw.get("fade_ms", 500)),
            show_position_word=bool(raw.get("show_position_word", True)),
            artifact=bool(raw.get("artifact", True)),
            artifact_text=str(raw.get("artifact_text", "SG")),
            artifact_opacity=float(raw.get("artifact_opacity", 0.32)),
            artifact_size=float(raw.get("artifact_size", 0.030)),
            artifact_margin_x=float(raw.get("artifact_margin_x", 0.035)),
            artifact_margin_y=float(raw.get("artifact_margin_y", 0.115)),
            artifact_box=bool(raw.get("artifact_box", True)),
        )


@dataclass(frozen=True, slots=True)
class EraPack:
    styles: dict[str, StylePack]
    brand: dict[str, str] = field(default_factory=dict)
    position_words: dict[str, dict[str, str]] = field(default_factory=dict)

    def style_for(self, year: int | None) -> StylePack:
        """Nearest style at or before *year*; the earliest style otherwise."""
        if not self.styles:
            raise BumpGenError("no styles configured")
        keys = sorted(self.styles)
        if year is None:
            return self.styles[keys[-1]]
        chosen = keys[0]
        for key in keys:
            try:
                if int(key) <= year:
                    chosen = key
            except ValueError:
                continue
        return self.styles[chosen]

    def brand_for(self, style_key: str, default: str = "[adult swim]") -> str:
        return self.brand.get(style_key, default)

    def word_for(self, position: str, style_key: str) -> str:
        table = self.position_words.get(position.upper()) or {}
        return table.get(style_key) or table.get("default") or position.upper()


#: Artifact keys that may be set once at the top of the file and inherited by
#: every style, so the mark does not have to be repeated per era.
_ARTIFACT_DEFAULTS = (
    "artifact",
    "artifact_text",
    "artifact_opacity",
    "artifact_size",
    "artifact_margin_x",
    "artifact_margin_y",
    "artifact_box",
)


def load_era_packs(path: Path) -> EraPack:
    if not path.is_file():
        raise BumpGenError(f"missing era style file: {path}")
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    styles_raw = raw.get("styles") or {}
    if not styles_raw:
        raise BumpGenError(f"{path.name}: no styles defined")

    # The artifact is a channel-wide policy, not an era look, so it is declared
    # once at the top level and merged into each style. A style may still
    # override any key, which matters if one era should read differently.
    inherited = {key: raw[key] for key in _ARTIFACT_DEFAULTS if key in raw}
    styles_raw = {
        str(key): {**inherited, **(value or {})} for key, value in styles_raw.items()
    }
    styles = {
        str(key): StylePack.from_mapping(str(key), value or {})
        for key, value in styles_raw.items()
    }
    return EraPack(
        styles=styles,
        brand={str(k): str(v) for k, v in (raw.get("brand") or {}).items()},
        position_words={
            str(pos): {str(k): str(v) for k, v in (table or {}).items()}
            for pos, table in (raw.get("position_words") or {}).items()
        },
    )


@dataclass(frozen=True, slots=True)
class BumpSpec:
    """What a generated card should say."""

    subject: str
    position: str = "NEXT"
    year: int | None = None
    show_title: str | None = None
    clock: str | None = None
    """Optional wall clock, e.g. ``11:30``."""

    max_seconds: float | None = None


def cache_key(spec: BumpSpec, style: StylePack, width: int, height: int) -> str:
    parts = [
        style.key,
        spec.position.upper(),
        spec.subject,
        spec.show_title or "",
        spec.clock or "",
        f"{width}x{height}",
    ]
    safe = "-".join(_slug(part) for part in parts if part)
    return f"gen:{safe}"


def _slug(text: str) -> str:
    return "".join(char if char.isalnum() else "-" for char in text.lower()).strip("-")


def render(
    spec: BumpSpec,
    *,
    pack: EraPack,
    out_dir: Path,
    width: int = 1280,
    height: int = 720,
    ffmpeg: str | None = None,
    force: bool = False,
) -> Asset:
    """Render (or reuse) one bump card and return it as a pool asset."""
    style = pack.style_for(spec.year)
    key = cache_key(spec, style, width, height)
    out_dir.mkdir(parents=True, exist_ok=True)
    target = out_dir / f"{key.split(':', 1)[1]}.mp4"

    duration_ms = style.hold_ms
    if spec.max_seconds is not None:
        duration_ms = min(duration_ms, int(spec.max_seconds * 1000) - 1)

    if not target.exists() or force:
        binary = ffmpeg or _find_ffmpeg()
        if binary is None:
            raise BumpGenError(
                "ffmpeg is required to render bumps; install it or set ffmpeg_path"
            )
        command = _build_command(
            spec, style, pack, width=width, height=height, duration_ms=duration_ms
        )
        _run(binary, command, target)

    # The sidecar is what makes the card legible to the catalog: a generated
    # folder is not a show, so folder inference would tag every card "generated".
    write_tag(
        target,
        BumpTag(
            subject=None if spec.subject in ("", "neutral") else spec.subject,
            position=spec.position.upper(),
            era=spec.year,
            era_to=spec.year,
            source="generated",
            confidence=1.0,
            confirmed=True,
        ),
    )

    return Asset(
        key=key,
        path=str(target),
        kind="bump",
        duration_ms=_actual_duration(target, duration_ms),
        subject=None if spec.subject in ("", "neutral") else spec.subject,
        position=spec.position.upper(),
        era_from=spec.year,
        era_to=spec.year,
        tag_source="generated",
    )


def render_pool(
    specs: Sequence[BumpSpec],
    *,
    pack: EraPack,
    out_dir: Path,
    width: int = 1280,
    height: int = 720,
    ffmpeg: str | None = None,
    force: bool = False,
) -> list[Asset]:
    assets: list[Asset] = []
    for spec in specs:
        try:
            assets.append(
                render(
                    spec,
                    pack=pack,
                    out_dir=out_dir,
                    width=width,
                    height=height,
                    ffmpeg=ffmpeg,
                    force=force,
                )
            )
        except BumpGenError:
            raise
    return assets


WINDOWS_FONT_DIRS = (
    Path("C:/Windows/Fonts"),
    Path.home() / "AppData/Local/Microsoft/Windows/Fonts",
)

#: Candidate file names per configured family, most-desirable first.
FONT_FILES: dict[str, tuple[str, ...]] = {
    "arial": ("arial.ttf", "arialbd.ttf"),
    # Arial Narrow is an optional Windows component; Constantia is a condensed-ish
    # fallback that is always present, before dropping to plain Arial.
    "arial narrow": ("arialn.ttf", "constan.ttf", "arial.ttf"),
    "helvetica": ("arial.ttf", "Helvetica.ttc"),
    "univers": ("arial.ttf",),
    "verdana": ("verdana.ttf",),
    "tahoma": ("tahoma.ttf",),
    "dejavu sans": ("DejaVuSans.ttf",),
    "liberation sans": ("LiberationSans-Regular.ttf",),
}


def resolve_font(family: str) -> Path | None:
    """Find a usable font *file* for *family*.

    ``drawtext`` on Windows cannot resolve fontconfig family names -- it needs a
    real path -- so the family named in a style pack is mapped to a file here.
    That is what makes the renderer behave the same on Windows, on Linux and in a
    container.
    """
    candidates = FONT_FILES.get(family.strip().lower(), (f"{family}.ttf", family))
    for candidate in candidates:
        absolute = Path(candidate)
        if absolute.is_absolute() and absolute.is_file():
            return absolute
        for directory in WINDOWS_FONT_DIRS:
            found = directory / candidate
            if found.is_file():
                return found
        for directory in (
            Path("/usr/share/fonts"),
            Path("/usr/local/share/fonts"),
            Path.home() / ".fonts",
        ):
            if directory.is_dir():
                matches = sorted(directory.rglob(candidate))
                if matches:
                    return matches[0]
    return None


def _font_option(family: str) -> str:
    """``fontfile=...`` when a path resolves, else ``font=...``."""
    resolved = resolve_font(family)
    if resolved is None:
        return f"font={family}"
    # ffmpeg filter args treat ':' and '\' as special, so the path is escaped.
    escaped = str(resolved).replace("\\", "/").replace(":", "\\:")
    return f"fontfile='{escaped}'"


def _build_command(
    spec: BumpSpec,
    style: StylePack,
    pack: EraPack,
    *,
    width: int,
    height: int,
    duration_ms: int,
) -> list[str]:
    """ffmpeg arguments for one card.

    Text is drawn with ``drawtext`` on a generated background, because
    text-to-image models still mangle letterforms -- this way the words are
    always crisp and always correct.
    """
    seconds = duration_ms / 1000
    font_size = max(12, round(height * style.font_size))
    margin_x = round(width * style.margin_x)
    margin_y = round(height * style.margin_y)
    track = round(font_size * style.tracking)

    brand = pack.brand_for(style.key)
    subject = spec.show_title or spec.subject.replace("-", " ").title()
    lines: list[tuple[str, int, int]] = []

    if brand:
        lines.append((_case(brand, style), margin_y - round(font_size * 1.4)))
    if style.show_position_word:
        word = pack.word_for(spec.position, style.key)
        lines.append((_case(word, style), margin_y))
        lines.append((_case(subject, style), margin_y + round(font_size * 1.5)))
    else:
        lines.append((_case(subject, style), margin_y))
    if spec.clock:
        lines.append((spec.clock, margin_y + round(font_size * 3.0)))

    filters = [f"color=c={style.background}:s={width}x{height}:r=30000/1001:d={seconds:.3f}"]
    if style.letterbox:
        bar = round(height * 0.055)
        filters.append(f"drawbox=x=0:y=0:w={width}:h={bar}:color=black:t=fill")
        filters.append(
            f"drawbox=x=0:y={height - bar}:w={width}:h={bar}:color=black:t=fill"
        )
    if style.vignette > 0:
        filters.append(f"vignette=angle=PI/{round(4 - style.vignette * 2, 2)}")
    if style.grain > 0:
        filters.append(f"noise=alls={round(style.grain * 100)}:allf=t")

    for text, y in lines:
        x = _x_expression(style.align, margin_x)
        filters.append(
            "drawtext="
            f"text='{_escape_text(text)}':"
            f"{_font_option(style.font)}:"
            f"fontcolor={style.colour}:"
            f"fontsize={font_size}:"
            f"x={x}:y={y}"
            + (f":line_spacing={track}" if track else "")
        )

    filters.extend(_artifact_filters(style, width=width, height=height))

    filters.append("format=yuv420p")
    # Film grain is incompressible: at CRF 20 a grainy card ballooned to ~17 MB.
    # Cards are cheap to regenerate, so a softer CRF keeps the cache sane.
    crf = "30" if style.grain > 0 else "22"
    return [
        "-hide_banner",
        "-loglevel",
        "error",
        "-f",
        "lavfi",
        "-i",
        filters[0],
        "-vf",
        ",".join(filters[1:]),
        "-t",
        f"{seconds:.3f}",
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-crf",
        crf,
        "-pix_fmt",
        "yuv420p",
        "-an",
        "-y",
    ]


def _artifact_filters(style: StylePack, *, width: int, height: int) -> list[str]:
    """The "this card was generated" mark.

    Three properties make it the right kind of tell:

    * **It is a real thing in the picture**, not a watermark the player adds, so
      it survives any transcode and any client. A subtitle or overlay would be
      invisible on some players, which is the one place you might be watching.
    * **It is quiet.** A third opacity, small, in a corner. A viewer reads it as
      grime; you read it as a signature.
    * **It is mirrored.** Correct at a glance, wrong when read -- so you notice it
      in a screenshot without it ever looking like a caption.

    Rendered as an ``hstack`` of a mirrored copy and its reverse, which flips the
    glyphs about their own vertical axis and leaves the block in place: readable
    as *something*, obviously not readable as *text*.
    """
    size = max(9, round(height * style.artifact_size))
    margin_x = round(width * style.artifact_margin_x)
    margin_y = round(height * style.artifact_margin_y)
    text = _escape_text(style.artifact_text)
    alpha = max(0.05, min(1.0, style.artifact_opacity))
    font = _font_option(style.font)

    # One guard here rather than at every call site: the mark is optional, and a
    # style with no text or `artifact: false` must produce no filters at all.
    if not style.artifact or not style.artifact_text:
        return []

    # Literal pixels, not `w - ...`: `drawbox` did not resolve `w` inside its own
    # x expression the way `drawtext` does, and put the box at the far *left*
    # edge instead of the right. Both are computed from the frame here.
    pad_x = max(4, size // 2)
    box_w = size * 2 + pad_x * 2
    box_h = size + pad_x * 2
    box_x = width - margin_x - box_w
    box_y = margin_y

    filters: list[str] = []
    if style.artifact_box:
        # A drawn box, not an image: it costs nothing, survives every transcode,
        # and no archive card ever had one. Together with the low alpha that is
        # the whole tell -- visible when you look for it, grime when you don't.
        filters.append(
            "drawbox="
            f"x={box_x}:y={box_y}:w={box_w}:h={box_h}:"
            f"color={style.colour}@{alpha * 0.55:.2f}:t=1"
        )
        text_x = box_x + pad_x
        text_y = box_y + pad_x
    else:
        text_x = width - margin_x - size * 2
        text_y = margin_y

    filters.append(
        "drawtext="
        f"text='{text}':{font}:fontcolor={style.colour}:fontsize={size}:"
        f"x={text_x}:y={text_y}:"
        f"alpha={alpha}"
    )
    return filters


def _x_expression(align: str, margin: int) -> str:
    if align == "center":
        return "(w-text_w)/2"
    if align == "right":
        return f"w-text_w-{margin}"
    return str(margin)


def _case(text: str, style: StylePack) -> str:
    return text.upper() if style.uppercase else text


def _escape_text(text: str) -> str:
    """Escape for ffmpeg drawtext: backslash, colon, percent, comma, brackets."""
    out = text.replace("\\", "\\\\")
    for char in (":", "'", "%", ",", "[", "]"):
        out = out.replace(char, "\\" + char)
    return out


def _find_ffmpeg(explicit: str | None = None) -> str | None:
    if explicit and Path(explicit).is_file():
        return explicit
    found = shutil.which("ffmpeg")
    if found:
        return found
    bundled = Path(__file__).resolve().parent / "_bin" / "ffmpeg.exe"
    return str(bundled) if bundled.is_file() else None


def _run(binary: str, command: list[str], target: Path) -> None:
    temporary = target.with_name(f".{target.name}.part.mp4")
    try:
        result = subprocess.run(
            [binary, *command, str(temporary)],
            capture_output=True,
            text=True,
            timeout=180,
            check=False,
        )
    except OSError as exc:
        raise BumpGenError(f"cannot run ffmpeg: {exc}") from exc
    if result.returncode != 0:
        temporary.unlink(missing_ok=True)
        raise BumpGenError(f"ffmpeg failed: {result.stderr.strip()[:500]}")
    temporary.replace(target)


def _actual_duration(path: Path, fallback_ms: int) -> int:
    """Trust ffprobe when available, else the intended duration."""
    if not path.is_file():
        return fallback_ms
    if find_ffprobe() is None:
        return fallback_ms
    try:
        return probe(path).duration_ms
    except Exception:  # noqa: BLE001 - duration is a nicety here, not a contract
        return fallback_ms


def specs_for_schedule(
    *,
    shows: Sequence[str],
    titles: dict[str, str],
    year: int | None,
    positions: Sequence[str] = ("INTO", "AFTER", "NEXT"),
    max_seconds: float | None = 12,
) -> list[BumpSpec]:
    """The full cross-product of shows x positions, for pre-rendering a cache."""
    return [
        BumpSpec(
            subject=show,
            position=position,
            year=year,
            show_title=titles.get(show),
            max_seconds=max_seconds,
        )
        for show in shows
        for position in positions
    ]


__all__ = [
    "BumpGenError",
    "BumpSpec",
    "EraPack",
    "StylePack",
    "cache_key",
    "load_era_packs",
    "render",
    "render_pool",
    "specs_for_schedule",
]
