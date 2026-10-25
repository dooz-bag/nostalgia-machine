"""Emit playout JSON and XMLTV from a :class:`~nostalgia.plan.Plan`.

Both outputs come from the same object, which is the entire reason the guide can
never drift from the stream.

Playout files are named exactly as ErsatzTV Next expects
(``{start}_{finish}.json`` with compact ISO 8601), and are written atomically so
the running channel never reads a half-written file.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Iterable
from xml.sax.saxutils import escape

from .plan import Plan, PlannedItem
from .timeutil import compact_stamp, from_epoch_ms, rfc3339, tz

PLAYOUT_VERSION = "https://ersatztv.org/playout/version/0.0.7"
PLAYOUT_SCHEMA = "https://ersatztv.org/schema/playout/0.0.7.json"
LINEUP_VERSION = "https://ersatztv.org/lineup/version/0.0.1"
# ErsatzTV version strings are ``0.<breaking>.<compatible>`` and a build refuses
# to load a file whose ``compatible`` is *newer* than it supports. Older ones keep
# working, so this is deliberately pinned to the oldest supported release rather
# than to whatever upstream main declares:
#   0.2.3 (the current ghcr image) supports 0.1.2
#   0.1.3 exists only on main / in unreleased builds
# Hitting this from the other direction is how a channel silently 503s: the
# streamer logs "unsupported schema version" and exits the session.
CHANNEL_VERSION = "https://ersatztv.org/channel/version/0.1.2"


@dataclass(frozen=True, slots=True)
class EmitPaths:
    playout_dir: Path
    xmltv_dir: Path


def playout_filename(plan: Plan) -> str:
    """The filename ErsatzTV Next scans for."""
    start = from_epoch_ms(plan.day_start_ms, tz(_zone_hint(plan)))
    finish = from_epoch_ms(plan.day_end_ms, tz(_zone_hint(plan)))
    return f"{compact_stamp(start)}_{compact_stamp(finish)}.json"


_ZONE_CACHE: dict[str, str] = {}

#: Optional host-root -> container-root remap for paths written into the playout
#: document. Inside Docker the mounts do not mirror the repo, so a host path would
#: point at nothing; see ``Paths.saw``.
_PATH_MAP: dict[str, tuple[str, str] | None] = {"map": None}


def set_channel_timezone(name: str) -> None:
    _ZONE_CACHE["name"] = name


def set_path_mapping(host_root: str | Path | None, target_root: str | Path | None) -> None:
    """Remap ``host_root`` to ``target_root`` in emitted media paths."""
    if host_root and target_root:
        # Normalise the host side only. `target_root` is a *container* path, and
        # resolving it on Windows would rewrite "/app" to "C:/app" and produce a
        # path that is wrong inside the container.
        host = _posix(Path(host_root).resolve()).rstrip("/")
        _PATH_MAP["map"] = (host, _posix(target_root).rstrip("/"))
    else:
        _PATH_MAP["map"] = None


def _seen_by_streamer(path: str | Path) -> str:
    text = _posix(path)
    mapping = _PATH_MAP["map"]
    if mapping is None:
        return text
    host_root, target_root = mapping
    if text == host_root:
        return target_root
    # The trailing separator matters: without it "/host/project-other" would look
    # like a child of "/host/project".
    prefix = host_root + "/"
    if text.startswith(prefix):
        return target_root + text[len(host_root) :]
    return text


def _zone_hint(plan: Plan) -> str:
    return _ZONE_CACHE.get("name", "America/New_York")


def to_playout_json(plan: Plan) -> dict:
    """Serialise a plan to the ErsatzTV Next playout document."""
    zone = tz(_zone_hint(plan))
    items = []
    for item in plan.items:
        start = from_epoch_ms(item.start_ms, zone)
        finish = from_epoch_ms(item.end_ms, zone)
        if item.lavfi_params:
            source: dict = {"source_type": "lavfi", "params": item.lavfi_params}
        else:
            source = {"source_type": "local", "path": _seen_by_streamer(item.path)}
            if item.in_point_ms is not None:
                source["in_point_ms"] = item.in_point_ms
            if item.out_point_ms is not None:
                source["out_point_ms"] = item.out_point_ms
        items.append(
            {
                "id": item.item_id,
                "start": rfc3339(start),
                "finish": rfc3339(finish),
                "source": source,
            }
        )
    return {
        "$schema": PLAYOUT_SCHEMA,
        "version": PLAYOUT_VERSION,
        "items": items,
    }


def write_playout(plan: Plan, paths: EmitPaths) -> Path:
    paths.playout_dir.mkdir(parents=True, exist_ok=True)
    target = paths.playout_dir / playout_filename(plan)
    payload = to_playout_json(plan)
    _write_atomic(target, json.dumps(payload, indent=2) + "\n")
    return target


def prune_playouts(paths: EmitPaths, *, keep_days: Iterable[date]) -> list[Path]:
    """Delete playout files for days we no longer intend to serve."""
    keep = {f"{day:%Y%m%d}" for day in keep_days}
    removed: list[Path] = []
    if not paths.playout_dir.is_dir():
        return removed
    for path in paths.playout_dir.glob("*.json"):
        if not path.name[:8] in keep:
            path.unlink(missing_ok=True)
            removed.append(path)
    return removed


# -- XMLTV -----------------------------------------------------------------


def to_xmltv(plan: Plan, *, channel_name: str | None = None) -> str:
    """XMLTV guide for one channel, built from the plan's guide items."""
    zone = tz(_zone_hint(plan))
    name = channel_name or plan.channel_name
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<!DOCTYPE tv SYSTEM "xmltv.dtd">',
        '<tv generator-info-name="Nostalgia Machine">',
        f'  <channel id="{escape(plan.tvg_id)}">',
        f"    <display-name>{escape(name)}</display-name>",
        f"    <display-name>{escape(plan.channel_number)}</display-name>",
        "  </channel>",
    ]

    for item in _guide_programmes(plan):
        start = from_epoch_ms(item.start_ms, zone)
        stop = from_epoch_ms(item.end_ms, zone)
        lines.append(
            f'  <programme start="{xmltv_stamp(start)}" stop="{xmltv_stamp(stop)}" '
            f'channel="{escape(plan.tvg_id)}">'
        )
        lines.append(f"    <title lang=\"en\">{escape(item.title or 'Programme')}</title>")
        if item.subtitle:
            lines.append(f"    <sub-title lang=\"en\">{escape(item.subtitle)}</sub-title>")
        if item.description:
            lines.append(f"    <desc lang=\"en\">{escape(item.description)}</desc>")
        lines.append(f"    <category lang=\"en\">{_category(item.kind)}</category>")
        if item.kind == "episode" and item.subtitle:
            season, episode = _season_episode(item.subtitle)
            if season is not None:
                lines.append(f'    <episode-num system="xmltv_ns">{season}.{episode}.0/1</episode-num>')
        lines.append("  </programme>")

    lines.append("</tv>")
    return "\n".join(lines) + "\n"


