"""Conform the break library: one picture, one loudness, one frame count.

A break is built from spots recorded off different networks, on different tapes,
at different resolutions. Played as they are, the streamer has to reconcile every
one of them *at the cut*: it scales and pillarboxes each spot on its own, so the
black bars change width at most transitions, and every spot is a separate decode
that ends in a hard edge.

Conforming does that reconciliation once, ahead of time, so the channel never has
to do it live:

* **Two canvases, not seven.** A 1280x720 frame holds either true 16:9 footage
  (fills the frame) or 4:3 footage (960x720 with fixed 160 px pillars). Nearly
  every "5:4" or "3:2" file in a recorded-TV library is 4:3 material stored at an
  odd size or with non-square pixels, so stretching it to 4:3 is the *correction*,
  not a distortion. The result is the picture width changing only when the
  footage genuinely switches between 4:3 and 16:9, which is what a real channel
  did.
* **Exact frame counts, constant 29.97 fps.** Durations become whole frames, so a
  conformed file's length is knowable and the planner can sum it.
* **Loudness measured and corrected here**, in two passes, to the same target the
  streamer uses. The streamer's own single-pass normalizer then has nothing left
  to do, instead of restarting its estimate at every spot.
* **A short dip to black and silence at each end of a commercial**, like the gap
  between spots on a recorded break. It hides the seam instead of exposing it.
  Bumps are left butt-cut: a real [adult swim] bump cut straight in.
* **Identical encode parameters**, so a run of conformed files can later be joined
  by stream copy (:mod:`nostalgia.mergebreaks`) with no re-encode.

The original is never touched. A conformed copy lives under ``media/derived/`` and
is recorded in the catalog beside the original, which keeps its tags, history and
on/off switch.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable

#: Bump whenever the encode recipe changes, so every conformed file is rebuilt.
RECIPE_VERSION = 1

FPS_NUM, FPS_DEN = 30000, 1001

#: DAR at or above this is treated as true widescreen; below it, 4:3 material.
WIDE_DAR = 1.6
#: Below this DAR the footage is neither -- keep its own shape rather than stretch.
STANDARD_DAR_MIN = 1.2

#: A file quieter than this has no usable audio, so normalising it would only turn
#: tape hiss up to full volume. Reported and switched off instead.
SILENT_LUFS = -45.0

CONFORM_KINDS = ("ad", "bump", "ident", "extra")


class ConformError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class ConformParams:
    """Everything that shapes the output. Hashed into the file's signature."""

    width: int = 1280
    height: int = 720
    target_lufs: float = -16.0
    true_peak: float = -1.5
    lra: float = 11.0
    dip_ms: int = 100
    """Fade to black/silence at each end of a commercial. ``0`` is a hard cut."""
    crf: int = 20
    audio_kbps: int = 160

    def signature(self) -> str:
        blob = json.dumps(
            [RECIPE_VERSION, self.width, self.height, self.target_lufs, self.true_peak,
             self.lra, self.dip_ms, self.crf, self.audio_kbps],
            sort_keys=True,
        )
        return hashlib.sha1(blob.encode()).hexdigest()[:12]


@dataclass(frozen=True, slots=True)
class SourceInfo:
    width: int
    height: int
    dar: float
    interlaced: bool
    duration_s: float
    has_audio: bool


@dataclass(frozen=True, slots=True)
class Geometry:
    """Where the picture sits on the output frame, in output pixels."""

    scale_w: int
    scale_h: int
    pad_x: int
    pad_y: int
    canvas: str
    """``wide`` | ``standard`` | ``other``"""


@dataclass(frozen=True, slots=True)
class ConformResult:
    key: str
    ok: bool
    path: Path | None = None
    duration_ms: int = 0
    signature: str = ""
    skipped: bool = False
    silent: bool = False
    note: str = ""


# -- geometry -----------------------------------------------------------------


def _even(value: float) -> int:
    return max(2, int(round(value / 2.0)) * 2)


def display_aspect(width: int, height: int, sar: str | None, dar: str | None) -> float:
    """The shape the picture is *meant* to have on screen.

    A declared display aspect wins; otherwise it is derived from the sample aspect,
    which is how a 720x480 NTSC capture with 8:9 pixels reveals that it is 4:3.
    """
    for text in (dar,):
        ratio = _parse_ratio(text)
        if ratio:
            return ratio
    sample = _parse_ratio(sar) or 1.0
    return (width * sample) / max(1, height)


