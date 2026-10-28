"""Join a break into a single file, so the streamer sees one item instead of ten.

The streamer starts a separate ffmpeg process for every playout item and marks the
join with a playlist discontinuity. A player has to flush and resync at each one,
and a day of this channel has ~840 of them -- roughly 35 an hour -- before any
footage is considered. Most are inside breaks: a pod of spots, flanked by bump
cards, is a run of 7-16 tiny items.

This module folds each such run into one file. It is only possible because
:mod:`nostalgia.conform` gave every break asset identical stream parameters, which
turns the join into a stream copy: no decode, no re-encode, milliseconds per break.

What it deliberately does **not** do is change the plan. The plan stays granular --
the commercials page, the airing history, per-spot switches and the guide all see
individual spots -- and only the playout document collapses a run into one entry.
That keeps one source of truth, and means a merged file is a cache of the plan, not
a second version of it.

A run is only ever made of items that are safe to join (``PlannedItem.mergeable``),
so anything not yet conformed, any black card, any trimmed spot and every programme
simply airs as it always did. Merging is therefore safe to turn on while the library
is only part conformed.
"""

from __future__ import annotations

import hashlib
import os
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from .plan import Plan, PlannedItem


@dataclass(frozen=True, slots=True)
class MergedRun:
    """Items ``first..last`` (inclusive, indices into ``plan.items``) as one file."""

    first: int
    last: int
    path: Path

    @property
    def count(self) -> int:
        return self.last - self.first + 1


def find_runs(items: Sequence[PlannedItem], *, max_ms: int) -> list[tuple[int, int]]:
    """Index ranges of consecutive mergeable items, split at *max_ms*.

    A single item is not a run: joining one file to nothing saves no boundary.
    Runs must also be contiguous in time, which a valid plan guarantees, but the
    check is cheap and a gap inside a "merged" break would silently shift
    everything after it.
    """
    runs: list[tuple[int, int]] = []
    start: int | None = None
    total = 0

    def close(end: int) -> None:
        nonlocal start, total
        if start is not None and end > start:
            runs.append((start, end))
        start, total = None, 0

    for index, item in enumerate(items):
        if not item.mergeable:
            close(index - 1)
            continue
        if start is not None:
            contiguous = items[index - 1].end_ms == item.start_ms
            if not contiguous or total + item.duration_ms > max_ms:
                close(index - 1)
        if start is None:
            start = index
        total += item.duration_ms
    close(len(items) - 1)
    return runs


def run_digest(paths: Sequence[Path]) -> str:
    """A key for the exact contents of a run.

    Size and mtime are in it so re-conforming a spot produces a new merged file
    rather than serving the stale join.
    """
    parts = []
    for path in paths:
        stat = path.stat()
        parts.append(f"{path}|{stat.st_size}|{stat.st_mtime_ns}")
    return hashlib.sha1("\n".join(parts).encode()).hexdigest()[:20]


def _concat_list(paths: Sequence[Path]) -> str:
    # The concat demuxer's quoting: wrap in single quotes, escape a quote as '\''.
    # Forward slashes always: ffmpeg reads a backslash in this list as an escape,
    # so a Windows path would be silently mangled.
    return "".join(
        "file '" + path.as_posix().replace("'", "'\\''") + "'\n" for path in paths
    )


def build_merged(paths: Sequence[Path], target: Path, *, ffmpeg: str) -> bool:
    """Stream-copy *paths* into *target*, atomically. True on success."""
    if target.is_file() and target.stat().st_size > 0:
        return True
    target.parent.mkdir(parents=True, exist_ok=True)
    listing = target.with_suffix(".txt")
    partial = target.with_suffix(".part.mkv")
    try:
        listing.write_text(_concat_list(paths), encoding="utf-8")
        run = subprocess.run(
            [ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
             "-f", "concat", "-safe", "0", "-i", str(listing),
             "-c", "copy", "-map_metadata", "-1", "-map_chapters", "-1",
             "-f", "matroska", str(partial)],
            capture_output=True, text=True, check=False, timeout=120,
        )
        if run.returncode != 0 or not partial.is_file() or partial.stat().st_size == 0:
            return False
        os.replace(partial, target)
        return True
    except (OSError, subprocess.SubprocessError):
        return False
    finally:
        listing.unlink(missing_ok=True)
        partial.unlink(missing_ok=True)


def merge_plan(
    plan: Plan,
    out_dir: Path,
    *,
    ffmpeg: str | None,
    max_ms: int,
) -> tuple[list[MergedRun], list[str]]:
    """Build every merged file one day needs.

    Returns the runs to write into the playout, and warnings. A run whose files are
    missing, or that fails to join, is simply left out: its items air one by one,
    which is the old behaviour rather than a gap.
    """
    warnings: list[str] = []
    if not ffmpeg:
        return [], ["merge_breaks is on but ffmpeg was not found; breaks air item by item"]

    merged: list[MergedRun] = []
    failed = 0
    for first, last in find_runs(plan.items, max_ms=max_ms):
        files = [Path(item.path) for item in plan.items[first : last + 1]]
        if not all(path.is_file() for path in files):
            failed += 1
            continue
        target = out_dir / f"{run_digest(files)}.mkv"
        if build_merged(files, target, ffmpeg=ffmpeg):
            merged.append(MergedRun(first, last, target))
        else:
            failed += 1
    if failed:
        warnings.append(
            f"{plan.day}: {failed} break(s) could not be merged and air item by item"
        )
    return merged, warnings


def prune_merged(out_dir: Path, keep: set[Path], *, grace_s: int = 6 * 3600) -> int:
    """Delete merged files no current playout refers to.

    The grace period matters: the streamer may still be reading a file the new plan
    has already stopped using, and deleting it under an open ffmpeg is a dead
    stream. Anything recent is left for a later pass.
    """
    if not out_dir.is_dir():
        return 0
    keep_names = {path.name for path in keep}
    now = time.time()
    removed = 0
    for path in out_dir.glob("*.mkv"):
        if path.name in keep_names:
            continue
        try:
            if now - path.stat().st_mtime > grace_s:
                path.unlink()
                removed += 1
        except OSError:
            continue
    for stray in list(out_dir.glob("*.part.mkv")) + list(out_dir.glob("*.txt")):
        try:
            if now - stray.stat().st_mtime > 3600:
                stray.unlink()
        except OSError:
            continue
    return removed


__all__ = [
    "MergedRun",
    "build_merged",
    "find_runs",
    "merge_plan",
    "prune_merged",
    "run_digest",
]
