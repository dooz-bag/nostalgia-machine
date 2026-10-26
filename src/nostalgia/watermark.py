"""The corner bug.

[adult swim] ran a persistent ``[adult swim]`` mark in the bottom-right corner of
every programme. Library rips usually do not have it, so the channel looks like a
media server rather than a cable network.

Two ways to put it back:

* **Burn a card over the picture** -- deterministic, needs no ffmpeg muxing, and
  the result lives in the media tree like everything else.
* **Ask the streamer to composite it** -- ErsatzTV Next supports per-item
  ``graphics`` layers with a ``bottom_right`` anchor, margins and opacity, which
  is cheaper (it happens during a transcode that is already running) but only
  works on builds that carry the field.

Both are built on the same idea: a *rule* says which items get the mark (episodes
of a show, during a year range), and a *composite* says what the mark looks like.
Keeping those separate is what lets the owner switch between them without
rewriting the look, and it is what makes "only add it if the episode doesn't
already have it" an explicit, overridable decision rather than a guess.
"""

from __future__ import annotations

import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Sequence

from .bumpgen import BumpGenError, _find_ffmpeg, _font_option, resolve_font

#: Kinds that can carry a network bug. Commercials and bumpers never did.
MARKABLE_KINDS = ("episode",)

#: Fraction of the frame height reserved for the mark's own box, by era. The
#: burned-in [adult swim] wordmark was small and tight to the corner.
DEFAULT_WIDTH_PERCENT = 14.0
DEFAULT_MARGIN_X_PERCENT = 2.4
DEFAULT_MARGIN_Y_PERCENT = 4.2
DEFAULT_OPACITY_PERCENT = 72.0

#: How many of an episode's opening seconds to check when asking "is it already
#: there?". A bug is present from the first frame, so a few seconds is enough.
DETECT_SECONDS = 6.0

#: Luma *range* (max - min) the corner must show. A drawn wordmark has one; a
#: uniform patch of colour has none, however bright it is -- which is why a
#: mean-luma test produced false positives on any frame with a bright
#: bottom-right quadrant. Kept low deliberately: contrast is the *weak* signal
#: here (measured 57 for a real bug but 48 for moving picture content), so it is
#: only used to rule out flat corners.
DETECT_CONTRAST_THRESHOLD = 20.0

#: The mark was white on whatever was behind it, so the crop's peak luma is high.
DETECT_BRIGHT_THRESHOLD = 55.0

#: The *strong* signal. A burned-in bug is the same pixels for the whole episode,
#: so two crops taken far apart differ by ~0 even in the worst pixel. Real
#: content in that corner moves. Measured: static corner 0, moving content 16-36.
#: Two things matter here: the *maximum* rather than the mean (the bug occupies
#: too few pixels to move an average -- 2.8 vs 0), and several sample pairs
#: reduced by median, because one pair can land on two similar frames of
#: repetitive content and read 16 where the typical pair reads 36.
DETECT_MOVEMENT_THRESHOLD = 24.0

#: How many frame pairs to compare. More is steadier but costs an ffmpeg seek
#: each; five is enough to make the median stable.
DETECT_SAMPLES = 5

#: Crop the bottom-right corner as a fraction of the frame, for detection.
DETECT_CROP = (0.34, 0.20)


class WatermarkError(RuntimeError):
    pass


#: The typeface the [adult swim] wordmark was set in. Requested by name, then
#: resolved through a chain of metrically-close faces, because this font ships
#: with nothing and is not free to redistribute.
WANTED_FONT = "Helvetica Neue Condensed Bold"

#: Resolution order. The first two *are* Helvetica Neue; the rest are bold faces
#: metrically compatible with Helvetica, which the ``condense`` factor then
#: squeezes to the condensed width. Liberation Sans is what a Debian container
#: actually has, so the chain must not depend on the host's font collection.
FONT_CHAIN: tuple[tuple[str, ...], ...] = (
    ("HelveticaNeue-CondensedBold.ttf", "HelveticaNeue-CondensedBold.otf"),
    ("HelveticaNeue.ttc", "Helvetica.ttc"),
    ("Arial Narrow Bold", "arialnb.ttf", "ARIALNB.TTF"),
    ("LiberationSansNarrow-Bold.ttf",),
    ("DejaVuSansCondensed-Bold.ttf",),
    ("arialbd.ttf", "Arial Bold"),
    ("LiberationSans-Bold.ttf",),
    ("DejaVuSans-Bold.ttf",),
)