def _guide_programmes(plan: Plan) -> list[PlannedItem]:
    """The guide's programmes: merged episode parts, each owning its whole slot.

    A 22-minute episode split by a mid-roll break is *one* programme in the guide,
    not two, so the parts are stitched back together.

    Each programme then runs until the next one starts. A broadcast guide is
    continuous -- a slot listed from 00:30 to 01:00 even though the episode is 22
    minutes -- and a guide that leaked gaps at every commercial break would make
    most players render a broken schedule.
    """
    return _fill_guide_gaps(plan, _merge_episode_parts(plan))


def _fill_guide_gaps(plan: Plan, programmes: list[PlannedItem]) -> list[PlannedItem]:
    if not programmes:
        return programmes
    filled: list[PlannedItem] = []
    for index, item in enumerate(programmes):
        # Only the *stop* is stretched. Extending the first programme's start to
        # the day boundary and also deriving it from the previous stop produced
        # overlaps; a programme simply runs until the next one begins.
        start = plan.day_start_ms if index == 0 else item.start_ms
        stop = (
            programmes[index + 1].start_ms
            if index + 1 < len(programmes)
            else plan.day_end_ms
        )
        if start == item.start_ms and stop == item.end_ms:
            filled.append(item)
            continue
        filled.append(
            PlannedItem(
                item_id=item.item_id,
                kind=item.kind,
                start_ms=start,
                end_ms=max(stop, item.end_ms),
                asset_key=item.asset_key,
                path=item.path,
                title=item.title,
                subtitle=item.subtitle,
                description=item.description,
                show_id=item.show_id,
                slot_index=item.slot_index,
                guide=True,
            )
        )
    return filled


