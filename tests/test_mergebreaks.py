"""Merging a break into one file.

What matters here is what must *not* be merged -- a programme, a trimmed spot, a
black card, anything not conformed -- and that a failed merge degrades to the old
item-by-item playout instead of leaving a hole. The join itself is a stream copy
verified against real footage; these tests pin the decisions around it.
"""

from __future__ import annotations

import os
import subprocess
import time
from datetime import date
from pathlib import Path

import pytest

from nostalgia import mergebreaks as M
from nostalgia.catalog import Catalog
from nostalgia.config import load_config
from nostalgia.emit import set_channel_timezone, set_path_mapping, to_playout_json
from nostalgia.pipeline import day_config_for
from nostalgia.plan import Plan, PlanContext, PlannedItem, build_plan


def _item(index, *, kind="ad", conformed=True, start=None, length=10_000, **extra) -> PlannedItem:
    start = index * length if start is None else start
    return PlannedItem(
        item_id=f"20260101-0001-a{index:07d}",
        kind=kind,
        start_ms=start,
        end_ms=start + length,
        asset_key=f"ad:{index}",
        path=f"/derived/{index}.mkv",
        conformed=conformed,
        **extra,
    )


def _plan(items) -> Plan:
    plan = Plan(day=date(2026, 1, 1), channel_name="t", channel_number="1", tvg_id="t.1")
    plan.items = list(items)
    plan.day_start_ms = items[0].start_ms
    plan.day_end_ms = items[-1].end_ms
    return plan


# -- what may be merged ------------------------------------------------------


def test_a_conformed_spot_is_mergeable():
    assert _item(0).mergeable


@pytest.mark.parametrize(
    "overrides, why",
    [
        ({"kind": "episode"}, "a programme is the one thing kept separate"),
        ({"conformed": False}, "an unconformed file has different stream parameters"),
        ({"lavfi_params": "color=c=black:s=640x480"}, "a black card has no file to join"),
        ({"out_point_ms": 5_000}, "a trimmed spot would lose its dip and cannot be copied"),
        ({"in_point_ms": 1_000}, "a cut start cannot be stream-copied cleanly"),
        ({"stretched": True}, "its file is shorter than its entry, so it must end where its file ends"),
    ],
)
def test_what_must_never_be_merged(overrides, why):
    assert not _item(0, **overrides).mergeable, why


def test_an_item_stretched_to_land_a_slot_is_marked_and_splits_the_run():
    """The slot-end remainder is absorbed into the last item; its file stays as long as it was.

    Merging it would move that shortfall from the programme boundary to the end of
    the whole run, which can be a different programme.
    """
    from nostalgia.plan import Plan, _absorb_remainder

    plan = Plan.__new__(Plan)
    plan.items = [_item(0), _item(1, start=10_000)]
    assert _absorb_remainder(plan, 951)
    assert plan.items[-1].stretched and plan.items[-1].end_ms == 20_951
    assert not plan.items[0].stretched

    rounding = Plan.__new__(Plan)
    rounding.items = [_item(0)]
    assert _absorb_remainder(rounding, 12)
    assert not rounding.items[-1].stretched, "a few ms is rounding; it must not cost a boundary"

    items = plan.items + [_item(2, start=20_951), _item(3, start=30_951)]
    assert M.find_runs(items, max_ms=600_000) == [(2, 3)], (
        "item 0 is alone, item 1 is stretched and left out, items 2 and 3 join"
    )


# -- finding runs ------------------------------------------------------------


def test_consecutive_mergeable_items_form_one_run():
    items = [_item(i) for i in range(5)]
    assert M.find_runs(items, max_ms=10**9) == [(0, 4)]


def test_a_programme_splits_a_break_into_two_runs():
    items = [_item(0), _item(1), _item(2, kind="episode"), _item(3), _item(4)]
    assert M.find_runs(items, max_ms=10**9) == [(0, 1), (3, 4)]