#: Faces that are already condensed. Anything else is squeezed by ``condense``
#: so the result reads as Helvetica Neue *Condensed* rather than plain bold.
CONDENSED_HINTS = ("condensed", "cond", "narrow")


@dataclass(frozen=True, slots=True)
class WatermarkLook:
    """What the mark looks like. Fractions of the output frame, so it scales."""

    text: str = "[adult swim]"
    font: str = WANTED_FONT
    font_size: float = 0.033
    """Font size as a fraction of frame height. Small: it is a corner bug."""
    colour: str = "white"
    opacity: float = DEFAULT_OPACITY_PERCENT / 100
    uppercase: bool = False
    margin_x: float = DEFAULT_MARGIN_X_PERCENT / 100
    margin_y: float = DEFAULT_MARGIN_Y_PERCENT / 100
    width_percent: float = DEFAULT_WIDTH_PERCENT
    condense: float = 0.84
    """Horizontal squeeze applied when the resolved face is not condensed.

    Helvetica Neue Condensed Bold is about 84% the width of Helvetica Neue Bold
    at the same size. Squeezing a Helvetica-metric bold face reproduces the
    wordmark closely, and keeps the look identical on a host with the real font
    and in a container that only has Liberation Sans.
    """

    def opacity_percent(self) -> float:
        return round(max(0.0, min(1.0, self.opacity)) * 100, 2)

    def normalized(self) -> "WatermarkLook":
        """Clamp every fraction into a sane range.

        A config typo that put the mark at 400% width or off the edge would be
        invisible rather than obvious, so it is corrected here and reported by
        ``doctor`` instead.
        """
        return WatermarkLook(
            text=self.text,
            font=self.font,
            font_size=_clamp(self.font_size, 0.008, 0.12),
            colour=self.colour,
            opacity=_clamp(self.opacity, 0.05, 1.0),
            uppercase=self.uppercase,
            margin_x=_clamp(self.margin_x, 0.0, 0.4),
            margin_y=_clamp(self.margin_y, 0.0, 0.4),
            width_percent=_clamp(self.width_percent, 2.0, 60.0),
            condense=_clamp(self.condense, 0.5, 1.0),
        )


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, float(value)))


@dataclass(frozen=True, slots=True)
class WatermarkRule:
    """Which items get the mark.

    ``shows`` empty means every show. ``era_from``/``era_to`` bound it to the
    channel's era, so a bug is not stamped onto something explicitly outside it.
    """

    enabled: bool = True
    shows: tuple[str, ...] = ()
    except_shows: tuple[str, ...] = ()
    """Shows whose rips already carry a bug, so they must never be marked again.

    This is the dependable answer. The marks in a real library are not uniform
    enough to find automatically: the design differs between shows (a white
    wordmark, a red one, a Cartoon Network logo) and even between episodes of one
    show, and the corner crop always contains moving picture. Erring towards
    marking is the wrong way to be wrong -- a second logo over a real one is
    obviously broken, while a missing logo just looks like a clean master.
    """
    era_from: int | None = None
    era_to: int | None = None
    skip_when_burned_in: bool = True
    """Leave an episode alone when it already carries a corner mark.

    Applies to both modes. ``burn`` checks the picture directly at composite
    time; ``overlay`` reads the verdict the last scan recorded, because the
    streamer composites during a transcode that must not wait on detection.
    """

    def applies_to(self, *, kind: str, show_id: str | None, era_year: int) -> bool:
        if not self.enabled:
            return False
        if kind not in MARKABLE_KINDS:
            return False
        if self.shows and (show_id or "") not in self.shows:
            return False
        if (show_id or "") in self.except_shows:
            return False
        if self.era_from is not None and era_year < self.era_from:
            return False
        if self.era_to is not None and era_year > self.era_to:
            return False
        return True


