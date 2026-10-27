"""The end-to-end build: scan → plan → emit.

Both the CLI and the web UI call into here, so there is exactly one code path that
can produce a playout file. Anything that "just changes one thing" goes through
:func:`build` again, which is what keeps regeneration reproducible.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path

from .catalog import Catalog, ScanReport
from .config import DayConfig, RootConfig, load_config
from .emit import (
    EmitPaths,
    prune_playouts,
    set_channel_timezone,
    set_path_mapping,
    write_ersatztv_config,
    write_guide,
    write_playout,
)
from .grid import GridError, build_day, is_dst_transition_day
from .plan import Plan, PlanContext, PlanError, build_plan
from .paths import Paths
from .skip import (
    OverrideFile,
    load_overrides,
    mark_used,
    pending_for,
    prune,
)
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


def break_budget_summary(config: RootConfig, catalog: Catalog) -> dict:
    """How much of the day is break, and how much of that is actual commercials.

    The two figures differ and are easy to conflate. ``break`` is arithmetic
    (``slot - episode``, which is what templates divide); ``ad`` is what a real
    plan actually spends on spots, the rest going to bumps. Reporting only the
    first invites the reading "27% of my channel is commercials!" when the spots
    are in fact ~14 min/hour and the remainder is [adult swim]'s own continuity,
    which was a signature rather than overhead.

    The ad figures cost one day's planning, which is fine for a diagnostic and is
    the only way to be exact: pod filling is a subset-sum over real ragged
    durations, not a multiplication.
    """
    from .grid import build_day
    from .rotation import build_rings, select as _select
    from .schedule import assign_day

    day = config.channel.on_air_date or date.today()
    while day.weekday() >= 5:
        day += timedelta(days=1)

    day_config = day_config_for(config, day)
    slots = build_day(day, day_config, config.channel.grid)
    schedule = assign_day(slots, day_config, config, day)

    orders = {show_id: show.order for show_id, show in config.shows.items()}
    rings = build_rings(catalog.episodes(), orders, seed_salt=config.channel.name)
    if not rings:
        return {}

    break_ms = 0
    episode_ms = 0
    for assigned in schedule.slots:
        ring = rings.get(assigned.show_id)
        if ring is None or not ring.episodes:
            continue
        try:
            episode = _select(
                ring,
                day=day,
                anchor=config.channel.grid.anchor,
                slots_per_day=max(1, assigned.of_same_show),
                slot_in_day=assigned.slot_in_day,
            )
        except Exception:  # noqa: BLE001 - a summary must never fail a build
            continue
        episode_ms += episode.duration_ms
        break_ms += max(0, assigned.slot.length_ms - episode.duration_ms)

    day_ms = sum(slot.length_ms for slot in slots)
    if not day_ms:
        return {}
    hours = day_ms / 3_600_000

    summary = {
        "day": day,
        "slots": len(slots),
        "episode_minutes": episode_ms / 60_000,
        "break_minutes": break_ms / 60_000,
        "break_percent": break_ms / day_ms * 100,
        "break_per_hour": break_ms / 60_000 / hours,
    }

    # Ad-only figures need a real plan: pod filling is a subset-sum over ragged
    # durations, so it cannot be derived arithmetically.
    try:
        from .plan import PlanContext, build_plan

        context = PlanContext.load(config, catalog)
        plan = build_plan(day, day_config, context, allow_generated=False)
    except Exception:  # noqa: BLE001 - the arithmetic figures still stand alone
        return summary

    for kind in ("ad", "bump"):
        total = sum(item.duration_ms for item in plan.items if item.kind == kind)
        summary[f"{kind}_minutes"] = total / 60_000
        summary[f"{kind}_per_hour"] = total / 60_000 / hours
    return summary


def _is_video(path: Path) -> bool:
    from .probe import VIDEO_EXTENSIONS

    return path.is_file() and path.suffix.lower() in VIDEO_EXTENSIONS


def _now_ms() -> int:
    """Wall clock in epoch ms, in one place so tests can freeze it."""
    return int(time.time() * 1000)


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
    detect_burned_in: bool | None = None,
    detect_spots: bool = False,
) -> ScanReport:
    """Scan every configured media root into the catalog.

    ``detect_burned_in`` defaults to the config's ``skip_when_burned_in`` but only
    when the watermark is actually in use: examining every episode costs several
    ffmpeg seeks each, and it is pointless when no mark will ever be drawn. An
    explicit value overrides the guess, so ``scan --detect-marks`` works even with
    the watermark currently off.
    """
    if detect_burned_in is None:
        detect_burned_in = (
            config.watermark.enabled and config.watermark.skip_when_burned_in
        )
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
        detect_burned_in=detect_burned_in,
        ffmpeg_path=config.channel.ffmpeg_path,
        detect_spots=detect_spots,
        # The longest pod the channel actually books. A spot longer than this can
        # never play whole, which is the only honest multi-spot signal available.
        longest_pod_ms=max(
            (target.seconds * 1000 for target in config.ads.pod_targets),
            default=90_000,
        ),
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
    use_skips: bool = True,
) -> BuildResult:
    """Build and (optionally) write playout + XMLTV for a run of days.

    ``use_skips`` applies pending "skip this programme" overrides from
    ``data/skips.json``. They are consumed as they are planned, so a skip fires
    once and the schedule returns to normal behind it.
    """
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

    # Read once: every day in the horizon is planned from the same set of pending
    # clicks, and the file is only rewritten after the whole run succeeds.
    overrides = load_overrides(paths.skips) if use_skips else OverrideFile()
    applied: dict = {}

    for offset in range(horizon):
        day = start_day + timedelta(days=offset)
        day_config = day_config_for(config, day)
        pending = pending_for(overrides, day)
        try:
            plan = build_plan(
                day,
                day_config,
                context,
                ignore_cooldowns=ignore_cooldowns,
                allow_generated=allow_generated,
                skips=pending,
            )
        except (PlanError, GridError) as exc:
            raise BuildError(f"{day}: {exc}") from exc
        if pending:
            applied.update(
                {f"{day.isoformat()}#{index}": skip for index, skip in pending.items()}
            )

        result.plans.append(plan)
        result.warnings.extend(plan.warnings)
        result.fallbacks += len(plan.bump_fallbacks)
        result.fallback_reasons.extend(plan.bump_fallback_reasons)

        # Record what has actually aired, which is what the commercials page reads
        # and what seeds tomorrow's cooldowns. Only items at or before "now" go in:
        # a plan covers the whole broadcast day, and a spot scheduled for 41:00 is
        # tomorrow afternoon, not something that has run. The day is always
        # rewritten, so a replan cannot leave yesterday's stale rows behind.
        cutoff = _now_ms()
        catalog.record_day_airings(
            day, [item for item in plan.items if item.start_ms <= cutoff]
        )

        if emit:
            result.playout_files.append(
                write_playout(plan, emit_paths, watermark_layer=watermark_layer)
            )

    if emit:
        # One guide for the whole horizon. Writing it per day overwrote the same
        # file each time, so only the *last* day survived -- the stream served,
        # but the schedule started 48 hours in the future and a player showed an
        # empty guide.
        result.xmltv_files.append(
            write_guide(result.plans, emit_paths, channel_name=config.channel.name)
        )
        # Drop playouts for days outside the horizon. Without this a file written
        # once stays forever: the streamer keeps serving a day whose plan has since
        # been rebuilt with a fix, so the channel plays the old, broken version and
        # nothing on disk says why. Pruning is what makes "rebuild" mean rebuilt.
        prune_playouts(emit_paths, keep_days=[plan.day for plan in result.plans])
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

        # Only now are the clicks consumed. If any day failed to plan, the build
        # raised above and every override is still pending -- which is correct: the
        # channel is still serving the old playout, so the skip has not happened.
        mark_used(paths.skips, {index: skip for index, skip in applied.items()})
        prune(paths.skips, before=start_day)

    return result


def _programming_findings(
    config: RootConfig, catalog: Catalog
) -> list[tuple[str, str, str]]:
    """Report the active programming mode and whether it can actually air.

    In ``lineup``/``shuffle`` the day files are not consulted at all, so saying
    "N day files" and nothing else would describe a schedule that is not being
    used. Worse, a lineup whose shows have no media plans to nothing, and this is
    the one place that can say so before the channel goes black.
    """
    from .programming import ProgrammingError, build_lineup, build_shuffle

    programming = config.programming
    if not programming.active():
        return [("ok", "schedule", "dayparts -- the day files decide what airs")]

    findings: list[tuple[str, str, str]] = []
    episodes = catalog.episodes()
    try:
        if programming.mode == "lineup":
            report = build_lineup(config, episodes, slots_per_day=4)
            shape = (
                f"{len(programming.lineup)} show(s), "
                f"{len(programming.pinned)} pinned episode(s)"
            )
        else:
            report = build_shuffle(config, episodes, slots_per_day=4)
            shape = (
                "the whole roster"
                if not programming.shuffle_shows
                else f"{len(programming.shuffle_shows)} selected show(s)"
            )
    except ProgrammingError as exc:
        return [("error", "schedule", f"{programming.mode}: {exc}")]

    findings.append(
        (
            "ok",
            "schedule",
            f"{programming.mode} -- {shape}; slots are sized from each show's "
            f"'slot' family, not the day files",
        )
    )
    for warning in report.warnings:
        findings.append(("warn", "schedule", warning))
    return findings


def doctor(config: RootConfig, catalog: Catalog, paths: Paths) -> list[tuple[str, str, str]]:
    """Return ``(level, area, message)`` diagnostics."""
    findings: list[tuple[str, str, str]] = []

    findings.append(("ok", "config", f"{len(config.shows)} shows, {len(config.days)} day files"))
    findings.extend(_programming_findings(config, catalog))

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

    summary = break_budget_summary(config, catalog)
    if summary:
        if "ad_per_hour" in summary:
            # Ad load is the figure that matters for authenticity: US cable ran
            # ~14-16 min/hour of spots. Bumps are [adult swim] continuity, not
            # commercials, so lumping them together overstates the ad load by
            # about 1.5 min/hour.
            ad_rate = summary["ad_per_hour"]
            verdict = "in the US 14-16 min/hour band" if 12 <= ad_rate <= 17 else (
                "above the usual 14-16 min/hour" if ad_rate > 17
                else "below the usual 14-16 min/hour"
            )
            findings.append(
                (
                    "ok" if 12 <= ad_rate <= 17 else "warn",
                    "breaks",
                    f"commercials {summary['ad_minutes']:.0f} min/day "
                    f"({ad_rate:.1f} min/hour, {verdict}); "
                    f"bumps a further {summary['bump_minutes']:.0f} min/day",
                )
            )
            findings.append(
                (
                    "ok",
                    "breaks",
                    f"break budget {summary['break_minutes']:.0f} min/day "
                    f"({summary['break_per_hour']:.1f} min/hour, "
                    f"{summary['break_percent']:.0f}% of the day) = slot minus "
                    f"episode, so pod shares only redistribute it",
                )
            )
        else:
            findings.append(
                (
                    "ok",
                    "breaks",
                    f"break budget {summary['break_minutes']:.0f} min/day "
                    f"({summary['break_per_hour']:.1f} min/hour, "
                    f"{summary['break_percent']:.0f}% of the day) = slot minus "
                    f"episode, so pod shares only redistribute it",
                )
            )

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

    # Daypart emptiness only matters when the day files decide the schedule. In a
    # hand-programmed mode these warnings describe a file nothing reads, and they
    # would bury the lineup's own problems.
    if not config.programming.active():
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