def test_a_lone_item_is_not_a_run():
    """Joining one file to nothing saves no boundary."""
    items = [_item(0), _item(1, kind="episode"), _item(2), _item(3, kind="episode"), _item(4)]
    assert M.find_runs(items, max_ms=10**9) == []


def test_an_unconformed_item_splits_the_run_and_the_rest_still_merge():
    items = [_item(0), _item(1), _item(2, conformed=False), _item(3), _item(4)]
    assert M.find_runs(items, max_ms=10**9) == [(0, 1), (3, 4)]


def test_a_trimmed_spot_or_black_card_does_not_stop_the_neighbours_merging():
    items = [_item(0), _item(1), _item(2, out_point_ms=3), _item(3), _item(4)]
    assert M.find_runs(items, max_ms=10**9) == [(0, 1), (3, 4)]


def test_a_long_run_is_split_at_the_cap():
    items = [_item(i, length=60_000) for i in range(10)]
    runs = M.find_runs(items, max_ms=3 * 60_000)  # three minutes
    assert runs == [(0, 2), (3, 5), (6, 8)]  # the 10th is left alone, not merged to nothing
    for first, last in runs:
        assert sum(i.duration_ms for i in items[first : last + 1]) <= 3 * 60_000


def test_a_gap_in_time_is_never_merged_across():
    """A merged file plays back to back, so a hole inside it would shift every item
    after it. A valid plan has none; this is the cheap guard that notices if not."""
    # items 0-1 end at 20_000; item 2 starts at 50_000 (a 30 s hole); 3 follows 2.
    items = [_item(0), _item(1), _item(2, start=50_000), _item(3, start=60_000)]
    assert M.find_runs(items, max_ms=10**9) == [(0, 1), (2, 3)]


def test_every_item_lands_in_at_most_one_run():
    items = [_item(i, kind="episode" if i % 7 == 0 else "ad") for i in range(60)]
    seen = []
    for first, last in M.find_runs(items, max_ms=45_000):
        seen.extend(range(first, last + 1))
    assert len(seen) == len(set(seen))
    assert all(items[i].mergeable for i in seen)


# -- the playout document ---------------------------------------------------


@pytest.fixture()
def tz_set():
    set_channel_timezone("America/New_York")
    set_path_mapping(None, None)


def test_a_merged_run_is_one_entry_that_spans_exactly_its_items(tz_set):
    items = [_item(0), _item(1), _item(2), _item(3, kind="episode"), _item(4)]
    run = M.MergedRun(0, 2, Path("/derived/breaks/abc.mkv"))
    entries = to_playout_json(_plan(items), merged=[run])["items"]

    assert len(entries) == 3  # one merged entry, the episode, the trailing spot
    merged = entries[0]
    assert merged["source"]["path"] == "/derived/breaks/abc.mkv"
    assert merged["id"].endswith("+2"), "the id says how many items it swallowed"
    assert merged["start"] != merged["finish"]
    # contiguity is the invariant the whole channel stands on
    for a, b in zip(entries, entries[1:]):
        assert a["finish"] == b["start"]


def test_without_runs_the_document_is_exactly_what_it_always_was(tz_set):
    items = [_item(i) for i in range(4)]
    plain = to_playout_json(_plan(items))
    assert to_playout_json(_plan(items), merged=[]) == plain
    assert to_playout_json(_plan(items), merged=None) == plain
    assert len(plain["items"]) == 4


def test_merging_changes_the_playout_but_never_the_plan(tz_set):
    items = [_item(i) for i in range(6)]
    plan = _plan(items)
    before = [(i.item_id, i.start_ms, i.end_ms, i.path) for i in plan.items]
    to_playout_json(plan, merged=[M.MergedRun(0, 5, Path("/m.mkv"))])
    after = [(i.item_id, i.start_ms, i.end_ms, i.path) for i in plan.items]
    assert before == after, "history, the guide and per-spot switches read the plan"


