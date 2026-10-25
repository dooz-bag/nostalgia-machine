"""The media catalog (SQLite).

Everything the scheduler needs to know about the library lives here, and nothing
else does. The catalog is *derived* state: deleting ``data/catalog.sqlite`` and
re-running ``scan`` rebuilds it exactly, apart from the human confirmations in
``asset_tags``, which are therefore kept in their own table and preserved across
rescans.
"""

from __future__ import annotations

import json
import sqlite3
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
from .sidecars import SidecarError, subject_from_folder
from .sidecars import read as read_tags

SCHEMA_VERSION = 1

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
    probed_by  TEXT NOT NULL DEFAULT 'ffprobe'
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


@dataclass
class ScanReport:
    scanned: int = 0
    probed: int = 0
    from_sidecar: int = 0
    skipped_existing: int = 0
    issues: list[tuple[str, str]] = field(default_factory=list)
    per_kind: dict[str, int] = field(default_factory=dict)

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
            connection = sqlite3.connect(self.path)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA foreign_keys=ON")
            connection.executescript(_SCHEMA)
            connection.execute(
                "INSERT OR IGNORE INTO meta(key, value) VALUES ('schema_version', ?)",
                (str(SCHEMA_VERSION),),
            )
            self._connection = connection
        return self._connection

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
    ) -> ScanReport:
        """Walk every media root and (re)build the ``media`` table."""
        report = ScanReport()
        ffprobe = find_ffprobe(ffprobe_path)

        rows: list[tuple] = []
        for show_id, folder in sorted(show_folders.items()):
            if not Path(folder).is_dir():
                report.note(Path(folder), f"show folder for {show_id!r} is missing")
                continue
            rows.extend(self._scan_show(show_id, Path(folder), ffprobe, report, write_sidecars))

        for root, kind in ((bump_root, "bump"), (ad_root, "ad"), (ident_root, "ident")):
            if root and Path(root).is_dir():
                rows.extend(self._scan_flat(Path(root), kind, ffprobe, report, write_sidecars))

        if rows:
            with self.transaction() as connection:
                connection.executemany(
                    """
                    INSERT INTO media (key, kind, path, folder, show_id, season, episode,
                                       title, duration_ms, format_name, video_codec, width,
                                       height, audio_codec, audio_channels, size_bytes,
                                       mtime_ns, probed_by)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
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
                connection.execute("DELETE FROM scan_issues")
                connection.executemany(
                    "INSERT OR REPLACE INTO scan_issues(path, reason) VALUES (?,?)",
                    report.issues,
                )
        for row in rows:
            report.per_kind[row[1]] = report.per_kind.get(row[1], 0) + 1
            report.scanned += 1
        return report

    def _scan_show(
        self,
        show_id: str,
        folder: Path,
        ffprobe: str | None,
        report: ScanReport,
        write_sidecars: bool,
    ) -> list[tuple]:
        rows: list[tuple] = []
        for path in _walk_media(folder):
            stat = path.stat()
            parsed = parse_episode(path.name, season_dir=path.parent.name)
            if parsed is None:
                report.note(path, "cannot determine season/episode from the filename")
                continue
            info, probed_by = self._info(path, stat.st_mtime_ns, ffprobe, report, write_sidecars)
            if info is None:
                continue
            key = f"{show_id}:s{parsed.season:02d}e{parsed.episode:03d}"
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
                )
            )
        return rows

    def _scan_flat(
        self,
        root: Path,
        kind: str,
        ffprobe: str | None,
        report: ScanReport,
        write_sidecars: bool,
    ) -> list[tuple]:
        rows: list[tuple] = []
        for path in _walk_media(root):
            stat = path.stat()
            info, probed_by = self._info(path, stat.st_mtime_ns, ffprobe, report, write_sidecars)
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
    ) -> tuple[MediaInfo | None, str]:
        sidecar = sidecar_duration_ms(path)
        if sidecar and not ffprobe:
            report.from_sidecar += 1
            return MediaInfo(duration_ms=sidecar, source="sidecar"), "sidecar"
        if not ffprobe:
            report.note(path, "no duration: install ffmpeg or add a .json sidecar")
            return None, "missing"
        try:
            info = probe(path, ffprobe)
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
                                   mtime_ns, probed_by)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(key) DO UPDATE SET
                    duration_ms=excluded.duration_ms, mtime_ns=excluded.mtime_ns,
                    size_bytes=excluded.size_bytes, probed_by=excluded.probed_by
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
