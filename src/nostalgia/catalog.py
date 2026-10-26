"""The media catalog (SQLite).

Everything the scheduler needs to know about the library lives here, and nothing
else does. The catalog is *derived* state: deleting ``data/catalog.sqlite`` and
re-running ``scan`` rebuilds it exactly, apart from the human confirmations in
``asset_tags``, which are therefore kept in their own table and preserved across
rescans.
"""

from __future__ import annotations

import json
import os
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Iterator, Sequence

import re

from .assets import Asset
from .naming import is_media_name, parse_episode, season_from_dir
from .probe import (
    MediaInfo,
    ProbeError,
    find_ffprobe,
    probe,
    sidecar_duration_ms,
    write_sidecar,
)
from .rotation import EpisodeRef
from .sidecars import (
    RESERVED_BUMP_FOLDERS,
    SIDECAR_SUFFIX,
    SidecarError,
    subject_from_folder,
)
from .sidecars import read as read_tags

def _bug_from_row(row) -> bool | None:
    """Read ``burned_in_mark`` defensively.

    An old catalog that has not been migrated yet has no such column, and reading
    it by name would raise on every query. ``None`` ("not examined") is also the
    correct answer in that case.
    """
    try:
        value = row["burned_in_mark"]
    except (IndexError, KeyError):
        return None
    return None if value is None else bool(value)


def _migrate(connection: sqlite3.Connection) -> None:
    """Bring an existing catalog up to the current schema, in place.

    Refusing to open an old catalog would force a full rescan after every upgrade,
    and a rescan of this library takes minutes. Only additive changes are handled
    here -- anything that would need a rewrite is a reason to bump the version and
    say so instead of guessing.
    """
    columns = {
        row["name"] for row in connection.execute("PRAGMA table_info(media)")
    }
    if "burned_in_mark" not in columns:
        connection.execute("ALTER TABLE media ADD COLUMN burned_in_mark INTEGER")
        # Deliberately left NULL for existing rows: "not examined" must not be
        # confused with "examined and absent", or every episode in a catalog that
        # predates this column would be treated as needing a mark.
    connection.execute(
        "INSERT OR REPLACE INTO meta(key, value) VALUES ('schema_version', ?)",
        (str(SCHEMA_VERSION),),
    )
    connection.commit()


SCHEMA_VERSION = 2
"""Bumped for ``media.burned_in_mark``.

An existing catalog is *migrated in place* rather than refused: the new column is
added by ``ALTER TABLE`` and left NULL, which reads as "not examined yet" and
means an un-rescanned library behaves exactly as it did before the column existed.
"""

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS media (
    key        TEXT PRIMARY KEY,
    kind       TEXT NOT NULL,
    path       TEXT NOT NULL,
    folder     TEXT NOT NULL,
    show_id    TEXT,
    season     INTEGER,
    episode    INTEGER,
    title      TEXT NOT NULL DEFAULT '',
    duration_ms INTEGER NOT NULL,
    format_name TEXT,
    video_codec TEXT,
    width      INTEGER,
    height     INTEGER,
    audio_codec TEXT,
    audio_channels INTEGER,
    size_bytes INTEGER,
    mtime_ns   INTEGER,
    probed_by  TEXT NOT NULL DEFAULT 'ffprobe',
    -- 1 when the picture already carries a corner bug of its own. Nullable on
    -- purpose: NULL means "not examined", which is different from "examined and
    -- not present", and only the latter may be marked by the channel.
    burned_in_mark INTEGER
);
CREATE INDEX IF NOT EXISTS media_kind  ON media(kind);
CREATE INDEX IF NOT EXISTS media_show  ON media(show_id, season, episode);

