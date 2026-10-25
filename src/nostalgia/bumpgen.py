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


def load_era_packs(path: Path) -> EraPack:
    if not path.is_file():
        raise BumpGenError(f"missing era style file: {path}")
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    styles_raw = raw.get("styles") or {}
    if not styles_raw:
        raise BumpGenError(f"{path.name}: no styles defined")
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
