"""Staged schedule deploys.

The properties that matter, in the order they can hurt you:

* the stream is never touched until a launch, and a launch that cannot work changes
  nothing;
* the switch lands on an item boundary, so the picture is not replaced mid-episode;
* nothing is dropped and nothing is played twice across the seam.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from nostalgia import deploy as D

ZONE = timezone(timedelta(hours=9))


def _ms(text: str) -> int:
    return int(datetime.fromisoformat(text).timestamp() * 1000)


def _item(kind: str, start: str, seconds: float) -> dict:
    start_ms = _ms(start)
    finish = datetime.fromtimestamp((start_ms + seconds * 1000) / 1000, tz=ZONE)
    # The real document carries no `kind`: an item is named by its id, and the
    # boundary rule reads the id exactly as the emitter writes it.
    return {
        "id": f"20261030-0001-{kind}",
        "start": start,
        "finish": finish.isoformat(),
        "source": {"source_type": "local", "path": "/media/x.mkv"},
    }


def _spans(document: dict) -> list[D.Interval]:
    return D.intervals(document)


BREAK_DAY = {
    "items": [
        _item("episode", "2026-10-30T21:00:00+09:00", 1320),
        _item("ad", "2026-10-30T21:22:00+09:00", 30),
        _item("ad", "2026-10-30T21:22:30+09:00", 30),
        _item("episode", "2026-10-30T21:23:00+09:00", 1320),
        _item("episode", "2026-10-30T21:45:00+09:00", 900),
    ]
}


# -- choosing the boundary -------------------------------------------------------------


def test_the_switch_waits_for_the_programme_on_air_to_end():
    now = _ms("2026-10-30T21:10:00+09:00")
    assert D.switch_boundary(_spans(BREAK_DAY), now) == _ms("2026-10-30T21:22:00+09:00")


def test_a_break_never_gets_cut_in_half():
    """Cutting a break shows the tail of the old spots, then the new schedule."""
    now = _ms("2026-10-30T21:22:10+09:00")
    assert D.switch_boundary(_spans(BREAK_DAY), now) == _ms("2026-10-30T21:45:00+09:00")


def test_one_minute_into_a_break_waits_for_the_programme_behind_it():
    now = _ms("2026-10-30T21:22:40+09:00")
    assert D.switch_boundary(_spans(BREAK_DAY), now) == _ms("2026-10-30T21:45:00+09:00")


def test_a_programme_about_to_end_switches_now():
    """Waiting four seconds would be a jump indistinguishable from a mid-item one."""
    now = _ms("2026-10-30T21:21:57+09:00")
    assert D.switch_boundary(_spans(BREAK_DAY), now) == now


def test_with_nothing_on_air_the_next_whole_minute_is_used():
    """Inside a gap, the only certain item boundary is the next minute."""
    now = _ms("2026-10-30T03:14:07+09:00")
    assert D.switch_boundary(_spans(BREAK_DAY), now) == _ms("2026-10-30T03:15:00+09:00")
    exact = _ms("2026-10-30T03:15:00+09:00")
    assert D.switch_boundary(_spans(BREAK_DAY), exact) == exact


def test_a_boundary_past_the_end_of_the_live_day_is_capped():
    """A carry-over cannot honour a boundary the document does not reach.

    The document can be shorter than the programme it names -- a playout written
    before an episode's length was known -- and switching after its last item would
    mean carrying nothing over.
    """
    spans = _spans(BREAK_DAY)
    truncated = spans[:-1]
    horizon = spans[-1].start_ms  # the document ends where the last item starts
    assert D.plan_switch(truncated, _ms("2026-10-30T21:40:00+09:00"), horizon) == horizon
    assert D.plan_switch(spans, _ms("2026-10-30T21:20:00+09:00"), horizon) == _ms(
        "2026-10-30T21:22:00+09:00"
    )


def test_the_horizon_does_not_get_mistaken_for_the_day_on_air(tmp_path):
    """The live folder holds every day of the horizon; only one of them is playing.

    Reading the last file computes a boundary from a schedule two days away, which
    is how a switch lands in the middle of a programme instead of at its end.
    """
    roots = _roots(tmp_path)
    _write(roots.live.playout_dir / "day1.json", BREAK_DAY)
    _write(
        roots.live.playout_dir / "day3.json",
        {"items": [_item("episode", "2026-11-01T09:00:00+09:00", 1320)]},
    )
    today = D.live_day_for(roots, _ms("2026-10-30T21:10:00+09:00"))
    assert today is not None and today.name == "day1.json"
    assert D.live_day_for(roots, _ms("2026-11-02T21:10:00+09:00")) is None


def test_a_malformed_item_is_ignored_rather_than_fatal():
    document = {"items": [{"id": "bad", "start": "not a time", "finish": "x"}] + BREAK_DAY["items"]}
    assert len(D.intervals(document)) == len(BREAK_DAY["items"])


# -- the merge across the seam ---------------------------------------------------------


def _staged() -> dict:
    """The new schedule: the same day, but the 21:23 programme replaced."""
    return {
        "items": [
            _item("episode", "2026-10-30T21:00:00+09:00", 1320),
            _item("ad", "2026-10-30T21:22:00+09:00", 15),
            _item("ad", "2026-10-30T21:22:15+09:00", 15),
            _item("episode", "2026-10-30T21:22:30+09:00", 1395),
        ]
    }


def test_the_item_on_air_is_kept_and_the_rest_is_replaced():
    swap = _ms("2026-10-30T21:18:00+09:00")
    merged, kept = D.merge_playout(BREAK_DAY, _staged(), swap)
    assert kept == 1
    first = merged["items"][0]
    assert first["id"] == "20261030-0001-episode"
    assert first["finish"] == "2026-10-30T21:22:00+09:00"
    assert [entry["start"] for entry in merged["items"][1:]] == [
        "2026-10-30T21:22:00+09:00",
        "2026-10-30T21:22:15+09:00",
        "2026-10-30T21:22:30+09:00",
    ]


def test_nothing_is_dropped_or_played_twice_across_the_seam():
    swap = _ms("2026-10-30T21:22:10+09:00")
    merged, kept = D.merge_playout(BREAK_DAY, _staged(), swap)
    spans = _spans(merged)
    starts = [span.start_ms for span in spans]
    assert starts == sorted(starts), "the document must be in order"
    for earlier, later in zip(spans, spans[1:]):
        assert earlier.finish_ms == later.start_ms, "a gap or an overlap at the seam"
    # The programme is over and the first spot of the break is on air until
    # 21:22:30; the next live spot would start before that, so it is dropped and the
    # new plan takes over where the spot that is playing ends.
    assert kept == 1
    assert spans[0].finish_ms == _ms("2026-10-30T21:22:30+09:00")
    assert spans[1].start_ms == _ms("2026-10-30T21:22:30+09:00")


def test_the_on_air_programme_is_never_trimmed_to_the_switch():
    """Cutting the last seconds off a programme is the thing this exists to prevent."""
    live = {"items": [_item("episode", "2026-10-30T21:00:00+09:00", 1320)]}
    swap = _ms("2026-10-30T21:21:40+09:00")  # switched twenty seconds early
    merged, kept = D.merge_playout(live, _staged(), swap)
    assert kept == 1
    assert merged["items"][0]["finish"] == "2026-10-30T21:22:00+09:00"
    assert merged["items"][1]["start"] == "2026-10-30T21:22:00+09:00", "the seam moved with it"


def test_everything_between_now_and_the_boundary_is_kept():
    """The stream has to have something to play right now.

    Dropping the live items before the boundary leaves the streamer with no item
    covering the present, which is what a launched day starting at 16:26 looked like
    while the clock said 16:18.
    """
    now = _ms("2026-10-30T21:15:00+09:00")
    swap = _ms("2026-10-30T21:23:00+09:00")  # mid-way through the second programme
    merged, kept = D.merge_playout(BREAK_DAY, _staged(), swap, now)
    spans = _spans(merged)
    assert spans[0].start_ms <= now, "the document must cover this moment"
    assert kept >= 3, "the programme on air, the break and the start of the next"
    for earlier, later in zip(spans, spans[1:]):
        assert earlier.finish_ms == later.start_ms
    assert spans[0].start_ms == _ms("2026-10-30T21:00:00+09:00")


def test_an_item_that_ended_before_the_launch_is_not_kept():
    now = _ms("2026-10-30T21:22:45+09:00")  # the break is over by now
    merged, kept = D.merge_playout(BREAK_DAY, _staged(), _ms("2026-10-30T21:45:00+09:00"), now)
    starts = [span.start_ms for span in _spans(merged)]
    assert _ms("2026-10-30T21:00:00+09:00") not in starts, "the old programme is gone"
    assert starts[0] == _ms("2026-10-30T21:22:30+09:00"), "the spot on air when it was pressed"


def test_the_first_new_item_is_trimmed_to_the_end_of_what_is_on_air():
    """A new item must not start inside an old one, or two pictures share a second."""
    late = _staged()
    late["items"][2] = _item("ad", "2026-10-30T21:22:15+09:00", 60)  # spans the seam
    swap = _ms("2026-10-30T21:22:20+09:00")
    merged, kept = D.merge_playout(BREAK_DAY, late, swap)
    assert kept == 1, "the spot on air, which runs to 21:22:30"
    seam = _ms("2026-10-30T21:22:30+09:00")
    assert merged["items"][1]["start"] == datetime.fromtimestamp(seam / 1000, tz=ZONE).isoformat()
    spans = _spans(merged)
    for earlier, later in list(zip(spans, spans[1:]))[:-1]:
        # The last pair is the deliberately over-long fixture ad, which is the only
        # item allowed to run past the end of the staged day.
        assert earlier.finish_ms == later.start_ms


def test_the_merged_document_keeps_the_staged_header():
    merged, _ = D.merge_playout(BREAK_DAY, {**_staged(), "version": "9.9", "items": []}, 0)
    assert merged["version"] == "9.9"


def test_a_switch_at_an_exact_boundary_keeps_only_what_starts_there():
    """An item ending exactly on the boundary is over; one starting there is on air."""
    ended = _ms("2026-10-30T21:22:00+09:00")  # the programme's last second
    merged, kept = D.merge_playout(BREAK_DAY, _staged(), ended)
    assert kept == 0, "the programme had ended, so nothing was mid-item"
    assert merged["items"][0]["start"] == "2026-10-30T21:22:00+09:00"

    started = _ms("2026-10-30T21:22:00+09:00") + 1  # a second into the first spot
    merged, kept = D.merge_playout(BREAK_DAY, _staged(), started)
    assert kept == 1, "that spot is on air, so it finishes"


def test_items_from_a_previous_day_are_not_carried_over():
    swap = _ms("2026-10-30T21:18:00+09:00")
    other_day = {"items": [_item("episode", "2026-10-30T02:00:00+09:00", 600)]}
    merged, kept = D.merge_playout(other_day, _staged(), swap)
    assert kept == 0
    assert all(entry["start"] != "2026-10-30T02:00:00+09:00" for entry in merged["items"])


# -- the staging folder ------------------------------------------------------------------


def _roots(tmp_path):
    return D.Roots.of(tmp_path / "playout", tmp_path / "xmltv")


def _write(path, document):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document), encoding="utf-8")


def test_a_missing_staging_folder_is_not_an_error(tmp_path):
    roots = _roots(tmp_path)
    assert D.load(roots) is None and D.last(roots) is None
    assert D.staged_playouts(roots) == [] and D.missing_inputs(roots) == []
    assert D.cancel(roots) == 0


def test_the_request_file_is_not_a_day(tmp_path):
    roots = _roots(tmp_path)
    _write(roots.pending.playout_dir / "20261030T000000.json", {"items": []})
    _write(D.request_path(roots), json.loads(D.Deploy(1, 2, ("2026-10-30",)).to_json()))
    assert [path.name for path in D.staged_playouts(roots)] == ["20261030T000000.json"]


def test_a_deploy_survives_a_round_trip(tmp_path):
    roots = _roots(tmp_path)
    deploy = D.Deploy(staged_at_ms=5, switch_at_ms=9, days=("2026-10-30",), fingerprint="abc123")
    D.save_request(roots, deploy)
    assert D.load(roots) == deploy


def test_a_half_written_staging_folder_is_not_a_deploy(tmp_path):
    """The request file is written last; without it there is nothing to launch."""
    roots = _roots(tmp_path)
    _write(roots.pending.playout_dir / "20261030T000000.json", {"items": []})
    assert D.load(roots) is None


def test_a_corrupt_request_file_reads_as_nothing(tmp_path):
    roots = _roots(tmp_path)
    D.request_path(roots).parent.mkdir(parents=True)
    D.request_path(roots).write_text("{not json", encoding="utf-8")
    assert D.load(roots) is None


def test_missing_inputs_names_the_files_a_staged_document_needs(tmp_path):
    roots = _roots(tmp_path)
    present = tmp_path / "here.mkv"
    present.write_bytes(b"x")
    document = {
        "items": [
            {"id": "a", "start": "", "finish": "", "source": {"source_type": "local", "path": str(present)}},
            {"id": "b", "start": "", "finish": "", "source": {"source_type": "local", "path": str(tmp_path / "gone.mkv")}},
            {"id": "c", "start": "", "finish": "", "source": {"source_type": "lavfi", "params": "color=black"}},
        ]
    }
    _write(roots.pending.playout_dir / "day.json", document)
    assert D.missing_inputs(roots) == [str(tmp_path / "gone.mkv")]


# -- launching -------------------------------------------------------------------------


def test_launching_swaps_the_live_day_and_clears_the_staging(tmp_path):
    roots = _roots(tmp_path)
    _write(roots.live.playout_dir / "20261030T000000.json", BREAK_DAY)
    _write(roots.pending.playout_dir / "20261030T000000.json", _staged())
    _write(roots.pending.xmltv_dir / "test.1.xml", {})
    (roots.pending.xmltv_dir / "test.1.xml").write_text("<tv/>", encoding="utf-8")
    D.save_request(roots, D.Deploy(staged_at_ms=1, switch_at_ms=2, days=("2026-10-30",)))

    report = D.launch(roots, now_ms=_ms("2026-10-30T21:18:00+09:00"))

    live = json.loads((roots.live.playout_dir / "20261030T000000.json").read_text())
    # The launch is pressed at 21:18, so the rest of the programme on air (to 21:22)
    # is part of what has to stay: the stream is playing it right now.
    assert report.kept == 1 and report.days == 1 and report.from_live
    assert report.swap_ms == _ms("2026-10-30T21:22:00+09:00")
    assert live["items"][0]["start"] == "2026-10-30T21:00:00+09:00"
    assert live["items"][0]["finish"] == "2026-10-30T21:22:00+09:00"
    assert live["items"][1]["start"] == "2026-10-30T21:22:00+09:00", "the seam"
    assert len(live["items"]) == 4, "the kept programme plus the staged day from its end"
    assert (roots.live.xmltv_dir / "test.1.xml").read_text() == "<tv/>"
    assert D.load(roots) is None, "the staging folder is gone"
    assert D.last(roots) == report


def test_a_launch_with_nothing_staged_says_so_and_touches_nothing(tmp_path):
    roots = _roots(tmp_path)
    _write(roots.live.playout_dir / "20261030T000000.json", BREAK_DAY)
    with pytest.raises(LookupError, match="Replan"):
        D.launch(roots)
    assert json.loads((roots.live.playout_dir / "20261030T000000.json").read_text()) == BREAK_DAY


def test_a_launch_with_no_live_day_starts_the_new_schedule_whole(tmp_path):
    roots = _roots(tmp_path)
    _write(roots.pending.playout_dir / "20261030T000000.json", _staged())
    report = D.launch(roots, now_ms=_ms("2026-10-30T21:18:00+09:00"))
    assert not report.from_live and report.kept == 0
    assert "no live playout" in report.note
    assert json.loads((roots.live.playout_dir / "20261030T000000.json").read_text()) == _staged()


def test_an_unreadable_staged_day_leaves_the_live_files_alone(tmp_path):
    roots = _roots(tmp_path)
    _write(roots.live.playout_dir / "20261030T000000.json", BREAK_DAY)
    (roots.pending.playout_dir).mkdir(parents=True)
    (roots.pending.playout_dir / "20261030T000000.json").write_text("{oops", encoding="utf-8")
    with pytest.raises(LookupError):
        D.launch(roots)
    assert json.loads((roots.live.playout_dir / "20261030T000000.json").read_text()) == BREAK_DAY


def test_the_last_launch_is_readable_for_the_pages(tmp_path):
    roots = _roots(tmp_path)
    _write(roots.pending.playout_dir / "20261030T000000.json", _staged())
    report = D.launch(roots, now_ms=_ms("2026-10-30T21:18:00+09:00"))
    again = D.last(roots)
    assert again is not None and again.swap_ms == report.swap_ms and again.days == 1