def _parse_ratio(text: str | None) -> float | None:
    if not text or text in ("N/A", "0:1", "0/1"):
        return None
    match = re.match(r"^\s*(\d+)\s*[:/]\s*(\d+)\s*$", str(text))
    if not match:
        return None
    num, den = int(match.group(1)), int(match.group(2))
    return num / den if num > 0 and den > 0 else None


def geometry_for(dar: float, width: int = 1280, height: int = 720) -> Geometry:
    """Pick the canvas and the scale for footage of display aspect *dar*."""
    standard_w = _even(height * 4 / 3)
    if dar >= WIDE_DAR:
        # Fit inside the frame at the footage's own shape (a 2.35:1 film trailer
        # keeps its letterbox rather than being stretched to fill).
        scale_w = width
        scale_h = min(height, _even(width / dar))
        return Geometry(scale_w, scale_h, 0, (height - scale_h) // 2, "wide")
    if dar >= STANDARD_DAR_MIN:
        # Treated as 4:3. 5:4, 3:2 and the odd 41:30 sizes are 4:3 programmes
        # stored at awkward dimensions, so filling the 4:3 box *corrects* them.
        return Geometry(standard_w, height, (width - standard_w) // 2, 0, "standard")
    # Square or taller than that: keep the shape, fit the height.
    scale_w = min(standard_w, _even(height * dar))
    return Geometry(scale_w, height, (width - scale_w) // 2, 0, "other")


# -- probing --------------------------------------------------------------------


def probe_source(path: Path, ffprobe: str) -> SourceInfo:
    result = subprocess.run(
        [ffprobe, "-v", "error", "-show_entries",
         "stream=codec_type,width,height,sample_aspect_ratio,display_aspect_ratio,"
         "field_order,duration:format=duration", "-of", "json", str(path)],
        capture_output=True, text=True, check=False,
    )
    if result.returncode != 0:
        raise ConformError(f"ffprobe failed: {result.stderr.strip()[:200]}")
    data = json.loads(result.stdout or "{}")
    video = next((s for s in data.get("streams", []) if s.get("codec_type") == "video"), None)
    if video is None:
        raise ConformError("no video stream")
    audio = any(s.get("codec_type") == "audio" for s in data.get("streams", []))
    width, height = int(video["width"]), int(video["height"])
    dar = display_aspect(
        width, height, video.get("sample_aspect_ratio"), video.get("display_aspect_ratio")
    )
    # Prefer the video stream's own length: an audio track that outlasts the
    # picture would otherwise add frames of frozen last-frame.
    duration = _to_float(video.get("duration")) or _to_float(
        (data.get("format") or {}).get("duration")
    )
    if not duration or duration <= 0:
        raise ConformError("no duration")
    return SourceInfo(
        width=width,
        height=height,
        dar=dar,
        interlaced=str(video.get("field_order", "progressive")) in ("tt", "bb", "tb", "bt"),
        duration_s=duration,
        has_audio=audio,
    )


def _to_float(value: object) -> float | None:
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def measure_loudness(
    path: Path,
    ffmpeg: str,
    params: ConformParams,
    *,
    start: float | None = None,
    duration: float | None = None,
) -> dict | None:
    """First loudnorm pass. ``None`` when there is no usable audio.

    *start*/*duration* measure just a segment, which is what a music bed needs: the
    few seconds laid under a card, not the whole track.
    """
    window: list[str] = []
    if start is not None:
        window += ["-ss", f"{start:.3f}"]
    if duration is not None:
        window += ["-t", f"{duration:.3f}"]
    result = subprocess.run(
        [ffmpeg, "-hide_banner", "-nostats", *window, "-i", str(path), "-vn", "-af",
         f"loudnorm=I={params.target_lufs}:TP={params.true_peak}:LRA={params.lra}"
         ":print_format=json", "-f", "null", "-"],
        capture_output=True, text=True, check=False,
    )
    match = re.search(r"\{[^{}]*\"input_i\"[^{}]*\}", result.stderr, re.S)
    if not match:
        return None
    try:
        values = json.loads(match.group(0))
        if float(values["input_i"]) in (float("-inf"),) or values["input_i"] in ("-inf",):
            return None
        return {key: float(value) for key, value in values.items()
                if key.startswith(("input_", "target_offset"))}
    except (ValueError, KeyError):
        return None


# -- command --------------------------------------------------------------------


def frame_count(duration_s: float) -> int:
    return max(1, int(duration_s * FPS_NUM / FPS_DEN))


def frames_to_seconds(frames: int) -> float:
    return frames * FPS_DEN / FPS_NUM


def build_command(
    ffmpeg: str,
    source: Path,
    target: Path,
    info: SourceInfo,
    geometry: Geometry,
    params: ConformParams,
    *,
    loudness: dict | None,
    fade: bool,
) -> list[str]:
    frames = frame_count(info.duration_s)
    seconds = frames_to_seconds(frames)
    dip = min(params.dip_ms / 1000.0, seconds / 4) if fade and params.dip_ms > 0 else 0.0

    video: list[str] = []
    if info.interlaced:
        video.append("yadif=mode=0:deint=all")
    video += [
        f"fps={FPS_NUM}/{FPS_DEN}:round=near",
        f"scale={geometry.scale_w}:{geometry.scale_h}:flags=bicubic",
        "setsar=1",
        f"pad={params.width}:{params.height}:{geometry.pad_x}:{geometry.pad_y}:color=black",
        "format=yuv420p",
    ]
    if dip:
        video.append(f"fade=t=in:st=0:d={dip:.3f}")
        video.append(f"fade=t=out:st={seconds - dip:.3f}:d={dip:.3f}")

    audio = ["aresample=48000", "aformat=sample_fmts=fltp:channel_layouts=stereo"]
    if loudness:
        audio.append(
            f"loudnorm=I={params.target_lufs}:TP={params.true_peak}:LRA={params.lra}"
            f":measured_I={loudness['input_i']}:measured_TP={loudness['input_tp']}"
            f":measured_LRA={loudness['input_lra']}:measured_thresh={loudness['input_thresh']}"
            f":offset={loudness['target_offset']}:linear=true"
        )
        audio.append("aresample=48000")
    if dip:
        audio.append(f"afade=t=in:st=0:d={dip:.3f}")
        audio.append(f"afade=t=out:st={seconds - dip:.3f}:d={dip:.3f}")
    audio.append("apad")

    command = [ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin", "-y", "-i", str(source)]
    if not info.has_audio:
        command += ["-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo"]
    command += [
        "-map", "0:v:0",
        "-map", "0:a:0" if info.has_audio else "1:a:0",
        "-vf", ",".join(video),
        "-af", ",".join(audio),
        "-frames:v", str(frames),
        "-t", f"{seconds:.6f}",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", str(params.crf),
        "-profile:v", "high", "-level:v", "4.0", "-pix_fmt", "yuv420p",
        "-r", f"{FPS_NUM}/{FPS_DEN}",
        # Every file opens on a keyframe and shares one GOP shape, which is what
        # lets a run of them be joined by stream copy later. No B-frames: they make
        # decode order differ from display order, and at a join that is exactly
        # where two packets end up with the same decode timestamp.
        "-x264-params", "keyint=30:min-keyint=30:scenecut=0:open-gop=0:bframes=0",
        "-c:a", "aac", "-b:a", f"{params.audio_kbps}k", "-ar", "48000", "-ac", "2",
        "-map_metadata", "-1", "-map_chapters", "-1",
        "-f", "matroska", str(target),
    ]
    return command


def find_tools(explicit: str | None = None) -> tuple[str | None, str | None]:
    """``(ffmpeg, ffprobe)``: the configured path, then ``PATH``. Either may be None."""
    import shutil

    ffmpeg = None
    if explicit and Path(explicit).is_file():
        ffmpeg = explicit
    ffmpeg = ffmpeg or shutil.which("ffmpeg")
    ffprobe = None
    if ffmpeg:
        sibling = Path(ffmpeg).with_name("ffprobe" + Path(ffmpeg).suffix)
        if sibling.is_file():
            ffprobe = str(sibling)
    return ffmpeg, ffprobe or shutil.which("ffprobe")


def output_path(root: Path, kind: str, key: str) -> Path:
    """Where the conformed copy of *key* lives. Stable, filesystem-safe, unique."""
    stem = re.sub(r"[^A-Za-z0-9._-]+", "_", key.split(":", 1)[-1]).strip("_")[:80]
    digest = hashlib.sha1(key.encode()).hexdigest()[:8]
    return root / kind / f"{stem}-{digest}.mkv"


def conform_one(
    *,
    key: str,
    kind: str,
    source: Path,
    out_root: Path,
    params: ConformParams,
    ffmpeg: str,
    ffprobe: str,
) -> ConformResult:
    """Conform a single file, atomically. Never raises: failures come back as data."""
    target = output_path(out_root, kind, key)
    try:
        info = probe_source(source, ffprobe)
        geometry = geometry_for(info.dar, params.width, params.height)
        loudness = measure_loudness(source, ffmpeg, params) if info.has_audio else None
        quiet = info.has_audio and (loudness is None or loudness["input_i"] < SILENT_LUFS)
        if quiet and kind == "ad":
            # A commercial with no audio is a bad rip, and normalising it would
            # turn the hiss up to full volume. Silence is *not* a fault for a bump
            # or an ident: plenty of text cards were meant to be silent.
            return ConformResult(
                key, ok=False, silent=True,
                note=f"audio is {'silent' if loudness is None else format(loudness['input_i'], '.0f') + ' LUFS'}",
            )
        if quiet:
            loudness = None
        target.parent.mkdir(parents=True, exist_ok=True)
        partial = target.with_suffix(".part.mkv")
        command = build_command(
            ffmpeg, source, partial, info, geometry, params,
            loudness=loudness, fade=(kind == "ad"),
        )
        run = subprocess.run(command, capture_output=True, text=True, check=False)
        if run.returncode != 0 or not partial.is_file():
            partial.unlink(missing_ok=True)
            return ConformResult(key, ok=False, note=(run.stderr or "ffmpeg failed").strip()[-200:])
        measured = _output_duration_ms(partial, ffprobe)
        if measured <= 0:
            partial.unlink(missing_ok=True)
            return ConformResult(key, ok=False, note="output has no duration")
        os.replace(partial, target)
        return ConformResult(
            key, ok=True, path=target, duration_ms=measured, signature=params.signature(),
            note=f"{geometry.canvas}",
        )
    except ConformError as exc:
        return ConformResult(key, ok=False, note=str(exc))
    except Exception as exc:  # noqa: BLE001 - one bad file must not stop a library pass
        return ConformResult(key, ok=False, note=f"{type(exc).__name__}: {exc}")


def _output_duration_ms(path: Path, ffprobe: str) -> int:
    run = subprocess.run(
        [ffprobe, "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
        capture_output=True, text=True, check=False,
    )
    value = _to_float(run.stdout.strip())
    return round(value * 1000) if value else 0


def conform_many(
    jobs: Iterable[dict],
    *,
    out_root: Path,
    params: ConformParams,
    ffmpeg: str,
    ffprobe: str,
    workers: int = 3,
    on_result: Callable[[ConformResult, int, int], None] | None = None,
) -> list[ConformResult]:
    """Conform many files, a few at a time, reporting each as it finishes."""
    todo = list(jobs)
    results: list[ConformResult] = []
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        futures = [
            pool.submit(
                conform_one, key=job["key"], kind=job["kind"], source=Path(job["path"]),
                out_root=out_root, params=params, ffmpeg=ffmpeg, ffprobe=ffprobe,
            )
            for job in todo
        ]
        for done, future in enumerate(as_completed(futures), start=1):
            result = future.result()
            results.append(result)
            if on_result:
                on_result(result, done, len(todo))
    return results


__all__ = [
    "CONFORM_KINDS",
    "ConformError",
    "ConformParams",
    "ConformResult",
    "Geometry",
    "RECIPE_VERSION",
    "SILENT_LUFS",
    "SourceInfo",
    "build_command",
    "conform_many",
    "conform_one",
    "display_aspect",
    "find_tools",
    "frame_count",
    "geometry_for",
    "output_path",
    "probe_source",
]
