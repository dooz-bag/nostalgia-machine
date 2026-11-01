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
from threading import Lock
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
_REVIEW_LOCK = Lock()


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


@dataclass(frozen=True, slots=True)
class PendingImportClip:
    clip_id: str
    path: Path
    source_name: str
    start: float
    end: float

    @property
    def duration(self) -> float:
        return self.end - self.start


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
    ranges = _validated_ranges(raw_segments, duration)
    segments: list[tuple[float, float, str]] = []
    for index, (raw, (start, end)) in enumerate(zip(raw_segments, ranges), start=1):
        label = str(raw.get("label", ""))
        if label not in {"bumper", "commercial", "skip"}:
            raise MediaImportError(f"Segment {index} has an unknown classification.")
        segments.append((start, end, label))
    if not any(label != "skip" for _, _, label in segments):
        raise MediaImportError("Choose at least one bumper or commercial to export.")
    return segments


def _validated_ranges(raw_segments: Any, duration: float) -> list[tuple[float, float]]:
    if not isinstance(raw_segments, list) or not raw_segments:
        raise MediaImportError("Mark at least one clip before cutting.")
    if len(raw_segments) > 500:
        raise MediaImportError("Review at most 500 segments in one import.")
    ranges: list[tuple[float, float]] = []
    previous_end = 0.0
    for index, raw in enumerate(raw_segments, start=1):
        if not isinstance(raw, dict):
            raise MediaImportError(f"Segment {index} is invalid.")
        try:
            start = float(raw["start"])
            end = float(raw["end"])
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
        ranges.append((start, end))
        previous_end = end
    return ranges


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


def _pending_clip_path(paths: Paths, clip_id: str) -> Path:
    try:
        clip_id = str(uuid.UUID(clip_id))
    except ValueError as exc:
        raise MediaImportError("That imported clip is invalid or has already been handled.") from exc
    return paths.media("derived", "import-review", f"{clip_id}.mp4")


def _pending_manifest_path(paths: Paths, clip_id: str) -> Path:
    return paths.data_dir / "media-imports" / "review" / f"{clip_id}.json"


