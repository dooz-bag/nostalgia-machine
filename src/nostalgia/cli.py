"""Command line interface.

Design rule: every command is safe to run twice. ``scan`` is idempotent, ``plan``
overwrites its own outputs, and nothing deletes media or tags.
"""

from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

from .catalog import Catalog, summarise_by_show
from .config import ConfigError, load_config
from .durations import format_duration
from .grid import format_clock
from .paths import Paths
from .pipeline import BuildError, build, day_config_for, doctor, scan
from .plan import PlanContext, describe_plan
from .probe import ProbeError


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)

    if not getattr(args, "command", None):
        parser.print_help()
        return 2

    paths = Paths.discover(args.root)
    try:
        config = load_config(paths.config_dir)
    except ConfigError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    handler = {
        "scan": _cmd_scan,
        "plan": _cmd_plan,
        "emit": _cmd_emit,
        "doctor": _cmd_doctor,
        "shows": _cmd_shows,
        "review": _cmd_review,
        "bumps": _cmd_bumps,
        "autotag": _cmd_autotag,
        "import": _cmd_import,
        "tag": _cmd_tag,
        "serve": _cmd_serve,
    }[args.command]

    try:
        return handler(args, config, paths)
    except (BuildError, ConfigError, ProbeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 130


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="nostalgia",
        description="Deterministic 24/7 [adult swim] channel: scheduler and traffic engine.",
    )
    parser.add_argument("--root", default=None, help="project root (default: auto-detect)")
    sub = parser.add_subparsers(dest="command")

    scan_cmd = sub.add_parser("scan", help="scan media folders into the catalog")
    scan_cmd.add_argument("--force", action="store_true", help="re-probe every file")
    scan_cmd.add_argument(
        "--sidecars", action="store_true", help="write <file>.json duration sidecars"
    )
    scan_cmd.add_argument("--ffprobe", default=None, help="path to ffprobe")

    plan_cmd = sub.add_parser("plan", help="build a plan (no files written unless --emit)")
    _add_common_plan_args(plan_cmd)
    plan_cmd.add_argument("--emit", action="store_true", help="also write playout + XMLTV")
    plan_cmd.add_argument("--show", action="store_true", help="print the timeline")
    plan_cmd.add_argument("--quiet", action="store_true")

    emit_cmd = sub.add_parser("emit", help="build and write playout JSON + XMLTV")
    _add_common_plan_args(emit_cmd)

    sub.add_parser("doctor", help="check config, media and catalog")

    shows_cmd = sub.add_parser("shows", help="list shows and episode counts")
    shows_cmd.add_argument("--verbose", action="store_true")

    sub.add_parser("review", help="list bumps awaiting subject confirmation")

    bumps_cmd = sub.add_parser(
        "bumps", help="render generated bump cards (Phase 2)"
    )
    bumps_cmd.add_argument(
        "--style", default=None, help="force an era style pack key, e.g. 2003"
    )
    bumps_cmd.add_argument("--force", action="store_true", help="re-render cached cards")
    bumps_cmd.add_argument(
        "--out", default=None, help="output folder (default: media/bumps/generated)"
    )
    bumps_cmd.add_argument(
        "--dry-run", action="store_true", help="show what would be rendered"
    )

    autotag_cmd = sub.add_parser(
        "autotag", help="propose bump subjects with OCR (advisory)"
    )
    autotag_cmd.add_argument(
        "--write", action="store_true", help="write the proposals as unconfirmed sidecars"
    )
    autotag_cmd.add_argument(
        "--min-confidence", type=float, default=0.3, help="threshold for --write"
    )
    autotag_cmd.add_argument("--limit", type=int, default=None, help="examine at most N bumps")

    import_cmd = sub.add_parser(
        "import", help="file a downloaded show into media/shows/<show-id>/Season NN/"
    )
    import_cmd.add_argument("source", help="folder containing the download")
    import_cmd.add_argument(
        "--show", default=None, help="force one show id instead of matching folder names"
    )
    import_cmd.add_argument(
        "--move", action="store_true", help="move instead of copy"
    )
    import_cmd.add_argument(
        "--apply", action="store_true", help="actually write files (default is a dry run)"
    )
    import_cmd.add_argument(
        "--overwrite", action="store_true", help="replace a target that already exists"
    )
    import_cmd.add_argument(
        "--min-mb", type=int, default=20, help="ignore videos smaller than this (default 20)"
    )
    import_cmd.add_argument(
        "--recursive", action="store_true", default=True, help=argparse.SUPPRESS
    )

    tag_cmd = sub.add_parser("tag", help="set a bump's subject and position")
    tag_cmd.add_argument("key")
    tag_cmd.add_argument("--subject", required=True)
    tag_cmd.add_argument("--position", default=None)
    tag_cmd.add_argument("--era", type=int, default=None)
    tag_cmd.add_argument("--family", default=None)

    serve_cmd = sub.add_parser("serve", help="run the web UI (now playing + bump review)")
    serve_cmd.add_argument("--host", default="127.0.0.1")
    serve_cmd.add_argument("--port", type=int, default=8099)
    serve_cmd.add_argument("--reload", action="store_true")

    return parser


