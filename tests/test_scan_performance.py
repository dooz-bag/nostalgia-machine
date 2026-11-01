"""Scanning must not be dominated by ffprobe process spawns.

Regression: a 1,482-file library took 37s to scan, and profiling showed the time
was almost entirely 1,482 sequential ``ffprobe`` subprocess calls -- not SQLite,
not the file walk. That matters because ``scan`` runs on a timer inside the
scheduler, and a bulk bumper import makes the library an order of magnitude
bigger than the demo one.
"""

from __future__ import annotations

import time
from pathlib import Path

from nostalgia.catalog import Catalog, _scan_workers
from nostalgia.pipeline import scan
from nostalgia.probe import MediaInfo


def test_workers_are_bounded() -> None:
    """Never unbounded: a scan of thousands of files must not spawn thousands."""
    assert 4 <= _scan_workers() <= 16


def test_worker_override_is_honoured(monkeypatch) -> None:
    monkeypatch.setenv("NOSTALGIA_SCAN_WORKERS", "3")
    assert _scan_workers() == 3


def test_scan_of_a_larger_library_stays_quick(config, paths, tmp_path) -> None:
    """A few hundred stubs (each needing a probe) must scan in seconds."""
    bump_dir = paths.media_root / "bumps" / "neutral"
    bump_dir.mkdir(parents=True, exist_ok=True)
    for index in range(300):
        target = bump_dir / f"bulk-{index:04d}.mp4"
        target.touch()
        # Duration sidecars keep the test independent of ffmpeg being installed.
        target.with_name(target.name + ".json").write_text(
            '{"duration_ms": 10000}', encoding="utf-8"
        )

    started = time.perf_counter()
    with Catalog(paths.catalog) as catalog:
        report = scan(config, catalog, paths)
    elapsed = time.perf_counter() - started

    assert report.scanned > 300
    assert elapsed < 20.0, f"scan of {report.scanned} files took {elapsed:.1f}s"


def test_sidecar_tags_reach_the_database(config, paths) -> None:
    """A confirmed sidecar must mark the file tagged, not leave it in review.

    Regression: the review queue filters on ``asset_tags.confirmed``, but an
    importer writes sidecars, so 396 imported bumps sat in the queue forever even
    though every one of them had a confirmed tag next to it on disk.
    """
    import json

    bump = paths.media_root / "bumps" / "neutral" / "sidecar-probe.mp4"
    bump.parent.mkdir(parents=True, exist_ok=True)
    bump.touch()
    bump.with_name(bump.name + ".json").write_text('{"duration_ms": 10000}', encoding="utf-8")
    sidecar = bump.with_name(bump.name + ".tags.json")
    sidecar.write_text(
        json.dumps({"subject": "futurama", "position": "NEXT", "confirmed": True}),
        encoding="utf-8",
    )

    with Catalog(paths.catalog) as catalog:
        scan(config, catalog, paths)
        pending = [row["path"] for row in catalog.tags_pending_review()]
        assert not any(Path(path).name == bump.name for path in pending), (
            "a bump with a confirmed sidecar was still queued for review"
        )


def test_unchanged_files_reuse_probe_results_unless_forced(
    config, paths, monkeypatch
) -> None:
    import nostalgia.catalog as catalog_module

    calls = []
    media = paths.media_root / "bumps" / "probe-cache" / "probe-cache.mp4"
    media.parent.mkdir(parents=True, exist_ok=True)
    media.write_bytes(b"test")
    monkeypatch.setattr(catalog_module, "find_ffprobe", lambda _path=None: "ffprobe")

    def fake_probe(path, _ffprobe):
        calls.append(path)
        return MediaInfo(duration_ms=10_000)

    monkeypatch.setattr(catalog_module, "probe", fake_probe)
    with Catalog(paths.catalog) as catalog:
        kwargs = {
            "media_root": paths.media_root,
            "show_folders": {},
            "bump_root": media.parent,
            "ad_root": None,
            "ident_root": None,
            "extra_root": None,
        }
        first = catalog.scan(**kwargs)
        second = catalog.scan(**kwargs)
        third = catalog.scan(**kwargs, force=True)

    assert len(calls) == 2
    assert first.probed == 1
    assert second.probed == 0
    assert second.skipped_existing >= 1
    assert third.probed == 1


