"""Mixed bumper/commercial analysis and import safeguards."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import nostalgia.media_review as media_review
from nostalgia.media_review import Analysis, MediaImportError
from nostalgia.sidecars import path_for as tag_path


def _analyze(paths, monkeypatch, *, duration: float = 80.0) -> tuple[Analysis, Path]:
    source = paths.media_root / "imports" / "mixed tape.mp4"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(b"source video")
    monkeypatch.setattr(media_review.shutil, "which", lambda tool: tool)
    monkeypatch.setattr(media_review, "_probe_video", lambda *_: duration)
    monkeypatch.setattr(
        media_review,
        "_scene_cuts",
        lambda *_: (0.0, round(duration / 2, 3), duration),
    )
    return media_review.analyze_source(paths, source.name), source


def test_suggested_labels_are_only_duration_based_hints():
    assert media_review.suggested_label(8) == "bumper"
    assert media_review.suggested_label(30) == "commercial"
    assert media_review.suggested_label(90) == "skip"


def test_analyze_and_export_approved_bumper_and_commercials(paths, monkeypatch):
    analysis, source = _analyze(paths, monkeypatch, duration=80)
    source_bytes = source.read_bytes()

    def fake_encode(_ffmpeg, _source, start, end, target):
        target.write_bytes(f"{start:.3f}:{end:.3f}".encode())

    monkeypatch.setattr(media_review, "_encode_segment", fake_encode)
    exported = media_review.export_segments(
        paths,
        analysis.token,
        [
            {"start": 1, "end": 9, "label": "bumper"},
            {"start": 10, "end": 40, "label": "commercial"},
            {"start": 40, "end": 50, "label": "skip"},
        ],
        ad_year="2003",
        bump_subject="neutral",
        bump_era="2002",
        known_subjects={"futurama"},
    )

    assert len(exported) == 2
    bump = next(path for path in exported if "bumps" in path.parts)
    commercial = next(path for path in exported if "commercials" in path.parts)
    assert bump.parent == paths.media("bumps", "neutral")
    assert commercial.parent == paths.media("commercials", "2003")
    tag = json.loads(tag_path(bump).read_text(encoding="utf-8"))
    assert tag == {
        "confirmed": True,
        "confidence": 1.0,
        "era": 2002,
        "position": "ANY",
        "source": "import",
        "subject": "neutral",
    }
    assert source.read_bytes() == source_bytes


def test_cut_segments_wait_in_import_review_without_touching_source(paths, monkeypatch):
    analysis, source = _analyze(paths, monkeypatch, duration=80)
    source_bytes = source.read_bytes()
    monkeypatch.setattr(
        media_review,
        "_encode_segment",
        lambda _ffmpeg, _source, start, end, target: target.write_bytes(
            f"{start:.3f}:{end:.3f}".encode()
        ),
    )

    clips = media_review.stage_segments(
        paths,
        analysis.token,
        [{"start": 1, "end": 9}, {"start": 10, "end": 40}],
    )

    assert len(clips) == 2
    assert all(clip.path.is_file() for clip in clips)
    assert [clip.duration for clip in clips] == [8, 30]
    assert source.read_bytes() == source_bytes
    assert {
        clip.clip_id for clip in media_review.pending_import_clips(paths)
    } == {clip.clip_id for clip in clips}


def test_classifying_pending_bumper_sets_show_position_and_year(paths, monkeypatch):
    analysis, _source = _analyze(paths, monkeypatch, duration=80)
    monkeypatch.setattr(
        media_review,
        "_encode_segment",
        lambda _ffmpeg, _source, start, end, target: target.write_bytes(
            f"{start:.3f}:{end:.3f}".encode()
        ),
    )
    (clip,) = media_review.stage_segments(
        paths, analysis.token, [{"start": 1, "end": 9}]
    )

    target = media_review.classify_pending_clip(
        paths,
        clip.clip_id,
        classification="bumper",
        ad_year="",
        bump_subject="futurama",
        bump_era="2003",
        position="OUT",
        known_subjects={"futurama"},
    )

    assert target.parent == paths.media("bumps", "futurama")
    assert target.is_file()
    assert not media_review.pending_import_clips(paths)
    assert json.loads(tag_path(target).read_text(encoding="utf-8")) == {
        "confirmed": True,
        "confidence": 1.0,
        "era": 2003,
        "position": "OUT",
        "source": "import",
        "subject": "futurama",
    }


def test_trashing_pending_clip_removes_only_cut_copy(paths, monkeypatch):
    analysis, source = _analyze(paths, monkeypatch, duration=80)
    source_bytes = source.read_bytes()
    monkeypatch.setattr(
        media_review,
        "_encode_segment",
        lambda _ffmpeg, _source, start, end, target: target.write_bytes(
            f"{start:.3f}:{end:.3f}".encode()
        ),
    )
    (clip,) = media_review.stage_segments(
        paths, analysis.token, [{"start": 1, "end": 9}]
    )

    media_review.trash_pending_clip(paths, clip.clip_id)

    assert not clip.path.exists()
    assert source.read_bytes() == source_bytes
    assert not media_review.pending_import_clips(paths)


def test_review_queue_recovers_classification_after_process_restart(paths, monkeypatch):
    analysis, _source = _analyze(paths, monkeypatch, duration=80)
    monkeypatch.setattr(
        media_review,
        "_encode_segment",
        lambda _ffmpeg, _source, start, end, target: target.write_bytes(
            f"{start:.3f}:{end:.3f}".encode()
        ),
    )
    (clip,) = media_review.stage_segments(
        paths, analysis.token, [{"start": 1, "end": 9}]
    )
    manifest = media_review._pending_manifest_path(paths, clip.clip_id)
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    target = paths.media("bumps", "futurama", "recovered.mp4")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(clip.path.read_bytes())
    clip.path.unlink()
    manifest.unlink()
    journal = media_review._review_processing_dir(paths) / f"{clip.clip_id}.json"
    journal.parent.mkdir(parents=True, exist_ok=True)
    journal.write_text(
        json.dumps(
            {
                **payload,
                "action": "classify",
                "target": target.relative_to(paths.root).as_posix(),
                "tag": {
                    "subject": "futurama",
                    "position": "OUT",
                    "era": 2003,
                    "source": "import",
                    "confidence": 1.0,
                    "confirmed": True,
                },
            }
        ),
        encoding="utf-8",
    )

    assert media_review.pending_import_clips(paths) == []
    assert target.with_name(target.name + ".tags.json").is_file()
    assert not journal.exists()


def test_review_queue_rolls_back_interrupted_classification(paths, monkeypatch):
    analysis, _source = _analyze(paths, monkeypatch, duration=80)
    monkeypatch.setattr(
        media_review,
        "_encode_segment",
        lambda _ffmpeg, _source, start, end, target: target.write_bytes(
            f"{start:.3f}:{end:.3f}".encode()
        ),
    )
    (clip,) = media_review.stage_segments(
        paths, analysis.token, [{"start": 1, "end": 9}]
    )
    manifest = media_review._pending_manifest_path(paths, clip.clip_id)
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    manifest.unlink()
    journal = media_review._review_processing_dir(paths) / f"{clip.clip_id}.json"
    journal.parent.mkdir(parents=True, exist_ok=True)
    journal.write_text(
        json.dumps(
            {
                **payload,
                "action": "classify",
                "target": "media/bumps/futurama/recovered.mp4",
                "tag": None,
            }
        ),
        encoding="utf-8",
    )

    assert media_review.pending_import_clips(paths) == [clip]
    assert manifest.is_file()
    assert not journal.exists()


def test_bad_import_review_record_is_quarantined_without_blocking_valid_clips(
    paths, monkeypatch
):
    analysis, _source = _analyze(paths, monkeypatch, duration=80)
    monkeypatch.setattr(
        media_review,
        "_encode_segment",
        lambda _ffmpeg, _source, start, end, target: target.write_bytes(
            f"{start:.3f}:{end:.3f}".encode()
        ),
    )
    (clip,) = media_review.stage_segments(
        paths, analysis.token, [{"start": 1, "end": 9}]
    )
    broken = paths.data_dir / "media-imports" / "review" / (
        "00000000-0000-0000-0000-000000000000.json"
    )
    broken.write_text("{}", encoding="utf-8")

    valid, issues = media_review.pending_import_clips_with_issues(paths)

    assert valid == [clip]
    assert len(issues) == 1
    assert "quarantined" in issues[0]
    assert (broken.parent / ".broken" / broken.name).is_file()


def test_bad_recovery_journal_is_quarantined_without_blocking_valid_clips(
    paths, monkeypatch
):
    analysis, _source = _analyze(paths, monkeypatch, duration=80)
    monkeypatch.setattr(
        media_review,
        "_encode_segment",
        lambda _ffmpeg, _source, start, end, target: target.write_bytes(
            f"{start:.3f}:{end:.3f}".encode()
        ),
    )
    (clip,) = media_review.stage_segments(
        paths, analysis.token, [{"start": 1, "end": 9}]
    )
    journal = (
        media_review._review_processing_dir(paths)
        / "00000000-0000-0000-0000-000000000000.json"
    )
    journal.parent.mkdir(parents=True, exist_ok=True)
    journal.write_text("{", encoding="utf-8")

    valid, issues = media_review.pending_import_clips_with_issues(paths)

    assert valid == [clip]
    assert len(issues) == 1
    assert "journal" in issues[0]
    assert (journal.parent / ".broken" / journal.name).is_file()


def test_export_requires_year_for_commercial_segments(paths, monkeypatch):
    analysis, _ = _analyze(paths, monkeypatch)
    encoded = []
    monkeypatch.setattr(
        media_review,
        "_encode_segment",
        lambda *args: encoded.append(args),
    )

    with pytest.raises(MediaImportError, match="Commercial year"):
        media_review.export_segments(
            paths,
            analysis.token,
            [{"start": 1, "end": 31, "label": "commercial"}],
            ad_year="",
            bump_subject="neutral",
            bump_era="",
            known_subjects=set(),
        )
    assert not encoded


def test_export_rejects_unknown_bump_subject(paths, monkeypatch):
    analysis, _ = _analyze(paths, monkeypatch)
    with pytest.raises(MediaImportError, match="configured show or neutral"):
        media_review.export_segments(
            paths,
            analysis.token,
            [{"start": 1, "end": 9, "label": "bumper"}],
            ad_year="",
            bump_subject="../outside",
            bump_era="",
            known_subjects={"futurama"},
        )


def test_export_rejects_overlapping_segments_before_encoding(paths, monkeypatch):
    analysis, _ = _analyze(paths, monkeypatch)
    encoded = []
    monkeypatch.setattr(
        media_review,
        "_encode_segment",
        lambda *args: encoded.append(args),
    )

    with pytest.raises(MediaImportError, match="cannot overlap"):
        media_review.export_segments(
            paths,
            analysis.token,
            [
                {"start": 1, "end": 10, "label": "bumper"},
                {"start": 9, "end": 30, "label": "commercial"},
            ],
            ad_year="2003",
            bump_subject="neutral",
            bump_era="",
            known_subjects=set(),
        )
    assert not encoded


def test_export_failure_removes_partial_output(paths, monkeypatch):
    analysis, _ = _analyze(paths, monkeypatch)

    def fail_after_partial(_ffmpeg, _source, _start, _end, target):
        target.write_bytes(b"partial")
        raise MediaImportError("encoding failed")

    monkeypatch.setattr(media_review, "_encode_segment", fail_after_partial)
    with pytest.raises(MediaImportError, match="encoding failed"):
        media_review.export_segments(
            paths,
            analysis.token,
            [{"start": 1, "end": 9, "label": "bumper"}],
            ad_year="",
            bump_subject="neutral",
            bump_era="",
            known_subjects=set(),
        )

    assert list(paths.media("bumps").rglob("*.mp4")) == []


def test_analysis_rejects_changed_source(paths, monkeypatch):
    analysis, source = _analyze(paths, monkeypatch)
    source.write_bytes(b"changed source")
    with pytest.raises(MediaImportError, match="changed after analysis"):
        media_review.load_analysis(paths, analysis.token)


def test_source_resolution_rejects_paths_outside_staging(paths):
    with pytest.raises(MediaImportError, match="directly inside"):
        media_review.resolve_source(paths, "..\\outside.mp4")


def test_saved_analysis_requires_a_valid_uuid(paths):
    with pytest.raises(MediaImportError, match="expired or is invalid"):
        media_review.load_analysis(paths, "not-a-token")