def _add_common_plan_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--date", default=None, help="first day, YYYY-MM-DD (default: today)")
    parser.add_argument("--days", type=int, default=None, help="how many days to build")
    parser.add_argument(
        "--no-cooldowns",
        action="store_true",
        help="ignore cooldowns -- useful when previewing a fresh pool",
    )
    parser.add_argument(
        "--no-generated",
        action="store_true",
        help="never use generated bumps, even to fill gaps",
    )


# -- commands --------------------------------------------------------------


def _cmd_scan(args, config, paths: Paths) -> int:
    paths.ensure_runtime_dirs()
    with Catalog(paths.catalog) as catalog:
        report = scan(
            config,
            catalog,
            paths,
            ffprobe_path=args.ffprobe,
            write_sidecars=args.sidecars,
        )
    print(f"scanned {report.total} files ({report.probed} probed, {report.from_sidecar} from sidecars)")
    for kind, count in sorted(report.per_kind.items()):
        print(f"  {kind:<9} {count}")
    if report.issues:
        print(f"\n{len(report.issues)} file(s) could not be catalogued:")
        for path, reason in report.issues[:20]:
            print(f"  {path}\n    {reason}")
        if len(report.issues) > 20:
            print(f"  ... and {len(report.issues) - 20} more (see 'nostalgia doctor')")
    return 0


def _cmd_plan(args, config, paths: Paths) -> int:
    return _run_build(args, config, paths, emit=args.emit, show=args.show, quiet=args.quiet)


def _cmd_emit(args, config, paths: Paths) -> int:
    return _run_build(args, config, paths, emit=True, show=False, quiet=False)


def _run_build(args, config, paths: Paths, *, emit: bool, show: bool, quiet: bool) -> int:
    start = date.fromisoformat(args.date) if args.date else None
    with Catalog(paths.catalog) as catalog:
        result = build(
            config,
            catalog,
            paths,
            start=start,
            days=args.days,
            emit=emit,
            ignore_cooldowns=args.no_cooldowns,
            allow_generated=False if args.no_generated else None,
        )
        context = (
            PlanContext.load(
                config, catalog, config_dir=paths.config_dir, media_root=paths.media_root
            )
            if show
            else None
        )

        for plan in result.plans:
            zone = config.channel.grid
            first = format_clock(plan.day_start_ms, zone)
            last = format_clock(plan.day_end_ms, zone)
            episodes = len(plan.episode_items())
            print(
                f"{plan.day}  {first}->{last}  {len(plan.items)} items  "
                f"{episodes} episodes  {format_duration(plan.duration_ms())}"
            )
            if show and context:
                for line in describe_plan(plan, context):
                    print(f"  {line}")

    if result.warnings and not quiet:
        print(f"\n{len(result.warnings)} warning(s):")
        for warning in result.warnings[:25]:
            print(f"  {warning}")
        if len(result.warnings) > 25:
            print(f"  ... and {len(result.warnings) - 25} more")

    if result.fallbacks:
        # Only point at the review queue when there is something in it. A
        # fallback on a fully-tagged library means the archive has no card for
        # that show at that moment, which is ordinary operation.
        print(f"\n{result.fallbacks} bump slot(s) fell back to a neutral bump")
        for reason in result.fallback_reasons[:5]:
            print(f"  {reason}")
        pending = _pending_tag_count(paths)
        if pending:
            print(f"  {pending} card(s) are awaiting a subject: 'nostalgia review'")
        else:
            print(
                "  every bump is tagged; add more cards under media/bumps/<show-id>/ "
                "for more variety"
            )

    if emit:
        print(f"\nwrote {len(result.playout_files)} playout file(s) to {paths.playout_dir}")
        print(f"wrote {len(result.xmltv_files)} guide file(s) to {paths.xmltv_dir}")
        if result.lineup:
            print(f"wrote ErsatzTV lineup: {result.lineup}")
        print(
            "\nnext: docker compose up -d, then point a player at "
            "http://localhost:8409/channels.m3u"
        )
    return 0