CREATE TABLE IF NOT EXISTS asset_tags (
    key        TEXT PRIMARY KEY,
    subject    TEXT,
    position   TEXT,
    era_from   INTEGER,
    era_to     INTEGER,
    family     TEXT,
    tag_source TEXT NOT NULL DEFAULT 'folder',
    confidence REAL NOT NULL DEFAULT 1.0,
    brand      TEXT,
    confirmed  INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS airings (
    item_id    TEXT PRIMARY KEY,
    asset_key  TEXT NOT NULL,
    kind       TEXT NOT NULL,
    start_ms   INTEGER NOT NULL,
    end_ms     INTEGER NOT NULL,
    day        TEXT NOT NULL,
    slot_index INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS airings_asset ON airings(asset_key, start_ms);
CREATE INDEX IF NOT EXISTS airings_day   ON airings(day);

CREATE TABLE IF NOT EXISTS state (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS scan_issues (
    path   TEXT PRIMARY KEY,
    reason TEXT NOT NULL
);
"""


def _scan_workers() -> int:
    """How many ffprobe processes to run at once.

    Bounded rather than unbounded: a scan of 7,000 files should not spawn 7,000
    processes, and ffprobe is fast enough that CPU count is the useful ceiling.
    ``NOSTALGIA_SCAN_WORKERS`` overrides it for a slow disk or a shared machine.
    """
    override = os.environ.get("NOSTALGIA_SCAN_WORKERS")
    if override and override.isdigit() and int(override) > 0:
        return int(override)
    return max(4, min(16, (os.cpu_count() or 4)))


@dataclass
class ScanReport:
    scanned: int = 0
    probed: int = 0
    from_sidecar: int = 0
    skipped_existing: int = 0
    issues: list[tuple[str, str]] = field(default_factory=list)
    per_kind: dict[str, int] = field(default_factory=dict)
    marked: int = 0
    """Episodes found to carry their own corner bug."""
    mark_pending: int = 0
    """Episodes whose corner has not been examined yet."""

    def note(self, path: Path, reason: str) -> None:
        self.issues.append((str(path), reason))

    @property
    def total(self) -> int:
        return sum(self.per_kind.values())


class Catalog:
    def __init__(self, path: Path | str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._connection: sqlite3.Connection | None = None

    # -- lifecycle ---------------------------------------------------------

    def connect(self) -> sqlite3.Connection:
        if self._connection is None:
            # timeout: the scheduler container writes this file on a loop while a
            # human may run `doctor`/`review` alongside it. Without a timeout,
            # SQLite gives up after 5s with "database is locked" and the second
            # process dies for no good reason.
            connection = sqlite3.connect(self.path, timeout=30.0)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA journal_mode=WAL")
            # WAL lets one writer and many readers coexist, but a writer still
            # blocks another writer, so wait rather than fail.
            connection.execute("PRAGMA busy_timeout=30000")
            connection.execute("PRAGMA foreign_keys=ON")
            # Only write the schema when it is actually missing. `executescript`
            # implicitly COMMITs and takes a write lock, so running it on every
            # connection made even a read-only command (`doctor`, `review`) contend
            # with the scheduler's emit loop and fail with "database is locked".
            if not self._schema_present(connection):
                connection.executescript(_SCHEMA)
                connection.execute(
                    "INSERT OR IGNORE INTO meta(key, value) VALUES ('schema_version', ?)",
                    (str(SCHEMA_VERSION),),
                )
            else:
                _migrate(connection)
            self._connection = connection
        return self._connection

    @staticmethod
    def _schema_present(connection: sqlite3.Connection) -> bool:
        row = connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='meta'"
        ).fetchone()
        return row is not None



    def close(self) -> None:
        if self._connection is not None:
            self._connection.commit()
            self._connection.close()
            self._connection = None

    def __enter__(self) -> "Catalog":
        self.connect()
        return self

    def __exit__(self, *_exc) -> None:
        self.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        connection = self.connect()
        try:
            yield connection
        except Exception:
            connection.rollback()
            raise
        else:
            connection.commit()

    # -- scanning ----------------------------------------------------------

    def scan(
        self,
        *,
        media_root: Path,
        show_folders: dict[str, Path],
        bump_root: Path | None,
        ad_root: Path | None,
        ident_root: Path | None,
        ffprobe_path: str | None = None,
        force: bool = False,
        write_sidecars: bool = False,
        derived_root: Path | None = None,
        detect_burned_in: bool = False,
        ffmpeg_path: str | None = None,
    ) -> ScanReport:
        """Walk every media root and (re)build the ``media`` table.

        *derived_root* holds composited copies keyed by asset key
        (``watermark/aqua-teen-hunger-force_s01e001.mkv``). When one exists it is
        catalogued *instead of* the original, so the marked copy is what airs
        without anything downstream needing to know about watermarks.

        *detect_burned_in* examines each episode's bottom-right corner once and
        records the verdict, which is what lets the overlay path leave an episode
        that already carries its own bug alone. It costs several ffmpeg seeks per
        episode, so it is done here -- once per file, cached in the catalog --
        rather than on every plan build.
        """
        report = ScanReport()
        ffprobe = find_ffprobe(ffprobe_path)

        rows: list[tuple] = []
        # Probe in parallel. ffprobe is a process per file and a library of a few
        # thousand bumps made a scan take 37s, essentially all of it spawn cost.
        # Threads are the right tool: each call blocks on a subprocess, so there is
        # no GIL contention, and the workers touch no shared state.
        with ThreadPoolExecutor(max_workers=_scan_workers()) as pool:
            for show_id, folder in sorted(show_folders.items()):
                if not Path(folder).is_dir():
                    report.note(Path(folder), f"show folder for {show_id!r} is missing")
                    continue
                rows.extend(
                    self._scan_show(
                        show_id,
                        Path(folder),
                        ffprobe,
                        report,
                        write_sidecars,
                        pool,
                        derived_root=derived_root,
                        detect_burned_in=detect_burned_in,
                        ffmpeg_path=ffmpeg_path,
                    )
                )

            for root, kind in ((bump_root, "bump"), (ad_root, "ad"), (ident_root, "ident")):
                if root and Path(root).is_dir():
                    rows.extend(
                        self._scan_flat(Path(root), kind, ffprobe, report, write_sidecars, pool)
                    )

        if rows:
            with self.transaction() as connection:
                connection.executemany(
                    """
                    INSERT INTO media (key, kind, path, folder, show_id, season, episode,
                                       title, duration_ms, format_name, video_codec, width,
                                       height, audio_codec, audio_channels, size_bytes,
                                       mtime_ns, probed_by, burned_in_mark)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(key) DO UPDATE SET
                        path=excluded.path, folder=excluded.folder,
                        show_id=excluded.show_id, season=excluded.season,
                        episode=excluded.episode, title=excluded.title,
                        duration_ms=excluded.duration_ms,
                        format_name=excluded.format_name,
                        video_codec=excluded.video_codec, width=excluded.width,
                        height=excluded.height, audio_codec=excluded.audio_codec,
                        audio_channels=excluded.audio_channels,
                        size_bytes=excluded.size_bytes, mtime_ns=excluded.mtime_ns,
                        probed_by=excluded.probed_by
                    """,
                    rows,
                )
                self._delete_stale(connection, {row[0] for row in rows})
                self._sync_sidecar_tags(connection)
                connection.execute("DELETE FROM scan_issues")
                connection.executemany(
                    "INSERT OR REPLACE INTO scan_issues(path, reason) VALUES (?,?)",
                    report.issues,
                )
        for row in rows:
            report.per_kind[row[1]] = report.per_kind.get(row[1], 0) + 1
            report.scanned += 1
        return report

    def _sync_sidecar_tags(self, connection: sqlite3.Connection) -> None:
        """Copy confirmed sidecar tags into ``asset_tags``.

        The sidecar is the durable record -- it survives a wiped catalog and
        travels with the media -- but the review queue filters on the database
        flag, so a sidecar written by an importer (or restored alongside the media)
        would leave the file looking untagged forever. Syncing during scan keeps
        one on-disk source of truth *and* a fast query.
        """
        existing = {
            row["key"]: row["confirmed"]
            for row in connection.execute("SELECT key, confirmed FROM asset_tags")
        }
        pending: list[tuple] = []
        for key, kind, path in connection.execute(
            "SELECT key, kind, path FROM media WHERE kind = 'bump'"
        ).fetchall():
            if existing.get(key):
                continue
            media = Path(path)
            # Cheap existence check before parsing JSON: a scan touches every
            # bump, and most have no sidecar at all.
            if not Path(str(media) + SIDECAR_SUFFIX).is_file():
                continue
            try:
                sidecar = read_tags(media)
            except SidecarError:
                continue
            if sidecar is None or not sidecar.confirmed:
                continue
            pending.append(
                (
                    key,
                    sidecar.subject,
                    sidecar.position,
                    sidecar.era,
                    sidecar.era_to,
                    sidecar.family,
                    sidecar.source,
                    sidecar.confidence,
                )
            )

        for row in pending:
            key = row[0]
            connection.execute(
                """
                INSERT INTO asset_tags
                    (key, subject, position, era_from, era_to, family, tag_source,
                     confidence, confirmed)
                VALUES (?,?,?,?,?,?,?,?,1)
                ON CONFLICT(key) DO UPDATE SET
                    subject=excluded.subject, position=excluded.position,
                    era_from=excluded.era_from, era_to=excluded.era_to,
                    family=excluded.family, tag_source=excluded.tag_source,
                    confidence=excluded.confidence, confirmed=1
                """,
                (
                    key,
                    sidecar.subject,
                    sidecar.position,
                    sidecar.era,
                    sidecar.era_to,
                    sidecar.family,
                    sidecar.source,
                    sidecar.confidence,
                ),
            )

    def _scan_show(
        self,
        show_id: str,
        folder: Path,
        ffprobe: str | None,
        report: ScanReport,
        write_sidecars: bool,
        pool: "ThreadPoolExecutor | None" = None,
        derived_root: Path | None = None,
        detect_burned_in: bool = False,
        ffmpeg_path: str | None = None,
    ) -> list[tuple]:
        rows: list[tuple] = []
        candidates = list(_walk_media(folder))

        # A composited copy replaces the original for that episode. Resolved
        # before probing so only the file that will actually air is measured.
        chosen: list[tuple[Path, Path]] = []
        for path in candidates:
            parsed = parse_episode(path.name, season_dir=path.parent.name)
            key = f"{show_id}:s{parsed.season:02d}e{parsed.episode:03d}" if parsed else None
            derived = _derived_for(derived_root, key) if key else None
            chosen.append((derived or path, path))

        futures = {}
        if pool is not None and ffprobe:
            for media_path, _original in chosen:
                if not sidecar_duration_ms(media_path):
                    futures[media_path] = pool.submit(probe, media_path, ffprobe)

        for path, original in chosen:
            # Identity always comes from the *original* name, so a derived file's
            # own naming can never change which episode it is.
            stat = path.stat()
            parsed = parse_episode(original.name, season_dir=original.parent.name)
            if parsed is None:
                report.note(original, "cannot determine season/episode from the filename")
                continue
            info, probed_by = self._info(
                path, stat.st_mtime_ns, ffprobe, report, write_sidecars, futures.get(path)
            )
            if info is None:
                continue
            key = f"{show_id}:s{parsed.season:02d}e{parsed.episode:03d}"
            marked = None
            # Only an *original* is worth examining: a composited copy carries the
            # mark this channel drew, so detecting it there would report our own
            # work as the show's.
            if detect_burned_in and derived is None:
                marked = self._detect_mark(path, ffmpeg_path)
                if marked:
                    report.marked += 1
            rows.append(
                (
                    key,
                    "episode",
                    str(path),
                    str(folder),
                    show_id,
                    parsed.season,
                    parsed.episode,
                    parsed.title,
                    info.duration_ms,
                    info.format_name,
                    info.video_codec,
                    info.width,
                    info.height,
                    info.audio_codec,
                    info.audio_channels,
                    stat.st_size,
                    stat.st_mtime_ns,
                    probed_by,
                    None if marked is None else int(marked),
                )
            )
        return rows

    @staticmethod
    def _detect_mark(path: Path, ffmpeg_path: str | None) -> bool | None:
        """Whether *path* already carries a corner bug, or ``None`` if unknown.

        A failure to detect is reported as unknown rather than as "no mark": the
        two lead to opposite decisions, and guessing "no mark" would stamp a
        second bug onto an episode that already had one.
        """
        from .watermark import has_burned_in_mark

        try:
            return has_burned_in_mark(path, ffmpeg=ffmpeg_path)
        except Exception:  # noqa: BLE001 - detection must never fail a scan
            return None

    def _scan_flat(
        self,
        root: Path,
        kind: str,
        ffprobe: str | None,
        report: ScanReport,
        write_sidecars: bool,
        pool: "ThreadPoolExecutor | None" = None,
    ) -> list[tuple]:
        rows: list[tuple] = []
        # Submit every probe first, then collect. Calling .result() straight after
        # .submit() would serialise the pool back to one-at-a-time, which is the
        # mistake that made the first attempt at this change pointless.
        paths = list(_walk_media(root))
        stats = {path: path.stat() for path in paths}
        futures = {}
        if pool is not None and ffprobe:
            for path in paths:
                # Sidecar-only files need no subprocess at all.
                if not sidecar_duration_ms(path):
                    futures[path] = pool.submit(probe, path, ffprobe)

        for path in paths:
            stat = stats[path]
            info, probed_by = self._info(
                path, stat.st_mtime_ns, ffprobe, report, write_sidecars, futures.get(path)
            )
            if info is None:
                continue
            relative = path.relative_to(root)
            subject_folder = relative.parts[0] if len(relative.parts) > 1 else None
            rows.append(
                (
                    f"{kind}:{relative.as_posix()}",
                    kind,
                    str(path),
                    str(root),
                    subject_folder,
                    None,
                    None,
                    path.stem,
                    info.duration_ms,
                    info.format_name,
                    info.video_codec,
                    info.width,
                    info.height,
                    info.audio_codec,
                    info.audio_channels,
                    stat.st_size,
                    stat.st_mtime_ns,
                    probed_by,
                    # Bumps, commercials and idents are never bugged by the network
                    # and never carry a corner mark of their own, so there is
                    # nothing to detect and a definite 0 rather than "unknown".
                    0,
                )
            )
        return rows

    def _info(
        self,
        path: Path,
        mtime_ns: int,
        ffprobe: str | None,
        report: ScanReport,
        write_sidecars: bool,
        future=None,
    ) -> tuple[MediaInfo | None, str]:
        sidecar = sidecar_duration_ms(path)
        if sidecar and not ffprobe:
            report.from_sidecar += 1
            return MediaInfo(duration_ms=sidecar, source="sidecar"), "sidecar"
        if not ffprobe:
            report.note(path, "no duration: install ffmpeg or add a .json sidecar")
            return None, "missing"
        try:
            info = future.result() if future is not None else probe(path, ffprobe)
        except ProbeError as exc:
            if sidecar:
                report.from_sidecar += 1
                return MediaInfo(duration_ms=sidecar, source="sidecar"), "sidecar"
            report.note(path, str(exc))
            return None, "error"
        report.probed += 1
        if write_sidecars:
            try:
                write_sidecar(path, info)
            except OSError:
                pass
        return info, "ffprobe"

    def register_generated(self, folder: Path, *, ffprobe_path: str | None = None) -> int:
        """Catalogue cards in the generated-bump folder without a full rescan.

        ``nostalgia bumps`` writes cards, ``nostalgia emit`` reads them; requiring
        a manual ``scan`` between the two would be a trap, so the build registers
        them here. Idempotent, and a no-op when the folder is empty or missing.
        """
        folder = Path(folder)
        if not folder.is_dir():
            return 0

        report = ScanReport()
        ffprobe = find_ffprobe(ffprobe_path)
        rows = self._scan_flat(folder, "bump", ffprobe, report, False)
        if not rows:
            return 0

        with self.transaction() as connection:
            connection.executemany(
                """
                INSERT INTO media (key, kind, path, folder, show_id, season, episode,
                                   title, duration_ms, format_name, video_codec, width,
                                   height, audio_codec, audio_channels, size_bytes,
                                   mtime_ns, probed_by, burned_in_mark)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(key) DO UPDATE SET
                    duration_ms=excluded.duration_ms, mtime_ns=excluded.mtime_ns,
                    size_bytes=excluded.size_bytes, probed_by=excluded.probed_by,
                    -- Refreshed only when this scan actually looked; a NULL means
                    -- "not examined", and overwriting a known verdict with NULL
                    -- would silently un-mark an episode.
                    burned_in_mark=COALESCE(excluded.burned_in_mark, media.burned_in_mark)
                """,
                rows,
            )
        return len(rows)

    def _delete_stale(self, connection: sqlite3.Connection, keep: set[str]) -> None:
        existing = {row["key"] for row in connection.execute("SELECT key FROM media")}
        stale = existing - keep
        if stale:
            connection.executemany("DELETE FROM media WHERE key = ?", [(key,) for key in stale])

    # -- queries -----------------------------------------------------------

    def episodes(self, show_id: str | None = None) -> list[EpisodeRef]:
        query = "SELECT * FROM media WHERE kind='episode'"
        params: tuple = ()
        if show_id:
            query += " AND show_id = ?"
            params = (show_id,)
        query += " ORDER BY show_id, season, episode"
        return [
            EpisodeRef(
                key=row["key"],
                show_id=row["show_id"],
                season=row["season"] or 0,
                episode=row["episode"] or 0,
                path=row["path"],
                duration_ms=row["duration_ms"],
                title=row["title"] or "",
                order_key=f"{row['season'] or 0:04d}{row['episode'] or 0:04d}",
                carries_bug=_bug_from_row(row),
            )
            for row in self.connect().execute(query, params)
        ]

    def assets(self, kinds: Sequence[str] = ("bump", "ad", "ident")) -> list[Asset]:
        """Catalogued assets, with tags resolved from sidecar then sidebar DB.

        Resolution order is deliberate: a sidecar next to the file is the most
        specific statement of intent (it is what the review queue and
        ``nostalgia tag`` write), then a manual tag in the database, then the
        folder convention.
        """
        placeholders = ",".join("?" for _ in kinds)
        query = f"""
            SELECT m.*, t.subject AS tag_subject, t.position, t.era_from, t.era_to,
                   t.family, t.tag_source, t.confidence, t.brand, t.confirmed
            FROM media m
            LEFT JOIN asset_tags t ON t.key = m.key
            WHERE m.kind IN ({placeholders})
            ORDER BY m.kind, m.key
        """
        out: list[Asset] = []
        for row in self.connect().execute(query, tuple(kinds)):
            asset = self._asset_from_row(row)
            if asset is not None:
                out.append(asset)
        return out

    def _asset_from_row(self, row: sqlite3.Row) -> Asset | None:
        path = Path(row["path"])
        try:
            sidecar = read_tags(path)
        except SidecarError as exc:
            self._note_issue(path, str(exc))
            sidecar = None

        folder_subject = subject_from_folder(row["show_id"]) if row["kind"] == "bump" else row["show_id"]
        tag_hit = row["confirmed"] if row["confirmed"] is not None else 0

        if sidecar is not None:
            subject = sidecar.subject
            position = sidecar.position
            era_from = sidecar.era
            era_to = sidecar.era_to
            family = sidecar.family
            source = sidecar.source
            confidence = sidecar.confidence
            brand = sidecar.brand
        elif tag_hit:
            subject = row["tag_subject"]
            position = row["position"]
            era_from = row["era_from"]
            era_to = row["era_to"]
            family = row["family"]
            source = row["tag_source"] or "manual"
            confidence = row["confidence"] if row["confidence"] is not None else 1.0
            brand = row["brand"]
        else:
            subject = folder_subject
            position = _position_from_name(path.stem)
            era_from = _era_from_path(path)
            era_to = era_from
            family = None
            source = "folder" if subject else "unknown"
            confidence = 1.0 if subject else 0.0
            brand = None

        return Asset(
            key=row["key"],
            path=str(path),
            kind=row["kind"],
            duration_ms=row["duration_ms"],
            subject=subject,
            position=position,
            era_from=era_from,
            era_to=era_to,
            family=family,
            tag_source=source,
            confidence=confidence,
            brand=brand,
        )

    def _note_issue(self, path: Path, reason: str) -> None:
        with self.transaction() as connection:
            connection.execute(
                "INSERT OR REPLACE INTO scan_issues(path, reason) VALUES (?,?)",
                (str(path), reason),
            )

    def media_info(self, key: str) -> sqlite3.Row | None:
        return self.connect().execute("SELECT * FROM media WHERE key = ?", (key,)).fetchone()

    def scan_issues(self) -> list[sqlite3.Row]:
        return list(self.connect().execute("SELECT * FROM scan_issues ORDER BY path"))

    # -- tags --------------------------------------------------------------

    def set_tag(
        self,
        key: str,
        *,
        subject: str | None,
        position: str | None = None,
        era_from: int | None = None,
        era_to: int | None = None,
        family: str | None = None,
        tag_source: str = "manual",
        confidence: float = 1.0,
        brand: str | None = None,
        confirmed: bool = True,
    ) -> None:
        with self.transaction() as connection:
            connection.execute(
                """
                INSERT INTO asset_tags (key, subject, position, era_from, era_to, family,
                                        tag_source, confidence, brand, confirmed)
                VALUES (?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(key) DO UPDATE SET
                    subject=excluded.subject, position=excluded.position,
                    era_from=excluded.era_from, era_to=excluded.era_to,
                    family=excluded.family, tag_source=excluded.tag_source,
                    confidence=excluded.confidence, brand=excluded.brand,
                    confirmed=excluded.confirmed
                """,
                (
                    key,
                    subject,
                    position,
                    era_from,
                    era_to,
                    family,
                    tag_source,
                    confidence,
                    brand,
                    int(confirmed),
                ),
            )

    def tags_pending_review(self) -> list[dict]:
        """Bumps whose subject is not yet confirmed by a human.

        The *guess* is resolved the same way the planner resolves it (sidecar,
        then folder, then filename), so the queue shows what the scheduler would
        actually do rather than an empty field.

        Bumps in a reserved folder are excluded. ``media/bumps/neutral/`` is a
        deliberate statement that a card names no show, so asking a human to assign
        one is busywork -- and a bulk-imported archive contributes hundreds of
        them, burying the few that genuinely need a decision.
        """
        rows = list(
            self.connect().execute(
                """
                SELECT m.key, m.path, m.duration_ms, m.show_id AS folder_subject,
                       t.subject AS tag_subject, t.position, t.tag_source,
                       t.confidence, t.era_from, t.confirmed
                FROM media m LEFT JOIN asset_tags t ON t.key = m.key
                WHERE m.kind = 'bump' AND COALESCE(t.confirmed, 0) = 0
                ORDER BY m.key
                """
            )
        )
        out: list[dict] = []
        for row in rows:
            path = Path(row["path"])
            # A card deliberately filed as neutral needs no subject. Checked here
            # rather than in SQL because "reserved" is a Python-side convention.
            if row["folder_subject"] in RESERVED_BUMP_FOLDERS:
                continue
            guess = None
            source = "unknown"
            confidence = 0.0
            try:
                sidecar = read_tags(path)
            except SidecarError:
                sidecar = None
            if sidecar is not None:
                guess = sidecar.subject
                source = sidecar.source
                confidence = sidecar.confidence
            elif row["tag_subject"] is not None:
                guess = row["tag_subject"]
                source = row["tag_source"] or "manual"
                confidence = row["confidence"] or 1.0
            else:
                guess = subject_from_folder(row["folder_subject"])
                if guess:
                    source = "folder"
                    confidence = 0.5
            out.append(
                {
                    "key": row["key"],
                    "path": row["path"],
                    "duration_ms": row["duration_ms"],
                    "subject": guess,
                    "position": (sidecar.position if sidecar else row["position"]),
                    "tag_source": source,
                    "confidence": confidence,
                    "era_from": (sidecar.era if sidecar else row["era_from"]),
                }
            )
        return out

    # -- airings / cooldowns ----------------------------------------------

    def record_airing(
        self,
        *,
        item_id: str,
        asset_key: str,
        kind: str,
        start_ms: int,
        end_ms: int,
        day: str,
        slot_index: int,
    ) -> None:
        with self.transaction() as connection:
            connection.execute(
                """
                INSERT INTO airings (item_id, asset_key, kind, start_ms, end_ms, day, slot_index)
                VALUES (?,?,?,?,?,?,?)
                ON CONFLICT(item_id) DO UPDATE SET
                    start_ms=excluded.start_ms, end_ms=excluded.end_ms,
                    day=excluded.day, slot_index=excluded.slot_index
                """,
                (item_id, asset_key, kind, start_ms, end_ms, day, slot_index),
            )

    def last_aired(self, kinds: Sequence[str] = ("bump", "ad")) -> dict[str, int]:
        placeholders = ",".join("?" for _ in kinds)
        rows = self.connect().execute(
            f"SELECT asset_key, MAX(start_ms) AS last FROM airings "
            f"WHERE kind IN ({placeholders}) GROUP BY asset_key",
            tuple(kinds),
        )
        return {row["asset_key"]: row["last"] for row in rows}

    def clear_day(self, day: str) -> None:
        with self.transaction() as connection:
            connection.execute("DELETE FROM airings WHERE day = ?", (day,))

    def clear_airings(self) -> None:
        with self.transaction() as connection:
            connection.execute("DELETE FROM airings")

    # -- state (rotation cursors, channel state) --------------------------

    def get_state(self, key: str, default: str | None = None) -> str | None:
        row = self.connect().execute("SELECT value FROM state WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default

    def set_state(self, key: str, value: str) -> None:
        with self.transaction() as connection:
            connection.execute(
                "INSERT INTO state(key, value) VALUES (?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value),
            )

    def get_json_state(self, key: str) -> dict | None:
        raw = self.get_state(key)
        if not raw:
            return None
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return None

    def set_json_state(self, key: str, value: object) -> None:
        self.set_state(key, json.dumps(value, sort_keys=True))


BUMP_POSITION_RE = re.compile(
    r"(?:^|[^a-z])(into|out|next|after|any|bumper|return|stay)(?:[^a-z]|$)", re.IGNORECASE
)
"""Filename conventions people actually use, e.g. ``athf-into-03.mp4``."""

_BUMP_POSITION_ALIASES = {
    "into": "INTO",
    "out": "OUT",
    "next": "NEXT",
    "after": "AFTER",
    "any": "ANY",
    "bumper": "ANY",
    "return": "OUT",
    "stay": "INTO",
}


def _position_from_name(stem: str) -> str | None:
    match = BUMP_POSITION_RE.search(stem)
    if not match:
        return None
    return _BUMP_POSITION_ALIASES.get(match.group(1).lower())


def _era_from_path(path: Path) -> int | None:
    """Last 195x-204x year found in the path's directories (year folders win)."""
    for part in reversed(path.parts[:-1]):
        match = re.search(r"\b(19[5-9]\d|20[0-4]\d)\b", part)
        if match:
            return int(match.group(1))
    return None


def derived_key(asset_key: str) -> str:
    """Filename stem for an asset's composited copy (``show:s01e001`` -> a file)."""
    return asset_key.replace(":", "_")


def _derived_for(root: Path | None, asset_key: str) -> Path | None:
    """The composited copy for *asset_key*, if one has been made."""
    if root is None:
        return None
    stem = derived_key(asset_key)
    for suffix in (".mkv", ".mp4"):
        candidate = root / f"{stem}{suffix}"
        if candidate.is_file():
            return candidate
    return None


def _walk_media(root: Path) -> Iterator[Path]:
    for path in sorted(root.rglob("*")):
        if is_media_name(path.name):
            yield path


def build_show_folders(media_root: Path, show_ids: Iterable[str]) -> dict[str, Path]:
    base = Path(media_root) / "shows"
    return {show_id: base / show_id for show_id in show_ids}


def catalog_path(data_dir: Path) -> Path:
    return Path(data_dir) / "catalog.sqlite"


def summarise_by_show(catalog: Catalog) -> dict[str, int]:
    rows = catalog.connect().execute(
        "SELECT show_id, COUNT(*) AS n FROM media WHERE kind='episode' GROUP BY show_id"
    )
    return {row["show_id"]: row["n"] for row in rows}


def show_folder_for(media_root: Path, folder: str) -> Path:
    path = Path(folder)
    return path if path.is_absolute() else Path(media_root) / path


__all__ = [
    "Catalog",
    "SCHEMA_VERSION",
    "ScanReport",
    "build_show_folders",
    "catalog_path",
    "show_folder_for",
    "summarise_by_show",
]
