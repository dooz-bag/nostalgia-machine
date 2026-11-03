"""Observed HLS segment requests, mapped back to scheduled items."""

from __future__ import annotations

import json
import os
import sqlite3
import threading
from bisect import bisect_right
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Iterable, Iterator

if TYPE_CHECKING:
    from .plan import Plan


def schedule_rows(plans: Iterable[Plan]) -> list[dict[str, object]]:
    """Every planned item, keyed by channel and start time.

    Bumps and idents are included even though they are never counted: a segment
    that lands on one is *explained*, which is what keeps a real misalignment
    between the stream and the schedule visible in the proxy log.
    """
    return sorted(
        (
            {
                "channel": str(plan.channel_number),
                "item_id": item.item_id,
                "asset_key": item.asset_key,
                "kind": item.kind,
                "start_ms": int(item.start_ms),
                "end_ms": int(item.end_ms),
            }
            for plan in plans
            for item in plan.items
        ),
        key=lambda row: (row["channel"], row["start_ms"]),
    )


def write_schedule_map(path: Path, rows: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    with temporary.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(
            json.dumps({"version": 1, "items": rows}, separators=(",", ":")) + "\n"
        )
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def activate_schedule_map(
    live_path: Path,
    pending_path: Path,
    *,
    swap_ms: int,
    now_ms: int,
) -> None:
    """Promote a staged map while retaining the whole item carried over the switch."""
    live = _read_schedule_rows(live_path)
    pending = _read_schedule_rows(pending_path)
    kept = [
        row for row in live
        if int(row["start_ms"]) < swap_ms and int(row["end_ms"]) > now_ms
    ]
    kept_until = {
        channel: max(
            int(row["end_ms"]) for row in kept if row["channel"] == channel
        )
        for channel in {str(row["channel"]) for row in kept}
    }
    fresh: list[dict[str, object]] = []
    for row in sorted(pending, key=lambda item: (item["channel"], item["start_ms"])):
        channel = str(row["channel"])
        cursor = kept_until.get(channel, swap_ms)
        start, end = int(row["start_ms"]), int(row["end_ms"])
        if end <= cursor:
            continue
        if start < cursor:
            row = {**row, "start_ms": cursor}
        fresh.append(row)
        kept_until[channel] = int(row["end_ms"])
    write_schedule_map(live_path, sorted(
        kept + fresh, key=lambda row: (row["channel"], row["start_ms"])
    ))
    pending_path.unlink(missing_ok=True)


def _read_schedule_rows(path: Path) -> list[dict[str, object]]:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []
    rows = document.get("items", []) if isinstance(document, dict) else []
    if not isinstance(rows, list):
        return []
    return [
        row for row in rows
        if isinstance(row, dict)
        and isinstance(row.get("channel"), str)
        and isinstance(row.get("item_id"), str)
        and isinstance(row.get("asset_key"), str)
        and isinstance(row.get("kind"), str)
        and isinstance(row.get("start_ms"), int)
        and isinstance(row.get("end_ms"), int)
    ]


def parse_hls_segment_times(playlist: str) -> dict[str, int]:
    """Map segment filenames to their HLS program-date-time timestamps."""
    timestamps: dict[str, int] = {}
    next_ms: int | None = None
    duration_ms: int | None = None
    for line in playlist.splitlines():
        line = line.strip()
        if line.startswith("#EXT-X-PROGRAM-DATE-TIME:"):
            timestamp = datetime.fromisoformat(
                line.partition(":")[2].replace("Z", "+00:00")
            )
            next_ms = int(timestamp.timestamp() * 1000)
        elif line.startswith("#EXTINF:"):
            try:
                duration_ms = round(float(line.partition(":")[2].partition(",")[0]) * 1000)
            except ValueError:
                duration_ms = None
        elif line and not line.startswith("#") and duration_ms is not None:
            name = Path(line).name
            if next_ms is not None:
                timestamps[name] = next_ms
                next_ms += duration_ms
            duration_ms = None
    return timestamps


class ScheduleMap:
    def __init__(self, path: Path):
        self.path = path
        self._mtime_ns: int | None = None
        self._rows: dict[str, list[dict[str, object]]] = {}
        self._starts: dict[str, list[int]] = {}
        self._lock = threading.Lock()

    def find(self, channel: str, timestamp_ms: int) -> dict[str, object] | None:
        with self._lock:
            try:
                mtime_ns = self.path.stat().st_mtime_ns
            except FileNotFoundError:
                self._rows = {}
                self._starts = {}
                self._mtime_ns = None
                return None
            if mtime_ns != self._mtime_ns:
                self._rows = {}
                for row in _read_schedule_rows(self.path):
                    self._rows.setdefault(str(row["channel"]), []).append(row)
                self._starts = {
                    key: [int(row["start_ms"]) for row in rows]
                    for key, rows in self._rows.items()
                }
                self._mtime_ns = mtime_ns

            rows = self._rows.get(channel, [])
            starts = self._starts.get(channel, [])
            index = bisect_right(starts, timestamp_ms) - 1
            if index < 0:
                return None
            row = rows[index]
            if int(row["start_ms"]) <= timestamp_ms < int(row["end_ms"]):
                return row
            return None


class PlaybackStore:
    """Observed starts, one row per planned item.

    The key is ``(channel, item_id)``, not the scheduled start: a replan moves an
    item's start time, and keying on it would record the same airing twice.
    """

    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connection() as connection:
            table = connection.execute(
                "SELECT 1 FROM sqlite_master "
                "WHERE type='table' AND name='playback_events'"
            ).fetchone()
            if table is None:
                connection.execute("PRAGMA journal_mode=WAL")
                self._create(connection)
            elif not self._keyed_by_item(connection):
                self._migrate(connection)
            # A migration that failed part-way leaves the old rows behind under this
            # name. SQLite does not roll back DDL, so finishing the job here is what
            # makes the upgrade recoverable instead of silently losing history.
            self._recover(connection)

    @staticmethod
    def _create(connection: sqlite3.Connection) -> None:
        connection.execute(
            """
            CREATE TABLE playback_events (
                channel TEXT NOT NULL,
                item_id TEXT NOT NULL,
                asset_key TEXT NOT NULL,
                kind TEXT NOT NULL,
                scheduled_start_ms INTEGER NOT NULL,
                first_requested_ms INTEGER NOT NULL,
                last_requested_ms INTEGER NOT NULL,
                segment_requests INTEGER NOT NULL DEFAULT 1,
                PRIMARY KEY (channel, item_id)
            )
            """
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS playback_events_kind_time "
            "ON playback_events(kind, first_requested_ms)"
        )

    @staticmethod
    def _keyed_by_item(connection: sqlite3.Connection) -> bool:
        columns = connection.execute("PRAGMA table_info(playback_events)").fetchall()
        primary = [row["name"] for row in columns if row["pk"]]
        return primary == ["channel", "item_id"]

    @staticmethod
    def _migrate(connection: sqlite3.Connection) -> None:
        """Rebuild an early table that keyed on the scheduled start time."""
        connection.execute("ALTER TABLE playback_events RENAME TO playback_events_old")
        # Renaming a table keeps its indexes, and keeps their names: without this
        # the CREATE INDEX below fails with "index already exists".
        connection.execute("DROP INDEX IF EXISTS playback_events_kind_time")
        PlaybackStore._create(connection)
        PlaybackStore._recover(connection)

    @staticmethod
    def _recover(connection: sqlite3.Connection) -> None:
        """Fold any leftover pre-migration rows in, then drop the old table."""
        leftover = connection.execute(
            "SELECT 1 FROM sqlite_master "
            "WHERE type='table' AND name='playback_events_old'"
        ).fetchone()
        if leftover is None:
            return
        connection.execute(
            """
            INSERT INTO playback_events (
                channel, item_id, asset_key, kind, scheduled_start_ms,
                first_requested_ms, last_requested_ms, segment_requests
            )
            SELECT channel, item_id, asset_key, kind, MIN(scheduled_start_ms),
                   MIN(first_requested_ms), MAX(last_requested_ms),
                   SUM(segment_requests)
            FROM playback_events_old
            GROUP BY channel, item_id
            ON CONFLICT(channel, item_id) DO UPDATE SET
                first_requested_ms = MIN(first_requested_ms, excluded.first_requested_ms),
                last_requested_ms = MAX(last_requested_ms, excluded.last_requested_ms),
                segment_requests = segment_requests + excluded.segment_requests
            """
        )
        connection.execute("DROP TABLE playback_events_old")

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=30000")
        return connection

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def record_request(
        self, row: dict[str, object], *, requested_ms: int
    ) -> bool:
        values = (
            str(row["channel"]),
            str(row["item_id"]),
            str(row["asset_key"]),
            str(row["kind"]),
            int(row["start_ms"]),
            requested_ms,
        )
        with self._connection() as connection:
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO playback_events (
                    channel, item_id, asset_key, kind, scheduled_start_ms,
                    first_requested_ms, last_requested_ms
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (*values, requested_ms),
            )
            created = cursor.rowcount == 1
            if not created:
                connection.execute(
                    """
                    UPDATE playback_events
                    SET last_requested_ms = ?, segment_requests = segment_requests + 1,
                        scheduled_start_ms = ?
                    WHERE channel = ? AND item_id = ?
                    """,
                    (requested_ms, values[4], values[0], values[1]),
                )
            return created

    def airing_counts(
        self,
        kind: str,
        *,
        since_ms: int | None,
        before_ms: int,
        limit: int = 100,
    ) -> dict[str, object]:
        if kind not in {"episode", "ad"}:
            raise ValueError(f"unsupported playback kind: {kind!r}")
        conditions = ["e.kind = ?", "e.first_requested_ms <= ?"]
        params: list[object] = [kind, before_ms]
        if since_ms is not None:
            conditions.append("e.first_requested_ms >= ?")
            params.append(since_ms)
        where = " AND ".join(conditions)
        with self._connection() as connection:
            totals = connection.execute(
                f"""
                SELECT COUNT(*) AS total, COUNT(DISTINCT asset_key) AS distinct_assets
                FROM playback_events e WHERE {where}
                """,
                params,
            ).fetchone()
            items = connection.execute(
                f"""
                SELECT e.asset_key, COUNT(*) AS airings,
                       MAX(e.last_requested_ms) AS last_aired_ms,
                       SUM(e.segment_requests) AS segment_requests,
                       m.path, m.title, m.show_id, m.season, m.episode
                FROM playback_events e
                LEFT JOIN media m ON m.key = e.asset_key
                WHERE {where}
                GROUP BY e.asset_key, m.path, m.title, m.show_id, m.season, m.episode
                ORDER BY airings DESC, last_aired_ms DESC, e.asset_key
                LIMIT ?
                """,
                (*params, max(1, int(limit))),
            ).fetchall()
        return {
            "total_airings": int(totals["total"] or 0),
            "distinct_assets": int(totals["distinct_assets"] or 0),
            "items": [
                {
                    "key": row["asset_key"],
                    "airings": int(row["airings"]),
                    "last_aired_ms": int(row["last_aired_ms"]),
                    "segment_requests": int(row["segment_requests"]),
                    "path": row["path"] or "",
                    "title": row["title"] or "",
                    "show_id": row["show_id"] or "",
                    "season": int(row["season"] or 0),
                    "episode": int(row["episode"] or 0),
                }
                for row in items
            ],
        }


__all__ = [
    "PlaybackStore",
    "ScheduleMap",
    "activate_schedule_map",
    "parse_hls_segment_times",
    "schedule_rows",
    "write_schedule_map",
]