def test_a_merged_entry_leaves_the_neighbours_untouched(tz_set):
    items = [_item(0, kind="episode"), _item(1), _item(2), _item(3, kind="episode")]
    entries = to_playout_json(_plan(items), merged=[M.MergedRun(1, 2, Path("/m.mkv"))])["items"]
    assert [e["source"]["path"] for e in entries] == ["/derived/0.mkv", "/m.mkv", "/derived/3.mkv"]


# -- building: success, failure, caching ------------------------------------


def _files(tmp_path: Path, count: int) -> list[Path]:
    out = []
    for i in range(count):
        path = tmp_path / f"{i}.mkv"
        path.write_bytes(b"x" * (i + 1))
        out.append(path)
    return out


def test_the_digest_changes_when_a_file_does(tmp_path: Path):
    files = _files(tmp_path, 3)
    before = M.run_digest(files)
    assert before == M.run_digest(files)
    files[1].write_bytes(b"re-conformed with a different dip")
    assert M.run_digest(files) != before, "a re-conformed spot must not serve a stale join"
    assert M.run_digest(list(reversed(files))) != M.run_digest(files), "order is part of it"


def test_the_concat_list_quotes_awkward_filenames():
    listing = M._concat_list([Path("/a/it's here.mkv"), Path("/b/plain.mkv")])
    assert listing.splitlines()[0] == "file '/a/it'\\''s here.mkv'"
    assert listing.splitlines()[1] == "file '/b/plain.mkv'"


def _fake_ffmpeg(monkeypatch, *, returncode=0, writes=True):
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        if writes:
            Path(command[-1]).write_bytes(b"merged")
        return subprocess.CompletedProcess(command, returncode, "", "boom")

    monkeypatch.setattr(M.subprocess, "run", run)
    return calls


def test_a_successful_merge_lands_atomically_and_is_reused(tmp_path: Path, monkeypatch):
    calls = _fake_ffmpeg(monkeypatch)
    files = _files(tmp_path, 3)
    target = tmp_path / "out" / "x.mkv"

    assert M.build_merged(files, target, ffmpeg="ffmpeg")
    assert target.read_bytes() == b"merged"
    assert not list(target.parent.glob("*.part.mkv")) and not list(target.parent.glob("*.txt"))
    assert "-c" in calls[0] and calls[0][calls[0].index("-c") + 1] == "copy"

    assert M.build_merged(files, target, ffmpeg="ffmpeg")
    assert len(calls) == 1, "a cached join must not be rebuilt"


def test_a_failed_join_leaves_nothing_behind_and_reports_failure(tmp_path: Path, monkeypatch):
    _fake_ffmpeg(monkeypatch, returncode=1)
    target = tmp_path / "out" / "x.mkv"
    assert not M.build_merged(_files(tmp_path, 2), target, ffmpeg="ffmpeg")
    assert not target.exists() and not list(target.parent.glob("*"))


def test_ffmpeg_producing_no_file_is_a_failure_not_a_success(tmp_path: Path, monkeypatch):
    _fake_ffmpeg(monkeypatch, returncode=0, writes=False)
    assert not M.build_merged(_files(tmp_path, 2), tmp_path / "o" / "x.mkv", ffmpeg="ffmpeg")


def test_a_run_that_cannot_merge_airs_item_by_item_and_says_so(tmp_path: Path, monkeypatch):
    _fake_ffmpeg(monkeypatch, returncode=1)
    files = _files(tmp_path, 3)
    plan = _plan([_item(i, kind="ad") for i in range(3)])
    for item, path in zip(plan.items, files):
        object.__setattr__(item, "path", str(path))

    runs, warnings = M.merge_plan(plan, tmp_path / "out", ffmpeg="ffmpeg", max_ms=10**9)
    assert runs == [], "a failed join must not be announced to the streamer"
    assert warnings and "could not be merged" in warnings[0]