def _review_processing_dir(paths: Paths) -> Path:
    return paths.data_dir / "media-imports" / "review" / ".processing"


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    temporary = path.with_suffix(f".{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        os.replace(temporary, path)
    except OSError:
        temporary.unlink(missing_ok=True)
        raise


def _recover_review_operations(paths: Paths) -> list[str]:
    processing = _review_processing_dir(paths)
    if not processing.exists():
        return []
    if not processing.is_dir():
        raise MediaImportError(f"The import review journal path is not a folder: {processing}")
    try:
        journals = sorted(processing.glob("*.json"))
    except OSError as exc:
        raise MediaImportError(f"Could not inspect import review operations: {exc}") from exc
    issues: list[str] = []
    for journal in journals:
        try:
            clip_id = str(uuid.UUID(journal.stem))
            payload = json.loads(journal.read_text(encoding="utf-8"))
            if not isinstance(payload, dict) or payload.get("clip_id") != clip_id:
                raise ValueError("record does not match clip id")
            source_name = str(payload["source_name"])
            start = float(payload["start"])
            end = float(payload["end"])
            action = payload["action"]
            if (
                Path(source_name).name != source_name
                or not math.isfinite(start)
                or not math.isfinite(end)
                or start < 0
                or end - start < MIN_SEGMENT_SECONDS
            ):
                raise ValueError("invalid source name or clip range")
            if action not in {"trash", "classify"}:
                raise ValueError(f"unknown review action {action!r}")
            target = None
            if action == "classify":
                relative_target = Path(str(payload["target"]))
                if (
                    relative_target.is_absolute()
                    or len(relative_target.parts) < 3
                    or relative_target.parts[0] != "media"
                    or relative_target.parts[1] not in {"bumps", "commercials"}
                ):
                    raise ValueError("target must be inside the bumper or commercial library")
                target = (paths.root / relative_target).resolve()
                target.relative_to(paths.root.resolve())
                tag_payload = payload.get("tag")
                if tag_payload is not None:
                    BumpTag.from_json(tag_payload)
        except OSError as exc:
            raise MediaImportError(
                f"Could not read import review journal {journal.name}: {exc}"
            ) from exc
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            broken = processing / ".broken" / journal.name
            try:
                broken.parent.mkdir(parents=True, exist_ok=True)
                if broken.exists():
                    broken = broken.with_name(f"{journal.stem}-{uuid.uuid4().hex}.json")
                os.replace(journal, broken)
            except OSError as quarantine_exc:
                raise MediaImportError(
                    f"Invalid review journal {journal.name}: {exc}. Could not "
                    f"quarantine it: {quarantine_exc}"
                ) from quarantine_exc
            issues.append(
                f"Review journal {journal.name} was quarantined: {exc}. See {broken}."
            )
            continue

        manifest = _pending_manifest_path(paths, clip_id)
        if manifest.exists():
            try:
                journal.unlink()
            except OSError as exc:
                raise MediaImportError(
                    f"Could not clear duplicate review journal {journal.name}: {exc}"
                ) from exc
            continue

        source = _pending_clip_path(paths, clip_id)
        if action == "trash":
            if source.is_file():
                _write_json_atomic(
                    manifest,
                    {
                        "clip_id": clip_id,
                        "source_name": source_name,
                        "start": start,
                        "end": end,
                    },
                )
            try:
                journal.unlink()
            except OSError as exc:
                raise MediaImportError(
                    f"Could not finish recovering trashed clip {clip_id}: {exc}"
                ) from exc
            continue

        if source.exists() and target is not None and target.exists():
            broken = processing / ".broken" / journal.name
            try:
                broken.parent.mkdir(parents=True, exist_ok=True)
                if broken.exists():
                    broken = broken.with_name(f"{journal.stem}-{uuid.uuid4().hex}.json")
                os.replace(journal, broken)
            except OSError as exc:
                raise MediaImportError(
                    f"Both copies of review clip {clip_id} exist. Could not "
                    f"quarantine its journal: {exc}"
                ) from exc
            issues.append(
                f"Both pending and classified copies of {clip_id} exist; "
                f"the journal was quarantined at {broken}."
            )
            continue
        if source.exists():
            _write_json_atomic(
                manifest,
                {
                    "clip_id": clip_id,
                    "source_name": source_name,
                    "start": start,
                    "end": end,
                },
            )
        elif target is not None and target.is_file():
            tag_payload = payload.get("tag")
            if tag_payload is not None:
                sidecar = target.with_name(target.name + ".tags.json")
                if not sidecar.exists():
                    write_tag(target, BumpTag.from_json(tag_payload))
            journal.unlink()
            continue
        else:
            broken = processing / ".broken" / journal.name
            try:
                broken.parent.mkdir(parents=True, exist_ok=True)
                if broken.exists():
                    broken = broken.with_name(f"{journal.stem}-{uuid.uuid4().hex}.json")
                os.replace(journal, broken)
            except OSError as exc:
                raise MediaImportError(
                    f"Neither copy of review clip {clip_id} exists. Could not "
                    f"quarantine its journal: {exc}"
                ) from exc
            issues.append(
                f"Neither pending nor classified copy of {clip_id} exists; "
                f"the journal was quarantined at {broken}."
            )
            continue
        try:
            journal.unlink()
        except OSError as exc:
            raise MediaImportError(
                f"Could not clear recovered review journal {journal.name}: {exc}"
            ) from exc
    return issues


def _read_pending_clip(paths: Paths, clip_id: str) -> PendingImportClip:
    try:
        clip_id = str(uuid.UUID(clip_id))
    except ValueError as exc:
        raise MediaImportError("That imported clip is invalid or has already been handled.") from exc
    manifest = _pending_manifest_path(paths, clip_id)
    try:
        payload = json.loads(manifest.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise MediaImportError("That imported clip is invalid or has already been handled.") from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise MediaImportError(f"Could not read the imported clip review record: {exc}") from exc
    try:
        if not isinstance(payload, dict) or payload.get("clip_id") != clip_id:
            raise ValueError("record does not match clip")
        source_name = str(payload["source_name"])
        if Path(source_name).name != source_name:
            raise ValueError("invalid source name")
        start = float(payload["start"])
        end = float(payload["end"])
        if not math.isfinite(start) or not math.isfinite(end) or start < 0 or end - start < MIN_SEGMENT_SECONDS:
            raise ValueError("invalid clip range")
    except (KeyError, TypeError, ValueError) as exc:
        raise MediaImportError(f"The imported clip review record is invalid: {exc}") from exc
    clip_path = _pending_clip_path(paths, clip_id)
    if not clip_path.is_file():
        raise MediaImportError(f"The cut clip {clip_id} is missing from the review folder.")
    return PendingImportClip(clip_id, clip_path, source_name, start, end)


def pending_import_clips(paths: Paths) -> list[PendingImportClip]:
    return pending_import_clips_with_issues(paths)[0]


def pending_import_clips_with_issues(
    paths: Paths,
) -> tuple[list[PendingImportClip], list[str]]:
    with _REVIEW_LOCK:
        issues = _recover_review_operations(paths)
        folder = paths.data_dir / "media-imports" / "review"
        if not folder.exists():
            return [], issues
        if not folder.is_dir():
            raise MediaImportError(f"The import review path is not a folder: {folder}")
        clips: list[PendingImportClip] = []
        try:
            manifests = sorted(folder.glob("*.json"))
        except OSError as exc:
            raise MediaImportError(f"Could not list imported clips awaiting review: {exc}") from exc
        for manifest in manifests:
            try:
                clip_id = str(uuid.UUID(manifest.stem))
                clips.append(_read_pending_clip(paths, clip_id))
            except (MediaImportError, ValueError) as exc:
                quarantine = folder / ".broken" / manifest.name
                try:
                    quarantine.parent.mkdir(parents=True, exist_ok=True)
                    os.replace(manifest, quarantine)
                except OSError as quarantine_exc:
                    raise MediaImportError(
                        f"Invalid review record {manifest.name}: {exc}. Could not "
                        f"quarantine it: {quarantine_exc}"
                    ) from quarantine_exc
                issues.append(
                    f"Review record {manifest.name} was quarantined: {exc}. "
                    f"See {quarantine}."
                )
        return clips, issues


def resolve_pending_clip(paths: Paths, clip_id: str) -> Path:
    with _REVIEW_LOCK:
        _recover_review_operations(paths)
        return _read_pending_clip(paths, clip_id).path


def stage_segments(
    paths: Paths, token: str, raw_segments: Any
) -> list[PendingImportClip]:
    """Cut reviewed ranges into an import-only queue before they enter the library."""
    analysis, source = load_analysis(paths, token)
    ranges = _validated_ranges(raw_segments, analysis.duration)
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        raise MediaImportError("ffmpeg was not found. Install FFmpeg.")

    pending_dir = paths.media("derived", "import-review")
    manifest_dir = paths.data_dir / "media-imports" / "review"
    pending_dir.mkdir(parents=True, exist_ok=True)
    manifest_dir.mkdir(parents=True, exist_ok=True)
    staged: list[Path] = []
    manifests: list[Path] = []
    temporary: list[Path] = []
    temporary_manifests: list[Path] = []
    result: list[PendingImportClip] = []
    try:
        for start, end in ranges:
            clip_id = str(uuid.uuid4())
            target = _pending_clip_path(paths, clip_id)
            manifest = _pending_manifest_path(paths, clip_id)
            partial = target.with_name(f".{clip_id}.partial.mp4")
            temporary.append(partial)
            _encode_segment(ffmpeg, source, start, end, partial)
            stat = source.stat()
            if stat.st_size != analysis.source_size or stat.st_mtime_ns != analysis.source_mtime_ns:
                raise MediaImportError(
                    "The source video changed while its clips were being cut."
                )
            os.replace(partial, target)
            staged.append(target)
            payload = {
                "clip_id": clip_id,
                "source_name": analysis.source_name,
                "start": round(start, 3),
                "end": round(end, 3),
            }
            manifest_tmp = manifest.with_suffix(".tmp")
            temporary_manifests.append(manifest_tmp)
            manifest_tmp.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
            os.replace(manifest_tmp, manifest)
            temporary_manifests.remove(manifest_tmp)
            manifests.append(manifest)
            result.append(PendingImportClip(clip_id, target, analysis.source_name, start, end))
        return result
    except (MediaImportError, OSError) as exc:
        cleanup_errors: list[str] = []
        for path in manifests + staged + temporary + temporary_manifests:
            try:
                path.unlink(missing_ok=True)
            except OSError as cleanup_exc:
                cleanup_errors.append(f"{path}: {cleanup_exc}")
        if cleanup_errors:
            raise MediaImportError(
                f"Could not stage imported clips: {exc}. Cleanup also failed: "
                f"{'; '.join(cleanup_errors)}"
            ) from exc
        if isinstance(exc, MediaImportError):
            raise
        raise MediaImportError(f"Could not stage imported clips: {exc}") from exc


def classify_pending_clip(
    paths: Paths,
    clip_id: str,
    *,
    classification: str,
    ad_year: str | None,
    bump_subject: str | None,
    bump_era: str | None,
    position: str | None,
    known_subjects: set[str],
) -> Path:
    """Move a reviewed cut into its library or delete only that cut if trashed."""
    with _REVIEW_LOCK:
        _recover_review_operations(paths)
        return _classify_pending_clip_locked(
            paths,
            clip_id,
            classification=classification,
            ad_year=ad_year,
            bump_subject=bump_subject,
            bump_era=bump_era,
            position=position,
            known_subjects=known_subjects,
        )


def _classify_pending_clip_locked(
    paths: Paths,
    clip_id: str,
    *,
    classification: str,
    ad_year: str | None,
    bump_subject: str | None,
    bump_era: str | None,
    position: str | None,
    known_subjects: set[str],
) -> Path:
    clip = _read_pending_clip(paths, clip_id)
    manifest = _pending_manifest_path(paths, clip.clip_id)
    journal_dir = _review_processing_dir(paths)
    journal = journal_dir / f"{clip.clip_id}.json"
    if classification == "trash":
        journal_dir.mkdir(parents=True, exist_ok=True)
        _write_json_atomic(
            journal,
            {
                "clip_id": clip.clip_id,
                "source_name": clip.source_name,
                "start": clip.start,
                "end": clip.end,
                "action": "trash",
            },
        )
        try:
            manifest.unlink()
            clip.path.unlink()
            journal.unlink()
        except OSError as exc:
            try:
                _recover_review_operations(paths)
            except MediaImportError as recovery_exc:
                raise MediaImportError(
                    f"Could not trash the cut clip: {exc}. Recovery failed: {recovery_exc}"
                ) from exc
            if not clip.path.exists():
                return clip.path
            raise MediaImportError(f"Could not trash the cut clip: {exc}") from exc
        return clip.path
    if classification not in {"commercial", "bumper"}:
        raise MediaImportError("Choose commercial, bumper, or trash.")

    if classification == "commercial":
        year = _valid_year(ad_year, "Commercial year")
        if year is None:
            raise MediaImportError("Commercial year is required.")
        folder = paths.media("commercials", str(year))
        tag = None
    else:
        subject = (bump_subject or "").strip()
        if subject != "neutral" and subject not in known_subjects:
            raise MediaImportError("Choose a configured show or neutral for the bumper.")
        era = _valid_year(bump_era, "Bumper era")
        normalized_position = (position or "").strip().upper() or None
        if normalized_position is not None and normalized_position not in {"INTO", "OUT", "AFTER"}:
            raise MediaImportError("Choose before, after, middle, or unspecified placement.")
        folder = paths.media("bumps", "neutral" if subject == "neutral" else subject)
        tag = BumpTag(
            subject=subject,
            position=normalized_position,
            era=era,
            source="import",
            confidence=1.0,
            confirmed=True,
        )

    target = folder / f"{_slug(Path(clip.source_name).stem)}-import-{clip.clip_id[:8]}.mp4"
    if target.exists() or (tag is not None and target.with_name(target.name + ".tags.json").exists()):
        raise MediaImportError(f"Refusing to overwrite an existing import: {target.name}.")
    folder.mkdir(parents=True, exist_ok=True)
    journal_dir.mkdir(parents=True, exist_ok=True)
    _write_json_atomic(
        journal,
        {
            "clip_id": clip.clip_id,
            "source_name": clip.source_name,
            "start": clip.start,
            "end": clip.end,
            "action": "classify",
            "target": target.relative_to(paths.root).as_posix(),
            "tag": tag.to_json() if tag is not None else None,
        },
    )
    try:
        manifest.unlink()
        os.replace(clip.path, target)
        if tag is not None:
            write_tag(target, tag)
        journal.unlink()
    except OSError as exc:
        try:
            _recover_review_operations(paths)
        except MediaImportError as recovery_exc:
            raise MediaImportError(
                f"Could not finish classifying the cut clip: {exc}. "
                f"Recovery failed: {recovery_exc}"
            ) from exc
        if target.is_file() and not clip.path.exists():
            return target
        raise MediaImportError(f"Could not finish classifying the cut clip: {exc}") from exc
    return target


def trash_pending_clip(paths: Paths, clip_id: str) -> None:
    classify_pending_clip(
        paths,
        clip_id,
        classification="trash",
        ad_year=None,
        bump_subject=None,
        bump_era=None,
        position=None,
        known_subjects=set(),
    )


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
    "PendingImportClip",
    "analyze_source",
    "classify_pending_clip",
    "export_segments",
    "imports_dir",
    "list_sources",
    "load_analysis",
    "pending_import_clips",
    "pending_import_clips_with_issues",
    "resolve_pending_clip",
    "resolve_source",
    "stage_segments",
    "suggested_label",
    "trash_pending_clip",
]