def test_scan_command_passes_force_to_the_pipeline(config, paths, monkeypatch) -> None:
    from types import SimpleNamespace

    import nostalgia.cli as cli
    from nostalgia.catalog import ScanReport

    received = {}

    def fake_scan(_config, _catalog, _paths, **kwargs):
        received.update(kwargs)
        return ScanReport()

    monkeypatch.setattr(cli, "scan", fake_scan)
    args = SimpleNamespace(
        ffprobe=None,
        force=True,
        sidecars=False,
        detect_marks=False,
        detect_spots=False,
    )

    assert cli._cmd_scan(args, config, paths) == 0
    assert received["force"] is True


def test_scan_reconciles_an_empty_root(config, paths) -> None:
    folder = paths.media_root / "shows" / "temporary-show"
    episode = folder / "Temporary Show - S01E01.mkv"
    episode.parent.mkdir(parents=True)
    episode.write_bytes(b"")
    episode.with_suffix(".mkv.json").write_text(
        '{"duration_ms": 10000}', encoding="utf-8"
    )

    with Catalog(paths.catalog) as catalog:
        catalog.scan(
            media_root=paths.media_root,
            show_folders={"temporary-show": folder},
            bump_root=None,
            ad_root=None,
            ident_root=None,
            extra_root=None,
        )
        assert len(catalog.episodes("temporary-show")) == 1
        episode.unlink()
        episode.with_suffix(".mkv.json").unlink()
        catalog.scan(
            media_root=paths.media_root,
            show_folders={"temporary-show": folder},
            bump_root=None,
            ad_root=None,
            ident_root=None,
            extra_root=None,
        )
        assert catalog.episodes("temporary-show") == []


def test_burned_mark_detection_tracks_each_file_not_last_derived(
    config, paths, monkeypatch
) -> None:
    folder = paths.media_root / "shows" / "aqua-teen-hunger-force"
    original = folder / "Season 01" / "aqua-teen-hunger-force - S01E001.mkv"
    original.parent.mkdir(parents=True, exist_ok=True)
    original.write_bytes(b"")
    original.with_suffix(".mkv.json").write_text(
        '{"duration_ms": 10000}', encoding="utf-8"
    )
    final_episode = folder / "Season 01" / "aqua-teen-hunger-force - S01E002.mkv"
    final_episode.write_bytes(b"")
    final_episode.with_suffix(".mkv.json").write_text(
        '{"duration_ms": 10000}', encoding="utf-8"
    )
    derived_root = paths.media_root / "derived" / "watermark"
    derived_root.mkdir(parents=True)
    derived = derived_root / "aqua-teen-hunger-force_s01e002.mkv"
    derived.write_bytes(b"")
    derived.with_suffix(".mkv.json").write_text(
        '{"duration_ms": 10000}', encoding="utf-8"
    )
    detected = []
    monkeypatch.setattr(
        Catalog,
        "_detect_mark",
        staticmethod(lambda path, _ffmpeg: detected.append(path) or False),
    )

    with Catalog(paths.catalog) as catalog:
        catalog.scan(
            media_root=paths.media_root,
            show_folders={"aqua-teen-hunger-force": folder},
            bump_root=None,
            ad_root=None,
            ident_root=None,
            extra_root=None,
            derived_root=derived_root,
            detect_burned_in=True,
        )

    assert original in detected
    assert final_episode not in detected


def test_generated_registration_removes_stale_cards(config, paths) -> None:
    folder = paths.media_root / "bumps" / "generated"
    card = folder / "generated-card.mp4"
    card.parent.mkdir(parents=True)
    card.write_bytes(b"")
    card.with_suffix(".mp4.json").write_text(
        '{"duration_ms": 10000}', encoding="utf-8"
    )

    with Catalog(paths.catalog) as catalog:
        assert catalog.register_generated(folder) == 1
        card.unlink()
        card.with_suffix(".mp4.json").unlink()
        assert catalog.register_generated(folder) == 0
        assert catalog.connect().execute(
            "SELECT 1 FROM media WHERE key='bump:generated-card.mp4'"
        ).fetchone() is None