def _merge_episode_parts(plan: Plan) -> list[PlannedItem]:
    merged: list[PlannedItem] = []
    for item in plan.guide_items():
        if (
            merged
            and item.kind == "episode"
            and merged[-1].kind == "episode"
            and merged[-1].asset_key == item.asset_key
            and merged[-1].end_ms <= item.start_ms
        ):
            previous = merged[-1]
            merged[-1] = PlannedItem(
                item_id=previous.item_id,
                kind=previous.kind,
                start_ms=previous.start_ms,
                end_ms=item.end_ms,
                asset_key=previous.asset_key,
                path=previous.path,
                title=previous.title,
                subtitle=previous.subtitle,
                description=previous.description,
                show_id=previous.show_id,
                slot_index=previous.slot_index,
                guide=True,
            )
            continue
        merged.append(item)
    return merged


def _category(kind: str) -> str:
    return {
        "episode": "Series",
        "ad": "Commercial",
        "bump": "Bumper",
        "ident": "Ident",
    }.get(kind, "Other")


def _season_episode(subtitle: str) -> tuple[int | None, int | None]:
    # subtitle is "S01E02"
    if len(subtitle) >= 6 and subtitle[0] == "S" and "E" in subtitle:
        try:
            season = int(subtitle[1:3]) - 1
            episode = int(subtitle.split("E", 1)[1]) - 1
            return season, episode
        except ValueError:
            return None, None
    return None, None


def xmltv_stamp(dt: datetime) -> str:
    return dt.strftime("%Y%m%d%H%M%S") + " " + dt.strftime("%z")


def write_xmltv(plan: Plan, paths: EmitPaths, *, channel_name: str | None = None) -> Path:
    paths.xmltv_dir.mkdir(parents=True, exist_ok=True)
    target = paths.xmltv_dir / f"{plan.tvg_id}.xml"
    _write_atomic(target, to_xmltv(plan, channel_name=channel_name))
    return target


# -- lineup / channel config ----------------------------------------------


def lineup_json(
    *,
    channel_number: str,
    channel_name: str,
    tvg_id: str,
    channel_config_path: str,
    output_folder: str,
    xmltv_folder: str,
    logo: str | None = None,
    group: str = "Nostalgia",
    port: int = 8409,
) -> dict:
    entry: dict = {
        "number": channel_number,
        "name": channel_name,
        "config": channel_config_path,
        "tvg_id": tvg_id,
        "group": group,
    }
    if logo:
        entry["logo"] = logo
    return {
        "version": LINEUP_VERSION,
        "server": {"bind_address": "0.0.0.0", "port": port},
        "output": {"folder": output_folder},
        "xmltv": {"folder": xmltv_folder},
        "channels": [entry],
    }


