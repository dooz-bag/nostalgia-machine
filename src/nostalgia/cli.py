"""Command line interface.

Design rule: every command is safe to run twice. ``scan`` is idempotent, ``plan``
overwrites its own outputs, and nothing deletes media or tags.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import time
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

#: Container name from ``docker-compose.yml``. Used only to notice that this
#: project is deployed in Docker while the command is running on the host.
SCHEDULER_CONTAINER = "nostalgia-scheduler"


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)

    if not getattr(args, "command", None):
        parser.print_help()
        return 2

    paths = Paths.discover(args.root)
    if args.command == "health":
        return _cmd_health(args, None, paths)
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
        "conform": _cmd_conform,
        "lines": _cmd_lines,
        "music": _cmd_music,
        "shows": _cmd_shows,
        "review": _cmd_review,
        "bumps": _cmd_bumps,
        "autotag": _cmd_autotag,
        "import": _cmd_import,
        "watermark": _cmd_watermark,
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
    scan_cmd.add_argument(
        "--detect-marks",
        action="store_true",
        help=(
            "check each episode's corner for an existing network bug, so the "
            "overlay skips episodes that already have one (slow: several seeks "
            "per episode)"
        ),
    )
    scan_cmd.add_argument(
        "--detect-spots",
        action="store_true",
        help=(
            "flag commercials longer than the longest pod the channel books as "
            "likely multi-spot files. Advisory only: nothing is disabled"
        ),
    )

    plan_cmd = sub.add_parser("plan", help="build a plan (no files written unless --emit)")
    _add_common_plan_args(plan_cmd)
    plan_cmd.add_argument("--emit", action="store_true", help="also write playout + XMLTV")
    plan_cmd.add_argument("--show", action="store_true", help="print the timeline")
    plan_cmd.add_argument("--quiet", action="store_true")

    emit_cmd = sub.add_parser("emit", help="build and write playout JSON + XMLTV")
    _add_common_plan_args(emit_cmd)

    sub.add_parser("doctor", help="check config, media and catalog")
    sub.add_parser("health", help="check whether live playout covers the current time")

    conform_cmd = sub.add_parser(
        "conform",
        help="re-encode break footage to one picture shape and loudness, so cuts flow",
        description=(
            "Conforms commercials and bumps so the channel does not have to "
            "reconcile them live: one frame size (4:3 or 16:9, never seven "
            "widths), one loudness, exact frame counts, and a short dip to black "
            "between spots. Originals are never touched, and the run is resumable."
        ),
    )
    conform_cmd.add_argument(
        "--kind",
        default="ad,bump",
        help="comma list of: ad, bump, ident, extra (default: ad,bump)",
    )
    conform_cmd.add_argument("--jobs", type=int, default=3, help="files encoded at once")
    conform_cmd.add_argument("--limit", type=int, default=None, help="stop after N files")
    conform_cmd.add_argument(
        "--force", action="store_true", help="re-conform files that are already up to date"
    )
    conform_cmd.add_argument(
        "--keep-silent",
        action="store_true",
        help="do not switch off commercials with no usable audio (they are reported either way)",
    )
    conform_cmd.add_argument(
        "--status", action="store_true", help="show how much is conformed, then exit"
    )

    lines_cmd = sub.add_parser(
        "lines",
        help="the sentences generated bump cards say",
        description=(
            "Generated cards say the lines in config/bump-lines.yaml. A local model "
            "(Ollama) can propose more; every proposal is checked against what its "
            "position may truthfully claim, then waits for your approval."
        ),
    )
    lines_sub = lines_cmd.add_subparsers(dest="lines_command", required=True)
    lines_sub.add_parser("list", help="how many lines each position has, and what is pending")
    lines_sub.add_parser("check", help="re-check every line in the bank against its position")
    gen = lines_sub.add_parser("generate", help="ask a local model for new lines")
    gen.add_argument("--position", default="INTO,NEXT,AFTER,OUT,ANY", help="comma list")
    gen.add_argument("--show", default=None, help="write lines for one show id (default: general)")
    gen.add_argument("--count", type=int, default=12, help="lines to ask for per position")
    gen.add_argument("--model", default=None, help="Ollama model (default: config, else the first)")
    appr = lines_sub.add_parser("approve", help="move pending lines into the bank")
    appr.add_argument("--all", action="store_true", help="approve everything pending")
    appr.add_argument("--id", default=None, help="approve one pending line by id")
    lines_sub.add_parser("clear", help="discard every pending line")

    music_cmd = sub.add_parser(
        "music",
        help="music beds laid under generated bump cards",
        description=(
            "Beds come from media/music/ (anything you drop in) and from "
            "`music harvest`, which extracts the audio of text-on-black bumps in your "
            "own archive. Nothing is downloaded and nothing is committed."
        ),
    )
    music_sub = music_cmd.add_subparsers(dest="music_command", required=True)
    music_sub.add_parser("list", help="the beds available")
    harvest_cmd = music_sub.add_parser("harvest", help="take beds from your own real bumps")
    harvest_cmd.add_argument("--limit", type=int, default=None, help="look at N bumps only")

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

    mark_cmd = sub.add_parser(
        "watermark", help="composite the corner bug into the media (burn mode)"
    )
    mark_cmd.add_argument(
        "--apply", action="store_true", help="actually encode (default is a dry run)"
    )
    mark_cmd.add_argument(
        "--force", action="store_true", help="re-encode episodes already composited"
    )
    mark_cmd.add_argument(
        "--no-skip",
        action="store_true",
        help="composite even when a mark is already burned in",
    )
    mark_cmd.add_argument(
        "--only", default=None, help="just this show id"
    )

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
        "--min-mb",
        type=float,
        default=20,
        help=(
            "size floor for a file to count as an episode (default 20). Files "
            "below it are checked by runtime instead, so a low-bitrate 240p "
            "episode is still imported"
        ),
    )
    import_cmd.add_argument(
        "--ffprobe", default=None, help="path to ffprobe (default: search PATH)"
    )
    import_cmd.add_argument(
        "--show-root",
        action="append",
        default=[],
        metavar="FOLDER=SHOW_ID",
        help=(
            "declare that a top-level source folder holds one show, for releases "
            "whose folder or filenames do not name it (repeatable)"
        ),
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
            force=args.force,
            write_sidecars=args.sidecars,
            detect_burned_in=args.detect_marks,
            detect_spots=args.detect_spots,
        )
    print(
        f"scanned {report.total} files ({report.probed} probed, "
        f"{report.skipped_existing} cached, {report.from_sidecar} from sidecars)"
    )
    if args.detect_marks:
        print(f"  {report.marked} episode(s) already carry a corner bug of their own")
    if args.detect_spots:
        print(
            f"  {report.multi_spot} commercial(s) longer than the longest pod "
            f"-- likely a whole recorded break"
        )
        for key in report.multi_spot_keys[:20]:
            print(f"    {key}")
        if len(report.multi_spot_keys) > 20:
            print(f"    ... and {len(report.multi_spot_keys) - 20} more")
        print("  nothing was disabled; review them at /commercials")
    for kind, count in sorted(report.per_kind.items()):
        print(f"  {kind:<9} {count}")
    if report.issues:
        print(f"\n{len(report.issues)} file(s) could not be catalogued:")
        for path, reason in report.issues[:20]:
            print(f"  {path}\n    {reason}")
        if len(report.issues) > 20:
            print(f"  ... and {len(report.issues) - 20} more (see 'nostalgia doctor')")
    return 0


def _cmd_conform(args, config, paths: Paths) -> int:
    from .conform import CONFORM_KINDS
    from .pipeline import conform_library, conform_params

    kinds = tuple(part.strip() for part in args.kind.split(",") if part.strip())
    unknown = [kind for kind in kinds if kind not in CONFORM_KINDS]
    if unknown:
        print(f"error: unknown kind(s) {', '.join(unknown)}; choose from {', '.join(CONFORM_KINDS)}", file=sys.stderr)
        return 2

    signature = conform_params(config).signature()
    with Catalog(paths.catalog) as catalog:
        stats = catalog.conform_stats(signature)
        print("conformed library:")
        for kind in CONFORM_KINDS:
            row = stats.get(kind)
            if row:
                stale = f", {row['stale']} stale" if row["stale"] else ""
                print(f"  {kind:<6} {row['conformed']:>4} of {row['total']:<4}{stale}")
        if args.status:
            merge = "on" if config.ads.merge_breaks else "off"
            print(f"\nmerge_breaks is {merge}; frame {config.encoding.width}x{config.encoding.height}, dip {config.ads.dip_ms} ms")
            return 0

        def progress(result, done, total):
            if done % 25 == 0 or done == total:
                print(f"  {done}/{total}", flush=True)
            if not result.ok and not result.silent:
                print(f"  failed {result.key}: {result.note}", file=sys.stderr)

        print(f"\nconforming {', '.join(kinds)} (frame {config.encoding.width}x{config.encoding.height}, dip {config.ads.dip_ms} ms)")
        report = conform_library(
            config,
            catalog,
            paths,
            kinds=kinds,
            jobs=max(1, args.jobs),
            limit=args.limit,
            force=args.force,
            disable_silent=not args.keep_silent,
            on_result=progress,
        )

    print(f"\nconformed {report.done} file(s)")
    if report.silent:
        verb = "reported" if args.keep_silent else "switched off"
        print(f"{len(report.silent)} commercial(s) with no usable audio {verb}:")
        for key in report.silent[:15]:
            print(f"  {key}")
        if len(report.silent) > 15:
            print(f"  ... and {len(report.silent) - 15} more")
        if not args.keep_silent:
            print("  re-enable any of them from the Commercials page")
    if report.failed:
        print(f"{len(report.failed)} file(s) failed and still air as the original:")
        for key, note in report.failed[:10]:
            print(f"  {key}: {note}")
    if report.done and not config.ads.merge_breaks:
        print(
            "\nnext: set ads.merge_breaks: true (config page or ads.yaml), then emit, "
            "to join each break into one file"
        )
    return 0 if not report.failed else 1


def _lines_paths(paths: Paths):
    from .bumplines import CandidateStore

    return paths.config_dir / "bump-lines.yaml", CandidateStore(paths.data_dir / "bump-line-candidates.json")


def _cmd_lines(args, config, paths: Paths) -> int:
    from . import bumplines as bl

    bank_file, store = _lines_paths(paths)
    bank = bl.load_bank(bank_file)
    titles = [show.title for show in config.shows.values()]

    if args.lines_command == "list":
        print(f"bank: {bank_file}")
        for position in bl.POSITIONS:
            own = len(bank.lines.get(position, ()))
            show_specific = sum(len(t.get(position, ())) for t in bank.per_show.values())
            pending = len(store.pending(position))
            extra = f" + {show_specific} show-specific" if show_specific else ""
            wait = f", {pending} pending approval" if pending else ""
            print(f"  {position:<6} {own:>3}{extra}{wait}")
        return 0

    if args.lines_command == "check":
        bad = 0
        for position, items in bank.lines.items():
            for text in items:
                for problem in bl.check_line(position, text, other_titles=titles):
                    bad += 1
                    print(f"  {position}: {text!r}\n      {problem}")
        for show, table in bank.per_show.items():
            for position, items in table.items():
                for text in items:
                    for problem in bl.check_line(position, text, other_titles=titles):
                        bad += 1
                        print(f"  {show}/{position}: {text!r}\n      {problem}")
        print("every line is acceptable for its position" if not bad else f"\n{bad} problem(s)")
        return 0 if not bad else 1

    if args.lines_command == "clear":
        print(f"discarded {store.clear()} pending line(s)")
        return 0

    if args.lines_command == "approve":
        if not args.all and not args.id:
            print("error: give --all or --id <id>", file=sys.stderr)
            return 2
        approved = 0
        for candidate in store.pending():
            if not args.all and candidate.id != args.id:
                continue
            store.remove(candidate.id)
            if bl.append_line(bank_file, candidate.position, candidate.text, show=candidate.show):
                approved += 1
        print(f"approved {approved} line(s) into {bank_file.name}")
        return 0

    # generate
    generated = config.bumps.generated
    try:
        url, models = bl.find_ollama(generated.ollama_url)
    except bl.ModelError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    model = args.model or generated.ollama_model or (models[0] if models else "")
    if not model:
        print("error: Ollama has no models installed; `ollama pull` one first", file=sys.stderr)
        return 1

    show_title = config.shows[args.show].title if args.show in config.shows else None
    if args.show and show_title is None:
        print(f"error: unknown show {args.show!r}", file=sys.stderr)
        return 2
    positions = [p.strip().upper() for p in args.position.split(",") if p.strip()]
    print(f"asking {model} at {url}")
    queued = 0
    for position in positions:
        if position not in bl.POSITIONS:
            print(f"error: unknown position {position!r}", file=sys.stderr)
            return 2
        try:
            result = bl.propose(
                position, bank=bank, url=url, model=model, count=args.count,
                show=args.show, show_title=show_title, other_titles=titles,
                existing=[c.text for c in store.pending(position)],
            )
        except bl.ModelError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        for text in result.accepted:
            if store.add(position, text, show=args.show):
                queued += 1
        print(f"  {position:<6} {len(result.accepted):>2} accepted, {len(result.rejected):>2} rejected by the truth guard")
        for text, problems in result.rejected[:2]:
            print(f"           x {text!r}: {problems[0]}")
    print(f"\n{queued} line(s) waiting for your approval: the Lines page, or `nostalgia lines approve --all`")
    return 0


def _cmd_music(args, config, paths: Paths) -> int:
    from .conform import find_tools
    from .music import harvest, load_library, music_roots

    media_root = paths.media()
    roots = music_roots(media_root)
    harvest_dir = media_root / "derived" / "music" / "harvested"
    ffmpeg, ffprobe = find_tools(config.channel.ffmpeg_path)

    if args.music_command == "list":
        library = load_library(roots, ffprobe=ffprobe)
        if not library:
            print(f"no beds in {roots[0]}\n  drop audio files there, or run `nostalgia music harvest`")
            return 0
        print(f"{len(library.beds)} bed(s)")
        for bed in library.beds:
            era = f" [{bed.era}]" if bed.era else ""
            where = next((r for r in roots if r in bed.path.parents), bed.path.parent)
            print(f"  {bed.duration_s:5.1f}s{era:<8} {bed.path.relative_to(where).as_posix()}")
        return 0

    if not ffmpeg:
        print("error: harvest needs ffmpeg", file=sys.stderr)
        return 1
    with Catalog(paths.catalog) as catalog:
        bumps = catalog.assets(("bump",))
    print(f"looking at {len(bumps)} bump(s) for text cards with a music bed ...")

    def progress(done, total):
        if done % 50 == 0 or done == total:
            print(f"  {done}/{total}", flush=True)

    report = harvest(
        bumps, harvest_dir, ffmpeg=ffmpeg, ffprobe=ffprobe,
        limit=args.limit, on_progress=progress,
    )
    print(f"\ntook {len(report.taken)} bed(s) from {report.scanned} bump(s)")
    print(f"  {report.skipped} left alone (show footage, silent, or already taken)")
    for key, note in report.failed[:5]:
        print(f"  failed {key}: {note}", file=sys.stderr)
    print(f"\nbeds are in {harvest_dir}; delete any you do not like")
    return 0


def _cmd_plan(args, config, paths: Paths) -> int:
    return _run_build(args, config, paths, emit=args.emit, show=args.show, quiet=args.quiet)


def _cmd_emit(args, config, paths: Paths) -> int:
    # Emitting on the host of a Docker deployment silently breaks the streamer:
    # the mount layout does not mirror the repo, so `lineup.json` and
    # `channel.json` are rewritten with host paths that do not exist inside the
    # container, and the channel then serves black rather than failing loudly.
    # Refusing here is better than a working command with a broken result.
    if _must_emit_in_container():
        print(
            "refusing to emit on the host: this deployment streams from Docker.\n"
            "Run it in the container instead, which sees the paths it needs:\n\n"
            "  docker compose exec scheduler python -m nostalgia.cli --root /app emit\n\n"
            "or set NOSTALGIA_CONTAINER_ROOT=/app to write container paths from here.",
            file=sys.stderr,
        )
        return 1
    return _run_build(args, config, paths, emit=True, show=False, quiet=False)


def _cmd_health(args, config, paths: Paths) -> int:
    from .deploy import Roots, live_day_for

    current = live_day_for(
        Roots.of(paths.playout_dir, paths.xmltv_dir), int(time.time() * 1000)
    )
    if current is None:
        print("no readable live playout covers the current time", file=sys.stderr)
        return 1
    print(f"live playout is available: {current.name}")
    return 0


def _must_emit_in_container() -> bool:
    """Whether emitting here would write paths only this machine can read.

    True only for a host run of a Docker deployment. Emitting inside the container
    is the normal path, a native install has no container to defer to, and setting
    ``NOSTALGIA_CONTAINER_ROOT`` means the caller has taken responsibility for the
    remap -- all three are allowed.
    """
    import os
    import shutil

    if os.environ.get("NOSTALGIA_CONTAINER_ROOT"):
        return False
    if _running_in_docker():
        return False
    if shutil.which("docker") is None:
        return False
    try:
        result = subprocess.run(
            ["docker", "ps", "--filter", "name=nostalgia-scheduler", "--format", "{{.Names}}"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return SCHEDULER_CONTAINER in result.stdout


def _running_in_docker() -> bool:
    import os

    return os.path.exists("/.dockerenv")


def _streamer_port() -> int:
    """The port the streamer is published on, honouring ``ETV_PORT``."""
    import os

    try:
        return int(os.environ.get("ETV_PORT") or 8409)
    except ValueError:
        return 8409


def _run_build(args, config, paths: Paths, *, emit: bool, show: bool, quiet: bool) -> int:
    start = date.fromisoformat(args.date) if args.date else None
    with Catalog(paths.catalog) as catalog:
        status = catalog.get_json_state("scheduler_emit") or {}
        if emit:
            status.update(
                last_attempt_ms=int(time.time() * 1000),
                last_success_ms=status.get("last_success_ms"),
                last_error="emit did not complete",
            )
            catalog.set_json_state("scheduler_emit", status)
        try:
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
        except Exception as exc:
            if emit:
                status["last_error"] = f"{type(exc).__name__}: {exc}"[:1000]
                catalog.set_json_state("scheduler_emit", status)
            raise
        if emit:
            status.update(last_success_ms=int(time.time() * 1000), last_error=None)
            catalog.set_json_state("scheduler_emit", status)
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
        if result.merged_runs:
            print(
                f"\nmerged {result.merged_items} break item(s) into {result.merged_runs} "
                f"file(s): {result.merged_items - result.merged_runs} fewer playlist "
                f"boundaries"
            )
        print(f"\nwrote {len(result.playout_files)} playout file(s) to {paths.playout_dir}")
        print(f"wrote {len(result.xmltv_files)} guide file(s) to {paths.xmltv_dir}")
        if result.lineup:
            print(f"wrote ErsatzTV lineup: {result.lineup}")
        urls = lan_urls()
        if urls:
            print("\nnext: docker compose up -d, then point a player at " + urls[0])
            for extra in urls[1:]:
                print(f"      (also reachable at {extra})")
        else:
            # Emitting inside the container: the host's LAN address is not
            # visible from here, and inventing one would be worse than saying so.
            print(
                "\nnext: docker compose up -d, then point a player at "
                f"http://<this-machine's-LAN-IP>:{_streamer_port()}/channels.m3u"
            )
        print(
            "      Use a LAN address, not localhost: the streamer builds the\n"
            "      playlist from the address you requested it by, so a TV given\n"
            "      'localhost' would be told to fetch streams from itself."
        )
    return 0


def lan_urls(port: int | None = None) -> list[str]:
    """Addresses a *player on the network* can use to reach the streamer.

    ErsatzTV builds its playlist from the address it was requested by, so the URL
    you hand a TV becomes the URL the TV then fetches streams from. Telling someone
    to use ``localhost`` therefore works on the machine running the channel and
    fails on every TV in the house.

    The primary result is the address the OS would actually route the internet
    through, which is the one on the real LAN rather than a WSL or Hyper-V virtual
    adapter. Those virtual adapters are the reason a hostname is not enough: this
    machine's name resolves to three addresses, two of which a TV cannot reach.
    """
    port = _streamer_port() if port is None else port
    primary = _default_route_address()
    candidates = [primary] if primary else []
    candidates += [
        address
        for address in _local_ipv4_addresses()
        if address != primary and _looks_like_lan(address)
    ]
    return [f"http://{address}:{port}/channels.m3u" for address in candidates]


def _default_route_address() -> str | None:
    """The local address the OS would use to reach the outside world.

    Uses a UDP socket, which performs no traffic: ``connect`` on UDP only resolves
    the route, so this costs nothing and never leaves the machine.
    """
    import socket

    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect(("203.0.113.1", 9))  # TEST-NET-3, never routed
        address = probe.getsockname()[0]
        return address if _looks_like_lan(address) else None
    except OSError:
        return None
    finally:
        probe.close()


def _local_ipv4_addresses() -> list[str]:
    import socket

    try:
        infos = socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET)
    except OSError:
        return []
    seen: list[str] = []
    for info in infos:
        address = info[4][0]
        if address not in seen:
            seen.append(address)
    return seen


def _looks_like_lan(address: str) -> bool:
    """True for an address a phone or TV could plausibly reach.

    Deliberately excludes loopback and the ``172.16/12`` range WSL and Hyper-V
    use for their host-only switches, which are reachable only from this machine.
    """
    octets = address.split(".")
    if len(octets) != 4 or not all(part.isdigit() for part in octets):
        return False
    first, second = int(octets[0]), int(octets[1])
    if first == 127:
        return False
    if first == 172 and 16 <= second <= 31:
        return False
    if first == 169 and second == 254:
        return False
    return True


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


def _cmd_watermark(args, config, paths: Paths) -> int:
    from .watermark import (
        WatermarkError,
        composite_library,
        look_from_config,
        render_text_card,
        rule_from_config,
    )

    if config.watermark.mode != "burn":
        print(
            f"watermark mode is {config.watermark.mode!r}; nothing to composite.\n"
            "  set `mode: burn` in config/watermark.yaml to bake the mark in,\n"
            "  or `mode: overlay` to have the streamer add it (no re-encode)."
        )
        return 0

    look = look_from_config(config.watermark)
    rule = rule_from_config(config.watermark)
    if args.no_skip:
        rule = type(rule)(
            enabled=rule.enabled,
            shows=rule.shows,
            era_from=rule.era_from,
            era_to=rule.era_to,
            skip_when_burned_in=False,
        )

    with Catalog(paths.catalog) as catalog:
        episodes = catalog.episodes(args.only)
        if not episodes:
            print("no episodes catalogued; run 'nostalgia scan' first", file=sys.stderr)
            return 1
        items = [(episode.key, Path(episode.path)) for episode in episodes]
        out_dir = paths.media("derived", "watermark")
        report = composite_library(
            items,
            look=look,
            out_dir=out_dir,
            rule=rule,
            era_year=config.era_year,
            ffmpeg=config.channel.ffmpeg_path,
            force=args.force,
            dry_run=not args.apply,
        )

    if not args.apply:
        print(f"{len(items)} episode(s) checked; {report.made} would be composited")
        print("This is a dry run. Re-run with --apply to encode.")
        return 0

    print(report.line())
    for error in report.errors[:8]:
        print(f"  error: {error}", file=sys.stderr)
    if report.made or report.reused:
        print(
            f"\ncomposited copies are in {out_dir}"
            "\nnext: nostalgia scan   # the catalog picks the marked copies up"
        )
    return 1 if report.failed else 0


def _cmd_import(args, config, paths: Paths) -> int:
    from . import mediaimport

    source = Path(args.source)
    if not source.is_dir():
        print(f"error: not a folder: {source}", file=sys.stderr)
        return 1

    try:
        show_roots: dict[str, str] = {}
        for declaration in args.show_root:
            folder, _, show_id = declaration.partition("=")
            if not (folder and show_id):
                print(
                    f"error: --show-root needs FOLDER=SHOW_ID, got {declaration!r}",
                    file=sys.stderr,
                )
                return 1
            show_roots[folder] = show_id

        candidates = mediaimport.plan_import(
            source,
            config.shows,
            paths.media_root / "shows",
            min_bytes=int(max(0.0, args.min_mb) * 1024 * 1024),
            ffprobe=args.ffprobe or config.channel.ffmpeg_path,
            default_show=args.show,
            show_roots=show_roots,
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