@dataclass
class WatermarkReport:
    made: int = 0
    reused: int = 0
    skipped_burned_in: int = 0
    skipped_rule: int = 0
    failed: int = 0
    seconds: float = 0.0
    errors: list[str] = field(default_factory=list)

    def line(self) -> str:
        return (
            f"{self.made} composited, {self.reused} already cached, "
            f"{self.skipped_burned_in} left alone (mark already present), "
            f"{self.failed} failed"
        )


def _escape_filter_path(path: Path) -> str:
    """Path for use inside a filtergraph argument."""
    return str(path).replace("\\", "/").replace(":", "\\:")


def _escape_text(text: str) -> str:
    out = text.replace("\\", "\\\\")
    for char in (":", "'", "%", ",", "[", "]"):
        out = out.replace(char, "\\" + char)
    return out


def resolve_mark_font(requested: str = WANTED_FONT) -> Path | None:
    """Find the closest available face to the requested wordmark font.

    The chain is walked in order, so a host that genuinely has Helvetica Neue
    Condensed Bold uses it verbatim, and a Debian container that only ships
    Liberation Sans still gets a bold Helvetica-metric face to condense.
    """
    # An explicit absolute path is always honoured first.
    explicit = Path(requested)
    if explicit.is_absolute() and explicit.is_file():
        return explicit
    for candidate in (requested, *[name for group in FONT_CHAIN for name in group]):
        resolved = resolve_font(candidate)
        if resolved is not None:
            return resolved
    return None


def _is_condensed(path: Path | None) -> bool:
    if path is None:
        return False
    name = path.name.lower()
    return any(hint in name for hint in CONDENSED_HINTS)


def mark_filter(look: WatermarkLook, *, font_path: Path | None) -> str:
    """The graphics filter chain that draws the wordmark on a transparent canvas.

    Rendering the mark as a *card* rather than as live ``drawtext`` is what makes
    it look like a broadcast bug: the type can be condensed horizontally (which
    ``drawtext`` cannot do), and the alpha survives so the picture shows through.
    """
    look = look.normalized()
    text = look.text.upper() if look.uppercase else look.text
    # Drawn oversized then scaled, so the glyph edges stay clean at bug size.
    size = 128
    filters = [
        "format=rgba",
        (
            "drawtext="
            f"text='{_escape_text(text)}':"
            f"{_font_option_path(font_path, look.font)}:"
            f"fontcolor={look.colour}:"
            f"fontsize={size}:"
            "x=0:y=0"
        ),
        # `colorkey` is what actually yields a transparent background here.
        # Neither `color=c=black@0.0` nor a `geq` alpha channel survives: ffmpeg
        # re-expands the alpha to 255 in both cases (measured), so the card
        # rendered as an opaque black box over the picture.
        "colorkey=color=black:similarity=0.02:blend=0.0",
    ]
    if look.condense < 0.999 and not _is_condensed(font_path):
        # Horizontally squeeze, keeping the height: that is what turns a
        # Helvetica-metric bold into Helvetica Neue Condensed Bold.
        filters.append(f"scale=iw*{look.condense:.3f}:ih")
    # Trim to the ink, so the card is exactly the wordmark and the placement
    # margins in the output are the real ones.
    #
    # `bbox` finds the ink, rather than cropping a fixed fraction. The previous
    # `crop=iw*0.96:ih*0.96` cropped *symmetrically about the centre* while the
    # text is drawn at x=0, so it removed ~31px from the left and ate the opening
    # "[" entirely -- the mark rendered as "adult swim]" with a fragment of the
    # closing bracket.
    #
    # The trailing `crop` is what makes the card's aspect ratio match the ink,
    # which matters because the streamer scales the card to `width_percent`.
    # A canvas far wider than the text therefore rendered a mark at roughly half
    # the size it was asked for.
    filters.append("bbox=min_val=40")
    return ",".join(filters)


def _font_option_path(path: Path | None, family: str) -> str:
    if path is None:
        return f"font={family}"
    escaped = str(path).replace("\\", "/").replace(":", "\\:")
    return f"fontfile='{escaped}'"