def channel_json(
    *,
    playout_folder: str,
    width: int,
    height: int,
    bitrate_kbps: int,
    audio_bitrate_kbps: int,
    fallback_seconds: int = 60,
) -> dict:
    """Channel config with stream-copy-friendly 720p defaults."""
    return {
        "version": CHANNEL_VERSION,
        "playout": {"folder": playout_folder},
        "ffmpeg": {"ffmpeg_path": "", "ffprobe_path": "", "disabled_filters": []},
        "fallback": {"duration_seconds": fallback_seconds},
        "normalization": {
            "audio": {
                "mode": "transcode",
                "format": "aac",
                "bitrate_kbps": audio_bitrate_kbps,
                "buffer_kbps": audio_bitrate_kbps * 2,
                "channels": 2,
                "sample_rate_hz": 48000,
                "normalize_loudness": True,
                "loudness": {
                    "integrated_target": -16,
                    "range_target": 11,
                    "true_peak": -1.5,
                },
            },
            "video": {
                "mode": "transcode",
                "format": "h264",
                "bit_depth": 8,
                "width": width,
                "height": height,
                "bitrate_kbps": bitrate_kbps,
                "buffer_kbps": bitrate_kbps * 2,
                # No "accel" key: the schema only accepts the enum names
                # (qsv/nvenc/vaapi/...), and omitting it means software.
            },
            # Only "burn" and "convert" are valid; there is no "none".
            "subtitle": {"mode": "burn"},
        },
    }


def write_ersatztv_config(
    paths: EmitPaths,
    *,
    channel_number: str,
    channel_name: str,
    tvg_id: str,
    width: int,
    height: int,
    bitrate_kbps: int,
    audio_bitrate_kbps: int,
    logo: str | None = None,
    group: str = "Nostalgia",
    port: int = 8409,
    config_dir: Path | None = None,
) -> tuple[Path, Path]:
    """Write ``lineup.json`` and ``channels/<n>/channel.json``.

    *config_dir* defaults to the playout folder's parent, which keeps ad-hoc use
    working, but is passed explicitly by the pipeline so the files land in one
    mountable ``lineup/`` directory. ErsatzTV is then handed that single read-only
    folder and never needs to see the project layout.

    Every path written into those files is resolved from ``paths``, not from
    *config_dir*: the HLS output belongs beside the playout folder, so moving the
    config to its own directory must not drag ``hls/`` along with it.
    """
    root = Path(config_dir) if config_dir else paths.playout_dir.parent
    channel_dir = root / "channels" / channel_number
    channel_dir.mkdir(parents=True, exist_ok=True)
    hls_dir = paths.playout_dir.parent / "hls"

    channel_path = channel_dir / "channel.json"
    _write_atomic(
        channel_path,
        json.dumps(
            channel_json(
                playout_folder=_seen_by_streamer(paths.playout_dir),
                width=width,
                height=height,
                bitrate_kbps=bitrate_kbps,
                audio_bitrate_kbps=audio_bitrate_kbps,
            ),
            indent=2,
        )
        + "\n",
    )

    lineup_path = root / "lineup.json"
    _write_atomic(
        lineup_path,
        json.dumps(
            lineup_json(
                channel_number=channel_number,
                channel_name=channel_name,
                tvg_id=tvg_id,
                channel_config_path=f"./channels/{channel_number}/channel.json",
                output_folder=_seen_by_streamer(hls_dir),
                xmltv_folder=_seen_by_streamer(paths.xmltv_dir),
                logo=logo,
                group=group,
                port=port,
            ),
            indent=2,
        )
        + "\n",
    )
    return lineup_path, channel_path


# -- helpers ---------------------------------------------------------------


def _posix(path: str | Path) -> str:
    return str(path).replace("\\", "/")


def _write_atomic(target: Path, content: str) -> None:
    """Write via a sibling temp file + replace, so readers never see partial data."""
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.tmp")
    with open(temporary, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, target)


__all__ = [
    "CHANNEL_VERSION",
    "EmitPaths",
    "LINEUP_VERSION",
    "PLAYOUT_SCHEMA",
    "PLAYOUT_VERSION",
    "channel_json",
    "lineup_json",
    "playout_filename",
    "prune_playouts",
    "set_channel_timezone",
    "set_path_mapping",
    "to_playout_json",
    "to_xmltv",
    "write_ersatztv_config",
    "write_playout",
    "write_xmltv",
    "xmltv_stamp",
]
