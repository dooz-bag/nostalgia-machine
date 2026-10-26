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