def build_filtergraph(
    look: WatermarkLook,
    *,
    width: int,
    height: int,
    margin_right: int | None = None,
    margin_bottom: int | None = None,
) -> str:
    """The ``ffmpeg`` overlay filter that composites a rendered card.

    Only the placement is decided here; the *inputs* are the caller's business,
    so the same string serves the cached-composite path and the streaming path.
    """
    look = look.normalized()
    margin_x = (
        round(width * look.margin_x) if margin_right is None else margin_right
    )
    margin_y = (
        round(height * look.margin_y) if margin_bottom is None else margin_bottom
    )
    return (
        f"overlay=x=W-w-{margin_x}:y=H-h-{margin_y}:"
        f"format=auto:alpha=premultiplied"
    )


def _crop(path: Path | str, crop: tuple[float, float]) -> str:
    width_fraction, height_fraction = crop
    return (
        f"crop=iw*{width_fraction:.3f}:ih*{height_fraction:.3f}:"
        f"iw-iw*{width_fraction:.3f}:ih-ih*{height_fraction:.3f}"
    )


def _signalstats(binary: str, path: Path | str, graph: str, *args: str) -> dict[str, float] | None:
    """Run a filter graph and return the averaged ``signalstats`` keys."""
    try:
        result = subprocess.run(
            [
                binary,
                "-hide_banner",
                "-loglevel",
                "info",
                *args,
                "-i",
                str(path),
                "-vf",
                f"{graph},signalstats,metadata=print",
                "-f",
                "null",
                "-",
            ],
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return _average_metadata(result.stderr)


_KEYS = ("YAVG", "YMIN", "YMAX")


def _average_metadata(text: str) -> dict[str, float] | None:
    totals = {key: 0.0 for key in _KEYS}
    frames = 0
    for line in text.splitlines():
        if "lavfi.signalstats." not in line:
            continue
        hit = False
        for key in _KEYS:
            marker = f"lavfi.signalstats.{key}="
            if marker not in line:
                continue
            _, _, tail = line.partition(marker)
            try:
                totals[key] += float(tail.strip())
                hit = True
            except ValueError:
                pass
        if hit:
            frames += 1
    if not frames:
        return None
    return {key: value / frames for key, value in totals.items()}


def has_burned_in_mark(
    path: Path | str,
    *,
    ffmpeg: str | None = None,
    crop: tuple[float, float] = DETECT_CROP,
    contrast: float = DETECT_CONTRAST_THRESHOLD,
    brightness: float = DETECT_BRIGHT_THRESHOLD,
    movement: float = DETECT_MOVEMENT_THRESHOLD,
    samples: int = DETECT_SAMPLES,
    seconds: float = DETECT_SECONDS,
) -> bool | None:
    """Whether a corner mark is already in the picture.

    Two gates, both read off the bottom-right crop:

    1. **There is something drawn there** -- the crop's luma range and peak must
       clear a low bar, which rules out a flat or dark corner. This gate is
       deliberately weak because it cannot on its own tell a wordmark from
       picture content (measured 57 vs 48).
    2. **And it does not move** -- two crops taken far apart in the episode must
       be nearly identical in the *worst* pixel. This is the signal that
       actually distinguishes a bug (0) from moving content (108).

    Returns ``None`` when it cannot be decided (no ffmpeg, unreadable file),
    which the caller reports rather than silently guessing. A false positive only
    means an episode keeps its own correct bug, which is the safe direction.
    """
    binary = ffmpeg or _find_ffmpeg()
    if binary is None:
        return None
    graph = _crop(path, crop)

    stats = _signalstats(binary, path, graph, "-t", f"{seconds:.2f}")
    if stats is None:
        return None
    if stats["YMAX"] - stats["YMIN"] < contrast or stats["YMAX"] < brightness:
        return False

    # Duration is asked for rather than assumed: a 3-second fixture and a
    # 22-minute episode cannot both be sampled at one fixed timestamp.
    duration = _duration_seconds(Path(path), binary)
    if duration is None or duration < 2:
        # Only one usable frame, so movement cannot be judged. Something IS drawn
        # in the corner, so prefer leaving it alone.
        return True
    # Sample pairs spread across the *whole* episode, then take the median.
    # Adjacent frames only ~0.5s apart read as near-identical (15-21) even in
    # fast-moving content, because a 0.5s gap is barely one visual beat. Pairs
    # taken far apart separate cleanly: 0 for a static corner, 37 for moving
    # picture. The median keeps a single coincidental match from deciding it.
    fractions = (0.08, 0.29, 0.50, 0.71, 0.92)[: max(2, samples)]
    timestamps = [max(0.0, duration * fraction) for fraction in fractions]
    diffs: list[float] = []
    for left_at, right_at in zip(timestamps, timestamps[1:]):
        left = _crop_png(binary, path, graph, left_at)
        right = _crop_png(binary, path, graph, right_at)
        if left is None or right is None:
            continue
        worst = _max_absolute_difference(binary, left, right)
        if worst is not None:
            diffs.append(worst)
    if not diffs:
        return True
    diffs.sort()
    median = diffs[len(diffs) // 2]
    return median < movement


def _duration_seconds(path: Path, binary: str) -> float | None:
    from .probe import find_ffprobe, probe

    if find_ffprobe() is None:
        return None
    try:
        return probe(path).duration_ms / 1000
    except Exception:  # noqa: BLE001 - detection degrades, it does not fail
        return None


def _crop_png(binary: str, path: Path | str, graph: str, at: float) -> Path | None:
    import tempfile

    handle = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
    handle.close()
    target = Path(handle.name)
    result = subprocess.run(
        [
            binary,
            "-hide_banner",
            "-loglevel",
            "error",
            "-ss",
            f"{at:.3f}",
            "-i",
            str(path),
            "-vf",
            f"{graph},scale=96:56,format=gray",
            "-frames:v",
            "1",
            "-y",
            str(target),
        ],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    if result.returncode != 0 or not target.is_file():
        target.unlink(missing_ok=True)
        return None
    return target


def _max_absolute_difference(binary: str, left: Path, right: Path) -> float | None:
    """Worst pixel difference between two crops, on a 0-255 scale.

    The *maximum* rather than the mean: a corner bug is small, so it barely moves
    the average (measured 2.8 for genuinely moving content) while the worst pixel
    separates cleanly (108 moving, 0 static).
    """
    try:
        result = subprocess.run(
            [
                binary,
                "-hide_banner",
                "-loglevel",
                "info",
                "-i",
                str(left),
                "-i",
                str(right),
                "-lavfi",
                "[0:v][1:v]blend=all_mode=difference,signalstats,metadata=print",
                "-frames:v",
                "1",
                "-f",
                "null",
                "-",
            ],
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    finally:
        left.unlink(missing_ok=True)
        right.unlink(missing_ok=True)
    if result.returncode != 0:
        return None
    stats = _average_metadata(result.stderr)
    return stats["YMAX"] if stats else None


def composite(
    source: Path | str,
    target: Path | str,
    look: WatermarkLook,
    *,
    ffmpeg: str | None = None,
    width: int | None = None,
    height: int | None = None,
    crf: int = 20,
    preset: str = "veryfast",
    audio_copy: bool = True,
) -> Path:
    """Write *target* as *source* with the mark burned in.

    The resolution is not taken from the config: it is probed from the file, so a
    4:3 rip and a 16:9 episode each get a mark sized to their own frame rather
    than to the channel's output.
    """
    binary = ffmpeg or _find_ffmpeg()
    if binary is None:
        raise WatermarkError("ffmpeg is required to composite a watermark")

    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    probe_width, probe_height = width, height
    if not probe_width or not probe_height:
        probe_width, probe_height = _dimensions(Path(source), binary)
    if not probe_width or not probe_height:
        raise WatermarkError(f"cannot determine the frame size of {source}")

    card = card_asset(look, width=probe_width, height=probe_height, ffmpeg=binary)
    placement = build_filtergraph(look, width=probe_width, height=probe_height)
    temporary = target.with_name(f".{target.name}.part{target.suffix}")
    command = [
        binary,
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        str(source),
        "-i",
        str(card),
        "-filter_complex",
        f"[0:v][1:v]{placement}[v]",
        "-map",
        "[v]",
        "-map",
        "0:a?",
        "-c:v",
        "libx264",
        "-preset",
        preset,
        "-crf",
        str(crf),
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "copy" if audio_copy else "aac",
        "-y",
        str(temporary),
    ]
    try:
        result = subprocess.run(
            command, capture_output=True, text=True, timeout=3600, check=False
        )
    except (OSError, subprocess.SubprocessError) as exc:
        temporary.unlink(missing_ok=True)
        raise WatermarkError(f"cannot run ffmpeg: {exc}") from exc
    if result.returncode != 0:
        temporary.unlink(missing_ok=True)
        raise WatermarkError(
            f"ffmpeg failed on {Path(source).name}: {result.stderr.strip()[:400]}"
        )
    temporary.replace(target)
    return target


def _dimensions(path: Path, binary: str) -> tuple[int | None, int | None]:
    from .probe import find_ffprobe, probe

    if find_ffprobe() is not None:
        try:
            info = probe(path)
            return info.width, info.height
        except Exception:  # noqa: BLE001 - fall through to the ffmpeg parse
            pass
    try:
        result = subprocess.run(
            [
                binary,
                "-hide_banner",
                "-i",
                str(path),
                "-frames:v",
                "1",
                "-f",
                "null",
                "-",
            ],
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None, None
    for line in result.stderr.splitlines():
        marker = "Video: "
        if marker not in line:
            continue
        for token in line.split(","):
            piece = token.strip().split(" ")[0]
            if "x" in piece:
                left, _, right = piece.partition("x")
                if left.isdigit() and right.isdigit():
                    return int(left), int(right)
    return None, None


def graphics_layer(
    look: WatermarkLook,
    *,
    path: str,
    **extra,
) -> dict:
    """A playout ``graphics`` layer that asks the streamer to composite the mark.

    Verified against ``ersatztv-playout``'s ``GraphicsLayer``: ``location`` is
    required and ``bottom_right`` is a valid anchor, margins are percentages of
    the primary content, and ``within_source_content`` keeps the mark inside the
    visible picture when letterbox bars appear -- which is the whole reason to
    prefer this over a fixed pixel offset.
    """
    look = look.normalized()
    layer: dict = {
        "source": {"source_type": "local", "path": path},
        "location": "bottom_right",
        "width_percent": round(look.width_percent, 2),
        "within_source_content": True,
        "horizontal_margin_percent": round(look.margin_x * 100, 2),
        "vertical_margin_percent": round(look.margin_y * 100, 2),
        "opacity_percent": look.opacity_percent(),
    }
    layer.update(extra)
    return layer


_CARD_CACHE: dict[str, Path] = {}


def card_asset(
    look: WatermarkLook,
    *,
    width: int,
    height: int,
    ffmpeg: str | None = None,
    cache_dir: Path | str | None = None,
) -> Path:
    """The mark card, rendered once per (look, output size).

    Cached in a temp dir by default, because the card is channel furniture rather
    than content. The overlay path passes a directory under ``media/`` instead, so
    the *streamer* can read it: it only mounts the media tree.
    """
    look = look.normalized()
    card_width, card_height = card_size(look, width=width, height=height)
    key = (
        f"{abs(hash((look, card_width, card_height, str(resolve_mark_font(look.font)))))}"
        f"-{card_width}x{card_height}.png"
    )
    if key in _CARD_CACHE and _CARD_CACHE[key].is_file():
        return _CARD_CACHE[key]

    directory = Path(cache_dir) if cache_dir else Path(tempfile.gettempdir()) / "nostalgia-mark"
    target = directory / key
    if not target.is_file():
        render_text_card(
            look, target, width=card_width, height=card_height, ffmpeg=ffmpeg
        )
    _CARD_CACHE[key] = target
    return target


def card_size(look: WatermarkLook, *, width: int, height: int) -> tuple[int, int]:
    """Canvas size to render the mark card on.

    Deliberately generous and in a fixed aspect relative to the glyph height: the
    card is cropped down to its ink afterwards, so the canvas only has to be big
    enough to hold the wordmark without clipping. Sizing the canvas to the final
    width instead would clip the text, because the width is what is discovered by
    rendering, not what is decided up front.
    """
    look = look.normalized()
    glyph = max(48, round(height * look.font_size * 6))
    return glyph * 14, glyph * 3


#: Fully transparent, used as the card's background.
_TRANSPARENT = "black@0.0"


def render_text_card(
    look: WatermarkLook,
    target: Path | str,
    *,
    width: int = 320,
    height: int = 104,
    font: str | Path | None = None,
    ffmpeg: str | None = None,
) -> Path:
    """Render the mark on its own transparent canvas, for the streamer to overlay.

    Alpha is preserved so the picture shows through. The card is *not* drawn
    straight onto the program: keeping it separate is what lets the streamer
    position it with margins, and lets it respect letterbox bars.
    """
    binary = ffmpeg or _find_ffmpeg()
    if binary is None:
        raise WatermarkError("ffmpeg is required to render a watermark card")
    look = look.normalized()
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    font_path = Path(font) if font else resolve_mark_font(look.font)
    graph = mark_filter(look, font_path=font_path)
    temporary = target.with_name(f".{target.name}.part.png")
    result = subprocess.run(
        [
            binary,
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            f"color=c=black:s={width}x{height}:d=1",
            "-vf",
            graph,
            "-frames:v",
            "1",
            "-y",
            str(temporary),
        ],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    if result.returncode != 0:
        temporary.unlink(missing_ok=True)
        raise WatermarkError(
            f"cannot render the watermark card (font {font_path}): "
            f"{result.stderr.strip()[:300]}"
        )
    temporary.replace(target)
    return target


def composite_library(
    items: Sequence[tuple[str, Path]],
    *,
    look: WatermarkLook,
    out_dir: Path | str,
    rule: WatermarkRule | None = None,
    era_year: int = 2003,
    ffmpeg: str | None = None,
    force: bool = False,
    dry_run: bool = False,
) -> WatermarkReport:
    """Burn the mark into every eligible asset, caching the result.

    *items* is ``(key, path)`` for the raw media. The output is named after the
    asset key, so one episode maps to exactly one composite and a second run is a
    cache hit. Nothing here writes to the originals: an overlaid copy is a new
    file, which is what makes the whole feature reversible by deleting a folder.
    """
    rule = rule or WatermarkRule()
    out_dir = Path(out_dir)
    report = WatermarkReport()
    for key, path in items:
        if not path.is_file():
            continue
        if not rule.applies_to(kind="episode", show_id=_show_of(key), era_year=era_year):
            report.skipped_rule += 1
            continue
        target = out_dir / f"{key.replace(':', '_')}.mkv"
        if target.is_file() and not force:
            report.reused += 1
            continue
        if dry_run:
            report.made += 1
            continue
        if rule.skip_when_burned_in:
            present = has_burned_in_mark(path, ffmpeg=ffmpeg)
            if present:
                report.skipped_burned_in += 1
                continue
        try:
            composite(path, target, look, ffmpeg=ffmpeg)
            report.made += 1
        except (WatermarkError, BumpGenError) as exc:
            report.failed += 1
            report.errors.append(str(exc))
    return report


def _show_of(key: str) -> str | None:
    return key.split(":", 1)[0] if ":" in key else None


def look_from_config(config) -> WatermarkLook:
    """Build a :class:`WatermarkLook` from ``config/watermark.yaml``.

    Kept here rather than in ``config`` so the look has exactly one definition and
    the config object stays a plain description of intent.
    """
    return WatermarkLook(
        text=config.text,
        font=config.font,
        font_size=config.font_size,
        colour=config.colour,
        opacity=config.opacity,
        uppercase=config.uppercase,
        margin_x=config.margin_x_percent / 100,
        margin_y=config.margin_y_percent / 100,
        width_percent=config.width_percent,
        condense=getattr(config, "condense", 0.84),
    )


def rule_from_config(config, *, era_year: int | None = None) -> WatermarkRule:
    """Build the burn-mode rule from ``config/watermark.yaml``."""
    return WatermarkRule(
        enabled=config.mode == "burn",
        shows=tuple(config.shows),
        except_shows=tuple(config.except_shows),
        skip_when_burned_in=config.skip_when_burned_in,
    )


__all__ = [
    "DEFAULT_MARGIN_X_PERCENT",
    "DEFAULT_MARGIN_Y_PERCENT",
    "DEFAULT_OPACITY_PERCENT",
    "DEFAULT_WIDTH_PERCENT",
    "MARKABLE_KINDS",
    "WatermarkError",
    "WatermarkLook",
    "WatermarkReport",
    "WatermarkRule",
    "build_filtergraph",
    "composite",
    "composite_library",
    "graphics_layer",
    "has_burned_in_mark",
    "look_from_config",
    "render_text_card",
    "rule_from_config",
]