def _pending_tag_count(paths: Paths) -> int:
    """How many bumps are still waiting for a human, or 0 if it cannot be read."""
    try:
        with Catalog(paths.catalog) as catalog:
            return len(catalog.tags_pending_review())
    except Exception:  # noqa: BLE001 - a hint must never fail the build
        return 0


def _cmd_doctor(args, config, paths: Paths) -> int:
    with Catalog(paths.catalog) as catalog:
        findings = doctor(config, catalog, paths)
    errors = 0
    for level, area, message in findings:
        marker = {"ok": "  ok", "warn": "warn", "error": " ERR"}[level]
        print(f"[{marker}] {area:<8} {message}")
        if level == "error":
            errors += 1
    print()
    if errors:
        print(f"{errors} blocking problem(s)")
        return 1
    print("no blocking problems")
    return 0


def _cmd_shows(args, config, paths: Paths) -> int:
    with Catalog(paths.catalog) as catalog:
        counts = summarise_by_show(catalog)
        for show_id, show in config.shows.items():
            found = counts.get(show_id, 0)
            window = ""
            if show.airing_from or show.airing_to:
                window = f"  [{show.airing_from or '...'} .. {show.airing_to or '...'}]"
            flag = "" if show.enable else "  (disabled)"
            print(
                f"{show_id:<26} {show.slot:<5} {found:>4} eps  "
                f"eligibility={show.eligibility}{window}{flag}"
            )
            if args.verbose:
                for episode in catalog.episodes(show_id)[:10]:
                    print(f"    {episode.label:<34} {format_duration(episode.duration_ms)}")
                if found > 10:
                    print(f"    ... and {found - 10} more")
    return 0


def _cmd_review(args, config, paths: Paths) -> int:
    with Catalog(paths.catalog) as catalog:
        pending = catalog.tags_pending_review()
    if not pending:
        print("every bump has a confirmed subject")
        return 0
    print(f"{len(pending)} bump(s) awaiting confirmation:\n")
    for row in pending:
        subject = row["subject"] or "-"
        position = row["position"] or "-"
        source = row["tag_source"] or "-"
        print(f"  {row['key']}")
        print(f"    guess: subject={subject} position={position} source={source}")
        print(f"    confirm: nostalgia tag \"{row['key']}\" --subject <show-id>")
    return 0


def _cmd_bumps(args, config, paths: Paths) -> int:
    from .plan import PlanContext, PlanError, render_generated_bumps

    if args.dry_run:
        from .bumpgen import load_era_packs, specs_for_schedule

        era_file = paths.config_dir / "bumps-era.yaml"
        pack = load_era_packs(era_file)
        year = config.era_year
        style = pack.style_for(year) if not args.style else pack.styles.get(args.style)
        if style is None:
            print(f"error: unknown style pack {args.style!r}", file=sys.stderr)
            return 1
        specs = specs_for_schedule(
            shows=[show_id for show_id, show in config.shows.items() if show.enable],
            titles={show_id: show.title for show_id, show in config.shows.items()},
            year=year,
        )
        print(f"style {style.key} ({style.name}), {len(specs)} card(s):")
        for spec in specs:
            print(f"  {spec.position:<6} {spec.show_title or spec.subject}")
        return 0

    with Catalog(paths.catalog) as catalog:
        context = PlanContext.load(
            config, catalog, config_dir=paths.config_dir, media_root=paths.media_root
        )
        try:
            assets = render_generated_bumps(
                context,
                out_dir=Path(args.out) if args.out else None,
                style_pack=args.style,
                force=args.force,
            )
        except PlanError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1

    total_kb = sum(Path(asset.path).stat().st_size for asset in assets if Path(asset.path).is_file()) // 1024
    print(f"rendered {len(assets)} bump card(s), {total_kb} KB total")
    print("next: nostalgia scan   # pick the new cards up, then nostalgia emit")
    return 0


