"""The end-to-end build: scan → plan → emit.

Both the CLI and the web UI call into here, so there is exactly one code path that
can produce a playout file. Anything that "just changes one thing" goes through
:func:`build` again, which is what keeps regeneration reproducible.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path

from .catalog import Catalog, ScanReport
from .config import DayConfig, RootConfig, load_config
from .emit import (
    EmitPaths,
    set_channel_timezone,
    set_path_mapping,
    write_ersatztv_config,
    write_playout,
    write_xmltv,
)
from .grid import GridError, build_day, is_dst_transition_day
from .plan import Plan, PlanContext, PlanError, build_plan
from .paths import Paths
from .rotation import cycle_length


class BuildError(RuntimeError):
    pass


@dataclass
class BuildResult:
    plans: list[Plan] = field(default_factory=list)
    playout_files: list[Path] = field(default_factory=list)
    xmltv_files: list[Path] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    fallbacks: int = 0
    fallback_reasons: list[str] = field(default_factory=list)
    lineup: Path | None = None


def watermark_card_path(paths: Paths) -> Path:
    """Folder holding the rendered mark card.

    Deliberately under ``media/`` rather than ``data/``: the streamer only mounts
    the media tree, so a card in ``data/`` is written by the scheduler and then
    invisible to the process that has to composite it -- which showed up as
    ``/app/data/watermark.png: No such file or directory`` and every episode
    silently replaced by black. It is not catalogued because the scanner only
    walks the shows/bumps/commercials/idents roots.
    """
    return paths.media("derived")


def _overlay_layer(config: RootConfig, paths: Paths) -> dict | None:
    """Build the playout ``graphics`` layer, or ``None`` when not in overlay mode.

    The card is rendered lazily and cached under ``media/derived/``, which is the
    one tree the streamer is guaranteed to have mounted.
    """
    from .watermark import WatermarkError, card_asset, graphics_layer, look_from_config

    if config.watermark.mode != "overlay":
        return None
    look = look_from_config(config.watermark)
    try:
        card = card_asset(
            look,
            width=config.encoding.width,
            height=config.encoding.height,
            ffmpeg=config.channel.ffmpeg_path,
            # The *folder*, not its parent: taking `.parent` wrote the card into
            # the media root, which is read-only in the container.
            cache_dir=watermark_card_path(paths),
        )
    except WatermarkError:
        return None
    return graphics_layer(
        look,
        path=str(paths.saw(card)).replace("\\", "/"),
    )


#: Strings the streamer binary must contain for an overlay layer to be honoured.
#: These are the serde field names from ``GraphicsLayer`` in ersatztv-playout; if
#: a future build drops the field, the layer is ignored *silently* and the mark
#: just stops appearing -- so it is worth detecting rather than discovering later.
OVERLAY_BINARY_MARKERS = ("within_source_content", "opacity_percent", "bottom_right")


def streamer_supports_overlay(*, binary: Path | None = None, container: str | None = None) -> bool | None:
    """Whether the streamer knows about graphics layers.

    Checks a host binary when there is one, else greps the binary inside a running
    container -- which is the normal case here, because ErsatzTV only runs as a
    container on Windows.

    Returns ``None`` when it cannot be determined (no binary, unreadable, container
    not running), so the caller warns rather than claiming a false pass.
    """
    if container:
        return _container_supports_overlay(container)
    if binary is None or not binary.is_file():
        return None
    try:
        blob = binary.read_bytes()
    except OSError:
        return None
    return all(marker.encode() in blob for marker in OVERLAY_BINARY_MARKERS)


def _container_supports_overlay(container: str) -> bool | None:
    import subprocess

    found = 0
    for marker in OVERLAY_BINARY_MARKERS:
        try:
            result = subprocess.run(
                # `docker exec` has no -T (that is `compose exec`); -i is not
                # needed for a non-interactive grep.
                ["docker", "exec", container, "sh", "-c",
                 f'grep -qc "{marker}" /app/ersatztv-channel 2>/dev/null'],
                capture_output=True, text=True, timeout=20, check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        if result.returncode == 0:
            found += 1
    if found == 0:
        # Either the support is absent or the container is not reachable. Both
        # mean "cannot confirm", which is a warning, not a pass.
        return None
    return found == len(OVERLAY_BINARY_MARKERS)


def _watermark_findings(config: RootConfig, paths: Paths) -> list[tuple[str, str, str]]:
    """Report whether the configured watermark mode can actually work."""
    findings: list[tuple[str, str, str]] = []
    config_wm = config.watermark
    if config_wm.mode == "none":
        return findings

    from .watermark import WANTED_FONT, resolve_mark_font

    resolved = resolve_mark_font(config_wm.font)
    if resolved is None:
        findings.append(
            (
                "error",
                "watermark",
                f"no usable font for {config_wm.font!r}; the mark cannot be rendered",
            )
        )
    elif config_wm.font == WANTED_FONT and resolved.name.lower() != WANTED_FONT.lower().replace(
        " ", ""
    ):
        # Not an error: the chain deliberately falls back. Saying which face is
        # actually in use is what lets the owner notice a boxy-looking mark.
        findings.append(
            (
                "ok",
                "watermark",
                f"{WANTED_FONT} not installed; using {resolved.name} "
                f"condensed by {config_wm.condense:g}",
            )
        )
    else:
        findings.append(("ok", "watermark", f"font {resolved.name}"))

    if not any(show.enable for show in config.shows.values()):
        findings.append(("warn", "watermark", "no shows are enabled; nothing to mark"))

    if config_wm.mode == "overlay":
        binary = _streamer_binary(paths)
        # Inside the scheduler container the Docker CLI is not available, so the
        # generic "cannot confirm" warning is the honest answer there.
        container = os.environ.get("NOSTALGIA_STREAMER_CONTAINER")
        supported = streamer_supports_overlay(binary=binary, container=container)
        if supported is True:
            findings.append(
                ("ok", "watermark", "streamer composited per item (overlay mode)")
            )
        elif supported is False:
            findings.append(
                (
                    "error",
                    "watermark",
                    "overlay mode, but this streamer build has no graphics support; "
                    "the mark will be silently ignored -- use mode: burn",
                )
            )
        else:
            findings.append(
                (
                    "warn",
                    "watermark",
                    "overlay mode; cannot inspect the streamer binary to confirm it "
                    "honours graphics layers (burn mode does not depend on it)",
                )
            )
    else:
        card = watermark_card_path(paths)
        cached = list(card.glob("*.mkv")) if card.is_dir() else []
        if cached:
            findings.append(
                ("ok", "watermark", f"burn mode; {len(cached)} composited episode(s) cached")
            )
        else:
            findings.append(
                (
                    "warn",
                    "watermark",
                    "burn mode; no composited episodes yet -- run 'nostalgia watermark --apply'",
                )
            )
    return findings


def _streamer_binary(paths: Paths) -> Path | None:
    """The ErsatzTV executable, host-side (Docker or native), if we can find it."""
    candidates = [paths.root / "ersatztv" / "ersatztv.exe", paths.root / "ersatztv" / "ersatztv"]
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None


def _is_video(path: Path) -> bool:
    from .probe import VIDEO_EXTENSIONS

    return path.is_file() and path.suffix.lower() in VIDEO_EXTENSIONS


def day_config_for(config: RootConfig, day: date) -> DayConfig:
    """Pick the day file that covers *day*."""
    weekday = day.strftime("%a").lower()[:3]
    for entry in config.days:
        if weekday in entry.days:
            return entry
    if config.days:
        return config.days[0]
    raise BuildError("no day configuration found")


def scan(
    config: RootConfig,
    catalog: Catalog,
    paths: Paths,
    *,
    ffprobe_path: str | None = None,
    write_sidecars: bool = False,
) -> ScanReport:
    """Scan every configured media root into the catalog."""
    show_folders = {}
    for show_id, show in config.shows.items():
        folder = Path(show.folder)
        show_folders[show_id] = folder if folder.is_absolute() else paths.media_root / folder

    return catalog.scan(
        media_root=paths.media_root,
        show_folders=show_folders,
        bump_root=paths.media("bumps"),
        ad_root=paths.media("commercials"),
        ident_root=paths.media("idents"),
        ffprobe_path=ffprobe_path or config.channel.ffmpeg_path,
        write_sidecars=write_sidecars,
        # Composited watermark copies, when burn mode has produced any.
        derived_root=paths.media("derived", "watermark"),
    )


def build(
    config: RootConfig,
    catalog: Catalog,
    paths: Paths,
    *,
    start: date | None = None,
    days: int | None = None,
    emit: bool = True,
    ignore_cooldowns: bool = False,
    allow_generated: bool | None = None,
) -> BuildResult:
    """Build and (optionally) write playout + XMLTV for a run of days."""
    set_channel_timezone(config.channel.grid.timezone)
    # Inside Docker the mounts do not mirror the repo, so emitted media paths are
    # remapped to what the streamer will actually see. Identity when unset.
    set_path_mapping(
        paths.root,
        __import__("os").environ.get("NOSTALGIA_CONTAINER_ROOT") or None,
    )
    paths.ensure_runtime_dirs()

    start_day = start or config.channel.on_air_date or date.today()
    horizon = days if days is not None else config.channel.horizon_days

    # Cards rendered by `nostalgia bumps` live in the media tree, so a build picks
    # them up without needing a separate scan in between.
    if allow_generated is not False and config.bumps.generated.enabled:
        catalog.register_generated(
            paths.media_root / "bumps" / "generated",
            ffprobe_path=config.channel.ffmpeg_path,
        )

    context = PlanContext.load(
        config, catalog, config_dir=paths.config_dir, media_root=paths.media_root
    )
    if not context.rings:
        raise BuildError(
            "no episodes are catalogued. Run 'nostalgia scan' after putting media "
            "in media/shows/<show-id>/"
        )

    result = BuildResult()
    emit_paths = EmitPaths(paths.playout_dir, paths.xmltv_dir)
    # Overlay mode only. Burn mode already replaced the paths during scan, so a
    # layer here would double the mark; `plan.watermark_rule` enforces that.
    watermark_layer = _overlay_layer(config, paths)

    for offset in range(horizon):
        day = start_day + timedelta(days=offset)
        day_config = day_config_for(config, day)
        try:
            plan = build_plan(
                day,
                day_config,
                context,
                ignore_cooldowns=ignore_cooldowns,
                allow_generated=allow_generated,
            )
        except (PlanError, GridError) as exc:
            raise BuildError(f"{day}: {exc}") from exc

        result.plans.append(plan)
        result.warnings.extend(plan.warnings)
        result.fallbacks += len(plan.bump_fallbacks)
        result.fallback_reasons.extend(plan.bump_fallback_reasons)

        if emit:
            result.playout_files.append(
                write_playout(plan, emit_paths, watermark_layer=watermark_layer)
            )
            result.xmltv_files.append(write_xmltv(plan, emit_paths))

    if emit:
        lineup, _ = write_ersatztv_config(
            emit_paths,
            channel_number=config.channel.number,
            channel_name=config.channel.name,
            tvg_id=config.channel.tvg_id,
            width=config.encoding.width,
            height=config.encoding.height,
            bitrate_kbps=config.encoding.bitrate_kbps,
            audio_bitrate_kbps=config.encoding.audio_bitrate_kbps,
            logo=config.channel.logo,
            group=config.channel.group,
            config_dir=paths.ersatztv_dir,
        )
        result.lineup = lineup

    return result


def doctor(config: RootConfig, catalog: Catalog, paths: Paths) -> list[tuple[str, str, str]]:
    """Return ``(level, area, message)`` diagnostics."""
    findings: list[tuple[str, str, str]] = []

    findings.append(("ok", "config", f"{len(config.shows)} shows, {len(config.days)} day files"))

    from .probe import find_ffprobe

    ffprobe = find_ffprobe(config.channel.ffmpeg_path)
    if ffprobe:
        findings.append(("ok", "ffmpeg", f"ffprobe at {ffprobe}"))
    else:
        findings.append(
            ("warn", "ffmpeg", "ffprobe not found; durations must come from .json sidecars")
        )

    issues = catalog.scan_issues()
    for row in issues:
        findings.append(("warn", "scan", f"{row['path']}: {row['reason']}"))
    if not issues:
        findings.append(("ok", "scan", "no scan issues"))

    # A folder under media/shows/ that no roster entry points at is invisible to
    # the scanner, so a show can sit there fully downloaded and never air. That is
    # the most expensive silent failure in the project, so it is reported.
    show_root = paths.media_root / "shows"
    configured = set()
    for show in config.shows.values():
        folder = Path(show.folder)
        configured.add((folder if folder.is_absolute() else paths.media_root / folder).name.lower())
    if show_root.is_dir():
        for folder in sorted(p for p in show_root.iterdir() if p.is_dir()):
            if folder.name.lower() in configured:
                continue
            files = sum(1 for _ in folder.rglob("*") if _is_video(_))
            if files:
                findings.append(
                    (
                        "warn",
                        "library",
                        f"{folder.name}: {files} video file(s) but no entry in "
                        f"shows.yaml -- this folder is never scanned",
                    )
                )
            else:
                findings.append(
                    (
                        "warn",
                        "library",
                        f"{folder.name}: empty folder under media/shows/ (ignored)",
                    )
                )

    from .catalog import summarise_by_show

    counts = summarise_by_show(catalog)
    for show_id, show in config.shows.items():
        found = counts.get(show_id, 0)
        if found == 0:
            findings.append(
                ("warn", "library", f"{show_id}: no episodes found in {show.folder}")
            )
        else:
            findings.append(("ok", "library", f"{show_id}: {found} episodes"))

    assets = catalog.assets()
    bumps = [asset for asset in assets if asset.kind == "bump"]
    ads = [asset for asset in assets if asset.kind == "ad"]
    idents = [asset for asset in assets if asset.kind == "ident"]
    findings.append(("ok", "library", f"{len(bumps)} bumps, {len(ads)} commercials, {len(idents)} idents"))

    generated = [asset for asset in bumps if asset.is_generated]
    if generated:
        findings.append(("ok", "bumps", f"{len(generated)} generated bumps cached"))

    if not ads:
        findings.append(("error", "library", "no commercials; breaks will be dead air"))
    if not bumps:
        findings.append(("warn", "library", "no bumps; the channel will feel unscheduled"))

    # Bumps with a subject get matched to a show; the rest are neutral fallbacks.
    # A large neutral pool is healthy -- a channel needs generic cards -- so warn
    # only when a *show's* coverage is genuinely thin.
    for show_id, show in config.shows.items():
        if not show.enable:
            continue
        cards = [asset for asset in bumps if asset.subject == show_id]
        if len(cards) < 3:
            findings.append(
                (
                    "warn",
                    "bumps",
                    f"{show_id}: only {len(cards)} bump card(s); "
                    f"add some under media/bumps/{show_id}/",
                )
            )
    neutral = [asset for asset in bumps if asset.subject is None]
    if neutral:
        findings.append(
            ("ok", "bumps", f"{len(neutral)} neutral card(s) available as fallback")
        )

    for show_id, show in config.shows.items():
        if show.eligibility == "window" and show.airing_from is None:
            continue

    findings.extend(_watermark_findings(config, paths))

    from .rotation import build_rings, cycle_length as _cycle

    rings = build_rings(
        catalog.episodes(),
        {show_id: show.order for show_id, show in config.shows.items()},
        seed_salt=config.channel.name,
    )
    length = _cycle(rings)
    if length:
        findings.append(
            ("ok", "rotation", f"every show returns to its start after {length} plays")
        )

    for day_config in config.days:
        for part in day_config.dayparts:
            if not any(catalog.episodes(show_id) for show_id in part.shows):
                findings.append(
                    (
                        "warn",
                        "schedule",
                        f"{day_config.name}: daypart {part.start:%H:%M} has no episodes",
                    )
                )

    return findings


__all__ = [
    "BuildError",
    "BuildResult",
    "build",
    "day_config_for",
    "doctor",
    "scan",
]