def test_a_run_with_a_missing_file_is_skipped_rather_than_crashing(tmp_path: Path, monkeypatch):
    calls = _fake_ffmpeg(monkeypatch)
    plan = _plan([_item(i) for i in range(3)])  # paths point at nothing
    runs, warnings = M.merge_plan(plan, tmp_path / "out", ffmpeg="ffmpeg", max_ms=10**9)
    assert runs == [] and not calls
    assert warnings


def test_without_ffmpeg_everything_still_plays_item_by_item(tmp_path: Path):
    runs, warnings = M.merge_plan(_plan([_item(0), _item(1)]), tmp_path, ffmpeg=None, max_ms=10**9)
    assert runs == [] and "ffmpeg was not found" in warnings[0]


# -- pruning ----------------------------------------------------------------


def test_prune_removes_only_old_unreferenced_files(tmp_path: Path):
    keep, stale, fresh = (tmp_path / n for n in ("keep.mkv", "stale.mkv", "fresh.mkv"))
    for path in (keep, stale, fresh):
        path.write_bytes(b"x")
    old = time.time() - 24 * 3600
    for path in (keep, stale):
        os.utime(path, (old, old))

    assert M.prune_merged(tmp_path, {keep}, grace_s=3600) == 1
    assert keep.exists(), "still referenced"
    assert not stale.exists(), "old and unreferenced"
    assert fresh.exists(), "recent: the streamer may still be reading it"


def test_prune_tolerates_a_missing_directory(tmp_path: Path):
    assert M.prune_merged(tmp_path / "nope", set()) == 0


# -- through the planner ----------------------------------------------------

DAY = date(2003, 6, 4)


def _conform_everything(catalog: Catalog, root: Path) -> None:
    """Pretend the whole break library has been conformed."""
    for asset in catalog.assets(("ad", "bump", "ident")):
        copy = root / f"{asset.key.replace(':', '_').replace('/', '_')}.mkv"
        copy.write_bytes(b"x")
        catalog.set_conformed(asset.key, copy, asset.duration_ms, "sig")


def _plan_with(library_config: Path, catalog: Catalog, merge: bool):
    (library_config / "ads.yaml").write_text(
        f"ads:\n  merge_breaks: {str(merge).lower()}\n", encoding="utf-8"
    )
    config = load_config(library_config)
    context = PlanContext.load(config, catalog)
    return build_plan(DAY, day_config_for(config, DAY), context, allow_generated=False)


def test_conformed_assets_become_mergeable_items(catalog, library, tmp_path):
    _conform_everything(catalog, tmp_path)
    plan = _plan_with(library.config, catalog, merge=True)
    breaks = [i for i in plan.items if i.kind in ("ad", "bump")]
    assert breaks and all(i.conformed for i in breaks)
    assert all(str(tmp_path) in i.path for i in breaks), "the copy airs, not the original"
    assert M.find_runs(plan.items, max_ms=8 * 60_000), "a real day must yield runs"


def test_with_merging_on_and_everything_conformed_nothing_is_trimmed(catalog, library, tmp_path):
    """A trimmed spot would lose its dip and break the stream-copy join."""
    _conform_everything(catalog, tmp_path)
    plan = _plan_with(library.config, catalog, merge=True)
    assert not [i for i in plan.items if i.kind == "ad" and i.out_point_ms is not None]
    assert not plan.validate()


def test_a_partly_conformed_library_keeps_trimming_so_nothing_regresses(catalog, library, tmp_path):
    """Until the whole pool is conformed, pods behave exactly as before."""
    ads = catalog.assets(("ad",))
    copy = tmp_path / "one.mkv"
    copy.write_bytes(b"x")
    catalog.set_conformed(ads[0].key, copy, ads[0].duration_ms, "sig")
    plan = _plan_with(library.config, catalog, merge=True)
    assert not plan.validate()


def test_the_day_still_tiles_exactly_when_merging_is_on(catalog, library, tmp_path):
    _conform_everything(catalog, tmp_path)
    on = _plan_with(library.config, catalog, merge=True)
    assert not on.validate()
    assert on.items[0].start_ms == on.day_start_ms and on.items[-1].end_ms == on.day_end_ms