def _cmd_autotag(args, config, paths: Paths) -> int:
    from . import autotag

    if not autotag.available():
        print(
            "OCR is not available: needs ffmpeg, pytesseract, Pillow and a Tesseract binary.\n"
            "  pip install 'nostalgia-machine[tag]'   (plus Tesseract itself)",
            file=sys.stderr,
        )
        return 1

    titles = {show_id: show.title for show_id, show in config.shows.items()}
    with Catalog(paths.catalog) as catalog:
        result = autotag.run(
            catalog,
            show_titles=titles,
            min_confidence=args.min_confidence,
            limit=args.limit,
            write=args.write,
            ffmpeg=config.channel.ffmpeg_path,
        )

    if result.skipped_no_tools:
        print("OCR is not available (missing ffmpeg or Tesseract)", file=sys.stderr)
        return 1

    print(f"examined {result.examined} bump(s), {result.proposed} produced text")
    for suggestion in result.suggestions:
        marker = "?" if suggestion.subject is None else suggestion.subject
        detail = ", ".join(suggestion.reasons[:3]) or "no cues"
        print(f"  {marker:<26} {suggestion.confidence:>4.0%}  {suggestion.key}")
        print(f"    {detail}")
        if suggestion.text:
            print(f"    text: {suggestion.text[:90]!r}")
    if not args.write:
        print("\nThis was a report. Re-run with --write to save proposals (unconfirmed).")
    else:
        print("\nProposals saved as unconfirmed sidecars; confirm them with 'nostalgia review'.")
    return 0


def _cmd_import(args, config, paths: Paths) -> int:
    from . import mediaimport

    source = Path(args.source)
    if not source.is_dir():
        print(f"error: not a folder: {source}", file=sys.stderr)
        return 1

    try:
        candidates = mediaimport.plan_import(
            source,
            config.shows,
            paths.media_root / "shows",
            min_bytes=max(0, args.min_mb) * 1024 * 1024,
            default_show=args.show,
        )
    except (NotADirectoryError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    counts = {}
    for item in candidates:
        counts[item.status] = counts.get(item.status, 0) + 1

    ready = [item for item in candidates if item.actionable]
    print(f"{len(candidates)} file(s) under {source}")
    for status in sorted(counts):
        print(f"  {status:<12} {counts[status]}")

    for status in (
        mediaimport.UNKNOWN_SHOW,
        mediaimport.UNPARSED,
        mediaimport.COLLISION,
        mediaimport.DUPLICATE,
    ):
        problems = [item for item in candidates if item.status == status]
        if not problems:
            continue
        print(f"\n{status} ({len(problems)}):")
        for item in problems[:15]:
            print(f"  {item.source.relative_to(source)}")
            print(f"    {item.detail}")
        if len(problems) > 15:
            print(f"  ... and {len(problems) - 15} more")

    if not ready:
        print("\nNothing to import.")
        return 0

    by_show: dict[str, list] = {}
    for item in ready:
        by_show.setdefault(item.show_id or "?", []).append(item)
    print(f"\nready ({len(ready)}):")
    for show_id in sorted(by_show):
        items = by_show[show_id]
        episodes = sorted({(item.season, item.episode) for item in items})
        print(f"  {show_id:<26} {len(items):>3} file(s), {len(episodes)} unique episode(s)")
        for season, episode in episodes[:4]:
            print(f"      S{season:02d}E{episode:02d}")

    if not args.apply:
        print(
            "\nThis is a dry run. Re-run with --apply to "
            f"{'move' if args.move else 'copy'} them."
        )
        return 0

    report = mediaimport.apply_import(
        candidates, move=args.move, overwrite=args.overwrite
    )
    verb = "moved" if report.moved else "copied"
    print(
        f"\n{verb} {report.moved + report.copied} file(s), "
        f"{report.bytes_done / (1024 ** 3):.2f} GB; skipped {report.skipped}"
    )
    if report.created_dirs:
        print(f"created {len(report.created_dirs)} folder(s)")
    for error in report.errors[:10]:
        print(f"  error: {error}", file=sys.stderr)
    print("\nnext: nostalgia scan   # then nostalgia plan --date <day> --show")
    return 1 if report.errors else 0


def _cmd_tag(args, config, paths: Paths) -> int:
    with Catalog(paths.catalog) as catalog:
        exists = catalog.media_info(args.key)
        if exists is None:
            print(f"error: no catalogued asset with key {args.key!r}", file=sys.stderr)
            return 1
        catalog.set_tag(
            args.key,
            subject=None if args.subject in ("neutral", "-", "") else args.subject,
            position=args.position,
            era_from=args.era,
            era_to=args.era,
            family=args.family,
            tag_source="manual",
            confidence=1.0,
            confirmed=True,
        )
    print(f"tagged {args.key} -> subject={args.subject}")
    return 0


def _cmd_serve(args, config, paths: Paths) -> int:
    try:
        import uvicorn
    except ImportError:
        print(
            "error: the web UI needs extra packages.\n"
            "  pip install 'nostalgia-machine[web]'",
            file=sys.stderr,
        )
        return 1
    from .web.app import create_app

    app = create_app(paths, config)
    uvicorn.run(app, host=args.host, port=args.port, reload=args.reload)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
