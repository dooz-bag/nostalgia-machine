"""Analysis and safe export for mixed bumper/commercial source videos."""

from __future__ import annotations

import json
import math
import os
import re
import shutil
import subprocess
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Sequence

from .paths import Paths
from .sidecars import BumpTag, write as write_tag

VIDEO_EXTENSIONS = frozenset(
    {".avi", ".m4v", ".mkv", ".mov", ".mp4", ".mpeg", ".mpg", ".ogv", ".webm"}
)
SCENE_THRESHOLD = 0.35
MIN_SEGMENT_SECONDS = 2.0
MIN_YEAR = 1950
MAX_YEAR = 2099
_TIME_PATTERN = re.compile(r"pts_time:([0-9]+(?:\.[0-9]+)?)")


class MediaImportError(ValueError):
    """A user-correctable error in mixed-media analysis or import."""


@dataclass(frozen=True, slots=True)
class Analysis:
    token: str
    source_name: str
    duration: float
    cut_points: tuple[float, ...]
    source_size: int
    source_mtime_ns: int

    def to_json(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["cut_points"] = list(self.cut_points)
        return payload

    @classmethod
    def from_json(cls, payload: dict[str, Any]) -> "Analysis":
        try:
            analysis = cls(
                token=str(uuid.UUID(str(payload["token"]))),
                source_name=str(payload["source_name"]),
                duration=float(payload["duration"]),
                cut_points=tuple(float(point) for point in payload["cut_points"]),
                source_size=int(payload["source_size"]),
                source_mtime_ns=int(payload["source_mtime_ns"]),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise MediaImportError("The saved video analysis is invalid.") from exc
        if (
            not math.isfinite(analysis.duration)
            or analysis.duration <= 0
            or any(not math.isfinite(point) for point in analysis.cut_points)
        ):
            raise MediaImportError("The saved video analysis contains invalid times.")
        return analysis


def imports_dir(paths: Paths) -> Path:
    return paths.media_root / "imports"


def list_sources(paths: Paths) -> list[str]:
    folder = imports_dir(paths)
    folder.mkdir(parents=True, exist_ok=True)
    return sorted(
        path.name
        for path in folder.iterdir()
        if path.is_file() and path.suffix.lower() in VIDEO_EXTENSIONS
    )


def resolve_source(paths: Paths, name: str) -> Path:
    if not name or Path(name).name != name or "/" in name or "\\" in name:
        raise MediaImportError("Choose a video directly inside media/imports.")
    if Path(name).suffix.lower() not in VIDEO_EXTENSIONS:
        raise MediaImportError("That file type is not supported as a video source.")

    root = imports_dir(paths).resolve()
    try:
        source = (root / name).resolve(strict=True)
        source.relative_to(root)
    except (OSError, ValueError) as exc:
        raise MediaImportError("The selected video is not in media/imports.") from exc
    if not source.is_file():
        raise MediaImportError("The selected video is not a regular file.")
    return source


def _run(arguments: Sequence[str], tool_name: str, *, timeout: int) -> subprocess.CompletedProcess[str]:
    try:
        result = subprocess.run(
            list(arguments),
            capture_output=True,
            check=False,
            text=True,
            timeout=timeout,
        )
    except FileNotFoundError as exc:
        raise MediaImportError(f"{tool_name} was not found. Install FFmpeg.") from exc
    except subprocess.TimeoutExpired as exc:
        raise MediaImportError(f"{tool_name} timed out while processing the video.") from exc
    except OSError as exc:
        raise MediaImportError(f"Could not run {tool_name}: {exc}") from exc
    if result.returncode:
        detail = (result.stderr or result.stdout).strip()
        if len(detail) > 1200:
            detail = detail[-1200:]
        raise MediaImportError(
            f"{tool_name} failed with exit code {result.returncode}: {detail or 'no details'}"
        )
    return result


def _probe_video(path: Path, ffprobe: str) -> float:
    result = _run(
        [
            ffprobe,
            "-v",
            "error",
            "-show_entries",
            "format=duration:stream=codec_type",
            "-of",
            "json",
            str(path),
        ],
        "ffprobe",
        timeout=120,
    )
    try:
        payload = json.loads(result.stdout)
        duration = float(payload["format"]["duration"])
        has_video = any(
            stream.get("codec_type") == "video" for stream in payload.get("streams", [])
        )
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise MediaImportError("ffprobe could not read the video's duration.") from exc
    if not math.isfinite(duration) or duration <= 0:
        raise MediaImportError("The selected video has no usable duration.")
    if not has_video:
        raise MediaImportError("The selected file does not contain a video stream.")
    return duration


def _scene_cuts(path: Path, ffmpeg: str, duration: float) -> tuple[float, ...]:
    result = _run(
        [
            ffmpeg,
            "-hide_banner",
            "-nostdin",
            "-i",
            str(path),
            "-filter:v",
            f"select='gt(scene\\,{SCENE_THRESHOLD})',showinfo",
            "-an",
            "-f",
            "null",
            "-",
        ],
        "ffmpeg",
        timeout=3600,
    )
    candidates = {0.0, round(duration, 3)}
    for match in _TIME_PATTERN.finditer(result.stderr):
        point = float(match.group(1))
        if 0.05 < point < duration - 0.05:
            candidates.add(round(point, 3))
    return tuple(sorted(candidates))


def _analysis_path(paths: Paths, token: str) -> Path:
    try:
        token = str(uuid.UUID(token))
    except ValueError as exc:
        raise MediaImportError("That video review has expired or is invalid.") from exc
    return paths.data_dir / "media-imports" / f"{token}.json"


def analyze_source(paths: Paths, name: str) -> Analysis:
    source = resolve_source(paths, name)
    ffmpeg = shutil.which("ffmpeg")
    ffprobe = shutil.which("ffprobe")
    if ffmpeg is None or ffprobe is None:
        missing = "ffmpeg" if ffmpeg is None else "ffprobe"
        raise MediaImportError(f"{missing} was not found. Install FFmpeg.")

    duration = _probe_video(source, ffprobe)
    cuts = _scene_cuts(source, ffmpeg, duration)
    stat = source.stat()
    analysis = Analysis(
        token=str(uuid.uuid4()),
        source_name=source.name,
        duration=duration,
        cut_points=cuts,
        source_size=stat.st_size,
        source_mtime_ns=stat.st_mtime_ns,
    )

    folder = paths.data_dir / "media-imports"
    folder.mkdir(parents=True, exist_ok=True)
    target = _analysis_path(paths, analysis.token)
    temporary = target.with_suffix(".tmp")
    try:
        temporary.write_text(
            json.dumps(analysis.to_json(), indent=2) + "\n", encoding="utf-8"
        )
        os.replace(temporary, target)
    except OSError as exc:
        temporary.unlink(missing_ok=True)
        raise MediaImportError(f"Could not save the video analysis: {exc}") from exc
    return analysis


def load_analysis(paths: Paths, token: str) -> tuple[Analysis, Path]:
    manifest = _analysis_path(paths, token)
    try:
        payload = json.loads(manifest.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise MediaImportError("That video review has expired or is invalid.") from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise MediaImportError(f"Could not read the saved video analysis: {exc}") from exc
    if not isinstance(payload, dict):
        raise MediaImportError("The saved video analysis is invalid.")
    analysis = Analysis.from_json(payload)
    if analysis.token != str(uuid.UUID(token)):
        raise MediaImportError("The saved video analysis does not match this review.")
    source = resolve_source(paths, analysis.source_name)
    stat = source.stat()
    if stat.st_size != analysis.source_size or stat.st_mtime_ns != analysis.source_mtime_ns:
        raise MediaImportError(
            "The source video changed after analysis. Analyze it again before exporting."
        )
    return analysis, source


def suggested_label(duration: float) -> str:
    """Suggest, but never decide, a label using only common clip durations."""
    if duration < 4:
        return "skip"
    if any(abs(duration - target) <= 3 for target in (15, 30, 60)):
        return "commercial"
    if duration <= 13:
        return "bumper"
    if duration <= 65:
        return "commercial"
    return "skip"


def _valid_year(value: str | None, field: str) -> int | None:
    if value is None or not value.strip():
        return None
    if not value.isdigit():
        raise MediaImportError(f"{field} must be a four-digit year.")
    year = int(value)
    if not MIN_YEAR <= year <= MAX_YEAR:
        raise MediaImportError(f"{field} must be between {MIN_YEAR} and {MAX_YEAR}.")
    return year


def _validated_segments(
    raw_segments: Any, duration: float
) -> list[tuple[float, float, str]]:
    if not isinstance(raw_segments, list) or not raw_segments:
        raise MediaImportError("Mark at least one segment before exporting.")
    if len(raw_segments) > 500:
        raise MediaImportError("Review at most 500 segments in one import.")
    segments: list[tuple[float, float, str]] = []
    previous_end = 0.0
    for index, raw in enumerate(raw_segments, start=1):
        if not isinstance(raw, dict):
            raise MediaImportError(f"Segment {index} is invalid.")
        try:
            start = float(raw["start"])
            end = float(raw["end"])
            label = str(raw["label"])
        except (KeyError, TypeError, ValueError) as exc:
            raise MediaImportError(f"Segment {index} needs valid start and end times.") from exc
        if not math.isfinite(start) or not math.isfinite(end):
            raise MediaImportError(f"Segment {index} needs finite start and end times.")
        if start < 0 or end > duration or end - start < MIN_SEGMENT_SECONDS:
            raise MediaImportError(
                f"Segment {index} must be at least {MIN_SEGMENT_SECONDS:g} seconds "
                "and stay within the source video."
            )
        if start < previous_end:
            raise MediaImportError("Segments must be ordered and cannot overlap.")
        if label not in {"bumper", "commercial", "skip"}:
            raise MediaImportError(f"Segment {index} has an unknown classification.")
        segments.append((start, end, label))
        previous_end = end
    if not any(label != "skip" for _, _, label in segments):
        raise MediaImportError("Choose at least one bumper or commercial to export.")
    return segments


def _slug(value: str) -> str:
    result = re.sub(r"[^a-zA-Z0-9_-]+", "-", value).strip("-_").lower()
    return result or "source"


def _encode_segment(
    ffmpeg: str, source: Path, start: float, end: float, target: Path
) -> None:
    _run(
        [
            ffmpeg,
            "-hide_banner",
            "-nostdin",
            "-loglevel",
            "error",
            "-y",
            "-ss",
            f"{start:.3f}",
            "-i",
            str(source),
            "-t",
            f"{end - start:.3f}",
            "-map",
            "0:v:0?",
            "-map",
            "0:a:0?",
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "20",
            "-c:a",
            "aac",
            "-b:a",
            "128k",
            "-movflags",
            "+faststart",
            str(target),
        ],
        "ffmpeg",
        timeout=3600,
    )
    if not target.is_file() or target.stat().st_size == 0:
        raise MediaImportError(f"ffmpeg did not create the expected clip {target.name}.")


def export_segments(
    paths: Paths,
    token: str,
    raw_segments: Any,
    *,
    ad_year: str | None,
    bump_subject: str | None,
    bump_era: str | None,
    known_subjects: set[str],
) -> list[Path]:
    analysis, source = load_analysis(paths, token)
    segments = _validated_segments(raw_segments, analysis.duration)
    labels = {label for _, _, label in segments}
    commercial_year = None
    if "commercial" in labels:
        commercial_year = _valid_year(ad_year, "Commercial year")
        if commercial_year is None:
            raise MediaImportError(
                "Commercial year is required when exporting commercials."
            )
    era = _valid_year(bump_era, "Bumper era") if "bumper" in labels else None

    subject = (bump_subject or "").strip()
    if "bumper" in labels:
        if subject != "neutral" and subject not in known_subjects:
            raise MediaImportError("Choose a configured show or neutral for the bumpers.")
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        raise MediaImportError("ffmpeg was not found. Install FFmpeg.")

    token_suffix = analysis.token.replace("-", "")[:8]
    stem = _slug(Path(analysis.source_name).stem)
    exports: list[tuple[Path, Path, BumpTag | None]] = []
    index = 0
    for start, end, label in segments:
        if label == "skip":
            continue
        index += 1
        name = f"{stem}-import-{token_suffix}-{index:03}.mp4"
        if label == "commercial":
            folder = paths.media_root / "commercials" / str(commercial_year)
            tag = None
        else:
            folder_name = "neutral" if subject == "neutral" else subject
            folder = paths.media_root / "bumps" / folder_name
            tag = BumpTag(
                subject=subject,
                position="ANY",
                era=era,
                source="import",
                confidence=1.0,
                confirmed=True,
            )
        target = folder / name
        sidecar = target.with_name(target.name + ".tags.json") if tag else None
        if target.exists() or (sidecar is not None and sidecar.exists()):
            raise MediaImportError(
                f"Refusing to overwrite an existing import: {target.name}."
            )
        temporary = folder / f".{target.stem}.{analysis.token}.partial.mp4"
        exports.append((temporary, target, tag))

    staged: list[Path] = []
    committed: list[Path] = []
    sidecars: list[Path] = []
    try:
        for (start, end, _label), (temporary, _, _) in zip(
            (segment for segment in segments if segment[2] != "skip"), exports
        ):
            temporary.parent.mkdir(parents=True, exist_ok=True)
            if temporary.exists():
                raise MediaImportError(
                    f"A temporary import already exists: {temporary.name}."
                )
            staged.append(temporary)
            _encode_segment(ffmpeg, source, start, end, temporary)

        stat = source.stat()
        if stat.st_size != analysis.source_size or stat.st_mtime_ns != analysis.source_mtime_ns:
            raise MediaImportError(
                "The source video changed while its clips were being exported."
            )

        for temporary, target, tag in exports:
            if target.exists():
                raise MediaImportError(
                    f"Refusing to overwrite an existing import: {target.name}."
                )
            os.replace(temporary, target)
            committed.append(target)
            if tag is not None:
                sidecar = target.with_name(target.name + ".tags.json")
                sidecars.append(sidecar)
                sidecar = write_tag(target, tag)
    except (MediaImportError, OSError) as exc:
        cleanup_errors: list[str] = []
        for path in sidecars + committed + staged:
            try:
                path.unlink(missing_ok=True)
            except OSError as cleanup_exc:
                cleanup_errors.append(f"{path}: {cleanup_exc}")
        if cleanup_errors:
            raise MediaImportError(
                f"Import failed: {exc}. Cleanup also failed: {'; '.join(cleanup_errors)}"
            ) from exc
        if isinstance(exc, MediaImportError):
            raise
        raise MediaImportError(f"Could not finish importing clips: {exc}") from exc
    return committed


__all__ = [
    "Analysis",
    "MediaImportError",
    "analyze_source",
    "export_segments",
    "imports_dir",
    "list_sources",
    "load_analysis",
    "resolve_source",
    "suggested_label",
]
