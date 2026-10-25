"""ffprobe wrapper with a graceful no-ffmpeg path.

Durations must be exact for ad pods to sum exactly, so they are stored in
milliseconds from the container, not estimated. When ffprobe is unavailable the
catalog falls back to sidecar data, which keeps ``scan`` and every test usable on
a machine without ffmpeg installed.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

VIDEO_EXTENSIONS = {
    ".mkv",
    ".mp4",
    ".m4v",
    ".avi",
    ".mov",
    ".mpg",
    ".mpeg",
    ".ts",
    ".m2ts",
    ".webm",
    ".ogv",
    ".wmv",
    ".flv",
}


class ProbeError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class MediaInfo:
    duration_ms: int
    format_name: str | None = None
    video_codec: str | None = None
    width: int | None = None
    height: int | None = None
    audio_codec: str | None = None
    audio_channels: int | None = None
    source: str = "ffprobe"


def find_ffprobe(explicit: str | None = None) -> str | None:
    """Locate ffprobe: explicit config, ``PATH``, then a bundled sibling."""
    if explicit:
        path = Path(explicit)
        if path.is_file():
            return str(path)
        sibling = path.with_name("ffprobe" + path.suffix)
        if sibling.is_file():
            return str(sibling)
        raise ProbeError(f"ffprobe not found at {explicit}")

    found = shutil.which("ffprobe")
    if found:
        return found

    bundled = Path(__file__).resolve().parent / "_bin" / "ffprobe.exe"
    return str(bundled) if bundled.is_file() else None


def probe(path: Path, ffprobe: str | None = None) -> MediaInfo:
    """Return authoritative media info, or raise :class:`ProbeError`."""
    binary = find_ffprobe(ffprobe)
    if not binary:
        raise ProbeError("ffprobe is not installed; set ffmpeg_path or add sidecars")
    if not path.is_file():
        raise ProbeError(f"missing file: {path}")

    command = [
        binary,
        "-v",
        "error",
        "-show_entries",
        "format=duration,format_name:stream=codec_type,codec_name,width,height,channels",
        "-of",
        "json",
        str(path),
    ]
    try:
        result = subprocess.run(
            command, capture_output=True, text=True, timeout=120, check=False
        )
    except OSError as exc:
        raise ProbeError(f"cannot run ffprobe: {exc}") from exc
    if result.returncode != 0:
        raise ProbeError(f"ffprobe failed for {path.name}: {result.stderr.strip()}")

    payload = json.loads(result.stdout or "{}")
    fmt = payload.get("format", {})
    streams = payload.get("streams", [])
    duration = _duration_ms(fmt.get("duration"))
    if duration <= 0:
        raise ProbeError(f"no usable duration for {path.name}")

    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    return MediaInfo(
        duration_ms=duration,
        format_name=fmt.get("format_name"),
        video_codec=(video or {}).get("codec_name"),
        width=(video or {}).get("width"),
        height=(video or {}).get("height"),
        audio_codec=(audio or {}).get("codec_name"),
        audio_channels=(audio or {}).get("channels"),
    )


def _duration_ms(value: object) -> int:
    try:
        return round(float(value) * 1000)
    except (TypeError, ValueError):
        return 0


def sidecar_duration_ms(path: Path) -> int | None:
    """Duration from a ``<file>.json`` sidecar, if one exists.

    The sidecar is what :command:`nostalgia scan` writes when ffprobe is absent,
    so a catalogued library keeps working. Only ``duration_ms`` is read.
    """
    sidecar = path.with_suffix(path.suffix + ".json")
    if not sidecar.is_file():
        return None
    try:
        payload = json.loads(sidecar.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    duration = payload.get("duration_ms")
    return int(duration) if isinstance(duration, (int, float)) and duration > 0 else None


def write_sidecar(path: Path, info: MediaInfo) -> None:
    sidecar = path.with_suffix(path.suffix + ".json")
    payload = {
        "duration_ms": info.duration_ms,
        "format_name": info.format_name,
        "video": {"codec": info.video_codec, "width": info.width, "height": info.height},
        "audio": {"codec": info.audio_codec, "channels": info.audio_channels},
    }
    sidecar.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def is_media(path: Path) -> bool:
    return path.is_file() and path.suffix.lower() in VIDEO_EXTENSIONS


__all__ = [
    "MediaInfo",
    "ProbeError",
    "VIDEO_EXTENSIONS",
    "find_ffprobe",
    "is_media",
    "probe",
    "sidecar_duration_ms",
    "write_sidecar",
]
